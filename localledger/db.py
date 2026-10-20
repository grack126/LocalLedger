"""SQLite persistence layer.

* ``Database``        — connection handling, schema init/migrations, seeds, CRUD
* ``EntityResolver``  — VAT-number / exact / alias / fuzzy (rapidfuzz) matching
* Vendor memory       — ``Database.save_vendor_memory`` / ``remember_from_row``
* History             — ``log_exported_rows`` + ``fetch_duplicate_candidates``

A new SQLite connection is opened per operation. That is cheap, and it is
the safe choice under Streamlit, which re-runs scripts on worker threads.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import IO, Any, Iterable, Iterator, Mapping, Sequence

from rapidfuzz import fuzz, process

from . import config
from .models import (
    ZERO,
    DocType,
    StagingRow,
    clean_str,
    from_minor,
    normalize_invoice_number,
    normalize_name,
    normalize_vat_number,
    parse_date,
    to_minor,
)
from .rules import (
    BUILTIN_TEMPLATES,
    DEFAULT_DESCRIPTION_RULES,
    TemplateConfig,
    service_month_of,
    to_base,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3
ENTITY_TYPES = ("Client", "Supplier")

# Upgrade scripts: MIGRATIONS[n] takes a v(n-1) database to vn.
# Fresh databases are created straight from schema.sql at the latest version.
MIGRATIONS: dict[int, str] = {
    2: """
        CREATE TABLE IF NOT EXISTS fx_rates (
            base_currency   TEXT NOT NULL,
            currency        TEXT NOT NULL,
            rate_date       TEXT NOT NULL,
            effective_date  TEXT NOT NULL,
            rate            TEXT NOT NULL,
            source          TEXT NOT NULL CHECK (source IN ('api', 'manual')),
            fetched_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (base_currency, currency, rate_date, source)
        );
        CREATE INDEX IF NOT EXISTS idx_fx_effective ON fx_rates (base_currency, currency, effective_date);
    """,
    3: """
        CREATE TABLE IF NOT EXISTS staging_rows (
            row_id      TEXT PRIMARY KEY,
            group_id    TEXT NOT NULL,
            position    INTEGER NOT NULL,
            data        TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_staging_position ON staging_rows (position);
    """,
}
_SQL_CHUNK = 500  # stay well below SQLite's bound-parameter limit


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_batch_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _chunks(items: Sequence[Any], size: int = _SQL_CHUNK) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# =============================================================================
# Seeds
# =============================================================================
# (code, description, rate %, reverse charge, sort order) — Sage 50 UK defaults.
DEFAULT_VAT_CODES: list[tuple[str, str, float, int, int]] = [
    ("T0", "Zero rated", 0.0, 0, 10),
    ("T1", "Standard rate", 20.0, 0, 20),
    ("T2", "Exempt", 0.0, 0, 30),
    ("T4", "Sales of goods to VAT-registered EU customers (NI)", 0.0, 0, 40),
    ("T5", "Reduced rate", 5.0, 0, 50),
    ("T7", "Zero-rated purchases of goods from the EU (NI)", 0.0, 0, 70),
    ("T8", "Standard-rated purchases of goods from the EU (NI) – reverse charge", 20.0, 1, 80),
    ("T9", "Outside the scope of VAT", 0.0, 0, 90),
    ("T20", "Domestic reverse charge (CIS / specified services)", 20.0, 1, 100),
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "base_currency": "GBP",
    "date_order": "DMY",                     # DMY | MDY — how ambiguous dates are read
    "math_tolerance": 0.02,
    "vat_rate_tolerance_pp": 0.25,           # percentage points
    "fuzzy_auto_threshold": 88,
    "fuzzy_suggest_threshold": 60,
    "confidence_high": 85,
    "confidence_medium": 50,
    "duplicate_window_days": 3,
    "allow_export_with_errors": False,
    "active_template": "Sage 50 – Audit Trail Import",
    "description_rules": [r.to_dict() for r in DEFAULT_DESCRIPTION_RULES],
    "archive_enabled": True,
    "archive_mode": "copy",                  # copy | move
    "archive_dir": str(config.DEFAULT_ARCHIVE_DIR),
    "archive_subfolder_pattern": "{YYYY}-{MM}",
    "rename_pattern": "[AccountCode]_[InvoiceDate]_[InvoiceNumber].pdf",
    "fx_enabled": True,
    "fx_api_url": "https://api.frankfurter.dev/v1",
    "fx_max_stale_days": 7,                  # offline fallback: nearest cached rate within N days
    "own_company_name": "",                  # your business — used to tell sales from purchases
    "own_vat_number": "",
    "extraction_direction": "auto",          # auto | purchase | sales
    "ocr_engine": "tesseract",               # tesseract | easyocr | none
    "tesseract_cmd": "",
    "ocr_dpi": 300,
    "llm_enabled": False,
    "llm_mode": "fallback",                  # fallback (only when regex is incomplete) | always
    "llm_endpoint": "http://localhost:11434/v1",
    "llm_model": "llama3.1:8b",
    "llm_timeout": 120,
    "llm_allow_remote": False,
    "anomaly_threshold_pct": 25,
    "anomaly_lookback_months": 6,
}


# =============================================================================
# Data classes
# =============================================================================
@dataclass
class Entity:
    id: int | None
    entity_type: str
    company_name: str
    account_code: str
    vat_reg_no: str | None = None
    default_nominal_code: str | None = None
    default_vat_code: str | None = None
    default_currency: str | None = None
    source: str = "manual"
    is_active: bool = True
    aliases: list[str] = field(default_factory=list)

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.company_name)

    @classmethod
    def from_row(cls, row: sqlite3.Row, aliases: Sequence[str] = ()) -> Entity:
        return cls(
            id=row["id"], entity_type=row["entity_type"], company_name=row["company_name"],
            account_code=row["account_code"], vat_reg_no=row["vat_reg_no"],
            default_nominal_code=row["default_nominal_code"],
            default_vat_code=row["default_vat_code"], default_currency=row["default_currency"],
            source=row["source"], is_active=bool(row["is_active"]), aliases=list(aliases),
        )


@dataclass
class VatCode:
    code: str
    description: str
    rate: Decimal                 # percent
    is_reverse_charge: bool = False
    is_active: bool = True
    sort_order: int = 0

    @property
    def invoice_rate(self) -> Decimal:
        """Rate expected on the supplier's document (0 for reverse charge)."""
        return ZERO if self.is_reverse_charge else self.rate


@dataclass
class EntityMatch:
    entity: Entity | None
    score: float                                  # 0-100
    method: str                                   # vat | exact | alias | fuzzy | suggested | none
    candidates: list[tuple[Entity, float]] = field(default_factory=list)
    auto_assigned: bool = False
    note: str = ""


@dataclass
class ImportReport:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    column_map: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors or (self.inserted + self.updated) > 0


@dataclass(frozen=True)
class HistoricalInvoice:
    id: int
    doc_type: str
    account_code: str
    company_name: str | None
    invoice_number: str | None
    invoice_number_norm: str | None
    invoice_date: date | None
    gross_minor: int
    file_hash: str | None
    source_file_name: str | None
    exported_at: str


# =============================================================================
# Entity resolution
# =============================================================================
class EntityResolver:
    """In-memory index over the master list, built once per batch.

    Order of precedence:
      1. VAT registration number (exact, normalised)       -> score 100
      2. Exact normalised company name or learned alias    -> score 100
      3. rapidfuzz WRatio against names + aliases          -> 0-100
    Fuzzy matches are auto-assigned only above ``auto_threshold`` and when the
    runner-up is not within ``ambiguity_margin`` points.
    """

    def __init__(
        self,
        entities: Sequence[Entity],
        *,
        auto_threshold: float = 88.0,
        suggest_threshold: float = 60.0,
        ambiguity_margin: float = 3.0,
    ) -> None:
        self.auto_threshold = float(auto_threshold)
        self.suggest_threshold = float(suggest_threshold)
        self.ambiguity_margin = float(ambiguity_margin)
        self.entities = [e for e in entities if e.is_active]
        self._by_vat: dict[str, list[Entity]] = defaultdict(list)
        self._exact: dict[str, list[tuple[Entity, str]]] = defaultdict(list)
        self._by_account: dict[tuple[str, str], Entity] = {}
        self._choices: dict[str | None, tuple[list[str], list[Entity]]] = {}
        for entity in self.entities:
            self._by_account[(entity.entity_type, entity.account_code.upper())] = entity
            if entity.vat_reg_no:
                self._by_vat[normalize_vat_number(entity.vat_reg_no)].append(entity)
            names = [(entity.normalized_name, "exact")] + [(normalize_name(a), "alias") for a in entity.aliases]
            for norm, method in names:
                if norm:
                    self._exact[norm].append((entity, method))
        for entity_type in (None, *ENTITY_TYPES):
            choices: list[str] = []
            owners: list[Entity] = []
            for entity in self.entities:
                if entity_type and entity.entity_type != entity_type:
                    continue
                for norm in {entity.normalized_name, *(normalize_name(a) for a in entity.aliases)}:
                    if norm:
                        choices.append(norm)
                        owners.append(entity)
            self._choices[entity_type] = (choices, owners)

    def by_account(self, account_code: str, entity_type: str | None = None) -> Entity | None:
        code = (account_code or "").strip().upper()
        if not code:
            return None
        if entity_type:
            return self._by_account.get((entity_type, code))
        return next((e for (t, c), e in self._by_account.items() if c == code), None)

    def resolve(self, name: str | None, vat_reg_no: str | None = None,
                entity_type: str | None = None) -> EntityMatch:
        norm = normalize_name(name)

        def type_ok(entity: Entity) -> bool:
            return entity_type is None or entity.entity_type == entity_type

        vat = normalize_vat_number(vat_reg_no)
        if vat:
            hits = [e for e in self._by_vat.get(vat, []) if type_ok(e)]
            if hits:
                best = max(hits, key=lambda e: fuzz.WRatio(norm, e.normalized_name) if norm else 0)
                return EntityMatch(best, 100.0, "vat", [(best, 100.0)], True)

        if not norm:
            return EntityMatch(None, 0.0, "none")

        exact = {e.id: (e, m) for e, m in self._exact.get(norm, []) if type_ok(e)}
        if len(exact) == 1:
            entity, method = next(iter(exact.values()))
            return EntityMatch(entity, 100.0, method, [(entity, 100.0)], True)
        if len(exact) > 1:
            candidates = [(e, 100.0) for e, _ in exact.values()]
            return EntityMatch(candidates[0][0], 100.0, "suggested", candidates, False,
                               "Several accounts share this name — pick one.")

        choices, owners = self._choices.get(entity_type, ([], []))
        if not choices:
            return EntityMatch(None, 0.0, "none", note=f"No {entity_type or 'entity'} master list loaded.")
        best: dict[int | None, tuple[Entity, float]] = {}
        for _choice, score, idx in process.extract(
                norm, choices, scorer=fuzz.WRatio, limit=15, score_cutoff=self.suggest_threshold):
            entity = owners[idx]
            if entity.id not in best or score > best[entity.id][1]:
                best[entity.id] = (entity, float(score))
        ranked = sorted(best.values(), key=lambda pair: -pair[1])[:5]
        if not ranked:
            return EntityMatch(None, 0.0, "none", note="No similar name in the master list.")
        top, top_score = ranked[0]
        ambiguous = len(ranked) > 1 and top_score - ranked[1][1] < self.ambiguity_margin
        auto = top_score >= self.auto_threshold and not ambiguous
        note = "Close scores for several accounts — please confirm." if ambiguous else ""
        return EntityMatch(top, round(top_score, 1), "fuzzy" if auto else "suggested", ranked, auto, note)

    def apply(self, row: StagingRow, *, overwrite_defaults: bool = False) -> EntityMatch:
        """Resolve ``row`` and, when confident, fill account + default codes.

        A row whose account code was set by hand (method ``manual``) keeps it.
        """
        entity_type = row.doc_type.entity_type
        if row.account_code and row.entity_match_method == "manual":
            entity = self.by_account(row.account_code, entity_type)
            if entity:
                apply_entity_to_row(row, entity, overwrite_defaults=overwrite_defaults)
                row.entity_match_method = "manual"
            return EntityMatch(entity, 100.0 if entity else 0.0, "manual", auto_assigned=bool(entity))

        match = self.resolve(row.company_name, row.vat_reg_no, entity_type)
        row.entity_match_score = match.score
        row.entity_match_method = match.method
        if match.auto_assigned and match.entity:
            apply_entity_to_row(row, match.entity, overwrite_defaults=overwrite_defaults)
        return match


def apply_entity_to_row(row: StagingRow, entity: Entity, *, overwrite_defaults: bool = False) -> None:
    row.entity_id = entity.id
    row.account_code = entity.account_code
    row.entity_name = entity.company_name
    if entity.default_nominal_code and (overwrite_defaults or not row.nominal_code):
        row.nominal_code = entity.default_nominal_code
    if entity.default_vat_code and (overwrite_defaults or not row.vat_code):
        row.vat_code = entity.default_vat_code
    if entity.vat_reg_no and not row.vat_reg_no:
        row.vat_reg_no = entity.vat_reg_no


# =============================================================================
# Master-list file import helpers
# =============================================================================
ENTITY_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "entity_type": ("entity type", "type", "record type", "contact type", "ledger", "category"),
    "company_name": ("company name", "name", "company", "account name", "supplier name",
                     "customer name", "client name", "contact name", "contactname", "contact"),
    "account_code": ("account code", "account reference", "account ref", "a c ref", "a c",
                     "acc code", "account number", "account no", "account", "reference", "ref", "code"),
    "vat_reg_no": ("vat registration number", "vat reg no", "vat registration", "vat number",
                   "vat no", "vat reg", "tax number", "tax id", "taxnumber", "vat"),
    "default_nominal_code": ("default nominal code", "default nominal", "def nom code",
                             "nominal code", "nominal", "default gl", "gl code",
                             "default account code", "default account"),
    "default_vat_code": ("default vat code", "default tax code", "def tax code",
                         "vat code", "tax code", "default tax rate", "tax rate"),
    "default_currency": ("default currency", "currency"),
}
_ENTITY_TYPE_VALUES = {
    "client": "Client", "customer": "Client", "c": "Client", "sales": "Client",
    "debtor": "Client", "receivable": "Client", "sl": "Client",
    "supplier": "Supplier", "vendor": "Supplier", "s": "Supplier", "purchase": "Supplier",
    "purchases": "Supplier", "creditor": "Supplier", "payable": "Supplier", "pl": "Supplier",
}


