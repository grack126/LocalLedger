"""Upload & Review — ingest PDFs, review each one beside the original, export."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from localledger.exporter import ExportBlocked, ExportOptions, export_batch, plan_export
from localledger.extraction import ExtractionPipeline, page_count, render_page_png, sha256_bytes
from localledger.fx import FxService
from localledger.models import ISO_CURRENCIES, DocType, F, Severity, StagingRow
from localledger.rules import build_description
from localledger.staging import (
    SplitError,
    add_results,
    apply_changes,
    default_split,
    find_row,
    group_rows,
    make_manual_row,
    rebuild_descriptions,
    remove_group,
    split_document,
    unsplit_document,
)
from localledger.validator import (
    HIGH,
    LOW,
    MEDIUM,
    ValidationSettings,
    dismiss_flags,
    export_blockers,
    validate_batch,
)
from ui.common import (
    BAND_ICON,
    DOC_TYPES,
    SEVERITY_ICON,
    band_badge,
    description_rules,
    entity_options,
    flash,
    fmt,
    get_db,
    get_rows,
    get_store,
    group_label,
    page_header,
    save_rows,
    select_index,
    to_float,
    vat_code_options,
)

GRID_COLUMNS = {  # internal field -> grid column (for red/amber cell highlighting)
    F.TYPE: "Type", F.ACCOUNT_CODE: "Account", F.INVOICE_NUMBER: "Invoice No", F.INVOICE_DATE: "Date",
    F.NET: "Net", F.VAT: "VAT", F.GROSS: "Gross", F.VAT_CODE: "VAT Code", F.NOMINAL_CODE: "Nominal",
    F.DESCRIPTION: "Description", F.CURRENCY: "Ccy", F.EXCHANGE_RATE: "FX rate",
}
ERROR_CSS = "background-color: rgba(227, 73, 72, 0.22);"
WARN_CSS = "background-color: rgba(237, 161, 0, 0.18);"
CURRENCIES = sorted(ISO_CURRENCIES)


def _bump_grid() -> None:
    """Reset the grid's selection after structural changes (rows added/removed/split)."""
    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1


def _select(row_id: str | None) -> None:
    st.session_state["selected_row_id"] = row_id
    _bump_grid()


# =============================================================================
# Ingest
# =============================================================================
def run_extraction(rows: list[StagingRow], items: list) -> None:
    pipeline = ExtractionPipeline.from_db(get_db())
    bar = st.progress(0.0, text="Reading documents…")

    def progress(i: int, n: int, name: str) -> None:
        bar.progress((i - 1) / max(n, 1), text=f"Reading {name} ({i}/{n})")

    results = pipeline.process_many(items, progress=progress)
    bar.empty()
    add_results(rows, [r for r in results if r.ok])
    save_rows(rows)
    st.session_state["last_extraction"] = [
        {"file": r.filename, "ok": r.ok, "error": r.error, "warnings": r.warnings, "log": r.log}
        for r in results]
    ok = sum(r.ok for r in results)
    flash(f"Read {ok} of {len(results)} document(s).", "success" if ok == len(results) else "warning")
    _bump_grid()
    st.rerun()


