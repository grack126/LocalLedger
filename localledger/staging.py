"""Staging batch: persistence plus the row operations behind the staging UI.

The batch lives in SQLite (``staging_rows``) so a browser refresh or app
restart never loses work. Uploaded PDFs are copied into ``<data>/inbox`` so
they can be previewed and archived later; PDFs imported from a local folder
are referenced in place (and moved on export when archive mode is "move").
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from . import config
from .models import CENT, ZERO, DocType, StagingRow, clean_str, money, parse_date, to_decimal
from .rules import DescriptionRule, build_description

if TYPE_CHECKING:
    from .db import Database, Entity, EntityResolver
    from .extraction.pipeline import ExtractionResult

SPLIT_TOLERANCE = Decimal("0.02")


class SplitError(ValueError):
    pass


# =============================================================================
# Persistence
# =============================================================================
class StagingStore:
    def __init__(self, db: Database, inbox_dir: str | Path | None = None) -> None:
        self.db = db
        if inbox_dir is not None:
            self.inbox_dir = Path(inbox_dir)
        elif db.path is not None:
            self.inbox_dir = db.path.parent / "inbox"
        else:
            self.inbox_dir = config.INBOX_DIR

    def load(self) -> list[StagingRow]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT data FROM staging_rows ORDER BY position").fetchall()
        return [StagingRow.from_dict(json.loads(r["data"])) for r in rows]

    def save(self, rows: Sequence[StagingRow]) -> None:
        """Replace the stored batch with ``rows`` (in display order)."""
        payload = [(r.row_id, r.group_id, i, json.dumps(r.to_dict(), ensure_ascii=False))
                   for i, r in enumerate(rows)]
        with self.db.connect() as conn:
            conn.execute("DELETE FROM staging_rows")
            conn.executemany("INSERT INTO staging_rows (row_id, group_id, position, data) VALUES (?, ?, ?, ?)",
                             payload)

    def clear(self) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM staging_rows")

    def store_upload(self, filename: str, data: bytes, file_hash: str) -> Path:
        """Keep a private copy of an uploaded PDF for preview and archiving."""
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem)[:60] or "document"
        path = self.inbox_dir / f"{file_hash[:16]}_{stem}.pdf"
        if not path.exists():
            path.write_bytes(data)
        return path

    def is_inbox_file(self, path: str | Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.inbox_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    def discard_unreferenced(self, removed: Iterable[StagingRow], remaining: Iterable[StagingRow]) -> int:
        """Delete inbox copies no longer referenced by any staging row."""
        still_used = {r.file_path for r in remaining if r.file_path}
        deleted = 0
        for row in removed:
            if row.file_path and row.file_path not in still_used and self.is_inbox_file(row.file_path):
                try:
                    Path(row.file_path).unlink(missing_ok=True)
                    deleted += 1
                except OSError:
                    pass
        return deleted


# =============================================================================
# Row operations (pure functions on lists of rows)
# =============================================================================
def group_rows(rows: Sequence[StagingRow], group_id: str) -> list[StagingRow]:
    return sorted((r for r in rows if r.group_id == group_id), key=lambda r: r.line_no)


def find_row(rows: Sequence[StagingRow], row_id: str) -> StagingRow | None:
    return next((r for r in rows if r.row_id == row_id), None)


def add_results(rows: list[StagingRow], results: Iterable[ExtractionResult]) -> list[StagingRow]:
    """Append extracted rows to the batch; returns the newly added rows."""
    added = [row for res in results for row in res.rows]
    rows.extend(added)
    return added


def remove_group(rows: list[StagingRow], group_id: str) -> list[StagingRow]:
    """Remove every line of a document. Returns the removed rows."""
    removed = [r for r in rows if r.group_id == group_id]
    rows[:] = [r for r in rows if r.group_id != group_id]
    return removed


def make_manual_row(
    *,
    doc_type: DocType | str = DocType.PI,
    account_code: str = "",
    invoice_number: str = "",
    invoice_date: date | str | None = None,
    nominal_code: str = "",
    vat_code: str = "",
    net: Any = None,
    vat: Any = None,
    gross: Any = None,
    description: str = "",
    currency: str = "GBP",
    exchange_rate: Any = None,
    entity: Entity | None = None,
) -> StagingRow:
    """A row keyed in by hand (paper receipt, adjustment)."""
    net_d, vat_d, gross_d = money(net), money(vat), money(gross)
    if gross_d is None and net_d is not None and vat_d is not None:
        gross_d = net_d + vat_d
    if vat_d is None and net_d is not None and gross_d is not None:
        vat_d = gross_d - net_d
    row = StagingRow(
        doc_type=doc_type, account_code=clean_str(account_code), invoice_number=clean_str(invoice_number),
        invoice_date=parse_date(invoice_date), nominal_code=clean_str(nominal_code),
        vat_code=clean_str(vat_code).upper(), net=net_d, vat=vat_d, gross=gross_d,
        raw_description=description, description=description, currency=currency,
        exchange_rate=exchange_rate, extraction_method="manual", entity_match_method="manual",
        source_file="(manual entry)",
    )
    if entity is not None:
        row.entity_id, row.entity_name = entity.id, entity.company_name
        row.account_code = entity.account_code
        row.nominal_code = row.nominal_code or (entity.default_nominal_code or "")
        row.vat_code = row.vat_code or (entity.default_vat_code or "")
    return row


EDITABLE_FIELDS = {
    "doc_type", "account_code", "nominal_code", "invoice_number", "invoice_date",
    "service_period_start", "service_period_end", "description", "raw_description",
    "net", "vat_code", "vat", "gross", "currency", "exchange_rate", "company_name",
}


def apply_changes(
    row: StagingRow,
    changes: Mapping[str, Any],
    *,
    resolver: EntityResolver | None = None,
) -> dict[str, tuple[Any, Any]]:
    """Apply user edits to a row; returns {field: (old, new)} for what changed.

    Changing the account code re-links the master-list entity and fills
    missing nominal/VAT defaults, and marks the match as manual.
    """
    changed: dict[str, tuple[Any, Any]] = {}
    for name, value in changes.items():
        if name not in EDITABLE_FIELDS:
            raise KeyError(f"'{name}' is not editable")
        if name == "doc_type":
            value = DocType.coerce(value)
        elif name in ("net", "vat", "gross"):
            value = money(value)
        elif name == "exchange_rate":
            value = to_decimal(value) if value not in (None, "", 0) else None
        elif name in ("invoice_date", "service_period_start", "service_period_end"):
            value = parse_date(value)
        elif name in ("vat_code", "currency"):
            value = clean_str(value).upper()
        else:
            value = clean_str(value)
        old = getattr(row, name)
        if old != value:
            setattr(row, name, value)
            changed[name] = (old, value)

    if "account_code" in changed:
        row.entity_match_method = "manual"
        row.entity_match_score = None
        row.entity_id = None
        row.entity_name = ""
        entity = resolver.by_account(row.account_code, row.doc_type.entity_type) if resolver else None
        if entity is not None:
            row.entity_id, row.entity_name = entity.id, entity.company_name
            if not row.nominal_code and entity.default_nominal_code:
                row.nominal_code = entity.default_nominal_code
            if not row.vat_code and entity.default_vat_code:
                row.vat_code = entity.default_vat_code
    return changed


@dataclass
class SplitLine:
    nominal_code: str
    vat_code: str
    net: Decimal
    vat: Decimal
    description: str = ""

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SplitLine:
        net, vat = money(data.get("net")), money(data.get("vat"))
        gross = money(data.get("gross"))
        if net is None and gross is not None and vat is not None:
            net = gross - vat
        if vat is None and gross is not None and net is not None:
            vat = gross - net
        if net is None or vat is None:
            raise SplitError("Every split line needs a Net and a VAT amount (or Gross).")
        return cls(clean_str(data.get("nominal_code")), clean_str(data.get("vat_code")).upper(),
                   net, vat, clean_str(data.get("description")))


def split_document(
    rows: list[StagingRow],
    group_id: str,
    lines: Sequence[SplitLine | Mapping[str, Any]],
    *,
    description_rules: Sequence[DescriptionRule] | None = None,
) -> list[StagingRow]:
    """Replace a document's lines with ``lines`` (linked by group_id).

    The new lines must add up to the document's gross total (± 0.02).
    Returns the new rows, which take the original rows' position.
    """
    current = group_rows(rows, group_id)
    if not current:
        raise SplitError("Document not found in the batch.")
    split_lines = [ln if isinstance(ln, SplitLine) else SplitLine.from_mapping(ln) for ln in lines]
    if not split_lines:
        raise SplitError("Add at least one line.")
    head = current[0]
    target = head.doc_gross if head.doc_gross is not None else sum((r.gross or ZERO) for r in current)
    total = sum((ln.gross for ln in split_lines), ZERO)
    if abs(total - target) > SPLIT_TOLERANCE:
        raise SplitError(f"Split lines total {total:.2f} but the invoice gross is {target:.2f} "
                         f"(difference {total - target:+.2f}).")

    new_rows = []
    for idx, line in enumerate(split_lines, start=1):
        twin = copy.deepcopy(head)
        twin.row_id = uuid.uuid4().hex[:12]
        twin.group_id = group_id
        twin.line_no = idx
        twin.nominal_code = line.nominal_code or head.nominal_code
        twin.vat_code = line.vat_code or head.vat_code
        twin.net, twin.vat, twin.gross = line.net.quantize(CENT), line.vat.quantize(CENT), line.gross.quantize(CENT)
        twin.raw_description = line.description or head.raw_description
        twin.doc_gross = target if len(split_lines) > 1 else None
        twin.description = (build_description(twin, description_rules) if description_rules
                            else twin.raw_description)
        twin.issues, twin.error_fields, twin.field_scores = [], [], {}
        new_rows.append(twin)

    first_index = rows.index(current[0])
    rows[:] = [r for r in rows if r.group_id != group_id]
    rows[first_index:first_index] = new_rows
    return new_rows


def default_split(rows: Sequence[StagingRow], group_id: str) -> list[dict[str, Any]]:
    """Starting point for the split editor: existing lines plus one empty line."""
    current = group_rows(rows, group_id)
    lines = [{"nominal_code": r.nominal_code, "vat_code": r.vat_code, "net": float(r.net or 0),
              "vat": float(r.vat or 0), "description": r.raw_description} for r in current]
    if len(lines) == 1:
        lines.append({"nominal_code": "", "vat_code": current[0].vat_code, "net": 0.0, "vat": 0.0,
                      "description": ""})
    return lines


def unsplit_document(rows: list[StagingRow], group_id: str) -> StagingRow:
    """Merge a split document back into one line (codes from the first line)."""
    current = group_rows(rows, group_id)
    if not current:
        raise SplitError("Document not found in the batch.")
    totals = [sum(((getattr(r, a) or ZERO) for r in current), ZERO) for a in ("net", "vat", "gross")]
    merged = split_document(rows, group_id, [SplitLine(current[0].nominal_code, current[0].vat_code,
                                                       totals[0], totals[1], current[0].raw_description)])
    return merged[0]


def rebuild_descriptions(rows: Iterable[StagingRow], rules: Sequence[DescriptionRule]) -> int:
    count = 0
    for row in rows:
        new = build_description(row, rules)
        if new != row.description:
            row.description = new
            count += 1
    return count
