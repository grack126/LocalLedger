"""Data structures shared by the extraction modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


class ExtractionError(Exception):
    """A document could not be read at all (corrupt, encrypted, not a PDF)."""


class EngineUnavailable(Exception):
    """An optional engine (Tesseract, EasyOCR, local LLM) is not installed/reachable."""


@dataclass
class PageText:
    number: int                    # 1-based
    text: str
    method: str                    # "native" | "ocr"
    quality: float                 # 0-1
    char_count: int = 0


@dataclass
class DocumentText:
    pages: list[PageText]
    page_count: int

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)

    @property
    def method(self) -> str:
        methods = {p.method for p in self.pages if p.text.strip()}
        if "ocr" in methods:
            return "ocr"
        return "native" if methods else "none"

    @property
    def quality(self) -> float:
        """Character-weighted mean quality across pages with text."""
        weighted = [(p.quality, max(len(p.text), 1)) for p in self.pages if p.text.strip()]
        if not weighted:
            return 0.0
        return sum(q * w for q, w in weighted) / sum(w for _, w in weighted)


@dataclass
class ExtractedFields:
    """Raw values pulled from a document before they become a StagingRow.

    ``confidence`` holds a 0-1 score per internal field name (models.F.*).
    """

    issuer_name: str = ""
    recipient_name: str = ""
    issuer_candidates: list[str] = field(default_factory=list)
    recipient_candidates: list[str] = field(default_factory=list)
    vat_numbers: list[str] = field(default_factory=list)
    invoice_number: str = ""
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str = ""
    net: Decimal | None = None
    vat: Decimal | None = None
    gross: Decimal | None = None
    vat_rate: Decimal | None = None
    description: str = ""
    line_items: list[str] = field(default_factory=list)
    is_credit_hint: bool | None = None
    service_period_start: date | None = None
    service_period_end: date | None = None
    amounts_reconciled: bool = False
    confidence: dict[str, float] = field(default_factory=dict)
    source: str = "regex"          # "regex" | "llm" | "regex+llm"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        for key, value in out.items():
            if isinstance(value, (Decimal, date)):
                out[key] = str(value)
        return out
