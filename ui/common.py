"""Shared Streamlit helpers: database handle, staging batch state, formatting."""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import streamlit as st

from localledger import config
from localledger.db import Database
from localledger.models import DocType, StagingRow, fmt_money
from localledger.rules import load_description_rules
from localledger.staging import StagingStore
from localledger.validator import BatchReport, validate_batch

BAND_ICON = {"HIGH": "🟢", "MEDIUM": "🟠", "LOW": "🔴"}
SEVERITY_ICON = {"error": "🔴", "warning": "🟠", "info": "ℹ️"}
DOC_TYPES = [t.value for t in DocType]


# =============================================================================
# Database & batch state
# =============================================================================
def db_path() -> Path:
    return Path(os.environ.get("LOCALLEDGER_DB", str(config.DB_PATH))).expanduser()


@st.cache_resource(show_spinner=False)
def _open_db(path: str) -> Database:
    return Database(path)


def get_db() -> Database:
    return _open_db(str(db_path()))


def get_store() -> StagingStore:
    return StagingStore(get_db())


def get_rows() -> list[StagingRow]:
    """The staging batch for this browser session (loaded from SQLite once)."""
    if "rows" not in st.session_state:
        st.session_state["rows"] = get_store().load()
    return st.session_state["rows"]


def save_rows(rows: list[StagingRow], *, revalidate: bool = True) -> BatchReport | None:
    report = validate_batch(rows, get_db()) if revalidate and rows else None
    get_store().save(rows)
    st.session_state["rows"] = rows
    return report


def reload_rows() -> None:
    st.session_state.pop("rows", None)


def description_rules():
    return load_description_rules(get_db().get_setting("description_rules"))


# =============================================================================
# Messages that survive a rerun
# =============================================================================
def flash(message: str, kind: str = "success") -> None:
    st.session_state.setdefault("_flash", []).append((kind, message))


def show_flashes() -> None:
    for kind, message in st.session_state.pop("_flash", []):
        {"success": st.success, "warning": st.warning, "error": st.error}.get(kind, st.info)(message)


# =============================================================================
# Formatting
# =============================================================================
def is_dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except Exception:
        return False


def fmt(value: Decimal | float | None, currency: str = "") -> str:
    return fmt_money(value, currency) if value is not None else "—"


def band_badge(row: StagingRow) -> str:
    if not row.confidence_band:
        return "—"
    return f"{BAND_ICON.get(row.confidence_band, '')} {row.confidence_band} {row.confidence}%"


def to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def entity_options(entity_type: str) -> dict[str, str]:
    """account code -> 'CODE · Company' for select boxes."""
    return {e.account_code: f"{e.account_code} · {e.company_name}"
            for e in get_db().list_entities(entity_type)}


def vat_code_options() -> dict[str, str]:
    return {v.code: f"{v.code} · {v.description} ({format(v.rate.normalize(), 'f')}%)"
            for v in get_db().list_vat_codes(active_only=True)}


def select_index(options: list[Any], value: Any) -> int | None:
    try:
        return options.index(value)
    except ValueError:
        return None


def page_header(title: str, caption: str = "") -> None:
    st.title(title)
    if caption:
        st.caption(caption)
    show_flashes()


def group_label(rows: Iterable[StagingRow]) -> str:
    rows = list(rows)
    head = rows[0]
    name = head.display_name or head.source_file
    return f"{head.doc_type.value} {head.invoice_number or '(no number)'} · {name}"
