from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.errors import FileNotDecryptedError


def split_pdf(pdf_path, block_size):
    """Recorta um PDF em blocos de `block_size` páginas.

    Retorna uma lista de bytes, cada item sendo um sub-PDF válido.
    """
    try:
        with open(pdf_path, "rb") as f:
            reader = PdfReader(f)
            if reader.is_encrypted:
                raise ValueError("PDF protegido por senha não é suportado. Remova a senha antes de enviar.")
            total_pages = len(reader.pages)
            blocks = []
            for start in range(0, total_pages, block_size):
                end = min(start + block_size, total_pages)
                writer = PdfWriter()
                for page_index in range(start, end):
                    writer.add_page(reader.pages[page_index])
                buffer = BytesIO()
                writer.write(buffer)
                blocks.append({
                    "pdf_bytes": buffer.getvalue(),
                    "start_page": start + 1,
                    "end_page": end,
                })
    except FileNotDecryptedError:
        raise ValueError("PDF protegido por senha não é suportado. Remova a senha antes de enviar.")

    return blocks
