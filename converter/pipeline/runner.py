import traceback
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile

from .gemini_client import (
    BlockedByPolicyError,
    convert_block_to_markdown,
    reconvert_block_with_feedback,
    validate_block,
)
from .merger import merge_blocks
from .splitter import split_pdf

MAX_FIX_ATTEMPTS = 3


def run_pipeline(job):
    """Executa a pipeline completa de forma sequencial, atualizando `job` a cada passo.

    `job` é uma instância de ConversionJob já salva no banco.

    Se a validação reprovar um bloco, ele é reconvertido automaticamente com o
    feedback do validador (até MAX_FIX_ATTEMPTS vezes) antes de desistir e falhar.

    Erros de limite de requisição (429) são reexecutados automaticamente
    com espera crescente (ver gemini_client.py), independentemente disso.
    """
    from converter.models import ConversionJob

    def on_retry(attempt, max_attempts, wait_seconds):
        job.current_step = ConversionJob.STEP_RATE_LIMITED
        job.retry_attempt = attempt
        job.retry_max = max_attempts
        job.retry_wait_seconds = wait_seconds
        job.save(update_fields=["current_step", "retry_attempt", "retry_max", "retry_wait_seconds", "updated_at"])

    def clear_retry_info():
        job.retry_attempt = 0
        job.retry_max = 0
        job.retry_wait_seconds = 0

    def clear_fix_info():
        job.fix_attempt = 0
        job.fix_max = 0

    try:
        job.status = ConversionJob.STATUS_RUNNING
        job.current_step = ConversionJob.STEP_SPLITTING
        job.save(update_fields=["status", "current_step", "updated_at"])

        pdf_path = job.original_pdf.path
        blocks = split_pdf(pdf_path, settings.PDF_BLOCK_SIZE)

        if not blocks:
            raise ValueError("O arquivo PDF enviado não contém páginas legíveis.")

        job.total_blocks = len(blocks)
        job.current_block = 0
        job.save(update_fields=["total_blocks", "current_block", "updated_at"])

        converted_markdowns = []
        flagged_blocks = []

        for index, block in enumerate(blocks, start=1):
            pages_label = f"páginas {block['start_page']}-{block['end_page']}"

            clear_retry_info()
            clear_fix_info()
            job.current_step = ConversionJob.STEP_CONVERTING
            job.current_block = index
            job.save(update_fields=[
                "current_step", "current_block",
                "retry_attempt", "retry_max", "retry_wait_seconds",
                "fix_attempt", "fix_max", "updated_at",
            ])

            try:
                markdown = _call_with_rate_limit_handling(
                    convert_block_to_markdown, index, len(blocks), pages_label, block["pdf_bytes"], on_retry,
                )
            except BlockedByPolicyError as exc:
                flagged_blocks.append({
                    "index": index,
                    "total": len(blocks),
                    "pages_label": pages_label,
                    "motivo": f"Bloqueado por política do Gemini ({exc})",
                    "detalhes": "",
                    "blocked_by_policy": True,
                })
                continue

            if not markdown:
                raise ValueError(
                    f"Bloco {index}/{len(blocks)} ({pages_label}): "
                    "o Gemini retornou resposta vazia. Tente novamente."
                )

            validated_markdown = None
            last_validation = None

            for fix_attempt in range(0, MAX_FIX_ATTEMPTS + 1):
                clear_retry_info()
                job.current_step = ConversionJob.STEP_VALIDATING
                job.fix_attempt = fix_attempt
                job.fix_max = MAX_FIX_ATTEMPTS
                job.save(update_fields=[
                    "current_step", "retry_attempt", "retry_max", "retry_wait_seconds",
                    "fix_attempt", "fix_max", "updated_at",
                ])

                validation = _call_with_rate_limit_handling(
                    validate_block, index, len(blocks), pages_label, block["pdf_bytes"], markdown, on_retry,
                )
                last_validation = validation

                if validation.get("aprovado"):
                    validated_markdown = markdown
                    break

                if fix_attempt == MAX_FIX_ATTEMPTS:
                    break

                clear_retry_info()
                job.current_step = ConversionJob.STEP_FIXING
                job.fix_attempt = fix_attempt + 1
                job.fix_max = MAX_FIX_ATTEMPTS
                job.save(update_fields=[
                    "current_step", "retry_attempt", "retry_max", "retry_wait_seconds",
                    "fix_attempt", "fix_max", "updated_at",
                ])

                issues_text = _format_issues(validation)
                markdown = _call_with_rate_limit_handling(
                    reconvert_block_with_feedback, index, len(blocks), pages_label,
                    block["pdf_bytes"], markdown, issues_text, on_retry,
                )

            if validated_markdown is None:
                # Esgotou as tentativas de correção automática. Em vez de falhar o job
                # inteiro, aceita a última versão gerada (melhor esforço) e sinaliza
                # o bloco para revisão manual do usuário.
                validated_markdown = markdown
                motivo = last_validation.get("motivo", "motivo não informado") if last_validation else "desconhecido"
                detalhes = _format_issues(last_validation) if last_validation else ""
                flagged_blocks.append({
                    "index": index,
                    "total": len(blocks),
                    "pages_label": pages_label,
                    "motivo": motivo,
                    "detalhes": detalhes,
                })

            converted_markdowns.append(validated_markdown)

        if not converted_markdowns:
            blocked = [fb for fb in flagged_blocks if fb.get("blocked_by_policy")]
            pages = ", ".join(fb["pages_label"] for fb in blocked)
            raise ValueError(
                f"Nenhum bloco pôde ser convertido. Todo o conteúdo foi bloqueado "
                f"por política do Gemini ({pages}). Verifique se o PDF contém conteúdo "
                "protegido por copyright."
            )

        job.current_step = ConversionJob.STEP_MERGING
        job.save(update_fields=["current_step", "updated_at"])

        final_markdown = merge_blocks(converted_markdowns)

        result_name = Path(job.original_filename).stem + ".md"
        job.result_file.save(result_name, ContentFile(final_markdown.encode("utf-8")), save=False)

        job.status = ConversionJob.STATUS_DONE
        job.current_step = ""
        if flagged_blocks:
            job.needs_review = True
            job.review_notes = _format_review_notes(flagged_blocks)
        clear_retry_info()
        clear_fix_info()
        job.save()

    except (RateLimitExhaustedError, ValueError) as exc:
        job.status = ConversionJob.STATUS_FAILED
        job.error_message = str(exc)
        clear_retry_info()
        clear_fix_info()
        job.save()

    except Exception:
        job.status = ConversionJob.STATUS_FAILED
        job.error_message = f"Erro inesperado:\n{traceback.format_exc()}"
        clear_retry_info()
        clear_fix_info()
        job.save()


