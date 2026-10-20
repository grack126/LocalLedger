"""Analytics & Performance — monthly dashboard over the exported history."""
from __future__ import annotations

import streamlit as st

from localledger import analytics as an
from ui.common import get_db, is_dark, page_header

page_header("Analytics & Performance", "Built from every exported document. Credits reduce the totals; "
                                       "foreign currency is converted at the invoice's exchange rate.")
db = get_db()
settings = db.get_settings()
currency = settings["base_currency"]
dark = is_dark()

c1, c2 = st.columns([1, 2])
basis_label = c1.segmented_control("Group months by", ["Invoice date", "Service period"], default="Invoice date",
                                   help="Service period = the month the goods/services relate to (accrual view).")
basis = "service" if basis_label == "Service period" else "invoice"
full = an.load_ledger(db, basis)

if full.empty:
    st.info("No exported documents yet. Export a batch from **Upload & Review** and the dashboard fills in here.")
    st.stop()

months_all = an.available_months(full)
if len(months_all) > 1:
    start, end = c2.select_slider("Months", options=months_all, value=(months_all[max(0, len(months_all) - 12)],
                                                                         months_all[-1]),
                                  format_func=an.ym_label)
else:
    start = end = months_all[0]
    c2.caption(f"Showing {an.ym_label(start)}")
months = an.month_range(start, end)
df = an.filter_months(full, start, end)
symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency, "")


def money(v: float | None) -> str:
    return "—" if v is None else f"{'-' if v < 0 else ''}{symbol}{abs(v):,.0f}"


# ---------------------------------------------------------------- KPIs
perf = an.monthly_performance(df, months)
kpi = an.headline(perf)
st.subheader(f"{an.ym_label(kpi['month'])} at a glance")
k1, k2, k3, k4 = st.columns(4)
k1.metric("Revenue", money(kpi["revenue"]), None if kpi["revenue_delta"] is None else money(kpi["revenue_delta"]))
k2.metric("Costs", money(kpi["costs"]), None if kpi["costs_delta"] is None else money(kpi["costs_delta"]),
          delta_color="inverse")
k3.metric("Net margin", money(kpi["margin"]), None if kpi["margin_delta"] is None else money(kpi["margin_delta"]))
vat_monthly, vat_codes = an.vat_summary(df, months)
last_vat = float(vat_monthly.iloc[-1]["net_payable"])
k4.metric("VAT position", money(abs(last_vat)), "payable" if last_vat > 0 else ("reclaimable" if last_vat < 0 else "nil"),
          delta_color="off")

tab_perf, tab_nominal, tab_alerts, tab_vat, tab_top = st.tabs(
    ["📈 Monthly performance", "🗂 Nominal breakdown", "🚨 Run-rate alerts", "🧾 VAT summary", "🏆 Top suppliers & clients"])

with tab_perf:
    st.plotly_chart(an.fig_performance(perf, currency=currency, dark=dark), width="stretch", theme="streamlit")
    table = perf.assign(month=perf["month"].map(an.ym_label))
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        "month": "Month",
        "revenue": st.column_config.NumberColumn("Revenue", format="%.2f"),
        "costs": st.column_config.NumberColumn("Costs", format="%.2f"),
        "margin": st.column_config.NumberColumn("Net margin", format="%.2f"),
        "margin_pct": st.column_config.NumberColumn("Margin %", format="%.1f%%"),
        "sales_docs": "Sales docs", "purchase_docs": "Purchase docs"})

with tab_nominal:
    ledger = st.segmented_control("Ledger", ["Purchase", "Sales"], default="Purchase", key="nominal_ledger",
                                  format_func=lambda v: "Costs (purchases)" if v == "Purchase" else "Revenue (sales)")
    order = an.category_order(full, "nominal_code", ledger)        # all-time order keeps colours stable
    st.plotly_chart(an.fig_stacked_by_category(df, by="nominal_code", ledger=ledger, order=order, months=months,
                                               currency=currency, dark=dark), width="stretch", theme="streamlit")
    variance = an.variance_table(df, "nominal_code", ledger, months)
    if len(months) > 1:
        st.caption(f"Month-on-month: {an.ym_label(months[-1])} vs {an.ym_label(months[-2])}")
    cfg = {m: st.column_config.NumberColumn(an.ym_label(m), format="%.2f") for m in months}
    cfg.update({"nominal_code": "Nominal",
                "current": st.column_config.NumberColumn("This month", format="%.2f"),
                "previous": st.column_config.NumberColumn("Last month", format="%.2f"),
                "change": st.column_config.NumberColumn("Change", format="%+.2f"),
                "change_pct": st.column_config.NumberColumn("Change %", format="%+.1f%%")})
    st.dataframe(variance, hide_index=True, width="stretch", column_config=cfg)

