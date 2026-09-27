"""Server-side page rendering (the browser never parses untrusted PDFs)."""
from __future__ import annotations

import io
from functools import lru_cache

import pypdfium2 as pdfium


def _render(data: bytes, page_no: int, scale: float) -> "pdfium.PdfBitmap":
    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[page_no - 1]
        return page.render(scale=scale).to_pil()
    finally:
        pdf.close()


@lru_cache(maxsize=128)
def render_page_png(data: bytes, page_no: int, scale: float = 1.6) -> bytes:
    image = _render(data, page_no, scale)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_crop(data: bytes, page_no: int, box: list[int], scale: float = 3.0, pad: int = 12) -> bytes:
    image = _render(data, page_no, scale)
    w, h = image.size
    ymin, xmin, ymax, xmax = box
    crop = image.crop((max(0, xmin * w // 1000 - pad), max(0, ymin * h // 1000 - pad),
                       min(w, xmax * w // 1000 + pad), min(h, ymax * h // 1000 + pad)))
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()