def ingest_section(rows: list[StagingRow]) -> None:
    store = get_store()
    with st.expander("➕ Add documents", expanded=not rows):
        tab_upload, tab_folder, tab_manual = st.tabs(["Upload PDFs", "Import a folder", "Manual entry"])
        with tab_upload:
            key = f"uploader_{st.session_state.get('uploader_n', 0)}"
            files = st.file_uploader("Drop PDF invoices and credit notes here", type=["pdf"],
                                     accept_multiple_files=True, key=key)
            if files and st.button(f"Extract {len(files)} file(s)", type="primary", key="extract_uploads"):
                items = []
                for f in files:
                    data = f.getvalue()
                    path = store.store_upload(f.name, data, sha256_bytes(data))
                    items.append((f.name, data, str(path)))
                st.session_state["uploader_n"] = st.session_state.get("uploader_n", 0) + 1
                run_extraction(rows, items)
        with tab_folder:
            folder = st.text_input("Folder path", placeholder=r"C:\Users\me\Documents\Invoices\October")
            recursive = st.checkbox("Include sub-folders")
            st.caption("Files stay where they are until export; then they are copied (or moved, per "
                       "Settings › Archive) into the archive.")
            if st.button("Import folder", disabled=not folder.strip()):
                path = Path(folder.strip().strip('"')).expanduser()
                if not path.is_dir():
                    st.error(f"Folder not found: {path}")
                else:
                    staged = {r.file_path for r in rows}
                    pdfs = sorted(p for p in (path.rglob("*") if recursive else path.glob("*"))
                                  if p.suffix.lower() == ".pdf" and str(p) not in staged)
                    if not pdfs:
                        st.info("No new PDFs in that folder.")
                    else:
                        run_extraction(rows, [str(p) for p in pdfs])
        with tab_manual:
            st.write("Key in a paper receipt or an adjustment directly.")
            if st.button("＋ Add manual row", key="open_manual"):
                manual_dialog()
        status = ExtractionPipeline.from_db(get_db(), fx=None).engine_status()
        st.caption(" · ".join(f"**{k}:** {v}" for k, v in status.items()))

    report = st.session_state.get("last_extraction")
    if report:
        problems = [r for r in report if r["warnings"] or not r["ok"]]
        with st.expander(f"Extraction report — {len(report)} file(s), {len(problems)} with notes"):
            for item in report:
                icon = "✅" if item["ok"] and not item["warnings"] else ("🟠" if item["ok"] else "🔴")
                st.markdown(f"{icon} **{item['file']}**")
                for w in item["warnings"]:
                    st.caption(f"⚠ {w}")
                if item["log"]:
                    st.caption(" · ".join(item["log"]))
            if st.button("Clear report"):
                st.session_state.pop("last_extraction")
                st.rerun()


# =============================================================================
# Dialogs
# =============================================================================
@st.dialog("Add manual row", width="large")
def manual_dialog() -> None:
    db = get_db()
    vat_opts = vat_code_options()
    with st.form("manual_row"):
        c1, c2 = st.columns(2)
        doc_type = c1.selectbox("Type", DOC_TYPES, format_func=lambda t: f"{t} · {DocType(t).label}")
        entity_type = DocType(doc_type).entity_type
        accounts = entity_options(entity_type)
        account = c2.selectbox(f"{entity_type} account", list(accounts), index=None,
                               format_func=lambda c: accounts.get(c, c), accept_new_options=True,
                               placeholder="Choose or type a code")
        c1, c2 = st.columns(2)
        invoice_number = c1.text_input("Invoice / receipt number")
        invoice_date = c2.date_input("Date", value=None, format="DD/MM/YYYY")
        c1, c2 = st.columns(2)
        nominal = c1.text_input("Nominal code", help="Leave blank to use the supplier's default")
        vat_code = c2.selectbox("VAT code", list(vat_opts), index=None, format_func=lambda c: vat_opts[c],
                                placeholder="Supplier default")
        c1, c2, c3 = st.columns(3)
        net = c1.number_input("Net", value=None, step=0.01, format="%.2f")
        vat = c2.number_input("VAT", value=None, step=0.01, format="%.2f",
                              help="Leave blank to calculate from the VAT code")
        gross = c3.number_input("Gross", value=None, step=0.01, format="%.2f", help="Optional")
        description = st.text_input("Description")
        currency = st.selectbox("Currency", CURRENCIES, index=CURRENCIES.index(db.get_setting("base_currency")))
        if st.form_submit_button("Add row", type="primary"):
            resolver = db.entity_resolver()
            entity = resolver.by_account(account or "", entity_type)
            code = vat_code or (entity.default_vat_code if entity else "")
            if vat is None and net is not None and code:
                rate = db.vat_code_map().get(code.upper())
                if rate is not None:
                    vat = round(float(net) * float(rate.invoice_rate) / 100, 2)
            row = make_manual_row(doc_type=doc_type, account_code=account or "", invoice_number=invoice_number,
                                  invoice_date=invoice_date, nominal_code=nominal, vat_code=code or "",
                                  net=net, vat=vat, gross=gross, description=description,
                                  currency=currency, entity=entity)
            row.description = build_description(row, description_rules()) if description else ""
            rows = get_rows()
            rows.append(row)
            save_rows(rows)
            FxService.from_db(db).apply_to_row(row)
            save_rows(rows)
            _select(row.row_id)
            flash("Manual row added.")
            st.rerun()


