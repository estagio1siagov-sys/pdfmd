import io
import zipfile
from pathlib import Path

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

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


# POST obrigatório: esta view APAGA os jobs e os arquivos depois de montar o ZIP. Como GET,
# ela era destrutiva e sem proteção de CSRF por definição — um prefetch do navegador, um
# scanner de link ou um `<img src=".../download-all/?ids=1,2,3">` numa página de terceiro
# apagava a conversão de quem estivesse com a fila aberta. Os ids são inteiros sequenciais,
# então adivinhá-los é trivial.
@require_POST
def download_all(request):
    ids_param = request.POST.get("ids", "")
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
            # O aviso de revisao nao pode morrer na tela da fila. Quem baixa o ZIP e
            # deposita os .md na pasta nao volta a essa tela - e um arquivo incompleto
            # com nome normal e' indistinguivel de um arquivo integro. O sufixo carimba
            # a duvida NO NOME, onde ela viaja junto com o artefato e onde a esteira de
            # recepcao consegue barrar antes do acervamento.
            if job.needs_review:
                base = f"{base}__REVISAR"
                name = f"{base}.{ext}"
            candidate = name
            counter = 2
            while candidate in used_names:
                candidate = f"{base} ({counter}).{ext}"
                counter += 1
            used_names.add(candidate)

            with job.result_file.open("rb") as f:
                zip_file.writestr(candidate, f.read())

            if job.needs_review:
                motivo = job.review_notes or "sem detalhe registrado"
                zip_file.writestr(
                    f"{Path(candidate).stem}.MOTIVO.txt",
                    f"ARQUIVO: {candidate}\n"
                    f"ORIGEM:  {job.original_filename}\n\n"
                    f"Este .md NAO passou limpo pela conversao. Conferir antes de\n"
                    f"depositar no acervo.\n\n{motivo}\n",
                )

    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="markdowns_convertidos.zip"'

    # Limpa jobs e arquivos após montar o ZIP — não há razão para guardar após o download.
    for job in jobs:
        try:
            if job.original_pdf:
                job.original_pdf.delete(save=False)
            if job.result_file:
                job.result_file.delete(save=False)
        except Exception:
            pass
    jobs.delete()

    return response


# runner.py e queue_worker.py gravam o traceback completo em `error_message` quando a falha
# não é uma das esperadas. Isso é útil no banco/admin, mas este endpoint é público e o
# job_id é adivinhável — devolver o traceback expõe caminhos do servidor, trechos de código
# e o corpo de erro cru da API do Gemini. O texto técnico fica no banco; o usuário recebe
# uma mensagem que dá pra agir em cima.
PREFIXO_ERRO_INESPERADO = "Erro inesperado"
MSG_ERRO_INESPERADO = (
    "Erro inesperado no processamento deste arquivo. Tente enviar de novo; "
    "se continuar falhando, avise o responsável pelo sistema."
)


def _mensagem_de_erro_para_usuario(job):
    if job.error_message.startswith(PREFIXO_ERRO_INESPERADO):
        return MSG_ERRO_INESPERADO
    return job.error_message


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
        "error_message": _mensagem_de_erro_para_usuario(job),
        "result_url": job.result_file.url if job.result_file else None,
        "filename": job.original_filename,
        "needs_review": job.needs_review,
        "review_notes": job.review_notes,
    }
    return JsonResponse(data)
