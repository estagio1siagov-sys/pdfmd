import queue
import threading

from .runner import run_pipeline

# 3 workers paralelos — um por chave de API (CONVERTER / VALIDATOR / RECONVERTER).
# Cada chave tem cota própria de 15 RPM; o TokenBucket em gemini_client.py limita
# a 12 RPM por chave, mantendo margem mesmo com os workers concorrentes.
NUM_WORKERS = 3

_job_queue = queue.Queue()
_workers_started = False
_worker_lock = threading.Lock()


def _worker_loop():
    from converter.models import ConversionJob

    while True:
        job_id = _job_queue.get()
        try:
            job = ConversionJob.objects.get(pk=job_id)
            run_pipeline(job)
        except ConversionJob.DoesNotExist:
            pass
        except Exception:
            import traceback
            try:
                job = ConversionJob.objects.get(pk=job_id)
                if job.status not in (ConversionJob.STATUS_DONE, ConversionJob.STATUS_FAILED):
                    job.status = ConversionJob.STATUS_FAILED
                    job.error_message = f"Erro inesperado no worker:\n{traceback.format_exc()}"
                    job.save(update_fields=["status", "error_message", "updated_at"])
            except Exception:
                pass
        finally:
            _job_queue.task_done()


def _ensure_workers_started():
    global _workers_started
    with _worker_lock:
        if not _workers_started:
            for _ in range(NUM_WORKERS):
                thread = threading.Thread(target=_worker_loop, daemon=True)
                thread.start()
            _workers_started = True


def enqueue_job(job_id):
    """Adiciona um job à fila compartilhada. Até NUM_WORKERS jobs rodam em paralelo;
    o TokenBucket por chave em gemini_client.py garante que o rate limit não estoure."""
    _ensure_workers_started()
    _job_queue.put(job_id)
