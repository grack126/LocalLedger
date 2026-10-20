"""Native PDF text extraction (pdfplumber, falling back to PyMuPDF) and page rendering."""
from __future__ import annotations

import hashlib
import io
import logging
import re
from pathlib import Path

from .base import DocumentText, ExtractionError, PageText

log = logging.getLogger(__name__)

MIN_PAGE_CHARS = 40          # below this a page is treated as scanned
_CID_RE = re.compile(r"\(cid:\d+\)")


def read_bytes(source: str | Path | bytes) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    return Path(source).read_bytes()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_quality(text: str) -> float:
    """0-1 heuristic for how usable extracted text is.

    Penalises pdfminer '(cid:NN)' glyph garbage, replacement characters and a
    low share of letters/digits (typical of broken font encodings).
    """
    if not text or not text.strip():
        return 0.0
    cid_hits = len(_CID_RE.findall(text))
    cleaned = _CID_RE.sub("", text)
    visible = [c for c in cleaned if not c.isspace()]
    if not visible:
        return 0.0
    alnum_ratio = sum(c.isalnum() for c in visible) / len(visible)
    bad_ratio = (cleaned.count("�") + cid_hits * 3) / max(len(visible), 1)
    score = min(1.0, alnum_ratio / 0.6) * (1.0 - min(1.0, bad_ratio * 4))
    return round(max(0.0, min(1.0, score)), 3)


def _clean(text: str) -> str:
    text = _CID_RE.sub("", text or "")
    text = text.replace(" ", " ").replace("ﬁ", "fi").replace("ﬂ", "fl")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _visible_chars(pages: list[str]) -> int:
    return sum(len(_CID_RE.sub("", p)) - sum(c.isspace() for c in p) for p in pages)


def _with_pdfplumber(data: bytes) -> list[str]:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [page.extract_text(x_tolerance=1.5, y_tolerance=3) or "" for page in pdf.pages]


def _with_pymupdf(data: bytes) -> list[str]:
    from ._pymupdf import fitz

    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ExtractionError("PDF is password-protected.")
        return [page.get_text("text", sort=True) or "" for page in doc]


def extract_native(data: bytes, *, min_page_chars: int = MIN_PAGE_CHARS) -> DocumentText:
    """Extract the text layer of every page.

    pdfplumber is tried first (better reading order for invoice layouts);
    PyMuPDF is used if pdfplumber fails or returns noticeably less text.
    """
    if not data[:1024].lstrip().startswith(b"%PDF"):
        raise ExtractionError("File is not a PDF.")
    texts: list[str] | None = None
    errors: list[str] = []
    try:
        texts = _with_pdfplumber(data)
    except Exception as exc:  # pdfminer raises a wide range of exceptions on bad files
        errors.append(f"pdfplumber: {exc}")
    try:
        alt = _with_pymupdf(data)
        # Compare visible characters only: PyMuPDF's layout output is padded with
        # spaces, which says nothing about how much text was actually recovered.
        if texts is None or _visible_chars(alt) > 1.2 * _visible_chars(texts):
            texts = alt
    except ExtractionError:
        raise
    except Exception as exc:
        errors.append(f"PyMuPDF: {exc}")
    if texts is None:
        raise ExtractionError("Could not read PDF (" + "; ".join(errors) + ")")

    pages = []
    for number, raw in enumerate(texts, start=1):
        quality = text_quality(raw)
        cleaned = _clean(raw)
        pages.append(PageText(number, cleaned, "native", quality, len(cleaned)))
    return DocumentText(pages, len(pages))


def page_needs_ocr(page: PageText, *, min_chars: int = MIN_PAGE_CHARS, min_quality: float = 0.5) -> bool:
    return page.char_count < min_chars or page.quality < min_quality


def render_page_png(data: bytes, page_number: int = 1, *, zoom: float = 1.5) -> bytes:
    """Render one page (1-based) to PNG — used by OCR and the split-screen preview."""
    from ._pymupdf import fitz

    with fitz.open(stream=data, filetype="pdf") as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"Page {page_number} out of range (1-{doc.page_count}).")
        pix = doc[page_number - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")


def page_count(data: bytes) -> int:
    from ._pymupdf import fitz

    with fitz.open(stream=data, filetype="pdf") as doc:
        return doc.page_count
