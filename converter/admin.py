from django.contrib import admin

from .models import ConversionJob


@admin.register(ConversionJob)
class ConversionJobAdmin(admin.ModelAdmin):
    list_display = ("id", "original_filename", "status", "current_block", "total_blocks", "created_at")
    list_filter = ("status",)
    readonly_fields = [f.name for f in ConversionJob._meta.fields]