def _norm_header(text: Any) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in str(text).lower()).split())


def map_entity_columns(columns: Iterable[Any]) -> dict[str, str]:
    """Guess which file column holds which entity field (field -> column)."""
    available = {_norm_header(c): str(c) for c in columns}
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for fld, synonyms in ENTITY_COLUMN_SYNONYMS.items():
        for synonym in synonyms:
            original = available.get(synonym)
            if original and original not in used:
                mapping[fld] = original
                used.add(original)
                break
    return mapping


def coerce_entity_type(value: Any, default: str | None = None) -> str | None:
    text = clean_str(value).lower()
    return _ENTITY_TYPE_VALUES.get(text, default) if text else default


def read_master_file(source: str | Path | IO[bytes], filename: str | None = None):
    """Read a CSV/XLSX master list into a DataFrame of strings."""
    import pandas as pd

    name = (filename or getattr(source, "name", None) or str(source)).lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(source, dtype=str, keep_default_na=False)
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            if hasattr(source, "seek"):
                source.seek(0)
            return pd.read_csv(source, dtype=str, keep_default_na=False, sep=None,
                               engine="python", encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Could not decode {name}: {last_error}")


# =============================================================================
# Vendor memory helpers
# =============================================================================
MEMORY_FIELDS = {"account_code": "Account Code", "nominal_code": "Nominal Code", "vat_code": "VAT Code"}


def memory_changes(original: StagingRow, edited: StagingRow) -> dict[str, tuple[str, str]]:
    """Fields the user changed that can be remembered for the supplier/client.
    Used by the staging grid to show the "Save as default" prompt."""
    changes: dict[str, tuple[str, str]] = {}
    for attr, label in MEMORY_FIELDS.items():
        old, new = clean_str(getattr(original, attr)), clean_str(getattr(edited, attr))
        if new and old.upper() != new.upper():
            changes[label] = (old, new)
    return changes


# =============================================================================
# Database
# =============================================================================
class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self._memory = str(path) == ":memory:"
        self.path = Path(path) if (path and not self._memory) else (None if self._memory else config.DB_PATH)
        self._lock = threading.RLock()
        self._shared: sqlite3.Connection | None = None
        self.initialize()

    # ------------------------------------------------------------ connections
    def _open(self) -> sqlite3.Connection:
        if self._memory:
            conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            assert self.path is not None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection wrapped in a transaction (commit / rollback)."""
        if self._memory:
            with self._lock:
                if self._shared is None:
                    self._shared = self._open()
                conn = self._shared
                try:
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            return
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------- schema
    def initialize(self) -> None:
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema v{version} is newer than this app (v{SCHEMA_VERSION}). "
                    "Please update LocalLedger OCR.")
            if version == 0:
                conn.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version < SCHEMA_VERSION:
                self._migrate(conn, version)
            self._seed(conn)

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Apply incremental migrations. Add ``MIGRATIONS[n]`` scripts as the schema evolves."""
        for target in range(from_version + 1, SCHEMA_VERSION + 1):
            script = MIGRATIONS.get(target)
            if script:
                log.info("Migrating database to schema v%s", target)
                conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {target}")

    def _seed(self, conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            [(k, json.dumps(v)) for k, v in DEFAULT_SETTINGS.items()],
        )
        if conn.execute("SELECT COUNT(*) FROM vat_codes").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO vat_codes (code, description, rate, is_reverse_charge, sort_order) "
                "VALUES (?, ?, ?, ?, ?)", DEFAULT_VAT_CODES)
        for tpl in BUILTIN_TEMPLATES:
            existing = conn.execute(
                "SELECT id, is_builtin, builtin_version FROM templates WHERE name = ?", (tpl.name,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO templates (name, software, is_builtin, builtin_version, config) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (tpl.name, tpl.software, tpl.builtin_version, tpl.to_json()))
            elif existing["is_builtin"] and existing["builtin_version"] < tpl.builtin_version:
                conn.execute(
                    "UPDATE templates SET config = ?, software = ?, builtin_version = ?, updated_at = ? "
                    "WHERE id = ?",
                    (tpl.to_json(), tpl.software, tpl.builtin_version, _utcnow(), existing["id"]))

    # ------------------------------------------------------------- settings
    def get_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            stored = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM settings")}
        return {**DEFAULT_SETTINGS, **stored}

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return json.loads(row["value"])
        return DEFAULT_SETTINGS.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.set_settings({key: value})

    def set_settings(self, values: Mapping[str, Any]) -> None:
        now = _utcnow()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                [(k, json.dumps(v, default=str), now) for k, v in values.items()])

    # ------------------------------------------------------------- VAT codes
    def list_vat_codes(self, *, active_only: bool = False) -> list[VatCode]:
        sql = "SELECT * FROM vat_codes"
        if active_only:
            sql += " WHERE is_active = 1"
        with self.connect() as conn:
            rows = conn.execute(sql + " ORDER BY sort_order, code").fetchall()
        return [VatCode(r["code"], r["description"], Decimal(str(r["rate"])),
                        bool(r["is_reverse_charge"]), bool(r["is_active"]), r["sort_order"]) for r in rows]

    def vat_code_map(self) -> dict[str, VatCode]:
        """Upper-cased code -> VatCode (all codes, so historical rows still validate)."""
        return {v.code.upper(): v for v in self.list_vat_codes()}

    def upsert_vat_code(self, code: str, description: str, rate: float | Decimal, *,
                        is_reverse_charge: bool = False, is_active: bool = True, sort_order: int = 0) -> None:
        code = clean_str(code).upper()
        if not code:
            raise ValueError("VAT code is required.")
        rate_value = float(rate)
        if not 0 <= rate_value <= 100:
            raise ValueError("VAT rate must be between 0 and 100.")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO vat_codes (code, description, rate, is_reverse_charge, is_active, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(code) DO UPDATE SET description = excluded.description, "
                "rate = excluded.rate, is_reverse_charge = excluded.is_reverse_charge, "
                "is_active = excluded.is_active, sort_order = excluded.sort_order",
                (code, description, rate_value, int(is_reverse_charge), int(is_active), sort_order))

    def delete_vat_code(self, code: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM vat_codes WHERE code = ?", (code,))

    # ------------------------------------------------------------- entities
    def _aliases_by_entity(self, conn: sqlite3.Connection) -> dict[int, list[str]]:
        out: dict[int, list[str]] = defaultdict(list)
        for r in conn.execute("SELECT entity_id, alias FROM entity_aliases ORDER BY id"):
            out[r["entity_id"]].append(r["alias"])
        return out

    def list_entities(self, entity_type: str | None = None, *, active_only: bool = True) -> list[Entity]:
        clauses, params = [], []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if active_only:
            clauses.append("is_active = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            aliases = self._aliases_by_entity(conn)
            rows = conn.execute(f"SELECT * FROM entities{where} ORDER BY entity_type, company_name", params)
            return [Entity.from_row(r, aliases.get(r["id"], ())) for r in rows]

    def get_entity(self, entity_id: int) -> Entity | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
            if row is None:
                return None
            aliases = [r["alias"] for r in conn.execute(
                "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY id", (entity_id,))]
        return Entity.from_row(row, aliases)

    def find_entity_by_account(self, entity_type: str, account_code: str) -> Entity | None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM entities WHERE entity_type = ? AND account_code = ?",
                               (entity_type, clean_str(account_code))).fetchone()
        return self.get_entity(row["id"]) if row else None

    def known_account_codes(self) -> dict[str, set[str]]:
        """{'Supplier': {'ACME001', ...}, 'Client': {...}} (upper-cased, active only)."""
        out: dict[str, set[str]] = {t: set() for t in ENTITY_TYPES}
        with self.connect() as conn:
            for r in conn.execute("SELECT entity_type, account_code FROM entities WHERE is_active = 1"):
                out[r["entity_type"]].add(r["account_code"].upper())
        return out

    @staticmethod
    def _validate_entity(entity: Entity) -> None:
        if entity.entity_type not in ENTITY_TYPES:
            raise ValueError(f"Entity type must be Client or Supplier, got {entity.entity_type!r}.")
        if not clean_str(entity.account_code):
            raise ValueError("Account code is required.")
        if not clean_str(entity.company_name):
            raise ValueError("Company name is required.")

    def upsert_entity(self, entity: Entity, *, conn: sqlite3.Connection | None = None) -> int:
        """Insert or update by (entity_type, account_code). Blank optional
        fields never overwrite stored values. Returns the entity id."""
        self._validate_entity(entity)
        params = {
            "entity_type": entity.entity_type,
            "company_name": clean_str(entity.company_name),
            "normalized_name": normalize_name(entity.company_name),
            "account_code": clean_str(entity.account_code),
            "vat_reg_no": normalize_vat_number(entity.vat_reg_no) or None,
            "default_nominal_code": clean_str(entity.default_nominal_code) or None,
            "default_vat_code": clean_str(entity.default_vat_code).upper() or None,
            "default_currency": clean_str(entity.default_currency).upper() or None,
            "source": entity.source,
            "is_active": int(entity.is_active),
            "now": _utcnow(),
        }
        sql = """
            INSERT INTO entities (entity_type, company_name, normalized_name, account_code, vat_reg_no,
                                  default_nominal_code, default_vat_code, default_currency, source,
                                  is_active, created_at, updated_at)
            VALUES (:entity_type, :company_name, :normalized_name, :account_code, :vat_reg_no,
                    :default_nominal_code, :default_vat_code, :default_currency, :source,
                    :is_active, :now, :now)
            ON CONFLICT(entity_type, account_code) DO UPDATE SET
                company_name         = excluded.company_name,
                normalized_name      = excluded.normalized_name,
                vat_reg_no           = COALESCE(excluded.vat_reg_no, entities.vat_reg_no),
                default_nominal_code = COALESCE(excluded.default_nominal_code, entities.default_nominal_code),
                default_vat_code     = COALESCE(excluded.default_vat_code, entities.default_vat_code),
                default_currency     = COALESCE(excluded.default_currency, entities.default_currency),
                source               = CASE WHEN excluded.source = 'import' THEN 'import' ELSE entities.source END,
                is_active            = excluded.is_active,
                updated_at           = excluded.updated_at
        """

        def run(c: sqlite3.Connection) -> int:
            c.execute(sql, params)
            row = c.execute("SELECT id FROM entities WHERE entity_type = ? AND account_code = ?",
                            (params["entity_type"], params["account_code"])).fetchone()
            if entity.aliases:
                for alias in entity.aliases:
                    self._add_alias(c, row["id"], alias)
            return int(row["id"])

        if conn is not None:
            return run(conn)
        with self.connect() as c:
            return run(c)

    def delete_entity(self, entity_id: int) -> None:
        """Hard delete. Historical logs keep their account code (entity_id is set NULL)."""
        with self.connect() as conn:
            conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))

    def import_entities(self, frame: Any, *, default_entity_type: str | None = None,
                        column_map: Mapping[str, str] | None = None) -> ImportReport:
        """Merge a master-list DataFrame into ``entities``.

        Existing rows are matched on (type, account code) and updated; blank
        cells never wipe learned defaults.
        """
        report = ImportReport()
        mapping = dict(column_map or map_entity_columns(frame.columns))
        report.column_map = mapping
        missing = [f for f in ("company_name", "account_code") if f not in mapping]
        if "entity_type" not in mapping and not default_entity_type:
            missing.append("entity_type (or choose a default type)")
        if missing:
            report.errors.append("Missing column(s): " + ", ".join(missing))
            return report

        with self.connect() as conn:
            existing = {(r["entity_type"], r["account_code"].upper())
                        for r in conn.execute("SELECT entity_type, account_code FROM entities")}
            seen: set[tuple[str, str]] = set()
            for idx, record in enumerate(frame.to_dict(orient="records"), start=2):
                def cell(fld: str) -> str:
                    return clean_str(record.get(mapping[fld])) if fld in mapping else ""

                entity_type = coerce_entity_type(cell("entity_type"), default_entity_type)
                name, code = cell("company_name"), cell("account_code")
                if not (name or code):
                    report.skipped += 1
                    continue
                if entity_type not in ENTITY_TYPES:
                    report.errors.append(f"Row {idx}: unknown entity type {cell('entity_type')!r}.")
                    report.skipped += 1
                    continue
                if not name or not code:
                    report.errors.append(f"Row {idx}: company name and account code are both required.")
                    report.skipped += 1
                    continue
                key = (entity_type, code.upper())
                if key in seen:
                    report.errors.append(f"Row {idx}: duplicate account code {code} in file — later row used.")
                seen.add(key)
                entity = Entity(
                    id=None, entity_type=entity_type, company_name=name, account_code=code,
                    vat_reg_no=cell("vat_reg_no") or None,
                    default_nominal_code=cell("default_nominal_code") or None,
                    default_vat_code=cell("default_vat_code") or None,
                    default_currency=cell("default_currency") or None,
                    source="import",
                )
                self.upsert_entity(entity, conn=conn)
                if key in existing:
                    report.updated += 1
                else:
                    report.inserted += 1
                    existing.add(key)
        return report

    def _add_alias(self, conn: sqlite3.Connection, entity_id: int, alias: str) -> bool:
        norm = normalize_name(alias)
        if not norm:
            return False
        own = conn.execute("SELECT normalized_name FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if own and own["normalized_name"] == norm:
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, normalized_alias) VALUES (?, ?, ?)",
            (entity_id, clean_str(alias), norm))
        return cur.rowcount > 0

    def add_alias(self, entity_id: int, alias: str) -> bool:
        with self.connect() as conn:
            return self._add_alias(conn, entity_id, alias)

    def remove_alias(self, entity_id: int, alias: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND normalized_alias = ?",
                         (entity_id, normalize_name(alias)))

    def entity_resolver(self, **overrides: float) -> EntityResolver:
        settings = self.get_settings()
        return EntityResolver(
            self.list_entities(),
            auto_threshold=overrides.get("auto_threshold", settings["fuzzy_auto_threshold"]),
            suggest_threshold=overrides.get("suggest_threshold", settings["fuzzy_suggest_threshold"]),
            ambiguity_margin=overrides.get("ambiguity_margin", 3.0),
        )

    # ------------------------------------------------------------- vendor memory
    def save_vendor_memory(
        self,
        *,
        entity_type: str,
        account_code: str,
        extracted_name: str | None = None,
        nominal_code: str | None = None,
        vat_code: str | None = None,
        company_name: str | None = None,
        vat_reg_no: str | None = None,
    ) -> Entity:
        """Remember a user's correction for all future uploads.

        * ``extracted_name`` becomes an alias, so the same printed name
          resolves straight to ``account_code`` next time.
        * ``nominal_code`` / ``vat_code`` become the entity's defaults.
        * Unknown account codes create a new entity with ``source='learned'``.
        """
        if entity_type not in ENTITY_TYPES:
            raise ValueError("entity_type must be 'Client' or 'Supplier'.")
        account_code = clean_str(account_code)
        if not account_code:
            raise ValueError("An account code is required to save vendor memory.")
        vat_code = clean_str(vat_code).upper() or None
        nominal_code = clean_str(nominal_code) or None

        with self.connect() as conn:
            if vat_code and conn.execute("SELECT 1 FROM vat_codes WHERE code = ?", (vat_code,)).fetchone() is None:
                raise ValueError(f"Unknown VAT code {vat_code!r}; add it under Settings > VAT codes first.")
            row = conn.execute("SELECT * FROM entities WHERE entity_type = ? AND account_code = ?",
                               (entity_type, account_code)).fetchone()
            now = _utcnow()
            if row is None:
                name = clean_str(company_name) or clean_str(extracted_name) or account_code
                conn.execute(
                    "INSERT INTO entities (entity_type, company_name, normalized_name, account_code, vat_reg_no, "
                    "default_nominal_code, default_vat_code, source, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'learned', ?, ?)",
                    (entity_type, name, normalize_name(name), account_code,
                     normalize_vat_number(vat_reg_no) or None, nominal_code, vat_code, now, now))
                entity_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                entity_id = row["id"]
                conn.execute(
                    "UPDATE entities SET default_nominal_code = COALESCE(?, default_nominal_code), "
                    "default_vat_code = COALESCE(?, default_vat_code), "
                    "vat_reg_no = COALESCE(vat_reg_no, ?), updated_at = ? WHERE id = ?",
                    (nominal_code, vat_code, normalize_vat_number(vat_reg_no) or None, now, entity_id))
            if extracted_name:
                self._add_alias(conn, entity_id, extracted_name)
        entity = self.get_entity(entity_id)
        assert entity is not None
        log.info("Vendor memory saved for %s %s", entity_type, account_code)
        return entity

    def remember_from_row(self, row: StagingRow, fields_to_save: Iterable[str]) -> Entity:
        """Save the chosen fields ('Account Code', 'Nominal Code', 'VAT Code')
        of an edited staging row as defaults for its supplier/client."""
        chosen = set(fields_to_save)
        return self.save_vendor_memory(
            entity_type=row.doc_type.entity_type,
            account_code=row.account_code,
            extracted_name=row.company_name if "Account Code" in chosen else None,
            nominal_code=row.nominal_code if "Nominal Code" in chosen else None,
            vat_code=row.vat_code if "VAT Code" in chosen else None,
            company_name=row.entity_name or row.company_name,
            vat_reg_no=row.vat_reg_no or None,
        )

    # ------------------------------------------------------------- templates
    def list_templates(self) -> list[TemplateConfig]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM templates ORDER BY is_builtin DESC, name").fetchall()
        return [TemplateConfig.from_json(r["config"], id=r["id"], is_builtin=bool(r["is_builtin"]),
                                         builtin_version=r["builtin_version"]) for r in rows]

    def get_template(self, template_id: int | None = None, *, name: str | None = None) -> TemplateConfig | None:
        if template_id is None and name is None:
            raise ValueError("Pass template_id or name.")
        with self.connect() as conn:
            if template_id is not None:
                r = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
            else:
                r = conn.execute("SELECT * FROM templates WHERE name = ?", (name,)).fetchone()
        if r is None:
            return None
        return TemplateConfig.from_json(r["config"], id=r["id"], is_builtin=bool(r["is_builtin"]),
                                        builtin_version=r["builtin_version"])

    def active_template(self) -> TemplateConfig:
        tpl = self.get_template(name=self.get_setting("active_template"))
        return tpl or self.list_templates()[0]

    def save_template(self, template: TemplateConfig) -> int:
        """Create or update a user template. Built-in presets are read-only —
        use ``TemplateConfig.clone`` to customise one."""
        errors = template.validate()
        if errors:
            raise ValueError("; ".join(errors))
        with self.connect() as conn:
            clash = conn.execute("SELECT id, is_builtin FROM templates WHERE name = ?", (template.name,)).fetchone()
            if clash and clash["is_builtin"]:
                raise ValueError(f"'{template.name}' is a built-in preset; save a copy under a new name.")
            if clash and template.id is not None and clash["id"] != template.id:
                raise ValueError(f"A template named '{template.name}' already exists.")
            if template.id is not None:
                current = conn.execute("SELECT is_builtin FROM templates WHERE id = ?", (template.id,)).fetchone()
                if current and current["is_builtin"]:
                    raise ValueError("Built-in presets cannot be modified; save a copy under a new name.")
                if current:
                    conn.execute("UPDATE templates SET name = ?, software = ?, config = ?, updated_at = ? "
                                 "WHERE id = ?",
                                 (template.name, template.software, template.to_json(), _utcnow(), template.id))
                    return template.id
            if clash:
                conn.execute("UPDATE templates SET software = ?, config = ?, updated_at = ? WHERE id = ?",
                             (template.software, template.to_json(), _utcnow(), clash["id"]))
                template.id = clash["id"]
                return clash["id"]
            cur = conn.execute("INSERT INTO templates (name, software, is_builtin, config) VALUES (?, ?, 0, ?)",
                               (template.name, template.software, template.to_json()))
            template.id = int(cur.lastrowid)
            return template.id

    def delete_template(self, template_id: int) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT is_builtin FROM templates WHERE id = ?", (template_id,)).fetchone()
            if row and row["is_builtin"]:
                raise ValueError("Built-in presets cannot be deleted.")
            conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))

    # ------------------------------------------------------------- invoice history
    def log_exported_rows(
        self,
        rows: Iterable[StagingRow],
        *,
        batch_id: str | None = None,
        template_id: int | None = None,
        archived_paths: Mapping[str, str] | None = None,
        base_currency: str | None = None,
    ) -> list[int]:
        """Persist exported rows: one ``invoice_logs`` header per document
        (staging ``group_id``) with one ``invoice_lines`` row per line.
        ``archived_paths`` maps group_id -> archived PDF path."""
        base = (base_currency or self.get_setting("base_currency", "GBP")).upper()
        batch_id = batch_id or new_batch_id()
        archived_paths = archived_paths or {}
        groups: dict[str, list[StagingRow]] = defaultdict(list)
        for row in rows:
            groups[row.group_id].append(row)

        for gid, lines in groups.items():
            head = min(lines, key=lambda r: r.line_no)
            if not head.account_code:
                raise ValueError(f"Cannot log {head.source_file or gid}: account code missing.")
            if head.invoice_date is None:
                raise ValueError(f"Cannot log {head.source_file or gid}: invoice date missing.")

        ids: list[int] = []
        with self.connect() as conn:
            for gid, lines in groups.items():
                lines = sorted(lines, key=lambda r: r.line_no)
                head = lines[0]
                svc = service_month_of(head) or head.invoice_date.replace(day=1)  # type: ignore[union-attr]

                def amt(value: Decimal | None) -> Decimal:
                    return abs(value) if value is not None else ZERO

                line_values = []
                for line in lines:
                    net, vat, gross = amt(line.net), amt(line.vat), amt(line.gross)
                    base_vals = [to_base(v, head.currency, head.exchange_rate, base) for v in (net, vat, gross)]
                    line_values.append((line, [to_minor(v) for v in (net, vat, gross)],
                                        [to_minor(v) for v in base_vals]))
                totals = [sum(lv[1][i] for lv in line_values) for i in range(3)]
                base_totals = [sum(lv[2][i] for lv in line_values) for i in range(3)]
                cur = conn.execute(
                    """INSERT INTO invoice_logs (
                        batch_id, group_id, doc_type, entity_id, account_code, company_name,
                        invoice_number, invoice_number_norm, invoice_date, service_period_start,
                        service_period_end, service_month, currency, exchange_rate,
                        net_minor, vat_minor, gross_minor, base_net_minor, base_vat_minor, base_gross_minor,
                        source_file_name, file_hash, archived_path, extraction_method, confidence, template_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (batch_id, gid, head.doc_type.value, head.entity_id, head.account_code,
                     head.entity_name or head.company_name or None, head.invoice_number or None,
                     normalize_invoice_number(head.invoice_number) or None, head.invoice_date.isoformat(),
                     head.service_period_start.isoformat() if head.service_period_start else None,
                     head.service_period_end.isoformat() if head.service_period_end else None,
                     svc.strftime("%Y-%m"), head.currency,
                     str(head.exchange_rate) if head.exchange_rate is not None else None,
                     *totals, *base_totals, head.source_file or None, head.file_hash or None,
                     archived_paths.get(gid), head.extraction_method,
                     min(r.confidence for r in lines) if lines else None, template_id))
                log_id = int(cur.lastrowid)
                conn.executemany(
                    """INSERT INTO invoice_lines (log_id, line_no, nominal_code, description, vat_code,
                        net_minor, vat_minor, gross_minor, base_net_minor, base_vat_minor, base_gross_minor)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(log_id, n, line.nominal_code or None, (line.description or line.raw_description) or None,
                      line.vat_code or None, *minor, *base_minor)
                     for n, (line, minor, base_minor) in enumerate(line_values, start=1)])
                ids.append(log_id)
        return ids

    def fetch_duplicate_candidates(self, file_hashes: Iterable[str],
                                   account_codes: Iterable[str]) -> list[HistoricalInvoice]:
        """Exported (non-void) documents sharing a file hash or account code
        with the staging batch — the search space for tiers 1-3."""
        hashes = sorted({h for h in file_hashes if h})
        accounts = sorted({a.strip() for a in account_codes if a and a.strip()})
        found: dict[int, HistoricalInvoice] = {}
        cols = ("id, doc_type, account_code, company_name, invoice_number, invoice_number_norm, "
                "invoice_date, gross_minor, file_hash, source_file_name, exported_at")
        with self.connect() as conn:
            for column, values in (("file_hash", hashes), ("account_code", accounts)):
                for chunk in _chunks(values):
                    marks = ",".join("?" * len(chunk))
                    for r in conn.execute(
                            f"SELECT {cols} FROM invoice_logs WHERE status = 'exported' AND {column} IN ({marks})",
                            list(chunk)):
                        found[r["id"]] = HistoricalInvoice(
                            id=r["id"], doc_type=r["doc_type"], account_code=r["account_code"],
                            company_name=r["company_name"], invoice_number=r["invoice_number"],
                            invoice_number_norm=r["invoice_number_norm"],
                            invoice_date=parse_date(r["invoice_date"]), gross_minor=r["gross_minor"],
                            file_hash=r["file_hash"], source_file_name=r["source_file_name"],
                            exported_at=r["exported_at"])
        return list(found.values())

    def list_invoice_logs(self, *, search: str = "", limit: int = 200, offset: int = 0,
                          include_void: bool = False) -> list[dict[str, Any]]:
        clauses, params = [], []
        if not include_void:
            clauses.append("status = 'exported'")
        if search:
            like = f"%{search.strip()}%"
            clauses.append("(account_code LIKE ? OR company_name LIKE ? OR invoice_number LIKE ? "
                           "OR source_file_name LIKE ?)")
            params += [like] * 4
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM invoice_logs {where} ORDER BY invoice_date DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset]).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            for key in ("net", "vat", "gross", "base_net", "base_vat", "base_gross"):
                item[key] = from_minor(item.pop(f"{key}_minor"))
            out.append(item)
        return out

    def get_invoice_lines(self, log_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM invoice_lines WHERE log_id = ? ORDER BY line_no", (log_id,))
            return [dict(r) for r in rows]

    def void_invoice_log(self, log_id: int, reason: str = "") -> None:
        """Exclude a logged document from analytics and duplicate checks
        (e.g. after the export was rejected by the accounting package)."""
        with self.connect() as conn:
            conn.execute("UPDATE invoice_logs SET status = 'void', void_reason = ? WHERE id = ?",
                         (reason or None, log_id))

    def ledger_lines_frame(self, start: date | None = None, end: date | None = None, *,
                           basis: str = "invoice"):
        """Signed base-currency posting lines for analytics (pandas DataFrame).
        ``basis`` = 'invoice' filters on invoice date, 'service' on accrual month."""
        import pandas as pd

        clauses, params = [], []
        if basis == "service":
            if start:
                clauses.append("service_month >= ?")
                params.append(start.strftime("%Y-%m"))
            if end:
                clauses.append("service_month <= ?")
                params.append(end.strftime("%Y-%m"))
        else:
            if start:
                clauses.append("invoice_date >= ?")
                params.append(start.isoformat())
            if end:
                clauses.append("invoice_date <= ?")
                params.append(end.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            return pd.read_sql_query(f"SELECT * FROM v_ledger_lines{where}", conn, params=params)

    # ------------------------------------------------------------- FX rates
    def get_fx_rate(self, base: str, currency: str, on: date) -> dict[str, Any] | None:
        """Exact cached rate for the requested date; manual entries win over API ones."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM fx_rates WHERE base_currency = ? AND currency = ? AND rate_date = ? "
                "ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END LIMIT 1",
                (base.upper(), currency.upper(), on.isoformat())).fetchone()
        return dict(row) if row else None

    def nearest_fx_rate(self, base: str, currency: str, on: date, max_days: int) -> dict[str, Any] | None:
        """Closest stored rate within ``max_days`` of ``on`` (earlier dates and
        manual entries preferred on ties) — the offline fallback."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT *, ABS(julianday(effective_date) - julianday(?)) AS gap FROM fx_rates
                   WHERE base_currency = ? AND currency = ? AND gap <= ?
                   ORDER BY gap, CASE WHEN effective_date <= ? THEN 0 ELSE 1 END,
                            CASE source WHEN 'manual' THEN 0 ELSE 1 END
                   LIMIT 1""",
                (on.isoformat(), base.upper(), currency.upper(), max_days, on.isoformat())).fetchone()
        return dict(row) if row else None

    def save_fx_rate(self, base: str, currency: str, rate_date: date, rate: Decimal, *,
                     effective_date: date | None = None, source: str = "api") -> None:
        if rate <= 0:
            raise ValueError("Exchange rate must be greater than zero.")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fx_rates (base_currency, currency, rate_date, effective_date, rate, source, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(base_currency, currency, rate_date, source) DO UPDATE SET "
                "effective_date = excluded.effective_date, rate = excluded.rate, fetched_at = excluded.fetched_at",
                (base.upper(), currency.upper(), rate_date.isoformat(),
                 (effective_date or rate_date).isoformat(), str(rate), source, _utcnow()))

    def list_fx_rates(self, *, source: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM fx_rates"
        params: list[Any] = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY rate_date DESC, currency LIMIT ?",
                                                  [*params, limit])]

    def delete_fx_rate(self, base: str, currency: str, rate_date: date, source: str = "manual") -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM fx_rates WHERE base_currency = ? AND currency = ? AND rate_date = ? "
                         "AND source = ?", (base.upper(), currency.upper(), rate_date.isoformat(), source))

    # ------------------------------------------------------------- maintenance
    def backup_to(self, destination: str | Path) -> Path:
        """Consistent online backup (safe while the app is running)."""
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            target = sqlite3.connect(dest)
            try:
                conn.backup(target)
            finally:
                target.close()
        return dest