@st.dialog("Split invoice", width="large")
def split_dialog(group_id: str) -> None:
    rows = get_rows()
    current = group_rows(rows, group_id)
    if not current:
        st.error("Document no longer in the batch.")
        return
    head = current[0]
    target = head.doc_gross if head.doc_gross is not None else sum((r.gross or 0) for r in current)
    st.markdown(f"**{group_label(current)}** — invoice gross **{fmt(target, head.currency)}**")
    st.caption("Give each line its own nominal and VAT code. Lines must add up to the invoice gross.")
    vat_codes = list(vat_code_options())
    frame = pd.DataFrame(default_split(rows, group_id))
    edited = st.data_editor(
        frame, num_rows="dynamic", width="stretch", key=f"split_{group_id}",
        column_config={
            "nominal_code": st.column_config.TextColumn("Nominal", required=True),
            "vat_code": st.column_config.SelectboxColumn("VAT code", options=vat_codes, required=True),
            "net": st.column_config.NumberColumn("Net", format="%.2f", required=True),
            "vat": st.column_config.NumberColumn("VAT", format="%.2f", required=True),
            "description": st.column_config.TextColumn("Description"),
        })
    clean = edited.dropna(subset=["net", "vat"], how="all").fillna({"net": 0, "vat": 0})
    total = float((clean["net"] + clean["vat"]).sum()) if not clean.empty else 0.0
    diff = total - float(target)
    c1, c2, c3 = st.columns(3)
    c1.metric("Lines total", fmt(total, head.currency))
    c2.metric("Invoice gross", fmt(target, head.currency))
    c3.metric("Difference", fmt(diff, head.currency))
    if st.button("Apply split", type="primary", disabled=abs(diff) > 0.02 or clean.empty):
        try:
            new_rows = split_document(rows, group_id, clean.to_dict("records"),
                                      description_rules=description_rules())
        except SplitError as exc:
            st.error(str(exc))
            return
        save_rows(rows)
        _select(new_rows[0].row_id)
        flash(f"Invoice split into {len(new_rows)} line(s).")
        st.rerun()


# =============================================================================
# Grid
# =============================================================================
def grid_frame(rows: list[StagingRow]) -> tuple[pd.DataFrame, pd.DataFrame]:
    sizes = {}
    for r in rows:
        sizes[r.group_id] = sizes.get(r.group_id, 0) + 1
    records, styles = [], []
    for r in rows:
        open_flags = r.open_duplicate_flags
        dup = f"⚠ T{min(f.tier for f in open_flags)}" if open_flags else ("✓" if r.duplicate_flags else "")
        records.append({
            "Status": band_badge(r), "Dup": dup, "Type": r.doc_type.value,
            "Line": f"{r.line_no}/{sizes[r.group_id]}" if sizes[r.group_id] > 1 else "",
            "Account": r.account_code, "Name": r.display_name, "Invoice No": r.invoice_number,
            "Date": r.invoice_date, "Net": to_float(r.net), "VAT": to_float(r.vat), "Gross": to_float(r.gross),
            "VAT Code": r.vat_code, "Nominal": r.nominal_code, "Description": r.description or r.raw_description,
            "Ccy": r.currency, "FX rate": to_float(r.exchange_rate), "File": r.source_file,
        })
        css = {}
        for fld, score in r.field_scores.items():
            col = GRID_COLUMNS.get(fld)
            if col and score < 60:
                css[col] = WARN_CSS
        for fld in r.error_fields:
            col = GRID_COLUMNS.get(fld)
            if col:
                css[col] = ERROR_CSS
        if open_flags:
            css["Dup"] = ERROR_CSS
        styles.append(css)
    df = pd.DataFrame(records, index=[r.row_id for r in rows])
    style_df = pd.DataFrame(styles, index=df.index).reindex(columns=df.columns).fillna("")
    return df, style_df


