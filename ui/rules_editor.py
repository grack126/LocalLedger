"""Description Rules — token templates for the Description column, with live preview."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from localledger.rules import (
    DESCRIPTION_TOKENS,
    SAMPLE_ROW,
    DescriptionRule,
    description_context,
    render_description,
    select_rule,
    validate_description_template,
)
from localledger.staging import rebuild_descriptions
from ui.common import description_rules, flash, get_db, get_rows, page_header, save_rows

page_header("Description Rules", "Build the Description column from tokens. The most specific matching rule wins "
                                 "(account-specific › type-specific › general).")
db = get_db()
rules = description_rules()

with st.expander("Available tokens", expanded=False):
    st.dataframe(pd.DataFrame([{"Token": "{" + k + "}", "Meaning": v} for k, v in DESCRIPTION_TOKENS.items()]),
                 hide_index=True, width="stretch")

frame = pd.DataFrame([{
    "enabled": r.enabled, "name": r.name, "prefix": r.prefix, "template": r.template, "suffix": r.suffix,
    "doc_types": ", ".join(r.doc_types), "account_codes": ", ".join(r.account_codes),
    "max_length": r.max_length, "case": r.case} for r in rules],
    columns=["enabled", "name", "prefix", "template", "suffix", "doc_types", "account_codes", "max_length", "case"])
edited = st.data_editor(
    frame, num_rows="dynamic", width="stretch", hide_index=True, key="rules_editor",
    column_config={
        "enabled": st.column_config.CheckboxColumn("On", default=True, width="small"),
        "name": st.column_config.TextColumn("Rule", required=True),
        "prefix": st.column_config.TextColumn("Prefix", help="e.g. {Service_Month} - "),
        "template": st.column_config.TextColumn("Body", default="{Raw_Description}", required=True),
        "suffix": st.column_config.TextColumn("Suffix", help="e.g. . {Company_Name}"),
        "doc_types": st.column_config.TextColumn("Only types", help="Comma list, e.g. PI, PC. Blank = all"),
        "account_codes": st.column_config.TextColumn("Only accounts", help="Comma list. Blank = all"),
        "max_length": st.column_config.NumberColumn("Max length", min_value=10, step=1, help="Sage 50: 60"),
        "case": st.column_config.SelectboxColumn("Case", options=["none", "upper", "lower", "title", "sentence"],
                                                 default="none"),
    })


def split_list(value) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    return [v.strip().upper() for v in str(value).split(",") if v.strip()]


new_rules: list[DescriptionRule] = []
problems: list[str] = []
for rec in edited.to_dict("records"):
    if not str(rec.get("name") or "").strip():
        continue
    rule = DescriptionRule.from_dict({
        "name": rec["name"], "prefix": rec.get("prefix") or "", "template": rec.get("template") or "{Raw_Description}",
        "suffix": rec.get("suffix") or "", "doc_types": split_list(rec.get("doc_types")),
        "account_codes": split_list(rec.get("account_codes")),
        "max_length": None if pd.isna(rec.get("max_length")) else int(rec["max_length"]),
        "case": rec.get("case") or "none", "enabled": bool(rec.get("enabled", True))})
    bad_types = [t for t in rule.doc_types if t not in ("PI", "PC", "SI", "SC")]
    if bad_types:
        problems.append(f"{rule.name}: unknown type(s) {', '.join(bad_types)}")
    for part in (rule.prefix, rule.template, rule.suffix):
        problems += [f"{rule.name}: {p}" for p in validate_description_template(part)]
    new_rules.append(rule)

for p in problems:
    st.warning(p)

st.subheader("Live preview")
rows = get_rows()
choices = {"sample": "Sample invoice (ACME rent)"} | {
    r.row_id: f"{r.doc_type.value} {r.invoice_number or '—'} · {r.display_name or r.source_file}" for r in rows}
pick = st.selectbox("Preview with", list(choices), format_func=choices.get)
row = SAMPLE_ROW if pick == "sample" else next(r for r in rows if r.row_id == pick)
context = description_context(row)
winner = select_rule(row, new_rules)
c1, c2 = st.columns(2)
with c1:
    st.markdown("**Input**")
    st.json({k: v for k, v in context.items() if v}, expanded=True)
with c2:
    st.markdown("**Output per rule**")
    for rule in new_rules:
        mark = "✅ " if rule is winner else ("⏸ " if not rule.enabled else "▫️ ")
        text = render_description(rule, context) if rule.matches(row) or not rule.enabled else "(does not apply)"
        st.markdown(f"{mark}**{rule.name}** → `{text}`" + (f"  · {len(text)} chars" if text else ""))
    if winner is None:
        st.caption("No rule applies — the raw description is used as is.")

c1, c2, _ = st.columns([1, 1, 2])
if c1.button("Save rules", type="primary", disabled=bool(problems), width="stretch"):
    db.set_setting("description_rules", [r.to_dict() for r in new_rules])
    flash("Description rules saved.")
    st.rerun()
if rows and c2.button(f"Apply to {len(rows)} staging row(s)", disabled=bool(problems), width="stretch"):
    db.set_setting("description_rules", [r.to_dict() for r in new_rules])
    n = rebuild_descriptions(rows, new_rules)
    save_rows(rows)
    flash(f"Rules saved; {n} description(s) updated.")
    st.rerun()
