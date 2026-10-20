"""Export Templates — presets for Sage 50 / Xero / QuickBooks / FreeAgent plus custom layouts."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from localledger.models import ALL_FIELDS
from localledger.rules import (
    AMOUNT_SIGNS,
    DATE_FORMATS,
    FILE_FORMATS,
    SAMPLE_ROW,
    SERVICE_PERIOD_STYLES,
    ColumnSpec,
    map_rows,
)
from ui.common import DOC_TYPES, flash, get_db, get_rows, page_header

page_header("Export Templates", "Map the internal fields to your accounting software's import columns.")
db = get_db()
templates = db.list_templates()
names = [t.name for t in templates]
active = db.get_setting("active_template")

c1, c2 = st.columns([3, 1])
choice = c1.selectbox("Template", names, index=names.index(st.session_state.get("tpl_choice", active))
                      if st.session_state.get("tpl_choice", active) in names else 0,
                      format_func=lambda n: f"{'🔒 ' if templates[names.index(n)].is_builtin else ''}{n}"
                                            f"{'  ✓ active' if n == active else ''}")
tpl = templates[names.index(choice)]
if c2.button("Set as active", disabled=choice == active, width="stretch"):
    db.set_setting("active_template", choice)
    flash(f"“{choice}” is now the default export template.")
    st.rerun()
if tpl.notes:
    st.caption(tpl.notes)

if tpl.is_builtin:
    st.info("Built-in presets are read-only. Duplicate one to adjust columns or formats.")
    with st.form("clone"):
        new_name = st.text_input("Name for your copy", f"{tpl.name} (custom)")
        if st.form_submit_button("Duplicate", type="primary"):
            try:
                db.save_template(tpl.clone(new_name))
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state["tpl_choice"] = new_name
                flash(f"Created “{new_name}”.")
                st.rerun()

editable = not tpl.is_builtin
with st.form(f"tpl_{tpl.id}"):
    c1, c2, c3 = st.columns(3)
    name = c1.text_input("Name", tpl.name, disabled=not editable)
    software = c2.text_input("Software", tpl.software, disabled=not editable)
    date_keys = list(DATE_FORMATS)
    date_format = c3.selectbox("Date format", date_keys, index=date_keys.index(tpl.date_format)
                               if tpl.date_format in date_keys else 0, disabled=not editable)
    c1, c2, c3, c4 = st.columns(4)
    file_format = c1.selectbox("File type", FILE_FORMATS, index=FILE_FORMATS.index(tpl.file_format),
                               disabled=not editable)
    delimiter = c2.selectbox("CSV delimiter", [",", ";", "\t", "|"], index=[",", ";", "\t", "|"].index(tpl.delimiter)
                             if tpl.delimiter in [",", ";", "\t", "|"] else 0,
                             format_func=lambda d: {"\t": "Tab"}.get(d, d), disabled=not editable)
    encodings = ["utf-8", "utf-8-sig", "cp1252", "latin-1"]
    encoding = c3.selectbox("Encoding", encodings, index=encodings.index(tpl.encoding) if tpl.encoding in encodings else 0,
                            disabled=not editable, help="cp1252 suits Sage 50; utf-8-sig opens cleanly in Excel")
    decimals = c4.number_input("Decimal places", 0, 6, tpl.decimal_places, disabled=not editable)
    c1, c2, c3, c4 = st.columns(4)
    amount_sign = c1.selectbox("Credit note amounts", AMOUNT_SIGNS, index=AMOUNT_SIGNS.index(tpl.amount_sign),
                               format_func={"positive": "Positive (type carries sign)",
                                            "negative_for_credits": "Negative"}.get, disabled=not editable)
    period_style = c2.selectbox("Service period as", SERVICE_PERIOD_STYLES,
                                index=SERVICE_PERIOD_STYLES.index(tpl.service_period_style),
                                format_func={"month": "Month (Sep 2026)", "range": "Date range"}.get,
                                disabled=not editable)
    applies_to = c3.multiselect("Document types", DOC_TYPES, tpl.applies_to, disabled=not editable)
    include_header = c4.toggle("Header row", tpl.include_header, disabled=not editable)

    st.markdown("**Columns** — order top to bottom; leave *Field* empty for a static value")
    col_frame = pd.DataFrame([c.to_dict() for c in tpl.columns], columns=["header", "source", "default", "max_length"])
    columns = st.data_editor(
        col_frame, num_rows="dynamic" if editable else "fixed", disabled=not editable, width="stretch",
        hide_index=True, key=f"cols_{tpl.id}",
        column_config={
            "header": st.column_config.TextColumn("Output column", required=True),
            "source": st.column_config.SelectboxColumn("Field", options=list(ALL_FIELDS)),
            "default": st.column_config.TextColumn("Default / static value"),
            "max_length": st.column_config.NumberColumn("Max length", min_value=1, step=1),
        })
    value_maps = st.text_area("Value maps (JSON)", json.dumps(tpl.value_maps, indent=2), height=140,
                              disabled=not editable,
                              help='Translate values, e.g. {"VAT Code": {"purchase:T1": "20% (VAT on Expenses)"}}. '
                                   'Keys may be "PI:T1", "purchase:T1" or "T1".')
    saved = st.form_submit_button("Save template", type="primary", disabled=not editable)

if saved and editable:
    try:
        maps = json.loads(value_maps or "{}")
        tpl.name, tpl.software, tpl.date_format, tpl.file_format = name, software, date_format, file_format
        tpl.delimiter, tpl.encoding, tpl.decimal_places = delimiter, encoding, int(decimals)
        tpl.amount_sign, tpl.service_period_style, tpl.applies_to = amount_sign, period_style, applies_to
        tpl.include_header, tpl.value_maps = include_header, maps
        tpl.columns = [ColumnSpec.from_dict({k: (None if pd.isna(v) else v) for k, v in rec.items()})
                       for rec in columns.to_dict("records") if str(rec.get("header") or "").strip()]
        db.save_template(tpl)
    except (ValueError, json.JSONDecodeError) as exc:
        st.error(str(exc))
    else:
        st.session_state["tpl_choice"] = tpl.name
        flash("Template saved.")
        st.rerun()

if editable and st.button("Delete template"):
    db.delete_template(tpl.id)
    st.session_state.pop("tpl_choice", None)
    flash(f"Deleted “{tpl.name}”.")
    st.rerun()

st.subheader("Preview")
rows = get_rows() or [SAMPLE_ROW]
st.caption("Using your staging batch." if get_rows() else "Using a sample invoice (your staging batch is empty).")
try:
    result = map_rows(rows[:20], tpl, base_currency=db.get_setting("base_currency"))
except ValueError as exc:
    st.error(str(exc))
else:
    st.dataframe(result.to_dataframe(), hide_index=True, width="stretch")
    for w in result.warnings:
        st.caption(f"⚠ {w}")
    if result.excluded:
        st.caption(f"⏭ {len(result.excluded)} row(s) skipped — type not covered by this template.")
    st.download_button("Download preview", result.to_bytes(), file_name=f"preview{tpl.file_extension}",
                       mime=tpl.mime_type)