def grid(rows: list[StagingRow]) -> str | None:
    df, css = grid_frame(rows)
    styler = df.style.apply(lambda _: css, axis=None).format(
        {"Net": "{:,.2f}", "VAT": "{:,.2f}", "Gross": "{:,.2f}", "FX rate": "{:.4f}",
         "Date": lambda d: d.strftime("%d/%m/%Y") if pd.notna(d) and d else "—"}, na_rep="—")
    event = st.dataframe(
        styler, on_select="rerun", selection_mode="single-row", hide_index=True, width="stretch",
        height=min(460, 38 + 35 * max(len(df), 1)), key=f"grid_{st.session_state.get('grid_version', 0)}",
        column_config={
            "Status": st.column_config.TextColumn(width=110, help="Confidence: 🟢 high · 🟠 medium · 🔴 low"),
            "Dup": st.column_config.TextColumn(width=64, help="Duplicate flag tier (⚠ open, ✓ dismissed)"),
            "Description": st.column_config.TextColumn(width="large"),
        })
    picked = event.selection.rows if event and event.selection else []
    if picked:
        st.session_state["selected_row_id"] = df.index[picked[0]]
    return st.session_state.get("selected_row_id")


def summary(rows: list[StagingRow], vs: ValidationSettings) -> None:
    docs = {r.group_id for r in rows}
    bands = {b: sum(1 for r in rows if r.confidence_band == b) for b in (HIGH, MEDIUM, LOW)}
    dups = len({r.group_id for r in rows if r.open_duplicate_flags})
    blocked = {r.group_id for r in rows if export_blockers(r, vs)}
    cols = st.columns(6)
    cols[0].metric("Documents", len(docs))
    cols[1].metric(f"{BAND_ICON[HIGH]} High", bands[HIGH])
    cols[2].metric(f"{BAND_ICON[MEDIUM]} Medium", bands[MEDIUM])
    cols[3].metric(f"{BAND_ICON[LOW]} Low", bands[LOW])
    cols[4].metric("⚠ Duplicates", dups)
    cols[5].metric("Ready to export", len(docs - blocked))


def filter_rows(rows: list[StagingRow], vs: ValidationSettings) -> list[StagingRow]:
    choice = st.segmented_control("Show", ["All", "Needs attention", "Duplicates", "Ready"],
                                  default="All", key="grid_filter", label_visibility="collapsed")
    blocked = {r.group_id for r in rows if export_blockers(r, vs)}
    if choice == "Needs attention":
        return [r for r in rows if r.group_id in blocked or r.confidence_band != HIGH]
    if choice == "Duplicates":
        return [r for r in rows if r.duplicate_flags]
    if choice == "Ready":
        return [r for r in rows if r.group_id not in blocked]
    return rows


# =============================================================================
# Split-screen review
# =============================================================================
@st.cache_data(show_spinner=False, max_entries=64)
def _page_png(path: str, mtime: float, page: int, zoom: float) -> bytes:
    return render_page_png(Path(path).read_bytes(), page, zoom=zoom)


@st.cache_data(show_spinner=False, max_entries=256)
def _pages(path: str, mtime: float) -> int:
    return page_count(Path(path).read_bytes())


