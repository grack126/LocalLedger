"""Validation: math cross-checks, confidence scoring, 3-tier duplicate detection.

Confidence model (0-100)
------------------------
    score = 40% math  +  25% entity match  +  25% field completeness/format  +  10% extraction clarity

* Math (critical): Net + VAT = Gross (± tolerance) and VAT/Net matches the VAT
  code's rate. A failure forces the row to LOW (< 50) and marks the amount
  cells as errors (shown red in the staging grid).
* Entity: VAT-number/exact/alias match = 100, fuzzy = rapidfuzz score.
* Completeness: required fields present and plausible, multiplied by any
  per-field confidence the extractor reported.
* Clarity: OCR/LLM quality (native PDF text scores highest).
Other blocking errors (e.g. missing nominal code) cap the score below HIGH.

Duplicate tiers
---------------
    1. Exact file    — same SHA-256 of the PDF (renamed copies included)
    2. Strict        — same account + same normalised invoice number
    3. Fuzzy         — same account + same gross + invoice date within ±N days
Tiers 2/3 compare invoices with invoices and credits with credits, so a
credit note that reverses an invoice is not flagged. Each document is checked
against the rest of the staging batch and the exported history.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from .models import (
    CENT,
    ISO_CURRENCIES,
    ZERO,
    DocType,
    DuplicateFlag,
    F,
    Issue,
    Severity,
    StagingRow,
    fmt_money,
    from_minor,
    normalize_invoice_number,
    to_minor,
)

if TYPE_CHECKING:
    from .db import Database, HistoricalInvoice, VatCode

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"
MATH_FAIL_CAP = 49          # spec: math failure forces LOW (< 50)
ERROR_CAP = 64              # any other blocking error keeps the row out of HIGH

WEIGHTS = {"math": 0.40, "entity": 0.25, "completeness": 0.25, "clarity": 0.10}
CLARITY_BY_METHOD = {"manual": 1.0, "native": 0.95, "llm": 0.85, "ocr": 0.75}
CORE_FIELDS = (F.ACCOUNT_CODE, F.INVOICE_NUMBER, F.INVOICE_DATE, F.NET, F.VAT, F.GROSS,
               F.NOMINAL_CODE, F.VAT_CODE, F.CURRENCY, F.DESCRIPTION)
AMOUNT_FIELD_NAMES = (F.NET, F.VAT, F.GROSS)


# =============================================================================
# Settings
# =============================================================================
@dataclass
class ValidationSettings:
    math_tolerance: Decimal = Decimal("0.02")
    vat_rate_tolerance_pp: Decimal = Decimal("0.25")
    high_threshold: int = 85
    medium_threshold: int = 50
    duplicate_window_days: int = 3
    base_currency: str = "GBP"
    allow_export_with_errors: bool = False
    future_date_days: int = 7
    stale_date_days: int = 540

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any] | None) -> ValidationSettings:
        s = settings or {}
        default = cls()
        return cls(
            math_tolerance=Decimal(str(s.get("math_tolerance", default.math_tolerance))),
            vat_rate_tolerance_pp=Decimal(str(s.get("vat_rate_tolerance_pp", default.vat_rate_tolerance_pp))),
            high_threshold=int(s.get("confidence_high", default.high_threshold)),
            medium_threshold=min(int(s.get("confidence_medium", default.medium_threshold)),
                                 int(s.get("confidence_high", default.high_threshold))),
            duplicate_window_days=int(s.get("duplicate_window_days", default.duplicate_window_days)),
            base_currency=str(s.get("base_currency", default.base_currency)).upper(),
            allow_export_with_errors=bool(s.get("allow_export_with_errors", False)),
        )

    def band(self, score: int) -> str:
        if score >= self.high_threshold:
            return HIGH
        if score >= self.medium_threshold:
            return MEDIUM
        return LOW


# =============================================================================
# Math cross-check
# =============================================================================
@dataclass
class MathCheck:
    sum_ok: bool | None            # None = could not be checked
    vat_rate_ok: bool | None
    difference: Decimal | None     # net + vat - gross
    effective_rate: Decimal | None
    expected_rate: Decimal | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.sum_ok is not True or self.vat_rate_ok is False

    @property
    def passed(self) -> bool:
        return self.sum_ok is True and self.vat_rate_ok is True


def _vat_lookup(vat_codes: Mapping[str, Any], code: str) -> tuple[Decimal, bool] | None:
    """Return (rate %, reverse_charge) for a code. Accepts VatCode objects,
    plain numbers or (rate, reverse_charge) tuples as mapping values."""
    entry = vat_codes.get(code.upper()) if code else None
    if entry is None:
        return None
    if hasattr(entry, "rate"):
        return Decimal(str(entry.rate)), bool(getattr(entry, "is_reverse_charge", False))
    if isinstance(entry, (tuple, list)):
        return Decimal(str(entry[0])), bool(entry[1]) if len(entry) > 1 else False
    return Decimal(str(entry)), False


def check_math(
    net: Decimal | None,
    vat: Decimal | None,
    gross: Decimal | None,
    vat_code: str,
    vat_codes: Mapping[str, Any],
    *,
    tolerance: Decimal = Decimal("0.02"),
    rate_tolerance_pp: Decimal = Decimal("0.25"),
) -> MathCheck:
    """Validate Net + VAT = Gross and VAT / Net against the VAT code's rate."""
    issues: list[Issue] = []
    missing = [name for name, v in ((F.NET, net), (F.VAT, vat), (F.GROSS, gross)) if v is None]
    if missing:
        issues.append(Issue("amount_missing", f"Missing amount(s): {', '.join(missing)}.",
                            Severity.ERROR, tuple(missing)))
        return MathCheck(None, None, None, None, None, issues)
    assert net is not None and vat is not None and gross is not None

    for name, value in ((F.NET, net), (F.VAT, vat), (F.GROSS, gross)):
        if value < 0:
            issues.append(Issue("negative_amount",
                                f"{name} is negative — use a credit type (PC/SC) with positive amounts.",
                                Severity.WARNING, (name,)))

    difference = (net + vat - gross).quantize(CENT)
    sum_ok = abs(difference) <= tolerance
    if not sum_ok:
        issues.append(Issue(
            "sum_mismatch",
            f"Net {net:.2f} + VAT {vat:.2f} = {net + vat:.2f}, but Gross is {gross:.2f} "
            f"(difference {difference:+.2f}).",
            Severity.ERROR, AMOUNT_FIELD_NAMES))

    effective = (vat / net * 100).quantize(Decimal("0.01")) if net != 0 else None
    if not vat_code:
        issues.append(Issue("vat_code_missing", "No VAT code assigned — VAT rate not checked.",
                            Severity.ERROR, (F.VAT_CODE,)))
        return MathCheck(sum_ok, None, difference, effective, None, issues)

    lookup = _vat_lookup(vat_codes, vat_code)
    if lookup is None:
        issues.append(Issue("vat_code_unknown", f"VAT code '{vat_code}' is not set up.",
                            Severity.ERROR, (F.VAT_CODE,)))
        return MathCheck(sum_ok, None, difference, effective, None, issues)

    rate, reverse_charge = lookup
    expected_rate = ZERO if reverse_charge else rate
    expected_vat = (net * expected_rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    within_amount = abs(abs(vat) - abs(expected_vat)) <= tolerance
    within_rate = effective is not None and abs(abs(effective) - expected_rate) <= rate_tolerance_pp
    vat_rate_ok = within_amount or within_rate
    if not vat_rate_ok:
        shown = f"{effective:.2f}%" if effective is not None else "n/a"
        hint = " If the invoice mixes VAT rates, split it into one line per rate." \
            if expected_rate > 0 and vat > 0 else ""
        rc = " (reverse charge — supplier should show 0 VAT)" if reverse_charge else ""
        issues.append(Issue(
            "vat_rate_mismatch",
            f"VAT {vat:.2f} is {shown} of Net; code {vat_code.upper()} expects {expected_rate}%{rc} "
            f"(≈ {expected_vat:.2f}).{hint}",
            Severity.ERROR, (F.VAT, F.VAT_CODE)))
    return MathCheck(sum_ok, vat_rate_ok, difference, effective, expected_rate, issues)


def infer_missing_amounts(
    net: Decimal | None, vat: Decimal | None, gross: Decimal | None, rate_pct: Decimal | None = None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, list[str]]:
    """Fill one missing amount from the other two (or two from one + a rate).
    Returns (net, vat, gross, inferred_field_names). Used by the extraction
    step; inferred fields should get a lower field confidence."""
    inferred: list[str] = []
    q = lambda d: d.quantize(CENT, rounding=ROUND_HALF_UP)  # noqa: E731
    if net is not None and vat is not None and gross is None:
        gross, inferred = q(net + vat), [F.GROSS]
    elif net is not None and gross is not None and vat is None:
        vat, inferred = q(gross - net), [F.VAT]
    elif vat is not None and gross is not None and net is None:
        net, inferred = q(gross - vat), [F.NET]
    elif rate_pct is not None and [net, vat, gross].count(None) == 2:
        if net is not None:
            vat = q(net * rate_pct / 100)
            gross, inferred = q(net + vat), [F.VAT, F.GROSS]
        elif gross is not None:
            net = q(gross * 100 / (100 + rate_pct))
            vat, inferred = q(gross - net), [F.NET, F.VAT]
    return net, vat, gross, inferred


def check_split_groups(rows: Sequence[StagingRow], tolerance: Decimal = Decimal("0.02")) -> dict[str, list[Issue]]:
    """Split lines (same group_id) must add up to the document's gross total."""
    groups: dict[str, list[StagingRow]] = defaultdict(list)
    for row in rows:
        groups[row.group_id].append(row)
    out: dict[str, list[Issue]] = {}
    for lines in groups.values():
        target = next((r.doc_gross for r in lines if r.doc_gross is not None), None)
        if target is None or (len(lines) == 1 and lines[0].gross == target):
            continue
        total = sum((r.gross or ZERO) for r in lines)
        diff = (total - target).quantize(CENT)
        if abs(diff) > tolerance:
            issue = Issue(
                "split_total_mismatch",
                f"Split lines total {total:.2f} but the invoice gross is {target:.2f} "
                f"({diff:+.2f}). Adjust a line so they agree.",
                Severity.ERROR, (F.GROSS,))
            for r in lines:
                out[r.row_id] = [issue]
    return out


# =============================================================================
# Field-level checks
# =============================================================================
_INVOICE_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 /_.#:\-]{0,39}$")
_DATE_LIKE_RE = re.compile(r"^\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}$")


def score_invoice_number(value: str, *, required: bool = True) -> tuple[float, Issue | None]:
    value = (value or "").strip()
    if not value:
        sev = Severity.ERROR if required else Severity.WARNING
        return 0.0, Issue("invoice_number_missing", "Invoice number is missing.", sev, (F.INVOICE_NUMBER,))
    if not _INVOICE_NO_RE.match(value):
        return 0.4, Issue("invoice_number_format", f"Invoice number '{value}' has unusual characters or length.",
                          Severity.WARNING, (F.INVOICE_NUMBER,))
    if _DATE_LIKE_RE.match(value):
        return 0.5, Issue("invoice_number_date", f"Invoice number '{value}' looks like a date — check extraction.",
                          Severity.WARNING, (F.INVOICE_NUMBER,))
    if not any(ch.isdigit() for ch in value):
        return 0.6, Issue("invoice_number_nodigits", f"Invoice number '{value}' contains no digits.",
                          Severity.WARNING, (F.INVOICE_NUMBER,))
    return 1.0, None


def score_invoice_date(value: date | None, today: date, vs: ValidationSettings) -> tuple[float, Issue | None]:
    if value is None:
        return 0.0, Issue("date_missing", "Invoice date is missing or unreadable.", Severity.ERROR, (F.INVOICE_DATE,))
    if value > today + timedelta(days=vs.future_date_days):
        return 0.4, Issue("date_future", f"Invoice date {value:%d/%m/%Y} is in the future.",
                          Severity.WARNING, (F.INVOICE_DATE,))
    if value < today - timedelta(days=vs.stale_date_days):
        return 0.6, Issue("date_old", f"Invoice date {value:%d/%m/%Y} is more than "
                                      f"{vs.stale_date_days // 30} months old — check the year.",
                          Severity.WARNING, (F.INVOICE_DATE,))
    return 1.0, None


# =============================================================================
# Row validation & confidence
# =============================================================================
@dataclass
class RowValidation:
    row_id: str
    score: int
    band: str
    field_scores: dict[str, int]
    components: dict[str, float]
    issues: list[Issue]
    error_fields: list[str]
    math: MathCheck

    @property
    def has_errors(self) -> bool:
        return any(i.severity == Severity.ERROR for i in self.issues)


def _entity_component(row: StagingRow, known_accounts: Mapping[str, set[str]] | None) -> tuple[float, Issue | None]:
    code = row.account_code.strip().upper()
    if not code:
        return 0.0, Issue("account_missing", "No account code — pick the supplier/client.",
                          Severity.ERROR, (F.ACCOUNT_CODE,))
    known = (known_accounts or {}).get(row.doc_type.entity_type)
    in_master = known is not None and code in known
    if known_accounts is not None and not in_master:
        return 0.5, Issue("account_unknown",
                          f"Account '{row.account_code}' is not in the {row.doc_type.entity_type} master list.",
                          Severity.WARNING, (F.ACCOUNT_CODE,))
    if row.entity_match_method in ("vat", "exact", "alias", "manual") or row.entity_match_score is None:
        return 1.0, None
    score = max(0.0, min(100.0, float(row.entity_match_score))) / 100
    if score < 0.85:
        return score, Issue("entity_low_match",
                            f"Supplier/client matched at only {row.entity_match_score:.0f}% — please confirm.",
                            Severity.WARNING, (F.ACCOUNT_CODE,))
    return score, None


def validate_row(
    row: StagingRow,
    *,
    vat_codes: Mapping[str, Any],
    settings: ValidationSettings | None = None,
    known_accounts: Mapping[str, set[str]] | None = None,
    extra_issues: Sequence[Issue] = (),
    today: date | None = None,
    write_back: bool = True,
) -> RowValidation:
    """Validate one row and compute its confidence. With ``write_back`` the
    results are stored on the row (confidence, band, issues, ...)."""
    vs = settings or ValidationSettings()
    today = today or date.today()
    issues: list[Issue] = []
    fs: dict[str, float] = {}           # field -> 0..1 format score

    def conf(name: str) -> float:
        value = row.field_confidence.get(name)
        return 1.0 if value is None else max(0.0, min(1.0, float(value)))

    # --- math (critical) ---------------------------------------------------
    math = check_math(row.net, row.vat, row.gross, row.vat_code, vat_codes,
                      tolerance=vs.math_tolerance, rate_tolerance_pp=vs.vat_rate_tolerance_pp)
    issues += math.issues
    issues += list(extra_issues)
    split_failed = any(i.code == "split_total_mismatch" for i in extra_issues)
    math_failed = math.failed or split_failed
    for name in AMOUNT_FIELD_NAMES:
        value = getattr(row, {F.NET: "net", F.VAT: "vat", F.GROSS: "gross"}[name])
        fs[name] = 0.0 if value is None else (0.4 if math_failed else 1.0)
    if math.passed and not split_failed:
        math_component = 1.0
    elif math.sum_ok and math.vat_rate_ok is None and not split_failed:
        math_component = 0.6              # sum fine, VAT rate not checkable
    else:
        math_component = 0.0

    # --- entity -----------------------------------------------------------
    entity_component, entity_issue = _entity_component(row, known_accounts)
    fs[F.ACCOUNT_CODE] = entity_component
    if entity_issue:
        issues.append(entity_issue)

    # --- completeness / format --------------------------------------------
    s, issue = score_invoice_number(row.invoice_number, required=not row.is_manual)
    fs[F.INVOICE_NUMBER] = s
    if issue:
        issues.append(issue)
    s, issue = score_invoice_date(row.invoice_date, today, vs)
    fs[F.INVOICE_DATE] = s
    if issue:
        issues.append(issue)

    if row.nominal_code.strip():
        fs[F.NOMINAL_CODE] = 1.0
    else:
        fs[F.NOMINAL_CODE] = 0.0
        issues.append(Issue("nominal_missing", "Nominal code is missing.", Severity.ERROR, (F.NOMINAL_CODE,)))
    fs[F.VAT_CODE] = 1.0 if row.vat_code and math.vat_rate_ok is not None else (0.5 if row.vat_code else 0.0)

    currency = row.currency.upper()
    if len(currency) != 3 or not currency.isalpha():
        fs[F.CURRENCY] = 0.0
        issues.append(Issue("currency_invalid", f"Currency '{row.currency}' is not a 3-letter code.",
                            Severity.ERROR, (F.CURRENCY,)))
    elif currency not in ISO_CURRENCIES:
        fs[F.CURRENCY] = 0.7
        issues.append(Issue("currency_unusual", f"Currency '{currency}' is unusual — check it.",
                            Severity.WARNING, (F.CURRENCY,)))
    else:
        fs[F.CURRENCY] = 1.0

    if currency != vs.base_currency:
        if row.exchange_rate is None:
            fs[F.EXCHANGE_RATE] = 0.5
            issues.append(Issue("fx_missing", f"{currency} invoice has no exchange rate to {vs.base_currency}.",
                                Severity.WARNING, (F.EXCHANGE_RATE,)))
        elif row.exchange_rate <= 0:
            fs[F.EXCHANGE_RATE] = 0.0
            issues.append(Issue("fx_invalid", "Exchange rate must be greater than zero.",
                                Severity.ERROR, (F.EXCHANGE_RATE,)))
        else:
            fs[F.EXCHANGE_RATE] = 1.0
    elif row.exchange_rate not in (None, Decimal(1)):
        fs[F.EXCHANGE_RATE] = 0.7
        issues.append(Issue("fx_unexpected", f"Exchange rate set on a {vs.base_currency} invoice.",
                            Severity.WARNING, (F.EXCHANGE_RATE,)))

    if (row.description or row.raw_description).strip():
        fs[F.DESCRIPTION] = 1.0
    else:
        fs[F.DESCRIPTION] = 0.5
        issues.append(Issue("description_missing", "Description is empty.", Severity.WARNING, (F.DESCRIPTION,)))

    # --- combine with extractor confidence ---------------------------------
    field_scores = {name: int(round(100 * s * conf(name))) for name, s in fs.items()}
    error_fields = sorted({f for i in issues if i.severity == Severity.ERROR for f in i.fields})
    for name in error_fields:
        field_scores[name] = min(field_scores.get(name, 0), 40)

    completeness = sum(field_scores.get(name, 0) for name in CORE_FIELDS) / (100 * len(CORE_FIELDS))
    clarity = row.extraction_quality if row.extraction_quality is not None \
        else CLARITY_BY_METHOD.get(row.extraction_method, 0.8)
    clarity = max(0.0, min(1.0, float(clarity)))
    components = {"math": math_component, "entity": entity_component,
                  "completeness": completeness, "clarity": clarity}
    score = int(round(100 * sum(WEIGHTS[k] * v for k, v in components.items())))

    if math_failed:
        score = min(score, MATH_FAIL_CAP, vs.medium_threshold - 1)
    elif error_fields:
        score = min(score, ERROR_CAP, vs.high_threshold - 1)
    score = max(0, min(100, score))
    band = vs.band(score)

    result = RowValidation(row.row_id, score, band, field_scores, components, issues, error_fields, math)
    if write_back:
        row.confidence = score
        row.confidence_band = band
        row.field_scores = field_scores
        row.issues = issues
        row.error_fields = error_fields
    return result


# =============================================================================
# Duplicate detection
# =============================================================================
@dataclass
class _Doc:
    """One document (a staging group or a historical log) for comparison."""
    ref: str
    source: str                 # "batch" | "history"
    doc_type: str
    account: str
    invoice_number: str
    invoice_norm: str
    invoice_date: date | None
    gross_minor: int | None
    file_hash: str
    file_name: str
    exported_at: str = ""
    row_ids: list[str] = field(default_factory=list)


def _batch_docs(rows: Sequence[StagingRow]) -> list[_Doc]:
    groups: dict[str, list[StagingRow]] = defaultdict(list)
    for row in rows:
        groups[row.group_id].append(row)
    docs = []
    for gid, lines in groups.items():
        head = min(lines, key=lambda r: r.line_no)
        gross = head.doc_gross
        if gross is None and any(r.gross is not None for r in lines):
            gross = sum((r.gross or ZERO) for r in lines)
        docs.append(_Doc(
            ref=gid, source="batch", doc_type=head.doc_type.value,
            account=head.account_code.strip().upper(), invoice_number=head.invoice_number.strip(),
            invoice_norm=normalize_invoice_number(head.invoice_number), invoice_date=head.invoice_date,
            gross_minor=to_minor(abs(gross)) if gross is not None else None,
            file_hash=head.file_hash, file_name=head.source_file or "(manual entry)",
            row_ids=[r.row_id for r in lines]))
    return docs


def _history_doc(h: HistoricalInvoice) -> _Doc:
    return _Doc(
        ref=str(h.id), source="history", doc_type=h.doc_type, account=(h.account_code or "").upper(),
        invoice_number=h.invoice_number or "", invoice_norm=h.invoice_number_norm or
        normalize_invoice_number(h.invoice_number), invoice_date=h.invoice_date,
        gross_minor=abs(h.gross_minor) if h.gross_minor is not None else None,
        file_hash=h.file_hash or "", file_name=h.source_file_name or "", exported_at=h.exported_at)


def match_tier(a: _Doc, b: _Doc, window_days: int = 3) -> int | None:
    """Most severe tier at which ``a`` and ``b`` look like the same document."""
    if a.file_hash and a.file_hash == b.file_hash:
        return 1
    if not a.account or a.account != b.account:
        return None
    if DocType(a.doc_type).is_credit != DocType(b.doc_type).is_credit:
        return None
    if a.invoice_norm and a.invoice_norm == b.invoice_norm:
        return 2
    if (a.gross_minor is not None and a.gross_minor == b.gross_minor and a.gross_minor != 0
            and a.invoice_date and b.invoice_date
            and abs((a.invoice_date - b.invoice_date).days) <= window_days):
        return 3
    return None


def _describe(other: _Doc) -> str:
    inv = other.invoice_number or "(no number)"
    when = f"{other.invoice_date:%d/%m/%Y}" if other.invoice_date else "no date"
    if other.source == "history":
        exported = (other.exported_at or "")[:10]
        return f"{inv} dated {when}, exported {exported}" + (f" ({other.file_name})" if other.file_name else "")
    return f"{inv} dated {when} in this batch ({other.file_name})"


def _flag(tier: int, me: _Doc, other: _Doc) -> DuplicateFlag:
    if tier == 1:
        msg = f"Identical PDF already {'exported' if other.source == 'history' else 'in batch'}: {_describe(other)}."
    elif tier == 2:
        msg = f"Invoice number {me.invoice_number or other.invoice_number} for {me.account} already seen: {_describe(other)}."
    else:
        days = abs((me.invoice_date - other.invoice_date).days) if me.invoice_date and other.invoice_date else 0
        msg = (f"Same account and gross {fmt_money(from_minor(me.gross_minor))} within {days} day(s): "
               f"{_describe(other)} — possible misread invoice number.")
    return DuplicateFlag(tier, other.source, other.ref, msg)


def detect_duplicates(
    rows: Sequence[StagingRow],
    history: Iterable[HistoricalInvoice] = (),
    *,
    window_days: int = 3,
) -> dict[str, list[DuplicateFlag]]:
    """Return row_id -> duplicate flags. Split lines of one document share
    their flags; lines of the same document never flag each other."""
    docs = _batch_docs(rows)
    hist = [_history_doc(h) for h in history]
    by_hash: dict[str, list[_Doc]] = defaultdict(list)
    by_account: dict[str, list[_Doc]] = defaultdict(list)
    for h in hist:
        if h.file_hash:
            by_hash[h.file_hash].append(h)
        if h.account:
            by_account[h.account].append(h)

    flags: dict[str, dict[str, DuplicateFlag]] = defaultdict(dict)   # group ref -> key -> flag

    def add(me: _Doc, flag: DuplicateFlag) -> None:
        flags[me.ref].setdefault(flag.key, flag)

    for i, a in enumerate(docs):
        for b in docs[i + 1:]:
            tier = match_tier(a, b, window_days)
            if tier:
                add(a, _flag(tier, a, b))
                add(b, _flag(tier, b, a))
        candidates = {id(h): h for h in (by_hash.get(a.file_hash, []) if a.file_hash else [])}
        candidates.update({id(h): h for h in by_account.get(a.account, [])})
        for h in candidates.values():
            tier = match_tier(a, h, window_days)
            if tier:
                add(a, _flag(tier, a, h))

    out: dict[str, list[DuplicateFlag]] = {}
    for doc in docs:
        doc_flags = sorted(flags.get(doc.ref, {}).values(), key=lambda f: (f.tier, f.source, f.matched_ref))
        for row_id in doc.row_ids:
            out[row_id] = list(doc_flags)
    return out


def dismiss_flags(rows: Iterable[StagingRow], group_id: str, keys: Iterable[str] | None = None) -> None:
    """Mark duplicate flags as reviewed for every line of a document."""
    for row in rows:
        if row.group_id == group_id:
            row.dismissed_flags |= set(keys) if keys is not None else {f.key for f in row.duplicate_flags}


# =============================================================================
# Batch orchestration & export gating
# =============================================================================
@dataclass
class BatchReport:
    rows: list[StagingRow]
    results: dict[str, RowValidation]
    settings: ValidationSettings

    def blockers(self, row: StagingRow) -> list[str]:
        return export_blockers(row, self.settings)

    @property
    def exportable(self) -> list[StagingRow]:
        return [r for r in self.rows if not self.blockers(r)]

    @property
    def blocked(self) -> list[StagingRow]:
        return [r for r in self.rows if self.blockers(r)]

    def summary(self) -> dict[str, int]:
        bands = {HIGH: 0, MEDIUM: 0, LOW: 0}
        for r in self.rows:
            bands[r.confidence_band] = bands.get(r.confidence_band, 0) + 1
        return {
            "rows": len(self.rows),
            "documents": len({r.group_id for r in self.rows}),
            "high": bands[HIGH], "medium": bands[MEDIUM], "low": bands[LOW],
            "with_errors": sum(1 for r in self.rows if r.error_fields),
            "open_duplicates": sum(1 for r in self.rows if r.open_duplicate_flags),
            "exportable": len(self.exportable),
        }


def export_blockers(row: StagingRow, settings: ValidationSettings | None = None) -> list[str]:
    """Reasons a row may not be exported yet (empty list = OK to export)."""
    vs = settings or ValidationSettings()
    reasons = [f"Unresolved duplicate ({f.tier_name}) — dismiss the flag or remove the row."
               for f in row.open_duplicate_flags]
    if not vs.allow_export_with_errors:
        reasons += [i.message for i in row.issues if i.severity == Severity.ERROR]
    return reasons


def validate_batch(
    rows: Sequence[StagingRow],
    db: Database | None = None,
    *,
    settings: Mapping[str, Any] | ValidationSettings | None = None,
    vat_codes: Mapping[str, Any] | None = None,
    known_accounts: Mapping[str, set[str]] | None = None,
    history: Iterable[HistoricalInvoice] | None = None,
    today: date | None = None,
) -> BatchReport:
    """Validate a whole staging batch: split totals, math, confidence and
    duplicates (batch + history). Results are written back onto the rows.

    Dependencies are loaded from ``db`` unless passed explicitly, which keeps
    the function easy to unit-test without a database.
    """
    rows = list(rows)
    if isinstance(settings, ValidationSettings):
        vs = settings
    else:
        vs = ValidationSettings.from_settings(settings if settings is not None else
                                              (db.get_settings() if db else None))
    if db is not None:
        if vat_codes is None:
            vat_codes = db.vat_code_map()
        if known_accounts is None:
            known_accounts = db.known_account_codes()
        if history is None:
            history = db.fetch_duplicate_candidates((r.file_hash for r in rows),
                                                    (r.account_code for r in rows))
    vat_codes = {k.upper(): v for k, v in (vat_codes or {}).items()}

    split_issues = check_split_groups(rows, vs.math_tolerance)
    duplicate_flags = detect_duplicates(rows, history or (), window_days=vs.duplicate_window_days)
    results: dict[str, RowValidation] = {}
    for row in rows:
        results[row.row_id] = validate_row(
            row, vat_codes=vat_codes, settings=vs, known_accounts=known_accounts,
            extra_issues=split_issues.get(row.row_id, ()), today=today)
        row.duplicate_flags = duplicate_flags.get(row.row_id, [])
        # forget dismissals for flags that no longer apply
        row.dismissed_flags &= {f.key for f in row.duplicate_flags}
    return BatchReport(rows, results, vs)