def _call_with_rate_limit_handling(fn, index, total, pages_label, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if _is_rate_limit_exhausted(exc):
            raise RateLimitExhaustedError(
                f"Bloco {index}/{total} ({pages_label}): limite de requisições da API "
                f"do Gemini excedido mesmo após várias tentativas. Tente novamente mais tarde."
            ) from exc
        if _is_daily_quota_exhausted(exc):
            raise RateLimitExhaustedError(
                f"Bloco {index}/{total} ({pages_label}): cota diária da API do Gemini esgotada. "
                f"A cota reseta à meia-noite (horário do Google). Tente novamente amanhã."
            ) from exc
        raise


def _format_review_notes(flagged_blocks):
    parts = []
    for fb in flagged_blocks:
        if fb.get("blocked_by_policy"):
            parts.append(
                f"⚠ Bloco {fb['index']}/{fb['total']} ({fb['pages_label']}): "
                "bloqueado por copyright/política do Gemini — páginas ausentes no arquivo gerado."
            )
        else:
            parts.append(
                f"Bloco {fb['index']}/{fb['total']} ({fb['pages_label']}): {fb['motivo']}\n{fb['detalhes']}"
            )
    return "\n\n".join(parts)


def _format_issues(validation):
    motivo = validation.get("motivo", "")
    trechos = validation.get("trechos_problematicos", [])
    linhas = [f"Motivo geral: {motivo}"] if motivo else []
    linhas += [f"- [{t.get('tipo')}] {t.get('descricao')}" for t in trechos]
    return "\n".join(linhas)


def _is_rate_limit_exhausted(exc):
    from google.genai import errors
    if not (isinstance(exc, errors.APIError) and getattr(exc, "code", None) == 429):
        return False
    msg = str(exc).lower()
    return "per day" not in msg


def _is_daily_quota_exhausted(exc):
    from google.genai import errors
    if not (isinstance(exc, errors.APIError) and getattr(exc, "code", None) == 429):
        return False
    msg = str(exc).lower()
    return "quota" in msg and "per day" in msg


class RateLimitExhaustedError(Exception):
    pass
