import threading
import time

CLEANUP_INTERVAL_SECONDS = 2 * 60 * 60  # 2 horas
STALE_AFTER_SECONDS = 2 * 60 * 60  # 2 horas desde a última atualização

_started = False
_lock = threading.Lock()


def _cleanup_once():
    from datetime import timedelta

    from django.utils import timezone

    from converter.models import ConversionJob

    limite = timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS)
    jobs = ConversionJob.objects.filter(
        status__in=[ConversionJob.STATUS_DONE, ConversionJob.STATUS_FAILED],
        updated_at__lt=limite,
    )
    for job in jobs:
        try:
            if job.original_pdf:
                job.original_pdf.delete(save=False)
            if job.result_file:
                job.result_file.delete(save=False)
        except Exception:
            pass
    jobs.delete()


def _loop():
    # Limpa UMA VEZ no start, antes de entrar no ciclo. Dormir 2h primeiro fazia a limpeza
    # nunca acontecer na prática no Render free tier: o serviço hiberna com ~15 min de
    # ociosidade e a thread morre muito antes do primeiro disparo. Rodando no start, todo
    # boot recolhe o que ficou para trás do ciclo anterior.
    try:
        _cleanup_once()
    except Exception:
        pass
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            _cleanup_once()
        except Exception:
            pass


def ensure_cleanup_started():
    """Limpeza automática: jobs DONE/FAILED sem atualização há mais de 2h (arquivo +
    registro) são apagados de tempo em tempo — cobre o caso de reconversões repetidas de
    teste que nunca passam pelo botão 'Baixar todos' (única limpeza que já existia antes),
    deixando jobs órfãos que colidem de nome com uploads futuros do mesmo arquivo."""
    global _started
    with _lock:
        if not _started:
            threading.Thread(target=_loop, daemon=True).start()
            _started = True
