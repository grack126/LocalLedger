"""Batch export: upload sheet + PDF archiving/renaming + history logging.

``plan_export`` is a dry run (what would be exported, what is blocked and
why, where each PDF would be archived). ``export_batch`` then:

    1. builds the CSV/XLSX for the active template
    2. copies each document's PDF to  <archive>/<YYYY-MM>/<rename pattern>.pdf
    3. logs the documents to ``invoice_logs`` (rolling back the archive copies if that fails)
    4. removes source files: inbox copies always, user files only in "move" mode
    5. saves a copy of the upload sheet under <data>/exports/

Documents are exported whole: if one line of a split invoice is blocked,
the entire invoice stays in staging.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import config
from .db import new_batch_id
from .models import StagingRow
from .rules import TemplateConfig, archive_subfolder, map_rows, render_filename
from .validator import export_blockers, validate_batch

if TYPE_CHECKING:
    from .db import Database

log = logging.getLogger(__name__)


class ExportBlocked(Exception):
    def __init__(self, message: str, plan: ExportPlan) -> None:
        super().__init__(message)
        self.plan = plan


@dataclass
class ExportOptions:
    template: TemplateConfig
    base_currency: str = "GBP"
    archive_enabled: bool = True
    archive_dir: Path = config.DEFAULT_ARCHIVE_DIR
    archive_mode: str = "copy"                    # copy | move (applies to files imported from a folder)
    subfolder_pattern: str = "{YYYY}-{MM}"
    rename_pattern: str = "[AccountCode]_[InvoiceDate]_[InvoiceNumber].pdf"
    export_dir: Path | None = config.EXPORT_DIR
    inbox_dir: Path | None = config.INBOX_DIR

    @classmethod
    def from_db(cls, db: Database, template: TemplateConfig | None = None, **overrides) -> ExportOptions:
        s = db.get_settings()
        data_dir = db.path.parent if db.path is not None else config.DATA_DIR
        opts = cls(
            template=template or db.active_template(),
            base_currency=s["base_currency"],
            archive_enabled=bool(s["archive_enabled"]),
            archive_dir=Path(s["archive_dir"]).expanduser(),
            archive_mode=s["archive_mode"],
            subfolder_pattern=s["archive_subfolder_pattern"],
            rename_pattern=s["rename_pattern"],
            export_dir=data_dir / "exports",
            inbox_dir=data_dir / "inbox",
        )
        for key, value in overrides.items():
            setattr(opts, key, value)
        return opts


@dataclass
class ExportPlan:
    exportable: list[StagingRow]
    blocked: dict[str, list[str]]                    # group_id -> reasons
    excluded: dict[str, str]                         # group_id -> reason (template doesn't take this type)
    archive_targets: dict[str, Path]                 # group_id -> destination
    documents: int = 0

    @property
    def blocked_rows(self) -> int:
        return len(self.blocked)


@dataclass
class ExportResult:
    batch_id: str
    filename: str
    data: bytes
    mime_type: str
    exported_rows: list[StagingRow]
    log_ids: list[int]
    archived: dict[str, str] = field(default_factory=dict)
    saved_path: str = ""
    warnings: list[str] = field(default_factory=list)
    plan: ExportPlan | None = None

    @property
    def exported_groups(self) -> set[str]:
        return {r.group_id for r in self.exported_rows}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_target(target: Path, file_hash: str, taken: set[Path]) -> Path:
    """Avoid overwriting: identical existing file -> reuse; otherwise add _2, _3 ..."""
    candidate, n = target, 1
    while True:
        if candidate not in taken:
            if not candidate.exists():
                return candidate
            if file_hash and _sha256(candidate) == file_hash:
                return candidate
        n += 1
        candidate = target.with_name(f"{target.stem}_{n}{target.suffix}")


def plan_export(rows: Sequence[StagingRow], db: Database, options: ExportOptions, *,
                today: date | None = None) -> ExportPlan:
    report = validate_batch(rows, db, today=today)
    groups: OrderedDict[str, list[StagingRow]] = OrderedDict()
    for row in rows:
        groups.setdefault(row.group_id, []).append(row)

    allowed = set(options.template.applies_to)
    blocked: dict[str, list[str]] = {}
    excluded: dict[str, str] = {}
    exportable: list[StagingRow] = []
    targets: dict[str, Path] = {}
    taken: set[Path] = set()
    for gid, lines in groups.items():
        reasons = list(dict.fromkeys(m for r in lines for m in export_blockers(r, report.settings)))
        if reasons:
            blocked[gid] = reasons
            continue
        if lines[0].doc_type.value not in allowed:
            excluded[gid] = f"{lines[0].doc_type.label} not supported by '{options.template.name}'"
            continue
        exportable.extend(sorted(lines, key=lambda r: r.line_no))
        head = lines[0]
        if options.archive_enabled and head.file_path:
            folder = Path(options.archive_dir) / archive_subfolder(options.subfolder_pattern, head, today=today)
            target = _unique_target(folder / render_filename(options.rename_pattern, head), head.file_hash, taken)
            taken.add(target)
            targets[gid] = target
    return ExportPlan(exportable, blocked, excluded, targets, documents=len(groups))


def _is_within(path: Path, folder: Path | None) -> bool:
    if folder is None:
        return False
    try:
        path.resolve().relative_to(Path(folder).resolve())
        return True
    except (ValueError, OSError):
        return False


def export_batch(rows: Sequence[StagingRow], db: Database, options: ExportOptions, *,
                 today: date | None = None) -> ExportResult:
    plan = plan_export(rows, db, options, today=today)
    if not plan.exportable:
        raise ExportBlocked("Nothing is ready to export.", plan)

    template = options.template
    mapping = map_rows(plan.exportable, template, base_currency=options.base_currency)
    data = mapping.to_bytes()
    batch_id = new_batch_id()
    warnings = list(mapping.warnings)

    # 2. archive (copy first; sources are only removed after the DB commit)
    archived: dict[str, str] = {}
    created: list[Path] = []
    heads = {r.group_id: r for r in plan.exportable if r.line_no == min(
        x.line_no for x in plan.exportable if x.group_id == r.group_id)}
    for gid, target in plan.archive_targets.items():
        source = Path(heads[gid].file_path)
        if not source.is_file():
            warnings.append(f"PDF for {heads[gid].source_file} not found at {source}; not archived.")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
                created.append(target)
            archived[gid] = str(target)
        except OSError as exc:
            warnings.append(f"Could not archive {source.name}: {exc}")

    # 3. history
    try:
        log_ids = db.log_exported_rows(plan.exportable, batch_id=batch_id, template_id=template.id,
                                       archived_paths=archived, base_currency=options.base_currency)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise

    # 4. tidy sources
    for gid in archived:
        source = Path(heads[gid].file_path)
        if _is_within(source, options.inbox_dir) or options.archive_mode == "move":
            try:
                source.unlink(missing_ok=True)
            except OSError as exc:
                warnings.append(f"Archived but could not remove {source.name}: {exc}")

    # 5. keep a copy of the upload sheet
    software = "".join(ch for ch in template.software if ch.isalnum()) or "Export"
    filename = f"{software}_{datetime.now():%Y%m%d_%H%M%S}{template.file_extension}"
    saved_path = ""
    if options.export_dir is not None:
        try:
            Path(options.export_dir).mkdir(parents=True, exist_ok=True)
            saved = Path(options.export_dir) / filename
            saved.write_bytes(data)
            saved_path = str(saved)
        except OSError as exc:
            warnings.append(f"Could not save a copy of the export: {exc}")

    log.info("Exported %d rows (%d documents) as batch %s", len(plan.exportable), len(log_ids), batch_id)
    return ExportResult(batch_id, filename, data, template.mime_type, plan.exportable, log_ids,
                        archived, saved_path, warnings, plan)
