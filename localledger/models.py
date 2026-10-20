"""Shared domain models and parsing helpers.

This module is pure Python (no database, UI or PDF imports) so that
``rules``, ``validator`` and ``db`` can all depend on it without import cycles.
"""
from __future__ import annotations

import math
import re
import unicodedata
import uuid
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal("0")


# =============================================================================
# Internal field names
# =============================================================================
class F:
    """Canonical internal field names (used by templates and the staging grid)."""

    TYPE = "Type"
    ACCOUNT_CODE = "Account Code"
    NOMINAL_CODE = "Nominal Code"
    INVOICE_NUMBER = "Invoice Number"
    INVOICE_DATE = "Invoice Date"
    SERVICE_PERIOD = "Service Period"
    DESCRIPTION = "Description"
    NET = "Net Amount"
    VAT_CODE = "VAT Code"
    VAT = "VAT Amount"
    GROSS = "Gross Amount"
    CURRENCY = "Currency"
    EXCHANGE_RATE = "Exchange Rate"
    # Extended fields (needed by some presets, e.g. Xero wants a contact name)
    COMPANY_NAME = "Company Name"
    SERVICE_MONTH = "Service Month"
    SERVICE_START = "Service Period Start"
    SERVICE_END = "Service Period End"
    RAW_DESCRIPTION = "Raw Description"
    LINE_NUMBER = "Line Number"
    SOURCE_FILE = "Source File"
    BASE_NET = "Base Net Amount"
    BASE_VAT = "Base VAT Amount"
    BASE_GROSS = "Base Gross Amount"


STANDARD_FIELDS: tuple[str, ...] = (
    F.TYPE, F.ACCOUNT_CODE, F.NOMINAL_CODE, F.INVOICE_NUMBER, F.INVOICE_DATE,
    F.SERVICE_PERIOD, F.DESCRIPTION, F.NET, F.VAT_CODE, F.VAT, F.GROSS,
    F.CURRENCY, F.EXCHANGE_RATE,
)
EXTENDED_FIELDS: tuple[str, ...] = (
    F.COMPANY_NAME, F.SERVICE_MONTH, F.SERVICE_START, F.SERVICE_END,
    F.RAW_DESCRIPTION, F.LINE_NUMBER, F.SOURCE_FILE,
    F.BASE_NET, F.BASE_VAT, F.BASE_GROSS,
)
ALL_FIELDS: tuple[str, ...] = STANDARD_FIELDS + EXTENDED_FIELDS
AMOUNT_FIELDS = frozenset({F.NET, F.VAT, F.GROSS, F.BASE_NET, F.BASE_VAT, F.BASE_GROSS})
DATE_FIELDS = frozenset({F.INVOICE_DATE, F.SERVICE_START, F.SERVICE_END})

# Common ISO-4217 codes. Unknown codes raise a warning, not an error.
ISO_CURRENCIES = frozenset({
    "GBP", "EUR", "USD", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON",
    "BGN", "ISK", "TRY", "CAD", "AUD", "NZD", "JPY", "CNY", "HKD", "SGD", "INR",
    "ZAR", "AED", "SAR", "ILS", "KRW", "MXN", "BRL", "THB", "MYR", "IDR", "PHP",
})

MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_label(d: date | None) -> str:
    """'Oct 2026' — locale-independent."""
    return f"{MONTH_ABBR[d.month - 1]} {d.year}" if d else ""


# =============================================================================
# Document types
# =============================================================================
class DocType(StrEnum):
    PI = "PI"  # Purchase Invoice
    PC = "PC"  # Purchase Credit
    SI = "SI"  # Sales Invoice
    SC = "SC"  # Sales Credit

    @property
    def is_purchase(self) -> bool:
        return self in (DocType.PI, DocType.PC)

    @property
    def is_sales(self) -> bool:
        return not self.is_purchase

    @property
    def is_credit(self) -> bool:
        return self in (DocType.PC, DocType.SC)

    @property
    def entity_type(self) -> str:
        return "Supplier" if self.is_purchase else "Client"

    @property
    def ledger(self) -> str:
        return "purchase" if self.is_purchase else "sales"

    @property
    def label(self) -> str:
        return _DOC_LABELS[self]

    def as_credit(self) -> DocType:
        return DocType.PC if self.is_purchase else DocType.SC

    def as_invoice(self) -> DocType:
        return DocType.PI if self.is_purchase else DocType.SI

    @classmethod
    def coerce(cls, value: Any, default: DocType | None = None) -> DocType:
        if isinstance(value, cls):
            return value
        text = "" if value is None else str(value).strip().upper()
        if text in cls.__members__:
            return cls[text]
        alias = _DOC_ALIASES.get(re.sub(r"\s+", " ", text))
        if alias:
            return cls[alias]
        if default is not None:
            return default
        raise ValueError(f"Unknown document type: {value!r}")


