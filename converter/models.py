from django.db import models


class ConversionJob(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Na fila"),
        (STATUS_RUNNING, "Processando"),
        (STATUS_DONE, "Concluído"),
        (STATUS_FAILED, "Falhou"),
    ]

    STEP_SPLITTING = "splitting"
    STEP_CONVERTING = "converting"
    STEP_VALIDATING = "validating"
    STEP_MERGING = "merging"
    STEP_RATE_LIMITED = "rate_limited"
    STEP_FIXING = "fixing"

    original_pdf = models.FileField(upload_to="uploads/", max_length=500)
    original_filename = models.CharField(max_length=500)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    current_step = models.CharField(max_length=20, blank=True, default="")
    total_blocks = models.PositiveIntegerField(default=0)
    current_block = models.PositiveIntegerField(default=0)

    retry_attempt = models.PositiveIntegerField(default=0)
    retry_max = models.PositiveIntegerField(default=0)
    retry_wait_seconds = models.PositiveIntegerField(default=0)

    fix_attempt = models.PositiveIntegerField(default=0)
    fix_max = models.PositiveIntegerField(default=0)

    error_message = models.TextField(blank=True, default="")
    result_file = models.FileField(upload_to="results/", blank=True, null=True, max_length=500)

    needs_review = models.BooleanField(default=False)
    review_notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Job {self.pk} - {self.original_filename} ({self.status})"

    def progress_percent(self):
        if self.total_blocks == 0:
            return 0
        return int((self.current_block / self.total_blocks) * 100)