def pdf_preview(row: StagingRow) -> None:
    path = Path(row.file_path) if row.file_path else None
    if path is None or not path.is_file():
        st.info("No PDF for this row — it was keyed in manually." if row.is_manual
                else f"Original PDF not found at {row.file_path or '(unknown)'}.")
        return
    mtime = path.stat().st_mtime
    pages = _pages(str(path), mtime)
    c1, c2 = st.columns([1, 2])
    page = c1.number_input(f"Page (of {pages})", 1, pages, 1, key=f"page_{row.row_id}") if pages > 1 else 1
    zoom = c2.select_slider("Zoom", [1.0, 1.5, 2.0, 2.5], value=1.5, key=f"zoom_{row.row_id}")
    with st.container(border=True):
        st.image(_page_png(str(path), mtime, int(page), float(zoom)), width="stretch")
    st.caption(f"📄 {row.source_file}")
    with st.expander("Extracted text"):
        st.code(row.raw_text or "(no text layer)", language=None)


def issues_panel(row: StagingRow) -> None:
    method = row.entity_match_method or "none"
    score = f" {row.entity_match_score:.0f}%" if row.entity_match_score is not None else ""
    st.markdown(f"**Confidence:** {band_badge(row)} · **Match:** {method}{score} · "
                f"**Source:** {row.extraction_method}"
                + (f" ({row.extraction_quality:.0%} clarity)" if row.extraction_quality is not None else ""))
    for issue in row.issues:
        if issue.severity == Severity.INFO:
            continue
        text = f"{SEVERITY_ICON[str(issue.severity)]} {issue.message}"
        (st.error if issue.severity == Severity.ERROR else st.warning)(text)


def duplicates_panel(rows: list[StagingRow], row: StagingRow) -> None:
    for flag in row.duplicate_flags:
        if flag.key in row.dismissed_flags:
            st.caption(f"✓ Dismissed — {flag.tier_name}: {flag.message}")
            continue
        with st.container(border=True):
            st.markdown(f"**⚠ Possible duplicate — {flag.tier_name}**  \n{flag.message}")
            c1, c2 = st.columns(2)
            if c1.button("Dismiss flag", key=f"dismiss_{row.row_id}_{flag.key}", width="stretch"):
                dismiss_flags(rows, row.group_id, [flag.key])
                save_rows(rows)
                flash("Duplicate flag dismissed.")
                st.rerun()
            if c2.button("Remove document", key=f"remove_dup_{row.row_id}_{flag.key}", type="primary",
                         width="stretch"):
                _remove(rows, row.group_id)


def _remove(rows: list[StagingRow], group_id: str) -> None:
    removed = remove_group(rows, group_id)
    get_store().discard_unreferenced(removed, rows)
    save_rows(rows)
    _select(None)
    flash(f"Removed {group_label(removed)}.")
    st.rerun()


def _label(text: str, fld: str, row: StagingRow) -> str:
    if fld in row.error_fields:
        return f"🔴 {text}"
    if row.field_scores.get(fld, 100) < 60:
        return f"🟠 {text}"
    return text


