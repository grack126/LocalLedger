"""History — every exported document, with void (undo) support."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from localledger.models import fmt_money
from ui.common import flash, get_db, page_header

page_header("History", "Every exported document. Voiding removes it from analytics and duplicate checks.")
db = get_db()
c1, c2 = st.columns([3, 1])
search = c1.text_input("Search", placeholder="Account, supplier, invoice number or file name")
include_void = c2.toggle("Show voided", value=False)
logs = db.list_invoice_logs(search=search, limit=1000, include_void=include_void)
if not logs:
    st.info("Nothing exported yet." if not search else "No matches.")
    st.stop()

frame = pd.DataFrame(logs)
view = frame[["id", "invoice_date", "doc_type", "account_code", "company_name", "invoice_number", "currency",
              "net", "vat", "gross", "status", "source_file_name", "exported_at", "batch_id"]].copy()
for col in ("net", "vat", "gross"):
    view[col] = view[col].astype(float)
event = st.dataframe(view, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                     column_config={"id": None, "invoice_date": "Date", "doc_type": "Type", "account_code": "Account",
                                    "company_name": "Name", "invoice_number": "Invoice No", "currency": "Ccy",
                                    "net": st.column_config.NumberColumn("Net", format="%.2f"),
                                    "vat": st.column_config.NumberColumn("VAT", format="%.2f"),
                                    "gross": st.column_config.NumberColumn("Gross", format="%.2f"),
                                    "status": "Status", "source_file_name": "File", "exported_at": "Exported",
                                    "batch_id": "Batch"})
st.caption(f"{len(view)} document(s) · total gross {fmt_money(view['gross'].sum())} (mixed currencies not converted)")

picked = event.selection.rows if event and event.selection else []
if picked:
    log = logs[picked[0]]
    st.subheader(f"{log['doc_type']} {log['invoice_number'] or ''} — {log['company_name'] or log['account_code']}")
    lines = pd.DataFrame(db.get_invoice_lines(log["id"]))
    if not lines.empty:
        for col in ("net", "vat", "gross"):
            lines[col] = lines[f"{col}_minor"] / 100
        st.dataframe(lines[["line_no", "nominal_code", "vat_code", "description", "net", "vat", "gross"]],
                     hide_index=True, width="stretch")
    if log.get("archived_path"):
        st.caption(f"📁 Archived at {log['archived_path']}")
    if log["status"] == "exported":
        with st.form(f"void_{log['id']}"):
            reason = st.text_input("Reason for voiding", placeholder="e.g. rejected by Sage import, re-exporting")
            if st.form_submit_button("Void this document"):
                db.void_invoice_log(log["id"], reason)
                flash("Document voided.")
                st.rerun()
    else:
        st.caption(f"Voided: {log.get('void_reason') or 'no reason given'}")