with tab_alerts:
    cfg = an.AlertConfig(threshold_pct=float(settings["anomaly_threshold_pct"]),
                         lookback=int(settings["anomaly_lookback_months"]))
    st.caption(f"Flags anything whose {an.ym_label(end)} spend is more than {cfg.threshold_pct:.0f}% above its "
               f"average of the previous {cfg.lookback} months (months without spend count as zero).")
    alert_cols = {
        "name": "Name", "month": None,
        "current": st.column_config.NumberColumn("This month", format="%.2f"),
        "average": st.column_config.NumberColumn(f"{cfg.lookback}-month avg", format="%.2f"),
        "increase_pct": st.column_config.NumberColumn("Above avg", format="%+.0f%%"),
        "status": "Status"}
    a1, a2 = st.columns(2)
    with a1:
        st.markdown("**Suppliers**")
        sup = an.run_rate_alerts(full, by="account_code", month=end, config=cfg)
        if sup.empty:
            st.success("No supplier is running above its normal rate.")
        else:
            st.dataframe(sup.assign(status=sup["status"].map(lambda s: f"⚠ {s}")), hide_index=True,
                         width="stretch", column_config={"account_code": "Account", **alert_cols})
    with a2:
        st.markdown("**Nominal codes**")
        nom = an.run_rate_alerts(full, by="nominal_code", month=end, config=cfg)
        if nom.empty:
            st.success("No nominal code is running above its normal rate.")
        else:
            st.dataframe(nom.drop(columns=["name"]).assign(status=nom["status"].map(lambda s: f"⚠ {s}")),
                         hide_index=True, width="stretch", column_config={"nominal_code": "Nominal", **alert_cols})

with tab_vat:
    st.plotly_chart(an.fig_vat(vat_monthly, currency=currency, dark=dark), width="stretch", theme="streamlit")
    st.dataframe(vat_monthly.assign(month=vat_monthly["month"].map(an.ym_label)), hide_index=True, width="stretch",
                 column_config={"month": "Month",
                                "output_vat": st.column_config.NumberColumn("Output VAT", format="%.2f"),
                                "input_vat": st.column_config.NumberColumn("Input VAT", format="%.2f"),
                                "net_payable": st.column_config.NumberColumn("Net payable", format="%.2f"),
                                "position": "Position"})
    pick = st.selectbox("Breakdown by VAT code for", months[::-1], format_func=an.ym_label)
    st.dataframe(vat_codes[vat_codes["month"] == pick].drop(columns=["month"]), hide_index=True, width="stretch",
                 column_config={"vat_code": "VAT code",
                                "output_vat": st.column_config.NumberColumn("Output VAT", format="%.2f"),
                                "input_vat": st.column_config.NumberColumn("Input VAT", format="%.2f"),
                                "output_net": st.column_config.NumberColumn("Sales net", format="%.2f"),
                                "input_net": st.column_config.NumberColumn("Purchases net", format="%.2f")})

with tab_top:
    n = st.slider("How many", 5, 25, 10)
    t1, t2 = st.columns(2)
    for col, ledger, title in ((t1, "Purchase", "Top suppliers"), (t2, "Sales", "Top clients")):
        with col:
            st.markdown(f"**{title}** (gross, {an.ym_label(start)} – {an.ym_label(end)})")
            top = an.top_entities(df, ledger, n)
            if top.empty:
                st.caption("No documents in this period.")
                continue
            st.plotly_chart(an.fig_top_entities(top, currency=currency, dark=dark), width="stretch",
                            theme="streamlit", key=f"top_{ledger}")
            st.dataframe(top, hide_index=True, width="stretch", column_config={
                "account_code": "Account", "company_name": "Name", "documents": "Docs",
                "net": st.column_config.NumberColumn("Net", format="%.2f"),
                "gross": st.column_config.NumberColumn("Gross", format="%.2f"),
                "share_pct": st.column_config.NumberColumn("Share", format="%.1f%%")})
