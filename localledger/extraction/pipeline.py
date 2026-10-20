"""Extraction pipeline: PDF bytes -> StagingRow ready for the staging grid.

    1. native text layer (pdfplumber / PyMuPDF), per page
    2. OCR for pages without usable text (Tesseract / EasyOCR)
    3. regex field parsing with Net + VAT = Gross reconciliation
    4. optional local LLM — as a fallback when fields are missing/inconsistent,
       or always (merged, with grounding checks against the text)
    5. purchase vs sales detection (is the issuer *your* company?)
    6. entity resolution against the master list (+ vendor memory)
    7. VAT code inference from the printed rate / effective rate
    8. business rules: credit-note flip, service period, description builder
    9. FX rate for foreign currency (batched for multi-file uploads)

Every step degrades gracefully: a missing OCR engine or offline LLM adds a
warning, never an exception, so the document still reaches the staging area.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from rapidfuzz import fuzz

from ..models import DocType, F, StagingRow, normalize_name, normalize_vat_number
from ..rules import DescriptionRule, apply_rules, load_description_rules
from .base import DocumentText, EngineUnavailable, ExtractedFields, ExtractionError
from .llm import LLMConfig, LLMError, LocalLLMClient
from .native import MIN_PAGE_CHARS, extract_native, page_needs_ocr, read_bytes, sha256_bytes
from .ocr import INSTALL_HINT, OcrEngine, get_engine, ocr_pages
from .parsers import missing_core_fields, parse_text

if TYPE_CHECKING:
    from ..db import Database, EntityMatch, EntityResolver
    from ..fx import FxQuote, FxService

log = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int, str], None]


@dataclass
class PipelineOptions:
    base_currency: str = "GBP"
    dayfirst: bool = True
    direction: str = "auto"                 # auto | purchase | sales
    own_company_name: str = ""
    own_vat_number: str = ""
    ocr_engine: str = "tesseract"           # tesseract | easyocr | none
    tesseract_cmd: str = ""
    ocr_dpi: int = 300
    max_ocr_pages: int = 10
    min_page_chars: int = MIN_PAGE_CHARS
    llm_enabled: bool = False
    llm_mode: str = "fallback"              # fallback | always
    llm_endpoint: str = "http://localhost:11434/v1"
    llm_model: str = "llama3.1:8b"
    llm_timeout: float = 120.0
    llm_allow_remote: bool = False

    @classmethod
    def from_settings(cls, s: Mapping[str, Any], **overrides: Any) -> PipelineOptions:
        opts = cls(
            base_currency=str(s.get("base_currency", "GBP")).upper(),
            dayfirst=str(s.get("date_order", "DMY")).upper() != "MDY",
            direction=s.get("extraction_direction", "auto"),
            own_company_name=s.get("own_company_name", ""),
            own_vat_number=s.get("own_vat_number", ""),
            ocr_engine=s.get("ocr_engine", "tesseract"),
            tesseract_cmd=s.get("tesseract_cmd", ""),
            ocr_dpi=int(s.get("ocr_dpi", 300)),
            llm_enabled=bool(s.get("llm_enabled", False)),
            llm_mode=s.get("llm_mode", "fallback"),
            llm_endpoint=s.get("llm_endpoint", "http://localhost:11434/v1"),
            llm_model=s.get("llm_model", "llama3.1:8b"),
            llm_timeout=float(s.get("llm_timeout", 120)),
            llm_allow_remote=bool(s.get("llm_allow_remote", False)),
        )
        for key, value in overrides.items():
            setattr(opts, key, value)
        return opts


@dataclass
class ExtractionResult:
    filename: str
    file_hash: str
    rows: list[StagingRow]
    fields: ExtractedFields | None = None
    document: DocumentText | None = None
    log: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    entity_match: EntityMatch | None = None
    fx_quote: FxQuote | None = None

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def row(self) -> StagingRow:
        return self.rows[0]


# =============================================================================
# Field merging
# =============================================================================
_SCALARS = (
    ("invoice_number", F.INVOICE_NUMBER), ("invoice_date", F.INVOICE_DATE), ("currency", F.CURRENCY),
    ("description", F.DESCRIPTION), ("issuer_name", F.COMPANY_NAME),
)


def merge_fields(regex: ExtractedFields, llm: ExtractedFields, *, prefer_llm: bool = False) -> ExtractedFields:
    """Combine regex and LLM results field by field.

    An LLM value is taken when the regex found nothing, when the LLM is more
    confident, or (``prefer_llm``) when it is reasonably grounded. Amounts are
    taken as a reconciled triple only.
    """
    out = copy.deepcopy(regex)
    out.source = "regex+llm"
    taken: list[str] = []
    for attr, name in _SCALARS:
        new = getattr(llm, attr)
        if not new:
            continue
        old_conf, new_conf = regex.confidence.get(name, 0.0), llm.confidence.get(name, 0.0)
        if not getattr(regex, attr) or new_conf > old_conf or (prefer_llm and new_conf >= 0.7):
            setattr(out, attr, new)
            out.confidence[name] = new_conf
            taken.append(name)

    # Only accept LLM amounts that add up AND are all printed in the document;
    # otherwise the model may "fix" a genuine supplier math error the validator must flag.
    llm_amounts_ok = llm.amounts_reconciled and min(llm.confidence.get(n, 0) for n in (F.NET, F.VAT, F.GROSS)) >= 0.8
    if llm_amounts_ok and (not regex.amounts_reconciled or prefer_llm):
        out.net, out.vat, out.gross, out.amounts_reconciled = llm.net, llm.vat, llm.gross, True
        for name in (F.NET, F.VAT, F.GROSS):
            out.confidence[name] = llm.confidence[name]
        taken.append("Amounts")

    if llm.issuer_name:
        out.issuer_candidates = [llm.issuer_name] + [c for c in regex.issuer_candidates if c != llm.issuer_name]
        out.issuer_name = out.issuer_candidates[0] if (F.COMPANY_NAME in taken or not regex.issuer_name) \
            else regex.issuer_name
    if llm.recipient_name and llm.recipient_name not in out.recipient_candidates:
        out.recipient_candidates = [llm.recipient_name] + out.recipient_candidates
    for number in llm.vat_numbers:
        norm = normalize_vat_number(number)
        if norm and norm not in out.vat_numbers:
            out.vat_numbers.append(norm)
    out.vat_rate = regex.vat_rate if regex.vat_rate is not None else llm.vat_rate
    out.service_period_start = llm.service_period_start or regex.service_period_start
    out.service_period_end = llm.service_period_end or regex.service_period_end
    out.is_credit_hint = llm.is_credit_hint
    out.notes.append("LLM supplied: " + (", ".join(taken) if taken else "nothing new"))
    return out


# =============================================================================
# VAT code inference
# =============================================================================
def infer_vat_code(
    vat_codes: Mapping[str, Any],
    *,
    printed_rate: Decimal | None,
    net: Decimal | None,
    vat: Decimal | None,
    current: str = "",
    tolerance_pp: Decimal = Decimal("0.5"),
) -> tuple[str, str]:
    """Pick a VAT code for the document. Returns (code, reason).

    An existing code (e.g. the supplier's default) is kept when it agrees with
    the document; otherwise the active, non-reverse-charge code whose rate
    matches the printed or effective rate is chosen (lowest sort order wins,
    so 0% resolves to T0 rather than T2/T9).
    """
    effective: Decimal | None = None
    if net not in (None, Decimal(0)) and vat is not None:
        effective = abs(vat) / abs(net) * 100   # type: ignore[arg-type]
    observed = effective if effective is not None else printed_rate

    def invoice_rate(entry: Any) -> Decimal:
        return Decimal(0) if getattr(entry, "is_reverse_charge", False) else Decimal(str(entry.rate))

    if current:
        entry = vat_codes.get(current.upper())
        if entry is not None and (observed is None or abs(invoice_rate(entry) - observed) <= tolerance_pp):
            return current.upper(), "supplier default"
    if observed is None:
        return current.upper(), "no rate on document"
    candidates = sorted(
        (e for e in vat_codes.values()
         if getattr(e, "is_active", True) and not getattr(e, "is_reverse_charge", False)
         and abs(Decimal(str(e.rate)) - observed) <= tolerance_pp),
        key=lambda e: (abs(Decimal(str(e.rate)) - observed), getattr(e, "sort_order", 0)))
    if candidates:
        reason = f"{'printed' if effective is None else 'effective'} rate {observed:.1f}%"
        if current and current.upper() != candidates[0].code.upper():
            reason += f" (supplier default {current.upper()} does not match)"
        return candidates[0].code.upper(), reason
    return current.upper(), f"no VAT code with rate {observed:.1f}%"


# =============================================================================
# Pipeline
# =============================================================================
class ExtractionPipeline:
    def __init__(
        self,
        options: PipelineOptions | None = None,
        *,
        resolver: EntityResolver | None = None,
        vat_codes: Mapping[str, Any] | None = None,
        description_rules: Sequence[DescriptionRule] | None = None,
        fx: FxService | None = None,
        ocr_engine: OcrEngine | None = None,
        llm_client: LocalLLMClient | Any | None = None,
    ) -> None:
        self.options = options or PipelineOptions()
        self.resolver = resolver
        self.vat_codes = {k.upper(): v for k, v in (vat_codes or {}).items()}
        self.description_rules = list(description_rules or [])
        self.fx = fx
        self._ocr_engine = ocr_engine
        self._llm_client = llm_client

    @classmethod
    def from_db(cls, db: Database, **overrides: Any) -> ExtractionPipeline:
        from ..fx import FxService

        settings = db.get_settings()
        kwargs: dict[str, Any] = {
            "resolver": db.entity_resolver(),
            "vat_codes": db.vat_code_map(),
            "description_rules": load_description_rules(settings.get("description_rules")),
        }
        if "fx" not in overrides:
            kwargs["fx"] = FxService.from_db(db)
        kwargs.update(overrides)
        options = kwargs.pop("options", None) or PipelineOptions.from_settings(settings)
        return cls(options, **kwargs)

    # ----------------------------------------------------------- engines
    @property
    def ocr_engine(self) -> OcrEngine | None:
        if self._ocr_engine is None:
            self._ocr_engine = get_engine(self.options.ocr_engine, tesseract_cmd=self.options.tesseract_cmd)
        return self._ocr_engine

    def llm_client(self) -> Any:
        if self._llm_client is None:
            o = self.options
            self._llm_client = LocalLLMClient(LLMConfig(o.llm_endpoint, o.llm_model, o.llm_timeout, o.llm_allow_remote))
        return self._llm_client

    def engine_status(self) -> dict[str, str]:
        """Human-readable availability of optional engines (for the Settings page)."""
        status = {}
        engine = self.ocr_engine
        if engine is None:
            status["OCR"] = "disabled"
        elif engine.available():
            status["OCR"] = f"{engine.name} ready"
        else:
            import platform

            status["OCR"] = f"{engine.name} not found. " + INSTALL_HINT.get(platform.system(), "")
        if not self.options.llm_enabled:
            status["LLM"] = "disabled"
        else:
            try:
                ok = self.llm_client().available()
                status["LLM"] = f"{self.options.llm_model} @ {self.options.llm_endpoint} " + \
                                ("ready" if ok else "not responding")
            except EngineUnavailable as exc:
                status["LLM"] = str(exc)
        return status

    # ----------------------------------------------------------- steps
    def _read_text(self, data: bytes, result: ExtractionResult) -> DocumentText:
        doc = extract_native(data, min_page_chars=self.options.min_page_chars)
        scanned = [p.number for p in doc.pages if page_needs_ocr(p, min_chars=self.options.min_page_chars)]
        if not scanned:
            result.log.append(f"Native text extracted from {doc.page_count} page(s).")
            return doc
        engine = self.ocr_engine
        if engine is None or not engine.available():
            name = engine.name if engine else "OCR"
            result.warnings.append(
                f"{len(scanned)} page(s) look scanned but {name} is unavailable — enter values manually "
                "or install Tesseract.")
            return doc
        todo = scanned[: self.options.max_ocr_pages]
        if len(scanned) > len(todo):
            result.warnings.append(f"Only the first {len(todo)} scanned pages were OCR'd.")
        try:
            ocr_results = {p.number: p for p in ocr_pages(data, todo, engine, dpi=self.options.ocr_dpi)}
        except EngineUnavailable as exc:
            result.warnings.append(str(exc))
            return doc
        doc.pages = [ocr_results[p.number] if p.number in ocr_results and
                     ocr_results[p.number].char_count >= p.char_count else p for p in doc.pages]
        quality = sum(p.quality for p in ocr_results.values()) / max(len(ocr_results), 1)
        result.log.append(f"OCR ({engine.name}) on page(s) {todo}; mean word confidence {quality:.0%}.")
        if quality < 0.6:
            result.warnings.append("Low OCR confidence — check every field against the PDF.")
        return doc

    def _maybe_llm(self, text: str, fields: ExtractedFields, result: ExtractionResult) -> ExtractedFields:
        o = self.options
        if not o.llm_enabled or not text.strip():
            return fields
        missing = missing_core_fields(fields)
        if o.llm_mode != "always" and not missing:
            return fields
        try:
            llm_fields = self.llm_client().extract(text, dayfirst=o.dayfirst)
        except (EngineUnavailable, LLMError) as exc:
            result.warnings.append(f"LLM step skipped: {exc}")
            return fields
        merged = merge_fields(fields, llm_fields, prefer_llm=o.llm_mode == "always")
        reason = "always-on" if o.llm_mode == "always" else "missing " + ", ".join(missing)
        result.log.append(f"Local LLM ({o.llm_model}) used ({reason}). {merged.notes[-1]}")
        return merged

    def _direction(self, fields: ExtractedFields) -> tuple[DocType, list[str], str]:
        """Return (doc type, counterparty name candidates, reason)."""
        o = self.options
        own_name = normalize_name(o.own_company_name)
        own_vat = normalize_vat_number(o.own_vat_number)

        def is_own(name: str) -> bool:
            return bool(own_name) and fuzz.ratio(normalize_name(name), own_name) >= 90

        issuers = [c for c in fields.issuer_candidates if not is_own(c)]
        recipients = [c for c in fields.recipient_candidates if not is_own(c)]
        if o.direction == "purchase":
            return DocType.PI, issuers or recipients, "set to Purchases"
        if o.direction == "sales":
            return DocType.SI, recipients or issuers, "set to Sales"
        issuer_is_own = any(is_own(c) for c in fields.issuer_candidates[:2]) or \
            (bool(own_vat) and fields.vat_numbers[:1] == [own_vat])
        if issuer_is_own:
            return DocType.SI, recipients or issuers, "issued by your company"
        return DocType.PI, issuers, "issued by a third party"

    def _resolve_entity(self, row: StagingRow, candidates: list[str], fields: ExtractedFields,
                        result: ExtractionResult) -> None:
        own_vat = normalize_vat_number(self.options.own_vat_number)
        vat_numbers = [v for v in fields.vat_numbers if v != own_vat]
        row.vat_reg_no = vat_numbers[0] if vat_numbers else ""
        row.company_name = candidates[0] if candidates else ""
        if self.resolver is None:
            return
        from ..db import apply_entity_to_row

        entity_type = row.doc_type.entity_type
        best: EntityMatch | None = None
        best_name = row.company_name
        for vat_no in vat_numbers[:3] or [""]:
            for name in (candidates[:5] or [""]):
                match = self.resolver.resolve(name, vat_no or None, entity_type)
                if best is None or (match.auto_assigned, match.score) > (best.auto_assigned, best.score):
                    best, best_name = match, name
                if match.method == "vat":
                    break
        if best is None:
            return
        result.entity_match = best
        row.company_name = best_name or row.company_name
        row.entity_match_score = best.score
        row.entity_match_method = best.method
        if best.auto_assigned and best.entity:
            apply_entity_to_row(row, best.entity)
            result.log.append(f"Matched {entity_type.lower()} {best.entity.account_code} "
                              f"({best.method}, {best.score:.0f}%).")
        elif best.candidates:
            options = ", ".join(f"{e.account_code} {s:.0f}%" for e, s in best.candidates[:3])
            result.warnings.append(f"No confident {entity_type.lower()} match for '{row.company_name}'. "
                                   f"Suggestions: {options}. {best.note}".strip())
        else:
            result.warnings.append(f"'{row.company_name or 'Unknown'}' is not in the {entity_type} master list.")

    # ----------------------------------------------------------- public API
    def process_bytes(self, data: bytes, filename: str, *, file_path: str = "",
                      apply_fx: bool = True) -> ExtractionResult:
        o = self.options
        file_hash = sha256_bytes(data)
        row = StagingRow(source_file=filename, file_path=file_path, file_hash=file_hash,
                         currency=o.base_currency, extraction_method="native", extraction_quality=0.0)
        result = ExtractionResult(filename, file_hash, [row])
        try:
            doc = self._read_text(data, result)
        except ExtractionError as exc:
            result.error = str(exc)
            result.warnings.append(f"Could not read {filename}: {exc}")
            return result
        result.document = doc
        text = doc.text
        if not text.strip():
            result.warnings.append("No text could be read from this document.")

        fields = parse_text(text, dayfirst=o.dayfirst, base_currency=o.base_currency)
        fields = self._maybe_llm(text, fields, result)
        result.fields = fields
        result.log += fields.notes

        doc_type, candidates, reason = self._direction(fields)
        row.doc_type = doc_type
        if fields.is_credit_hint:
            row.doc_type = doc_type.as_credit()
        result.log.append(f"Treated as {row.doc_type.label} ({reason}).")

        row.invoice_number = fields.invoice_number
        row.invoice_date = fields.invoice_date
        row.currency = fields.currency or o.base_currency
        row.net, row.vat, row.gross = fields.net, fields.vat, fields.gross
        row.raw_description = fields.description
        row.raw_text = text
        row.service_period_start = fields.service_period_start
        row.service_period_end = fields.service_period_end
        row.extraction_method = doc.method if doc.method != "none" else "native"
        row.extraction_quality = round(doc.quality, 3)
        row.field_confidence = dict(fields.confidence)

        self._resolve_entity(row, candidates, fields, result)

        if self.vat_codes:
            code, why = infer_vat_code(self.vat_codes, printed_rate=fields.vat_rate, net=row.net,
                                       vat=row.vat, current=row.vat_code)
            if code and code != row.vat_code:
                result.log.append(f"VAT code {code} from {why}.")
            row.vat_code = code

        result.log += apply_rules(row, text=text, description_rules=self.description_rules, dayfirst=o.dayfirst)

        if apply_fx and self.fx is not None:
            self._apply_fx(row, result)
        return result

    def _apply_fx(self, row: StagingRow, result: ExtractionResult) -> None:
        assert self.fx is not None
        if row.currency == self.fx.base or row.invoice_date is None:
            return
        quote = self.fx.apply_to_row(row)
        result.fx_quote = quote
        if quote is None:
            result.warnings.append(f"No {row.currency} rate for {row.invoice_date:%d/%m/%Y} — enter it manually. "
                                   f"{self.fx.last_error}".strip())
        else:
            result.log.append(f"Exchange rate: {quote.describe()}")
            if quote.is_stale:
                result.warnings.append(f"Using a stale exchange rate ({quote.note}).")

    def process_file(self, path: str | Path, **kwargs: Any) -> ExtractionResult:
        path = Path(path)
        return self.process_bytes(read_bytes(path), path.name, file_path=str(path), **kwargs)

    def process_many(
        self,
        items: Iterable[str | Path | tuple[str, bytes] | tuple[str, bytes, str]],
        *,
        progress: ProgressCallback | None = None,
    ) -> list[ExtractionResult]:
        """Process a batch. Items are paths, (filename, bytes) or
        (filename, bytes, stored_path) tuples. FX rates are fetched once for the batch."""
        items = list(items)
        results: list[ExtractionResult] = []
        for idx, item in enumerate(items, start=1):
            if isinstance(item, tuple):
                name, data = item[0], item[1]
                path = str(item[2]) if len(item) > 2 else ""
            else:
                name, path = Path(item).name, str(item)
                data = b""
            if progress:
                progress(idx, len(items), name)
            try:
                payload = data or read_bytes(path)
                results.append(self.process_bytes(payload, name, file_path=path, apply_fx=False))
            except Exception as exc:  # one bad file must not stop the batch
                log.exception("Extraction failed for %s", name)
                failed = StagingRow(source_file=name, file_path=path, extraction_quality=0.0,
                                    extraction_method="native")
                results.append(ExtractionResult(name, "", [failed], error=f"Unexpected error: {exc}",
                                                warnings=[f"Unexpected error reading {name}: {exc}"]))
        if self.fx is not None:
            rows = [r.row for r in results if r.ok]
            self.fx.prefetch((r.currency, r.invoice_date) for r in rows if r.invoice_date)
            for res in results:
                if res.ok:
                    self._apply_fx(res.row, res)
        return results
