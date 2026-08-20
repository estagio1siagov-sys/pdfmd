import json
import threading
import time
from pathlib import Path

from django.conf import settings
from google import genai
from google.genai import errors, types
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_incrementing,
)

class BlockedByPolicyError(Exception):
    """Levantada quando o Gemini recusa processar um bloco por copyright ou safety filter."""


class _TokenBucket:
    """Rate limiter proativo: espaça chamadas à API para não ultrapassar o limite por minuto.
    Compartilhado entre todos os workers — previne 429 antes de acontecer."""

    def __init__(self, rate_per_minute):
        self._interval = 60.0 / rate_per_minute
        self._lock = threading.Lock()
        self._next_allowed = time.monotonic()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._interval
        if wait:
            time.sleep(wait)


# Buckets agrupados por chave real: se duas etapas usam a mesma chave,
# compartilham o mesmo bucket — evita somar RPM numa chave única.
# Inicializado em _init_buckets() após o Django carregar as settings.
_bucket_converter   = None
_bucket_validator   = None
_bucket_reconverter = None
_buckets_by_key: dict = {}
_buckets_lock = threading.Lock()


def _get_or_create_bucket(api_key):
    with _buckets_lock:
        if api_key not in _buckets_by_key:
            _buckets_by_key[api_key] = _TokenBucket(rate_per_minute=12)
        return _buckets_by_key[api_key]


def _init_buckets():
    global _bucket_converter, _bucket_validator, _bucket_reconverter
    if _bucket_converter is not None:
        return
    _bucket_converter   = _get_or_create_bucket(settings.GEMINI_API_KEY_CONVERTER)
    _bucket_validator   = _get_or_create_bucket(settings.GEMINI_API_KEY_VALIDATOR)
    _bucket_reconverter = _get_or_create_bucket(settings.GEMINI_API_KEY_RECONVERTER)


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

CONVERTER_PROMPT = (_PROMPTS_DIR / "converter_prompt.txt").read_text(encoding="utf-8")
VALIDATOR_PROMPT = (_PROMPTS_DIR / "validator_prompt.txt").read_text(encoding="utf-8")
RECONVERT_PROMPT_TEMPLATE = (_PROMPTS_DIR / "reconvert_prompt.txt").read_text(encoding="utf-8")
META_VALIDATOR_PROMPT_TEMPLATE = (_PROMPTS_DIR / "meta_validator_prompt.txt").read_text(encoding="utf-8")

_client_converter   = None
_client_validator   = None
_client_reconverter = None
_client_meta        = None

# Retry apenas para erro de limite de requisições (429 / RESOURCE_EXHAUSTED).
# Espera crescente: 30s, 60s, 90s, 120s entre as até 5 tentativas.
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WAIT_START = 30
RATE_LIMIT_WAIT_INCREMENT = 30


def _make_client(api_key, name):
    if not api_key:
        raise RuntimeError(f"{name} não configurada. Defina no arquivo .env.")
    return genai.Client(api_key=api_key)


def get_client_converter():
    global _client_converter
    if _client_converter is None:
        _client_converter = _make_client(settings.GEMINI_API_KEY_CONVERTER, "GEMINI_API_KEY_CONVERTER")
    return _client_converter


def get_client_validator():
    global _client_validator
    if _client_validator is None:
        _client_validator = _make_client(settings.GEMINI_API_KEY_VALIDATOR, "GEMINI_API_KEY_VALIDATOR")
    return _client_validator


def get_client_reconverter():
    global _client_reconverter
    if _client_reconverter is None:
        _client_reconverter = _make_client(settings.GEMINI_API_KEY_RECONVERTER, "GEMINI_API_KEY_RECONVERTER")
    return _client_reconverter


_client_default = None


def get_client_default():
    global _client_default
    if _client_default is None:
        _client_default = _make_client(settings.GEMINI_API_KEY, "GEMINI_API_KEY")
    return _client_default


def get_client_meta():
    global _client_meta
    if _client_meta is None:
        _client_meta = _make_client(settings.GEMINI_API_KEY_META, "GEMINI_API_KEY_META")
    return _client_meta