_DOC_LABELS = {
    DocType.PI: "Purchase Invoice", DocType.PC: "Purchase Credit",
    DocType.SI: "Sales Invoice", DocType.SC: "Sales Credit",
}
_DOC_ALIASES = {
    "PURCHASE INVOICE": "PI", "BILL": "PI", "PURCHASE": "PI",
    "PURCHASE CREDIT": "PC", "PURCHASE CREDIT NOTE": "PC", "SUPPLIER CREDIT": "PC",
    "SALES INVOICE": "SI", "INVOICE": "SI", "SALE": "SI", "SALES": "SI",
    "SALES CREDIT": "SC", "SALES CREDIT NOTE": "SC", "CUSTOMER CREDIT": "SC",
}


# =============================================================================
# Normalisation helpers
# =============================================================================
_LEGAL_SUFFIXES = frozenset({
    "ltd", "limited", "plc", "llp", "lp", "llc", "inc", "incorporated", "corp",
    "corporation", "co", "company", "gmbh", "ag", "sa", "sarl", "sas", "bv", "nv",
    "pty", "srl", "spa", "oy", "ab", "as", "aps", "the", "and",
})


def normalize_name(name: Any) -> str:
    """Lower-case, strip accents/punctuation and legal suffixes for fuzzy matching.

    ``"The ACME Widgets Co. Ltd."`` -> ``"acme widgets"``
    """
    if name is None:
        return ""
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = text.split()
    core = [t for t in tokens if t not in _LEGAL_SUFFIXES]
    return " ".join(core or tokens)


def normalize_vat_number(value: Any) -> str:
    """Upper-case alphanumeric VAT number; the UK 'GB' prefix is dropped."""
    if value is None:
        return ""
    text = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    if text.startswith("GB") and text[2:].isdigit():
        text = text[2:]
    return text


def normalize_invoice_number(value: Any) -> str:
    """Upper-case alphanumeric only: ``'inv-00123 '`` -> ``'INV00123'``."""
    if value is None:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()


def clean_str(value: Any) -> str:
    """Stringify a cell value; NaN/None become ''; 4000.0 becomes '4000'."""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null", "nat"} else text


