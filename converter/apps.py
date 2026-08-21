import sys

from django.apps import AppConfig


class ConverterConfig(AppConfig):
    name = 'converter'

    def ready(self):
        # Nao inicia durante `manage.py test` (evita threads soltas no ambiente de teste,
        # que nao precisa de limpeza automatica nem worker de fila).
        if "test" in sys.argv:
            return
        from .pipeline.cleanup_worker import ensure_cleanup_started
        ensure_cleanup_started()