def _should_fallback_to_default(exc, primary_key):
    """Só troca pra chave genérica se: (a) o erro é RPM estourado (não quota
    diária, não bloqueio de projeto/403), (b) existe uma GEMINI_API_KEY genérica
    configurada, e (c) ela é DIFERENTE da chave que acabou de falhar (senão
    bateria no mesmo limite de novo)."""
    if not _is_rate_limit_error(exc):
        return False
    if isinstance(exc, errors.APIError) and getattr(exc, "code", None) != 429:
        return False
    default_key = settings.GEMINI_API_KEY
    return bool(default_key) and default_key != primary_key


TRUNCATION_MAX_ATTEMPTS = 3
TRUNCATION_WAIT_SECONDS = 3


def _is_truncated(response):
    """True se a resposta parou por estouro de tokens (MAX_TOKENS) — sinal de corte
    no meio do conteúdo, não de bloqueio de safety/copyright (isso é outro finish_reason)."""
    try:
        candidates = response.candidates or []
        if not candidates:
            return False
        finish_reason = getattr(candidates[0], "finish_reason", None)
        name = getattr(finish_reason, "name", str(finish_reason))
        return name == "MAX_TOKENS"
    except Exception:
        return False


def _call_with_truncation_guard(generate_fn):
    """Reexecuta até TRUNCATION_MAX_ATTEMPTS vezes quando a resposta vem vazia ou
    cortada (MAX_TOKENS) — medido: sob instabilidade/rate-limit na nuvem, o Gemini
    às vezes devolve texto interrompido no meio da frase (ex.: reconversão do 167_2014
    terminando em "Em" sem a data), e isso passava direto pro arquivo final sem
    nenhuma tentativa nova. Sempre devolve o MELHOR texto obtido — se ainda estiver
    cortado depois de todas as tentativas, aceita como está (nunca lança exceção por
    isso, quem chama decide o que fazer com um resultado ainda vazio)."""
    text = ""
    for attempt in range(TRUNCATION_MAX_ATTEMPTS):
        response = generate_fn()
        text = (response.text or "").strip()
        if text and not _is_truncated(response):
            return text, response
        if attempt < TRUNCATION_MAX_ATTEMPTS - 1:
            time.sleep(TRUNCATION_WAIT_SECONDS)
    return text, response


def _blocked_reason(response):
    try:
        candidates = response.candidates or []
        if candidates:
            finish_reason = getattr(candidates[0], "finish_reason", None)
            if finish_reason:
                name = getattr(finish_reason, "name", str(finish_reason))
                labels = {
                    "SAFETY": "bloqueado por safety filter",
                    "RECITATION": "bloqueado por copyright/recitação",
                    "PROHIBITED_CONTENT": "conteúdo proibido",
                }
                return labels.get(name, f"finish_reason={name}")
        feedback = getattr(response, "prompt_feedback", None)
        if feedback:
            block_reason = getattr(feedback, "block_reason", None)
            if block_reason:
                return f"prompt bloqueado: {getattr(block_reason, 'name', block_reason)}"
    except Exception:
        pass
    return "resposta vazia sem motivo informado"


def _is_rate_limit_error(exc):
    if not isinstance(exc, errors.APIError):
        return False
    code = getattr(exc, "code", None)
    # 503 UNAVAILABLE: modelo temporariamente sobrecarregado — vale retentar.
    if code == 503:
        return True
    if code != 429:
        return False
    # Quota diária esgotada não reseta em minutos — não adianta retentar.
    # RPM (requests per minute) reseta em ~1 min e vale retentar.
    msg = str(exc).lower()
    is_daily_quota = "quota" in msg and "per day" in msg
    return not is_daily_quota


def _rate_limit_retry(on_retry):
    def before_sleep(retry_state):
        if on_retry is None:
            return
        wait_seconds = retry_state.next_action.sleep if retry_state.next_action else 0
        on_retry(retry_state.attempt_number, RATE_LIMIT_MAX_ATTEMPTS, round(wait_seconds))

    return retry(
        retry=retry_if_exception(_is_rate_limit_error),
        stop=stop_after_attempt(RATE_LIMIT_MAX_ATTEMPTS),
        wait=wait_incrementing(start=RATE_LIMIT_WAIT_START, increment=RATE_LIMIT_WAIT_INCREMENT),
        before_sleep=before_sleep,
        reraise=True,
    )


