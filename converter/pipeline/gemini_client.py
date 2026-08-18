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


# 12 RPM por chave (margem de 3 abaixo do limite de 15), compartilhado entre workers.
_bucket_converter   = _TokenBucket(rate_per_minute=12)
_bucket_validator   = _TokenBucket(rate_per_minute=12)
_bucket_reconverter = _TokenBucket(rate_per_minute=12)


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

CONVERTER_PROMPT = (_PROMPTS_DIR / "converter_prompt.txt").read_text(encoding="utf-8")
VALIDATOR_PROMPT = (_PROMPTS_DIR / "validator_prompt.txt").read_text(encoding="utf-8")
RECONVERT_PROMPT_TEMPLATE = (_PROMPTS_DIR / "reconvert_prompt.txt").read_text(encoding="utf-8")

_client_converter   = None
_client_validator   = None
_client_reconverter = None

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

    @_rate_limit_retry(on_retry)
    def _call():
        _bucket_converter.acquire()
        client = get_client_converter()
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                CONVERTER_PROMPT,
            ],
        )
        text = (response.text or "").strip()
        if not text:
            reason = _blocked_reason(response)
            raise BlockedByPolicyError(reason)
        return text

    return _call()


def reconvert_block_with_feedback(pdf_bytes, previous_markdown, issues_text, on_retry=None):
    """Reenvia o sub-PDF pedindo uma nova transcrição que corrija os problemas
    apontados pelo validador na tentativa anterior."""

    prompt = (
        RECONVERT_PROMPT_TEMPLATE
        .replace("{issues}", issues_text)
        .replace("{previous_markdown}", previous_markdown)
    )

    @_rate_limit_retry(on_retry)
    def _call():
        _bucket_reconverter.acquire()
        client = get_client_reconverter()
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                prompt,
            ],
        )
        return (response.text or "").strip()

    return _call()


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

    @_rate_limit_retry(on_retry)
    def _call():
        _bucket_validator.acquire()
        client = get_client_validator()
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

    return _sanitize_validation(_call())
