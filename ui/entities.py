"""Clients & Suppliers — master list import, editing and learned aliases."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from localledger.db import ENTITY_COLUMN_SYNONYMS, ENTITY_TYPES, Entity, map_entity_columns, read_master_file
from ui.common import flash, get_db, get_rows, page_header, save_rows, vat_code_options

page_header("Clients & Suppliers", "Your master list from the accounting software, plus everything the app has "
                                   "learned from your corrections.")
db = get_db()
FIELD_LABELS = {"entity_type": "Entity type", "company_name": "Company name", "account_code": "Account code",
                "vat_reg_no": "VAT registration no.", "default_nominal_code": "Default nominal code",
                "default_vat_code": "Default VAT code", "default_currency": "Default currency"}

tab_list, tab_import, tab_add = st.tabs(["📇 Master list", "⬆ Import CSV / Excel", "＋ Add"])

with tab_import:
    upload = st.file_uploader("Export of customers / suppliers from your accounting software",
                              type=["csv", "xlsx", "xls"])
    if upload is not None:
        try:
            frame = read_master_file(upload, upload.name)
        except Exception as exc:
            st.error(f"Could not read the file: {exc}")
            st.stop()
        st.dataframe(frame.head(10), width="stretch", hide_index=True)
        guessed = map_entity_columns(frame.columns)
        st.markdown("**Column mapping** — check the guesses")
        columns = ["(not in file)", *map(str, frame.columns)]
        mapping = {}
        cols = st.columns(len(ENTITY_COLUMN_SYNONYMS))
        for col, fld in zip(cols, ENTITY_COLUMN_SYNONYMS):
            pick = col.selectbox(FIELD_LABELS[fld], columns, index=columns.index(guessed.get(fld, "(not in file)")),
                                 key=f"map_{fld}")
            if pick != "(not in file)":
                mapping[fld] = pick
        default_type = None
        if "entity_type" not in mapping:
            default_type = st.radio("No type column — these rows are all", ENTITY_TYPES, horizontal=True)
        if st.button("Import", type="primary"):
            report = db.import_entities(frame, default_entity_type=default_type, column_map=mapping)
            for err in report.errors[:20]:
                st.warning(err)
            if report.inserted or report.updated:
                flash(f"Imported: {report.inserted} new, {report.updated} updated, {report.skipped} skipped.")
                st.rerun()

with tab_add:
    vat_opts = vat_code_options()
    with st.form("add_entity", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        etype = c1.selectbox("Type", ENTITY_TYPES, index=1)
        code = c2.text_input("Account code")
        name = c3.text_input("Company name")
        c1, c2, c3 = st.columns(3)
        vat_no = c1.text_input("VAT registration no.")
        nominal = c2.text_input("Default nominal code")
        vat_code = c3.selectbox("Default VAT code", list(vat_opts), index=None, format_func=lambda c: vat_opts[c])
        if st.form_submit_button("Add", type="primary"):
            try:
                db.upsert_entity(Entity(None, etype, name, code, vat_no or None, nominal or None, vat_code,
                                        source="manual"))
                flash(f"Added {code}.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

with tab_list:
    c1, c2 = st.columns([1, 3])
    etype = c1.segmented_control("Show", ["All", *ENTITY_TYPES], default="All")
    search = c2.text_input("Search", placeholder="Name, account code or VAT number", label_visibility="collapsed")
    entities = db.list_entities(None if etype in (None, "All") else etype, active_only=False)
    if search:
        s = search.lower()
        entities = [e for e in entities if s in e.company_name.lower() or s in e.account_code.lower()
                    or s in (e.vat_reg_no or "").lower() or any(s in a.lower() for a in e.aliases)]
    if not entities:
        st.info("No clients or suppliers yet — import your master list first.")
        st.stop()
    frame = pd.DataFrame([{
        "id": e.id, "Type": e.entity_type, "Account": e.account_code, "Name": e.company_name,
        "VAT no.": e.vat_reg_no or "", "Nominal": e.default_nominal_code or "", "VAT code": e.default_vat_code or "",
        "Aliases": ", ".join(e.aliases), "Source": e.source, "Active": e.is_active} for e in entities])
    event = st.dataframe(frame, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         column_config={"id": None})
    st.caption(f"{len(entities)} record(s). Source **learned** = created from your corrections in the staging area.")

    picked = event.selection.rows if event and event.selection else []
    if picked:
        entity = entities[picked[0]]
        st.subheader(f"{entity.account_code} · {entity.company_name}")
        vat_opts = vat_code_options()
        vat_list = list(vat_opts)
        with st.form(f"edit_entity_{entity.id}"):
            c1, c2 = st.columns(2)
            name = c1.text_input("Company name", entity.company_name)
            vat_no = c2.text_input("VAT registration no.", entity.vat_reg_no or "")
            c1, c2, c3 = st.columns(3)
            nominal = c1.text_input("Default nominal code", entity.default_nominal_code or "")
            vat_code = c2.selectbox("Default VAT code", vat_list,
                                    index=vat_list.index(entity.default_vat_code) if entity.default_vat_code in vat_list else None,
                                    format_func=lambda c: vat_opts[c])
            active = c3.toggle("Active", entity.is_active)
            if st.form_submit_button("Save", type="primary"):
                entity.company_name, entity.vat_reg_no = name, vat_no or None
                entity.default_nominal_code, entity.default_vat_code = nominal or None, vat_code
                entity.is_active, entity.aliases = active, []
                with db.connect() as conn:   # explicit update so blanks *can* clear values here
                    conn.execute("UPDATE entities SET default_nominal_code = ?, default_vat_code = ?, vat_reg_no = ? "
                                 "WHERE id = ?", (nominal or None, vat_code, vat_no or None, entity.id))
                db.upsert_entity(entity)
                flash("Saved.")
                st.rerun()
        if entity.aliases:
            st.markdown("**Learned aliases** — names on documents that map to this account")
            for alias in entity.aliases:
                a1, a2 = st.columns([4, 1])
                a1.write(alias)
                if a2.button("Forget", key=f"alias_{entity.id}_{alias}"):
                    db.remove_alias(entity.id, alias)
                    st.rerun()
        if st.button("Delete this record", key=f"del_{entity.id}"):
            db.delete_entity(entity.id)
            flash(f"Deleted {entity.account_code}.")
            st.rerun()

    rows = get_rows()
    unmatched = [r for r in rows if not r.account_code]
    if unmatched and st.button(f"Re-match {len(unmatched)} unmatched staging row(s)"):
        resolver = db.entity_resolver()
        hits = sum(resolver.apply(r).auto_assigned for r in unmatched)
        save_rows(rows)
        flash(f"Matched {hits} of {len(unmatched)} row(s).")
        st.rerun()
