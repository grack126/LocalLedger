"""Settings — business details, extraction engines, validation, archive, FX, VAT codes, data."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

from localledger.extraction import ExtractionPipeline
from localledger.fx import FxService
from localledger.models import ISO_CURRENCIES, StagingRow
from localledger.rules import archive_subfolder, render_filename, SAMPLE_ROW
from ui.common import db_path, flash, get_db, page_header

page_header("Settings")
db = get_db()
s = db.get_settings()
CURRENCIES = sorted(ISO_CURRENCIES)

tabs = st.tabs(["🏢 Business", "🔎 Extraction", "✅ Validation", "📁 Archive", "💱 Exchange rates",
                "🧾 VAT codes", "💾 Data"])

with tabs[0], st.form("business"):
    st.caption("Used to tell **your** sales invoices apart from supplier invoices.")
    c1, c2 = st.columns(2)
    own_name = c1.text_input("Your company name", s["own_company_name"])
    own_vat = c2.text_input("Your VAT number", s["own_vat_number"])
    c1, c2, c3 = st.columns(3)
    base = c1.selectbox("Base currency", CURRENCIES, index=CURRENCIES.index(s["base_currency"]))
    order = c2.selectbox("Read ambiguous dates as", ["DMY", "MDY"], index=["DMY", "MDY"].index(s["date_order"]),
                         format_func={"DMY": "Day first (01/02 = 1 Feb)", "MDY": "Month first (01/02 = 2 Jan)"}.get)
    direction = c3.selectbox("Document direction", ["auto", "purchase", "sales"],
                             index=["auto", "purchase", "sales"].index(s["extraction_direction"]),
                             format_func={"auto": "Detect automatically", "purchase": "Always purchases",
                                          "sales": "Always sales"}.get)
    if st.form_submit_button("Save", type="primary"):
        db.set_settings({"own_company_name": own_name, "own_vat_number": own_vat, "base_currency": base,
                         "date_order": order, "extraction_direction": direction})
        flash("Saved.")
        st.rerun()

with tabs[1]:
    with st.form("extraction"):
        st.markdown("**OCR** (for scanned documents)")
        c1, c2, c3 = st.columns(3)
        engines = ["tesseract", "easyocr", "none"]
        engine = c1.selectbox("Engine", engines, index=engines.index(s["ocr_engine"]))
        cmd = c2.text_input("Tesseract path (optional)", s["tesseract_cmd"],
                            help=r"e.g. C:\Program Files\Tesseract-OCR\tesseract.exe")
        dpi = c3.select_slider("Scan resolution (DPI)", [200, 250, 300, 400], value=int(s["ocr_dpi"]))
        st.markdown("**Local LLM** (optional, for messy layouts)")
        c1, c2, c3 = st.columns(3)
        llm_on = c1.toggle("Enable local LLM", s["llm_enabled"])
        mode = c2.selectbox("When to use it", ["fallback", "always"], index=["fallback", "always"].index(s["llm_mode"]),
                            format_func={"fallback": "Only when regex is incomplete",
                                         "always": "On every document"}.get)
        timeout = c3.number_input("Timeout (s)", 10, 600, int(s["llm_timeout"]))
        c1, c2 = st.columns(2)
        endpoint = c1.text_input("Endpoint (OpenAI-compatible)", s["llm_endpoint"],
                                 help="Ollama: http://localhost:11434/v1 · LM Studio: http://localhost:1234/v1")
        model = c2.text_input("Model", s["llm_model"])
        remote = st.checkbox("Allow a remote endpoint (sends document text off this machine)", s["llm_allow_remote"])
        if st.form_submit_button("Save", type="primary"):
            db.set_settings({"ocr_engine": engine, "tesseract_cmd": cmd, "ocr_dpi": dpi, "llm_enabled": llm_on,
                             "llm_mode": mode, "llm_timeout": timeout, "llm_endpoint": endpoint, "llm_model": model,
                             "llm_allow_remote": remote})
            flash("Saved.")
            st.rerun()
    if st.button("Check engines"):
        for name, status in ExtractionPipeline.from_db(db, fx=None).engine_status().items():
            (st.success if "ready" in status or status == "disabled" else st.warning)(f"**{name}:** {status}")

with tabs[2], st.form("validation"):
    c1, c2, c3 = st.columns(3)
    tol = c1.number_input("Net + VAT = Gross tolerance", 0.0, 1.0, float(s["math_tolerance"]), 0.01, format="%.2f")
    rate_tol = c2.number_input("VAT rate tolerance (points)", 0.0, 5.0, float(s["vat_rate_tolerance_pp"]), 0.05)
    window = c3.number_input("Duplicate date window (± days)", 0, 30, int(s["duplicate_window_days"]))
    c1, c2, c3, c4 = st.columns(4)
    high = c1.number_input("HIGH confidence from", 51, 100, int(s["confidence_high"]))
    auto = c2.number_input("Auto-match supplier at ≥", 50, 100, int(s["fuzzy_auto_threshold"]))
    suggest = c3.number_input("Suggest matches at ≥", 30, 100, int(s["fuzzy_suggest_threshold"]))
    allow = c4.toggle("Allow export with errors", s["allow_export_with_errors"],
                      help="Duplicates must always be dismissed or removed.")
    c1, c2 = st.columns(2)
    anomaly = c1.number_input("Run-rate alert above average by (%)", 5, 500, int(s["anomaly_threshold_pct"]))
    lookback = c2.number_input("Run-rate average over (months)", 2, 24, int(s["anomaly_lookback_months"]))
    st.caption("LOW confidence is always below 50% — a math failure forces LOW.")
    if st.form_submit_button("Save", type="primary"):
        db.set_settings({"math_tolerance": tol, "vat_rate_tolerance_pp": rate_tol, "duplicate_window_days": window,
                         "confidence_high": high, "fuzzy_auto_threshold": auto, "fuzzy_suggest_threshold": suggest,
                         "allow_export_with_errors": allow, "anomaly_threshold_pct": anomaly,
                         "anomaly_lookback_months": lookback})
        flash("Saved.")
        st.rerun()

with tabs[3]:
    with st.form("archive"):
        on = st.toggle("Archive PDFs on export", s["archive_enabled"])
        folder = st.text_input("Archive folder", s["archive_dir"])
        c1, c2, c3 = st.columns(3)
        sub = c1.text_input("Sub-folder pattern", s["archive_subfolder_pattern"], help="{YYYY} {MM} {Ledger} {Type} {AccountCode}")
        pattern = c2.text_input("File name pattern", s["rename_pattern"],
                                help="[AccountCode] [InvoiceDate] [InvoiceNumber] [Type] [CompanyName] [Gross] "
                                     "[Currency] [ServiceMonth] [OriginalName]")
        mode = c3.selectbox("Files imported from a folder", ["copy", "move"], index=["copy", "move"].index(s["archive_mode"]),
                            format_func={"copy": "Copy (leave originals)", "move": "Move into archive"}.get)
        st.caption(f"Example: `{Path(folder) / archive_subfolder(sub, SAMPLE_ROW) / render_filename(pattern, SAMPLE_ROW)}`")
        if st.form_submit_button("Save", type="primary"):
            db.set_settings({"archive_enabled": on, "archive_dir": folder, "archive_subfolder_pattern": sub,
                             "rename_pattern": pattern, "archive_mode": mode})
            flash("Saved.")
            st.rerun()

with tabs[4]:
    with st.form("fx"):
        c1, c2, c3 = st.columns([1, 2, 1])
        fx_on = c1.toggle("Online lookup", s["fx_enabled"])
        url = c2.text_input("Rates API", s["fx_api_url"], help="Frankfurter (ECB reference rates), no key needed")
        stale = c3.number_input("Offline: use rates up to (days) old", 0, 60, int(s["fx_max_stale_days"]))
        if st.form_submit_button("Save", type="primary"):
            db.set_settings({"fx_enabled": fx_on, "fx_api_url": url, "fx_max_stale_days": stale})
            flash("Saved.")
            st.rerun()
    st.markdown(f"**Manual rates** — units of currency per 1 {s['base_currency']}; these override fetched rates.")
    with st.form("manual_rate", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        cur = c1.selectbox("Currency", [c for c in CURRENCIES if c != s["base_currency"]])
        on_date = c2.date_input("Date", date.today(), format="DD/MM/YYYY")
        rate = c3.number_input("Rate", min_value=0.0, value=1.0, step=0.0001, format="%.6f")
        if st.form_submit_button("Add rate") and rate > 0:
            FxService.from_db(db).set_manual_rate(cur, on_date, Decimal(str(rate)))
            flash(f"Saved 1 {s['base_currency']} = {rate} {cur} on {on_date:%d/%m/%Y}.")
            st.rerun()
    rates = db.list_fx_rates(limit=200)
    if rates:
        st.dataframe(pd.DataFrame(rates)[["rate_date", "effective_date", "currency", "rate", "source", "fetched_at"]],
                     hide_index=True, width="stretch")

with tabs[5]:
    codes = db.list_vat_codes()
    frame = pd.DataFrame([{"code": v.code, "description": v.description, "rate": float(v.rate),
                           "reverse_charge": v.is_reverse_charge, "active": v.is_active, "order": v.sort_order}
                          for v in codes])
    edited = st.data_editor(frame, num_rows="dynamic", hide_index=True, width="stretch", key="vat_editor",
                            column_config={
                                "code": st.column_config.TextColumn("Code", required=True),
                                "description": st.column_config.TextColumn("Description", required=True),
                                "rate": st.column_config.NumberColumn("Rate %", min_value=0, max_value=100, format="%.2f"),
                                "reverse_charge": st.column_config.CheckboxColumn(
                                    "Reverse charge", help="Supplier shows 0 VAT; you account for it"),
                                "active": st.column_config.CheckboxColumn("Active", default=True),
                                "order": st.column_config.NumberColumn("Order", step=1, default=999)})
    if st.button("Save VAT codes", type="primary"):
        keep = set()
        try:
            for rec in edited.dropna(subset=["code"]).to_dict("records"):
                code = str(rec["code"]).strip().upper()
                keep.add(code)
                db.upsert_vat_code(code, str(rec.get("description") or code), rec.get("rate") or 0,
                                   is_reverse_charge=bool(rec.get("reverse_charge")),
                                   is_active=bool(rec.get("active", True)), sort_order=int(rec.get("order") or 999))
        except ValueError as exc:
            st.error(str(exc))
        else:
            for v in codes:
                if v.code.upper() not in keep:
                    db.delete_vat_code(v.code)
            flash("VAT codes saved.")
            st.rerun()

with tabs[6]:
    st.markdown(f"**Database:** `{db_path()}`")
    st.caption("Everything — master list, history, templates, settings and the current batch — lives in this one "
               "SQLite file on your computer.")
    if st.button("Create backup"):
        dest = db.backup_to(db_path().parent / "backups" / f"localledger_{datetime.now():%Y%m%d_%H%M%S}.db")
        st.session_state["backup_path"] = str(dest)
    if st.session_state.get("backup_path"):
        path = Path(st.session_state["backup_path"])
        st.success(f"Backup saved to {path}")
        st.download_button("Download backup", path.read_bytes(), file_name=path.name,
                           mime="application/vnd.sqlite3")
