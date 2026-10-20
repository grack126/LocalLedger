"""PDF extraction: native text -> OCR fallback -> optional local LLM."""
from .base import DocumentText, EngineUnavailable, ExtractedFields, ExtractionError, PageText
from .native import extract_native, page_count, render_page_png, sha256_bytes
from .parsers import parse_text
from .pipeline import ExtractionPipeline, ExtractionResult, PipelineOptions, infer_vat_code, merge_fields

__all__ = [
    "DocumentText", "EngineUnavailable", "ExtractedFields", "ExtractionError", "PageText",
    "extract_native", "page_count", "render_page_png", "sha256_bytes", "parse_text",
    "ExtractionPipeline", "ExtractionResult", "PipelineOptions", "infer_vat_code", "merge_fields",
]