# =============================================================================
# Money
# =============================================================================
def to_decimal(value: Any) -> Decimal | None:
    """Parse a money-like value. Handles '£1,234.56', '(12.00)', '12.00 CR',
    '1.234,56', '-€5' and plain numbers. Returns None if unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else Decimal(repr(value))

    text = str(value).strip()
    if not text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip()
    upper = text.upper()
    if upper.endswith("CR"):
        negative, text = True, text[:-2]
    elif upper.endswith("DR"):
        text = text[:-2]
    text = text.strip()
    if text.endswith("-"):
        negative, text = True, text[:-1]
    text = re.sub(r"[^\d,.\-]", "", text)
    if "-" in text:
        negative = True
        text = text.replace("-", "")
    if not text or not any(c.isdigit() for c in text):
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):   # 1.234,56
            text = text.replace(".", "").replace(",", ".")
        else:                                    # 1,234.56
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        # A single comma followed by 1-2 digits is a decimal comma ("12,5" / "12,50").
        text = f"{head}.{tail}" if text.count(",") == 1 and len(tail) in (1, 2) else text.replace(",", "")
    elif text.count(".") > 1:
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    return -result if negative else result


def money(value: Any) -> Decimal | None:
    """Parse and round half-up to 2 decimal places."""
    d = to_decimal(value)
    return None if d is None else d.quantize(CENT, rounding=ROUND_HALF_UP)


def to_minor(value: Any) -> int | None:
    """Money -> integer minor units (pence)."""
    d = money(value)
    return None if d is None else int(d * 100)


def from_minor(value: int | None) -> Decimal | None:
    return None if value is None else (Decimal(int(value)) / 100).quantize(CENT)


def fmt_money(value: Any, currency: str = "") -> str:
    d = money(value)
    if d is None:
        return "—"
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency.upper(), "")
    text = f"{abs(d):,.2f}"
    if symbol:
        text = symbol + text
    elif currency:
        text = f"{text} {currency.upper()}"
    return f"-{text}" if d < 0 else text


# =============================================================================
# Dates
# =============================================================================
_DMY_FORMATS = (
    "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%d %B %y",
    "%b %d %Y", "%B %d %Y", "%Y/%m/%d", "%Y%m%d",
)
_MDY_FORMATS = (
    "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%m.%d.%Y",
    "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d %b %y",
    "%Y/%m/%d", "%Y%m%d",
)
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$")
_MIN_YEAR, _MAX_YEAR = 1990, 2100


def parse_date(value: Any, *, dayfirst: bool = True) -> date | None:
    """Parse common invoice date formats. ``dayfirst`` resolves 01/02/2026."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):  # pandas Timestamp / NaT
        try:
            return value.to_pydatetime().date()
        except (AttributeError, ValueError, TypeError):
            return None

    text = clean_str(value)
    if not text:
        return None
    match = _ISO_RE.match(text)
    if match:
        try:
            result = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return None
        return result if _MIN_YEAR <= result.year <= _MAX_YEAR else None

    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    text = re.sub(r"\bsept\b", "Sep", text, flags=re.I)
    text = re.sub(r"[,]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    for fmt in (_DMY_FORMATS if dayfirst else _MDY_FORMATS):
        try:
            result = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if _MIN_YEAR <= result.year <= _MAX_YEAR:
            return result
    try:
        from dateutil import parser as du_parser

        result = du_parser.parse(text, dayfirst=dayfirst, fuzzy=False).date()
    except (ValueError, OverflowError, ImportError, TypeError):
        return None
    return result if _MIN_YEAR <= result.year <= _MAX_YEAR else None


# =============================================================================
# Validation artefacts
# =============================================================================
class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Issue:
    code: str
    message: str
    severity: Severity = Severity.WARNING
    fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "severity": str(self.severity), "fields": list(self.fields)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Issue:
        return cls(code=data["code"], message=data["message"],
                   severity=Severity(data.get("severity", "warning")),
                   fields=tuple(data.get("fields", ())))


DUPLICATE_TIER_NAMES = {
    1: "Exact file match",
    2: "Same account + invoice number",
    3: "Same account + amount within date window",
}


@dataclass
class DuplicateFlag:
    tier: int                 # 1 = exact file, 2 = strict accounting, 3 = fuzzy suspicion
    source: str               # "batch" | "history"
    matched_ref: str          # staging group_id or invoice_logs.id
    message: str

    @property
    def key(self) -> str:
        """Stable identifier, used to remember dismissals across re-validation."""
        return f"T{self.tier}:{self.source}:{self.matched_ref}"

    @property
    def tier_name(self) -> str:
        return DUPLICATE_TIER_NAMES.get(self.tier, f"Tier {self.tier}")

    def to_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "source": self.source,
                "matched_ref": self.matched_ref, "message": self.message}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DuplicateFlag:
        return cls(int(data["tier"]), data["source"], str(data["matched_ref"]), data["message"])