def edit_form(rows: list[StagingRow], row: StagingRow) -> None:
    db = get_db()
    entity_type = row.doc_type.entity_type
    accounts = entity_options(entity_type)
    account_list = list(accounts)
    if row.account_code and row.account_code not in accounts:
        account_list.insert(0, row.account_code)
    vat_opts = vat_code_options()
    vat_list = list(vat_opts)
    if row.vat_code and row.vat_code not in vat_opts:
        vat_list.insert(0, row.vat_code)
    currencies = CURRENCIES if row.currency in CURRENCIES else [row.currency, *CURRENCIES]

    with st.form(f"edit_{row.row_id}", border=True):
        c1, c2 = st.columns(2)
        doc_type = c1.selectbox(_label("Type", F.TYPE, row), DOC_TYPES, index=DOC_TYPES.index(row.doc_type.value),
                                format_func=lambda t: f"{t} · {DocType(t).label}")
        account = c2.selectbox(_label(f"{entity_type} account", F.ACCOUNT_CODE, row), account_list,
                               index=select_index(account_list, row.account_code),
                               format_func=lambda c: accounts.get(c, c), accept_new_options=True,
                               placeholder="Choose or type a code")
        company = st.text_input("Name on document", row.company_name)
        c1, c2 = st.columns(2)
        invoice_number = c1.text_input(_label("Invoice number", F.INVOICE_NUMBER, row), row.invoice_number)
        invoice_date = c2.date_input(_label("Invoice date", F.INVOICE_DATE, row), row.invoice_date,
                                     format="DD/MM/YYYY")
        c1, c2 = st.columns(2)
        svc_start = c1.date_input("Service period from", row.service_period_start, format="DD/MM/YYYY")
        svc_end = c2.date_input("Service period to", row.service_period_end, format="DD/MM/YYYY")
        c1, c2 = st.columns(2)
        nominal = c1.text_input(_label("Nominal code", F.NOMINAL_CODE, row), row.nominal_code)
        vat_code = c2.selectbox(_label("VAT code", F.VAT_CODE, row), vat_list,
                                index=select_index(vat_list, row.vat_code),
                                format_func=lambda c: vat_opts.get(c, c), placeholder="Choose a VAT code")
        c1, c2, c3 = st.columns(3)
        net = c1.number_input(_label("Net", F.NET, row), value=to_float(row.net), step=0.01, format="%.2f")
        vat = c2.number_input(_label("VAT", F.VAT, row), value=to_float(row.vat), step=0.01, format="%.2f")
        gross = c3.number_input(_label("Gross", F.GROSS, row), value=to_float(row.gross), step=0.01, format="%.2f")
        c1, c2 = st.columns(2)
        currency = c1.selectbox(_label("Currency", F.CURRENCY, row), currencies, index=currencies.index(row.currency))
        rate = c2.number_input(_label("Exchange rate (per 1 base)", F.EXCHANGE_RATE, row),
                               value=to_float(row.exchange_rate), step=0.0001, format="%.6f")
        description = st.text_input(_label("Description", F.DESCRIPTION, row), row.description)
        raw_description = st.text_input("Raw description (from document)", row.raw_description)
        submitted = st.form_submit_button("Save changes", type="primary", width="stretch")

    if submitted:
        changes = {
            "doc_type": doc_type, "account_code": account or "", "company_name": company,
            "invoice_number": invoice_number, "invoice_date": invoice_date,
            "service_period_start": svc_start, "service_period_end": svc_end,
            "nominal_code": nominal, "vat_code": vat_code or "", "net": net, "vat": vat, "gross": gross,
            "currency": currency, "exchange_rate": rate, "description": description,
            "raw_description": raw_description,
        }
        before = {k: getattr(row, k) for k in ("account_code", "nominal_code", "vat_code")}
        changed = apply_changes(row, changes, resolver=db.entity_resolver())
        if "raw_description" in changed and "description" not in changed:
            row.description = build_description(row, description_rules())
        if changed.keys() & {"account_code", "nominal_code", "vat_code", "doc_type"}:
            for line in group_rows(rows, row.group_id):
                if line is not row and "account_code" in changed:
                    apply_changes(line, {"account_code": row.account_code}, resolver=db.entity_resolver())
        rememberable = {k: v for k, v in before.items() if k in changed and getattr(row, k)}
        if rememberable and row.account_code:
            st.session_state["memory_prompt"] = {"row_id": row.row_id, "fields": list(rememberable)}
        save_rows(rows)
        flash(f"Saved {len(changed)} change(s)." if changed else "No changes.", "success" if changed else "info")
        st.rerun()


LABELS = {"account_code": "Account Code", "nominal_code": "Nominal Code", "vat_code": "VAT Code"}