def convert_block_to_markdown(pdf_bytes, on_retry=None):
    """Envia um sub-PDF ao Gemini e retorna o Markdown gerado.

    Reexecuta automaticamente em caso de erro 429 (limite de requisições),
    com espera crescente entre tentativas. `on_retry(attempt, max_attempts, wait_seconds)`
    é chamado antes de cada nova tentativa, útil para atualizar a UI.
    """

    def _generate(client):
        def _do_call():
            return client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                    CONVERTER_PROMPT,
                ],
            )

        text, response = _call_with_truncation_guard(_do_call)
        if not text:
            reason = _blocked_reason(response)
            raise BlockedByPolicyError(reason)
        return text

    @_rate_limit_retry(on_retry)
    def _call_primary():
        _init_buckets()
        _bucket_converter.acquire()
        return _generate(get_client_converter())

    try:
        return _call_primary()
    except Exception as exc:
        if not _should_fallback_to_default(exc, settings.GEMINI_API_KEY_CONVERTER):
            raise

        @_rate_limit_retry(on_retry)
        def _call_fallback():
            _get_or_create_bucket(settings.GEMINI_API_KEY).acquire()
            return _generate(get_client_default())

        return _call_fallback()


def reconvert_block_with_feedback(pdf_bytes, previous_markdown, issues_text, on_retry=None):
    """Reenvia o sub-PDF pedindo uma nova transcrição que corrija os problemas
    apontados pelo validador na tentativa anterior."""

    # Reenvia o CONVERTER_PROMPT inteiro junto — sem isso, a reconversão só via as
    # regras de formatação por uma linha de referência vaga ("siga as regras originais"),
    # sem o conteúdo delas. Cada reconversão "esquecia" um pouco mais de negrito, tabela
    # hierárquica, estrutura de título etc — perda que se acumulava a cada rodada do fix
    # loop (medido: quanto mais reconversões, mais formatação sumia).
    prompt = (
        CONVERTER_PROMPT
        + "\n\n---\n\n"
        + RECONVERT_PROMPT_TEMPLATE
            .replace("{issues}", issues_text)
            .replace("{previous_markdown}", previous_markdown)
    )

    def _generate(client):
        def _do_call():
            return client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                    prompt,
                ],
            )

        text, _response = _call_with_truncation_guard(_do_call)
        # Vazio mesmo após as tentativas do guard: não há nada melhor que a reconversão
        # possa oferecer aqui. Mantém a versão anterior (imperfeita, mas íntegra) em vez
        # de substituir por texto vazio/cortado — nunca piora o que já existia.
        return text or previous_markdown

    @_rate_limit_retry(on_retry)
    def _call_primary():
        _init_buckets()
        _bucket_reconverter.acquire()
        return _generate(get_client_reconverter())

    try:
        return _call_primary()
    except Exception as exc:
        if not _should_fallback_to_default(exc, settings.GEMINI_API_KEY_RECONVERTER):
            raise

        @_rate_limit_retry(on_retry)
        def _call_fallback():
            _get_or_create_bucket(settings.GEMINI_API_KEY).acquire()
            return _generate(get_client_default())

        return _call_fallback()


VALIDATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "aprovado": {"type": "BOOLEAN"},
        "motivo": {"type": "STRING"},
        "trechos_problematicos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "tipo": {"type": "STRING"},
                    "descricao": {"type": "STRING"},
                    "e_carimbo_rubrica_ou_ilegivel": {"type": "BOOLEAN"},
                },
                "required": ["tipo", "descricao", "e_carimbo_rubrica_ou_ilegivel"],
            },
        },
    },
    "required": ["aprovado", "motivo", "trechos_problematicos"],
}


def _sanitize_validation(validation):
    """Remove da lista de problemas qualquer item que o próprio validador
    classificou como carimbo/rubrica/ilegível, e recalcula `aprovado` com
    base no que sobrar. Só força aprovação se havia trechos problemáticos
    e todos foram filtrados — não sobrescreve reprovação sem trechos listados."""
    trechos = validation.get("trechos_problematicos", [])
    trechos_relevantes = [t for t in trechos if not t.get("e_carimbo_rubrica_ou_ilegivel")]

    validation["trechos_problematicos"] = trechos_relevantes
    # Só força aprovação quando havia itens e TODOS foram filtrados como irrelevantes.
    # Se o modelo retornou aprovado=False com lista vazia, respeita a reprovação.
    if trechos and not trechos_relevantes:
        validation["aprovado"] = True
    return validation


