"""OCR fallback for scanned pages: Tesseract (default) or EasyOCR.

Both engines are optional. If neither is installed, scanned PDFs still load
into the staging area with a warning so the user can key the values in.
"""
from __future__ import annotations

import io
import logging
import os
import platform
import shutil
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Protocol, Sequence

from .base import EngineUnavailable, PageText
from .native import render_page_png

log = logging.getLogger(__name__)

_COMMON_TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/usr/bin/tesseract",
)

INSTALL_HINT = {
    "Windows": "Install Tesseract from https://github.com/UB-Mannheim/tesseract/wiki and restart the app.",
    "Darwin": "Install Tesseract with: brew install tesseract",
    "Linux": "Install Tesseract with: sudo apt install tesseract-ocr  (or your distro's package)",
}


class OcrEngine(Protocol):
    name: str

    def available(self) -> bool: ...

    def recognize(self, png: bytes) -> tuple[str, float]:
        """Return (text, quality 0-1) for one rendered page image."""
        ...


def find_tesseract(configured: str | None = None) -> str | None:
    candidates = [configured] if configured else []
    which = shutil.which("tesseract")
    if which:
        candidates.append(which)
    candidates += list(_COMMON_TESSERACT_PATHS)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


class TesseractEngine:
    name = "tesseract"

    def __init__(self, cmd: str | None = None, *, lang: str = "eng", psm: int = 3) -> None:
        self.cmd = find_tesseract(cmd)
        self.lang = lang
        self.config = f"--oem 1 --psm {psm}"

    def available(self) -> bool:
        if not self.cmd:
            return False
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            return False
        return True

    def recognize(self, png: bytes) -> tuple[str, float]:
        if not self.available():
            raise EngineUnavailable("Tesseract is not installed. "
                                    + INSTALL_HINT.get(platform.system(), ""))
        import pytesseract
        from PIL import Image

        pytesseract.pytesseract.tesseract_cmd = self.cmd
        image = Image.open(io.BytesIO(png)).convert("L")
        data = pytesseract.image_to_data(image, lang=self.lang, config=self.config,
                                         output_type=pytesseract.Output.DICT)
        lines: dict[tuple[int, int, int], list[str]] = defaultdict(list)
        confidences: list[float] = []
        for i, word in enumerate(data["text"]):
            word = (word or "").strip()
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1
            if not word:
                continue
            lines[(data["block_num"][i], data["par_num"][i], data["line_num"][i])].append(word)
            if conf >= 0:
                confidences.append(conf)
        text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
        quality = (sum(confidences) / len(confidences) / 100) if confidences else 0.0
        return text, round(quality, 3)


@lru_cache(maxsize=2)
def _easyocr_reader(langs: tuple[str, ...]):
    import easyocr

    return easyocr.Reader(list(langs), gpu=False, verbose=False)


class EasyOcrEngine:
    name = "easyocr"

    def __init__(self, langs: Sequence[str] = ("en",)) -> None:
        self.langs = tuple(langs)

    def available(self) -> bool:
        try:
            import easyocr  # noqa: F401
        except ImportError:
            return False
        return True

    def recognize(self, png: bytes) -> tuple[str, float]:
        if not self.available():
            raise EngineUnavailable("EasyOCR is not installed (pip install easyocr).")
        results = _easyocr_reader(self.langs).readtext(png, detail=1, paragraph=False)
        # Group words into lines by vertical position.
        items = sorted(((min(p[1] for p in box), min(p[0] for p in box), text, conf)
                        for box, text, conf in results), key=lambda t: (t[0], t[1]))
        lines: list[list[tuple[float, str]]] = []
        last_y: float | None = None
        for y, x, text, _conf in items:
            if last_y is None or abs(y - last_y) > 12:
                lines.append([])
                last_y = y
            lines[-1].append((x, text))
        text = "\n".join(" ".join(t for _, t in sorted(line)) for line in lines)
        confs = [float(c) for *_, c in items]
        return text, round(sum(confs) / len(confs), 3) if confs else 0.0


def get_engine(name: str, *, tesseract_cmd: str | None = None) -> OcrEngine | None:
    """Return the configured engine, or None when OCR is disabled."""
    name = (name or "tesseract").lower()
    if name == "none":
        return None
    if name == "easyocr":
        return EasyOcrEngine()
    return TesseractEngine(tesseract_cmd or None)


def ocr_pages(data: bytes, page_numbers: Sequence[int], engine: OcrEngine, *, dpi: int = 300) -> list[PageText]:
    """OCR the given 1-based pages of a PDF."""
    zoom = max(1.0, dpi / 72)
    pages = []
    for number in page_numbers:
        png = render_page_png(data, number, zoom=zoom)
        text, quality = engine.recognize(png)
        text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
        pages.append(PageText(number, text, "ocr", quality, len(text)))
    return pages
