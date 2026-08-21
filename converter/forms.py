from django import forms
from django.conf import settings


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiplePdfField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"multiple": True, "accept": "application/pdf"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_field = forms.FileField(required=self.required)
        if isinstance(data, (list, tuple)):
            return [single_field.clean(item, initial) for item in data]
        return [single_field.clean(data, initial)]


class PdfUploadForm(forms.Form):
    pdf_files = MultiplePdfField(required=True)

    def clean_pdf_files(self):
        files = self.cleaned_data["pdf_files"]
        limite = settings.MAX_PDF_BYTES
        for f in files:
            if not f.name.lower().endswith(".pdf"):
                raise forms.ValidationError(f"'{f.name}' não é um arquivo PDF.")
            # Único limite de tamanho do lado do servidor. O aviso na tela de upload é
            # JavaScript e só desabilita o botão — um `curl` direto passa por cima dele.
            if f.size > limite:
                raise forms.ValidationError(
                    f"'{f.name}' tem {f.size / 1024 / 1024:.1f} MB e excede o limite de "
                    f"{limite / 1024 / 1024:.0f} MB por arquivo."
                )
            header = f.read(5)
            f.seek(0)
            if header != b"%PDF-":
                raise forms.ValidationError(
                    f"'{f.name}' não parece ser um PDF válido (assinatura incorreta)."
                )
        return files
