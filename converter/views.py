import io
import zipfile
from pathlib import Path

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import PdfUploadForm
from .models import ConversionJob
from .pipeline.queue_worker import enqueue_job


def upload_view(request):
    if request.method == "POST":
        form = PdfUploadForm(request.POST, request.FILES)
        if form.is_valid():
            job_ids = []
            for pdf_file in form.cleaned_data["pdf_files"]:
                job = ConversionJob.objects.create(
                    original_pdf=pdf_file,
                    original_filename=pdf_file.name,
                )
                job_ids.append(job.pk)

            for job_id in job_ids:
                enqueue_job(job_id)

            ids_param = ",".join(str(i) for i in job_ids)
            return redirect(f"{reverse('converter:queue')}?ids={ids_param}")
    else:
        form = PdfUploadForm()

    return render(request, "converter/upload.html", {"form": form})


def queue_view(request):
    ids_param = request.GET.get("ids", "")
    job_ids = [int(i) for i in ids_param.split(",") if i.strip().isdigit()]
    jobs = list(ConversionJob.objects.filter(pk__in=job_ids))
    jobs.sort(key=lambda j: job_ids.index(j.pk))
    return render(request, "converter/queue.html", {"jobs": jobs})


def download_all(request):
    ids_param = request.GET.get("ids", "")
    job_ids = [int(i) for i in ids_param.split(",") if i.strip().isdigit()]
    jobs = ConversionJob.objects.filter(
        pk__in=job_ids, status=ConversionJob.STATUS_DONE
    ).exclude(result_file="")

    if not jobs:
        return HttpResponse("Nenhum arquivo concluído para baixar.", status=404)

    buffer = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for job in jobs:
            name = Path(job.result_file.name).name
            base, ext = name.rsplit(".", 1) if "." in name else (name, "md")
            candidate = name
            counter = 2
            while candidate in used_names:
                candidate = f"{base} ({counter}).{ext}"
                counter += 1
            used_names.add(candidate)

            with job.result_file.open("rb") as f:
                zip_file.writestr(candidate, f.read())

    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="markdowns_convertidos.zip"'
    return response


def progress_status(request, job_id):
    job = get_object_or_404(ConversionJob, pk=job_id)

    step_labels = {
        ConversionJob.STEP_SPLITTING: "Dividindo PDF em blocos...",
        ConversionJob.STEP_CONVERTING: f"Convertendo bloco {job.current_block}/{job.total_blocks}...",
        ConversionJob.STEP_VALIDATING: (
            f"Validando bloco {job.current_block}/{job.total_blocks}"
            + (f" (tentativa {job.fix_attempt + 1}/{job.fix_max + 1})..." if job.fix_attempt else "...")
        ),
        ConversionJob.STEP_MERGING: "Unindo blocos no documento final...",
        ConversionJob.STEP_RATE_LIMITED: (
            f"Limite de requisições da API atingido. "
            f"Tentativa {job.retry_attempt}/{job.retry_max}, aguardando {job.retry_wait_seconds}s..."
        ),
        ConversionJob.STEP_FIXING: (
            f"Corrigindo bloco {job.current_block}/{job.total_blocks} com base na validação "
            f"(tentativa {job.fix_attempt}/{job.fix_max})..."
        ),
    }

    if job.status == ConversionJob.STATUS_QUEUED:
        step_label = "Na fila, aguardando sua vez..."
    else:
        step_label = step_labels.get(job.current_step, "")

    data = {
        "status": job.status,
        "step": job.current_step,
        "step_label": step_label,
        "current_block": job.current_block,
        "total_blocks": job.total_blocks,
        "progress_percent": job.progress_percent(),
        "error_message": job.error_message,
        "result_url": job.result_file.url if job.result_file else None,
        "filename": job.original_filename,
        "needs_review": job.needs_review,
        "review_notes": job.review_notes,
    }
    return JsonResponse(data)