def validate_block(pdf_bytes, markdown_text, on_retry=None):
    """Envia o sub-PDF + Markdown gerado ao Gemini validador.

    Retorna um dict {aprovado, motivo, trechos_problematicos}, já filtrado de
    itens que o próprio validador identificou como carimbo/rubrica/ilegível
    (ver `_sanitize_validation`).
    Mesma política de retry em caso de erro 429 descrita em `convert_block_to_markdown`.
    """

    def _generate(client):
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                f"MARKDOWN GERADO PARA VALIDAÇÃO:\n\n{markdown_text}",
                VALIDATOR_PROMPT,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VALIDATION_SCHEMA,
            ),
        )
        raw = response.text
        if not raw:
            raise ValueError(
                "Gemini retornou resposta vazia na validação "
                "(possível bloqueio por safety filter). Tente novamente."
            )
        return json.loads(raw)

    @_rate_limit_retry(on_retry)
    def _call_primary():
        _init_buckets()
        _bucket_validator.acquire()
        return _generate(get_client_validator())

    try:
        result = _call_primary()
    except Exception as exc:
        if not _should_fallback_to_default(exc, settings.GEMINI_API_KEY_VALIDATOR):
            raise

        @_rate_limit_retry(on_retry)
        def _call_fallback():
            _get_or_create_bucket(settings.GEMINI_API_KEY).acquire()
            return _generate(get_client_default())

        result = _call_fallback()

    return _sanitize_validation(result)


META_RECHECK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "itens": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "ainda_procede": {"type": "BOOLEAN"},
                    "justificativa": {"type": "STRING"},
                },
                "required": ["ainda_procede", "justificativa"],
            },
        },
    },
    "required": ["itens"],
}


def recheck_flagged_issues(pdf_bytes, final_markdown, trechos, on_retry=None):
    """Auditor de segunda instância, com chave/conta dedicada (GEMINI_API_KEY_META).

    Reexamina, um por um, os `trechos_problematicos` que sobraram depois do fix loop,
    contra a versão FINAL do markdown (a que de fato vai pro arquivo) — não contra a
    versão que o validador original viu. Existe porque o validador comum pode reprovar
    em cima de uma versão que já foi corrigida numa reconversão seguinte (medido: reprova
    citando 'CONSIDRERANDO'→'CONSIDERANDO' mesmo com a grafia já preservada no .md salvo).

    Fail-safe estrito: qualquer falha (rede, JSON quebrado, contagem de itens não bate)
    devolve `trechos` INTACTO. Só remove um item quando a resposta confirma
    explicitamente `ainda_procede: false` — nunca esconde aviso por falha técnica.
    """
    if not trechos:
        return trechos

    itens_txt = "\n".join(
        f"{i}. [{t.get('tipo')}] {t.get('descricao')}" for i, t in enumerate(trechos, start=1)
    )
    prompt = META_VALIDATOR_PROMPT_TEMPLATE.replace("{itens_para_reexaminar}", itens_txt)

    def _generate(client):
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                f"MARKDOWN FINAL:\n\n{final_markdown}",
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=META_RECHECK_SCHEMA,
            ),
        )
        raw = response.text
        if not raw:
            raise ValueError("Gemini retornou resposta vazia no recheck.")
        return json.loads(raw)

    @_rate_limit_retry(on_retry)
    def _call_primary():
        _get_or_create_bucket(settings.GEMINI_API_KEY_META).acquire()
        return _generate(get_client_meta())

    try:
        result = _call_primary()
    except Exception as exc:
        if not _should_fallback_to_default(exc, settings.GEMINI_API_KEY_META):
            return trechos

        try:
            @_rate_limit_retry(on_retry)
            def _call_fallback():
                _get_or_create_bucket(settings.GEMINI_API_KEY).acquire()
                return _generate(get_client_default())

            result = _call_fallback()
        except Exception:
            return trechos

    itens = result.get("itens", [])
    if len(itens) != len(trechos):
        return trechos

    return [t for t, item in zip(trechos, itens) if item.get("ainda_procede", True)]