# =============================================================================
# Staging row
# =============================================================================
def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class StagingRow:
    """One posting line in the staging area.

    A document normally produces one row. "Split Invoice" turns it into
    several rows that share ``group_id`` (and ``file_hash``); ``doc_gross``
    holds the document's gross total, which the split lines must add up to.
    """

    # --- accounting fields --------------------------------------------------
    doc_type: DocType = DocType.PI
    account_code: str = ""
    nominal_code: str = ""
    invoice_number: str = ""
    invoice_date: date | None = None
    service_period_start: date | None = None
    service_period_end: date | None = None
    raw_description: str = ""
    description: str = ""
    net: Decimal | None = None
    vat_code: str = ""
    vat: Decimal | None = None
    gross: Decimal | None = None
    currency: str = "GBP"
    exchange_rate: Decimal | None = None

    # --- entity resolution --------------------------------------------------
    company_name: str = ""             # name as printed on the document
    entity_name: str = ""              # clean name from the master list
    vat_reg_no: str = ""
    entity_id: int | None = None
    entity_match_score: float | None = None   # 0-100
    entity_match_method: str = ""             # vat | exact | alias | fuzzy | manual | suggested | none

    # --- provenance ---------------------------------------------------------
    source_file: str = ""
    file_path: str = ""
    file_hash: str = ""
    raw_text: str = ""
    extraction_method: str = "manual"         # native | ocr | llm | manual
    extraction_quality: float | None = None   # 0-1 overall OCR/LLM clarity
    field_confidence: dict[str, float] = field(default_factory=dict)  # 0-1 per internal field

    # --- splitting ----------------------------------------------------------
    row_id: str = field(default_factory=_new_id)
    group_id: str = ""
    line_no: int = 1
    doc_gross: Decimal | None = None

    # --- validation output (written by validator) ---------------------------
    confidence: int = 0
    confidence_band: str = ""
    field_scores: dict[str, int] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    error_fields: list[str] = field(default_factory=list)
    duplicate_flags: list[DuplicateFlag] = field(default_factory=list)
    dismissed_flags: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.doc_type = DocType.coerce(self.doc_type, DocType.PI)
        if not self.group_id:
            self.group_id = self.row_id
        for name in _DECIMAL_ATTRS:
            setattr(self, name, money(getattr(self, name)) if name != "exchange_rate"
                    else to_decimal(getattr(self, name)))
        for name in _DATE_ATTRS:
            setattr(self, name, parse_date(getattr(self, name)))
        self.currency = (self.currency or "GBP").strip().upper()

    # ------------------------------------------------------------------ props
    @property
    def is_manual(self) -> bool:
        return self.extraction_method == "manual"

    @property
    def open_duplicate_flags(self) -> list[DuplicateFlag]:
        return [f for f in self.duplicate_flags if f.key not in self.dismissed_flags]

    @property
    def display_name(self) -> str:
        return self.entity_name or self.company_name or self.account_code

    # ----------------------------------------------------------- serialise
    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (Decimals -> str, dates -> ISO)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, date):
                value = value.isoformat()
            elif isinstance(value, DocType):
                value = value.value
            elif f.name == "issues":
                value = [i.to_dict() for i in value]
            elif f.name == "duplicate_flags":
                value = [d.to_dict() for d in value]
            elif isinstance(value, set):
                value = sorted(value)
            elif isinstance(value, dict):
                value = dict(value)
            elif isinstance(value, list):
                value = list(value)
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StagingRow:
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["issues"] = [i if isinstance(i, Issue) else Issue.from_dict(i)
                            for i in kwargs.get("issues") or []]
        kwargs["duplicate_flags"] = [d if isinstance(d, DuplicateFlag) else DuplicateFlag.from_dict(d)
                                     for d in kwargs.get("duplicate_flags") or []]
        kwargs["dismissed_flags"] = set(kwargs.get("dismissed_flags") or ())
        for key in ("entity_match_score", "extraction_quality"):
            if kwargs.get(key) is not None:
                try:
                    kwargs[key] = float(kwargs[key])
                except (TypeError, ValueError):
                    kwargs[key] = None
        for key in _STR_ATTRS:
            if key in kwargs:
                kwargs[key] = clean_str(kwargs[key])
        return cls(**kwargs)


_DECIMAL_ATTRS = ("net", "vat", "gross", "exchange_rate", "doc_gross")
_DATE_ATTRS = ("invoice_date", "service_period_start", "service_period_end")
_STR_ATTRS = ("account_code", "nominal_code", "invoice_number", "vat_code",
              "raw_description", "description", "company_name", "entity_name", "vat_reg_no")
