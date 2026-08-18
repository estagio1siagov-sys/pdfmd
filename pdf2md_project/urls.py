"""
URL configuration for pdf2md_project project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import re

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve as serve_static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('converter.urls')),
]

# Serve mídia (PDFs enviados e Markdown gerado) tanto em DEBUG quanto em produção.
# Não uso django.conf.urls.static.static() aqui porque ele só registra a rota
# quando settings.DEBUG é True, mesmo fora desse if — e aqui rodamos com DEBUG=False
# em produção. É um app de baixo volume/uso pessoal e os arquivos são efêmeros de
# propósito (não persistem entre reinícios do serviço), então não há storage externo (S3).
urlpatterns += [
    re_path(
        r'^%s(?P<path>.*)$' % re.escape(settings.MEDIA_URL.lstrip('/')),
        serve_static,
        {'document_root': settings.MEDIA_ROOT},
    ),
]
