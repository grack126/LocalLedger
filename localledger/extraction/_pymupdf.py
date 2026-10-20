"""Import PyMuPDF under its modern name, falling back to the legacy ``fitz``."""
try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz  # type: ignore[no-redef]

__all__ = ["fitz"]