def memory_prompt(rows: list[StagingRow], row: StagingRow) -> None:
    prompt = st.session_state.get("memory_prompt")
    if not prompt or prompt["row_id"] != row.row_id:
        return
    who = row.entity_name or row.company_name or row.account_code
    with st.container(border=True):
        st.markdown(f"💡 **Save as default for {who}?** Future documents from this "
                    f"{row.doc_type.entity_type.lower()} will be filled in automatically.")
        chosen = []
        for fld in prompt["fields"]:
            value = getattr(row, fld)
            text = (f"Recognise “{row.company_name}” as account {value}" if fld == "account_code"
                    else f"Default {LABELS[fld]} → {value}")
            if st.checkbox(text, value=True, key=f"mem_{row.row_id}_{fld}"):
                chosen.append(LABELS[fld])
        c1, c2 = st.columns(2)
        if c1.button("Save as default", type="primary", key=f"mem_save_{row.row_id}", disabled=not chosen,
                     width="stretch"):
            try:
                entity = get_db().remember_from_row(row, chosen)
            except ValueError as exc:
                st.error(str(exc))
                return
            st.session_state.pop("memory_prompt", None)
            flash(f"Saved defaults for {entity.company_name} ({entity.account_code}).")
            st.rerun()
        if c2.button("Not now", key=f"mem_skip_{row.row_id}", width="stretch"):
            st.session_state.pop("memory_prompt", None)
            st.rerun()


def row_actions(rows: list[StagingRow], row: StagingRow) -> None:
    group = group_rows(rows, row.group_id)
    base = get_db().get_setting("base_currency")
    cols = st.columns(4)
    if cols[0].button("✂ Split invoice", key=f"split_{row.row_id}", width="stretch",
                      help="Spread one invoice across several nominal / VAT codes"):
        split_dialog(row.group_id)
    if len(group) > 1 and cols[1].button("⇲ Merge lines", key=f"merge_{row.row_id}", width="stretch"):
        merged = unsplit_document(rows, row.group_id)
        save_rows(rows)
        _select(merged.row_id)
        flash("Lines merged back into one.")
        st.rerun()
    if row.currency != base and cols[2].button("💱 Fetch FX rate", key=f"fx_{row.row_id}", width="stretch"):
        fx = FxService.from_db(get_db())
        quote = fx.apply_to_row(row, overwrite=True)
        save_rows(rows)
        flash(quote.describe() if quote else f"No rate found: {fx.last_error or 'offline'}",
              "success" if quote else "warning")
        st.rerun()
    if cols[3].button("🗑 Remove", key=f"remove_{row.row_id}", width="stretch",
                      help="Remove this whole document from the batch"):
        _remove(rows, row.group_id)


def review_panel(rows: list[StagingRow], row: StagingRow) -> None:
    group = group_rows(rows, row.group_id)
    line = f" · line {row.line_no} of {len(group)}" if len(group) > 1 else ""
    head, close = st.columns([6, 1])
    head.subheader(f"{BAND_ICON.get(row.confidence_band, '')} {row.doc_type.label} "
                   f"{row.invoice_number or '(no number)'} — {row.display_name or row.source_file}{line}")
    if close.button("Close ✕", key="close_review", width="stretch"):
        _select(None)
        st.rerun()
    left, right = st.columns([1, 1], gap="large")
    with left:
        pdf_preview(row)
    with right:
        issues_panel(row)
        duplicates_panel(rows, row)
        memory_prompt(rows, row)
        edit_form(rows, row)
        row_actions(rows, row)


