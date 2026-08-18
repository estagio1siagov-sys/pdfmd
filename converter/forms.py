from django import forms


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
        for f in files:
            if not f.name.lower().endswith(".pdf"):
                raise forms.ValidationError(f"'{f.name}' não é um arquivo PDF.")
            header = f.read(5)
            f.seek(0)
            if header != b"%PDF-":
                raise forms.ValidationError(
                    f"'{f.name}' não parece ser um PDF válido (assinatura incorreta)."
                )
        return files
