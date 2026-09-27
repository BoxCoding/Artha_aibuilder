"""Server-side page rendering (the browser never parses untrusted PDFs).

PDFium is not thread-safe: two threads inside it at once abort the whole process. Every call into
it therefore holds `_PDFIUM_LOCK`; only the PNG encoding runs outside the lock.

Rendered pages are cached by document digest, never by the document bytes themselves, so the cache
holds small PNGs rather than keeping every uploaded statement alive in memory.
"""
from __future__ import annotations

import hashlib
import io
import threading
from collections import OrderedDict

import pypdfium2 as pdfium

_PDFIUM_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[tuple[str, int, float], bytes] = OrderedDict()
_CACHE_MAX = 96


def _render(data: bytes, page_no: int, scale: float):  # type: ignore[no-untyped-def]
    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(data)
        try:
            if page_no < 1 or page_no > len(pdf):
                raise IndexError(page_no)
            page = pdf[page_no - 1]
            return page.render(scale=scale).to_pil()
        finally:
            pdf.close()


def render_page_png(data: bytes, page_no: int, scale: float = 1.6) -> bytes:
    key = (hashlib.sha256(data).hexdigest(), page_no, scale)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    image = _render(data, page_no, scale)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    png = buf.getvalue()
    with _CACHE_LOCK:
        _CACHE[key] = png
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return png


def render_crop(data: bytes, page_no: int, box: list[int], scale: float = 3.0, pad: int = 12) -> bytes:
    image = _render(data, page_no, scale)
    w, h = image.size
    ymin, xmin, ymax, xmax = box
    crop = image.crop((max(0, xmin * w // 1000 - pad), max(0, ymin * h // 1000 - pad),
                       min(w, xmax * w // 1000 + pad), min(h, ymax * h // 1000 + pad)))
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()