# =============================================================================
# Export
# =============================================================================
def export_section(rows: list[StagingRow]) -> None:
    db = get_db()
    st.subheader("Export")
    last = st.session_state.get("last_export")
    if last:
        with st.container(border=True):
            st.success(f"Exported {last['documents']} document(s) with “{last['template']}”. "
                       f"{last['archived']} PDF(s) archived.")
            c1, c2 = st.columns([1, 1])
            c1.download_button(f"⬇ Download {last['filename']}", last["data"], file_name=last["filename"],
                               mime=last["mime"], type="primary", width="stretch")
            if c2.button("Done", width="stretch"):
                st.session_state.pop("last_export")
                st.rerun()
            if last["saved_path"]:
                st.caption(f"A copy was saved to {last['saved_path']}")
            for w in last["warnings"]:
                st.warning(w)
    if not rows:
        return

    templates = db.list_templates()
    names = [t.name for t in templates]
    active = db.get_setting("active_template")
    c1, c2 = st.columns([2, 1])
    name = c1.selectbox("Template", names, index=names.index(active) if active in names else 0)
    template = templates[names.index(name)]
    options = ExportOptions.from_db(db, template)
    plan = plan_export(rows, db, options)
    ready_docs = len({r.group_id for r in plan.exportable})
    c2.metric("Ready", f"{ready_docs} of {plan.documents}")
    if options.archive_enabled:
        st.caption(f"📁 PDFs will be archived to `{options.archive_dir}` › `{options.subfolder_pattern}` "
                   f"as `{options.rename_pattern}` ({options.archive_mode}).")
    else:
        st.caption("📁 PDF archiving is off (Settings › Archive).")
    by_group = {r.group_id: r for r in rows}
    if plan.blocked:
        with st.expander(f"🚫 {len(plan.blocked)} document(s) blocked — fix or remove them first"):
            for gid, reasons in plan.blocked.items():
                st.markdown(f"**{group_label(group_rows(rows, gid))}**")
                for reason in reasons:
                    st.caption(f"• {reason}")
                if st.button("Review", key=f"goto_{gid}"):
                    _select(by_group[gid].row_id)
                    st.rerun()
    if plan.excluded:
        with st.expander(f"⏭ {len(plan.excluded)} document(s) not supported by this template"):
            for gid, reason in plan.excluded.items():
                st.caption(f"• {group_label(group_rows(rows, gid))}: {reason}")
    if st.button(f"Export {ready_docs} document(s)", type="primary", disabled=not plan.exportable):
        try:
            result = export_batch(rows, db, options)
        except ExportBlocked as exc:
            st.error(str(exc))
            return
        remaining = [r for r in rows if r.group_id not in result.exported_groups]
        save_rows(remaining)
        db.set_setting("active_template", template.name)
        st.session_state["last_export"] = {
            "documents": len(result.exported_groups), "template": template.name, "filename": result.filename,
            "data": result.data, "mime": result.mime_type, "archived": len(result.archived),
            "saved_path": result.saved_path, "warnings": result.warnings}
        _select(None)
        st.rerun()


# =============================================================================
# Page
# =============================================================================
def main() -> None:
    page_header("Upload & Review", "Drop PDFs, check each one beside the original, then export an upload sheet.")
    db = get_db()
    rows = get_rows()
    vs = ValidationSettings.from_settings(db.get_settings())
    if rows:
        # Re-check on every render: history, master list or settings may have changed elsewhere.
        validate_batch(rows, db)
    ingest_section(rows)

    if rows:
        summary(rows, vs)
        top = st.columns([3, 1, 1])
        with top[0]:
            visible = filter_rows(rows, vs)
        if top[1].button("↻ Re-check", width="stretch", help="Re-run matching defaults and validation"):
            resolver = db.entity_resolver()
            for r in rows:
                if not r.account_code:
                    resolver.apply(r)
            save_rows(rows)
            st.rerun()
        if top[2].button("✎ Rebuild descriptions", width="stretch"):
            n = rebuild_descriptions(rows, description_rules())
            save_rows(rows)
            flash(f"Updated {n} description(s).")
            st.rerun()
        if visible:
            selected_id = grid(visible)
        else:
            st.info("Nothing matches this filter.")
            selected_id = st.session_state.get("selected_row_id")
        row = find_row(rows, selected_id) if selected_id else None
        if row is None:
            st.caption("👆 Select a row to review it side by side with the original PDF.")
        else:
            review_panel(rows, row)
        st.divider()
        cols = st.columns([1, 5])
        if cols[0].button("＋ Manual row", width="stretch"):
            manual_dialog()
    export_section(rows)


main()
