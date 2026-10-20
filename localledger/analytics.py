"""Monthly performance analytics over the exported history.

All metrics are computed from ``v_ledger_lines`` (signed, base-currency net /
VAT / gross per posting line, credits negative). The dashboard can group by
**invoice date** or by **service period** (accrual month).

Charts follow one-axis-per-chart and a fixed categorical colour order
(blue, orange, aqua, ...) so a series keeps its colour across views.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Sequence

import pandas as pd

from .models import MONTH_ABBR

if TYPE_CHECKING:
    from .db import Database

SALES, PURCHASE = "Sales", "Purchase"
OTHER = "Other"

# Reference categorical palette (validated set, fixed order). Light / dark steps.
PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
NEUTRAL_LIGHT, NEUTRAL_DARK = "#8a8985", "#6f6e69"     # "Other" bucket


def palette(dark: bool = False) -> list[str]:
    return PALETTE_DARK if dark else PALETTE_LIGHT


# =============================================================================
# Months
# =============================================================================
def ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def ym_label(value: str) -> str:
    """'2026-10' -> 'Oct 2026'."""
    try:
        year, month = value.split("-")
        return f"{MONTH_ABBR[int(month) - 1]} {year}"
    except (ValueError, IndexError, AttributeError):
        return str(value)


def month_range(start: str, end: str) -> list[str]:
    if not start or not end or start > end:
        return []
    return [p.strftime("%Y-%m") for p in pd.period_range(start, end, freq="M")]


def shift_month(value: str, months: int) -> str:
    return (pd.Period(value, freq="M") + months).strftime("%Y-%m")


# =============================================================================
# Loading
# =============================================================================
LEDGER_COLUMNS = ["log_id", "ledger", "doc_type", "account_code", "company_name", "invoice_date",
                  "invoice_month", "service_month", "nominal_code", "vat_code", "net", "vat", "gross"]


def prepare(frame: pd.DataFrame, basis: str = "invoice") -> pd.DataFrame:
    """Add the ``month`` column for the chosen basis ('invoice' | 'service')."""
    df = frame.copy() if not frame.empty else pd.DataFrame(columns=LEDGER_COLUMNS)
    df["month"] = df["service_month"] if basis == "service" else df["invoice_month"]
    df["month"] = df["month"].fillna(df["invoice_month"])
    df["nominal_code"] = df["nominal_code"].fillna("(none)")
    df["vat_code"] = df["vat_code"].fillna("(none)")
    df["company_name"] = df["company_name"].fillna(df["account_code"])
    for col in ("net", "vat", "gross"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


def load_ledger(db: Database, basis: str = "invoice") -> pd.DataFrame:
    return prepare(db.ledger_lines_frame(), basis)


def filter_months(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    if start:
        mask &= df["month"] >= start
    if end:
        mask &= df["month"] <= end
    return df[mask]


def available_months(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    return month_range(df["month"].min(), df["month"].max())


# =============================================================================
# Metrics
# =============================================================================
def monthly_performance(df: pd.DataFrame, months: Sequence[str] | None = None) -> pd.DataFrame:
    """Revenue (sales net), costs (purchase net), margin per month."""
    months = list(months) if months is not None else available_months(df)
    pivot = df.pivot_table(index="month", columns="ledger", values="net", aggfunc="sum", fill_value=0.0)
    out = pd.DataFrame(index=pd.Index(months, name="month"))
    out["revenue"] = pivot[SALES] if SALES in pivot else 0.0
    out["costs"] = pivot[PURCHASE] if PURCHASE in pivot else 0.0
    out = out.fillna(0.0)
    out["margin"] = out["revenue"] - out["costs"]
    out["margin_pct"] = (out["margin"] / out["revenue"].where(out["revenue"] != 0) * 100).round(1)
    docs = df.groupby(["month", "ledger"])["log_id"].nunique().unstack(fill_value=0)
    out["sales_docs"] = docs[SALES].reindex(out.index).fillna(0).astype(int) if SALES in docs else 0
    out["purchase_docs"] = docs[PURCHASE].reindex(out.index).fillna(0).astype(int) if PURCHASE in docs else 0
    return out.reset_index()


def variance_table(df: pd.DataFrame, by: str = "nominal_code", ledger: str = PURCHASE,
                   months: Sequence[str] | None = None) -> pd.DataFrame:
    """Net by ``by`` x month, plus month-on-month change for the last two months."""
    sub = df[df["ledger"] == ledger]
    months = list(months) if months is not None else available_months(df)
    if sub.empty or not months:
        return pd.DataFrame(columns=[by, *months, "current", "previous", "change", "change_pct"])
    pivot = sub.pivot_table(index=by, columns="month", values="net", aggfunc="sum", fill_value=0.0)
    pivot = pivot.reindex(columns=months, fill_value=0.0)
    current = pivot[months[-1]]
    previous = pivot[months[-2]] if len(months) > 1 else pd.Series(0.0, index=pivot.index)
    pivot["current"], pivot["previous"] = current, previous
    pivot["change"] = current - previous
    pivot["change_pct"] = (pivot["change"] / previous.where(previous != 0).abs() * 100).round(1)
    pivot = pivot.sort_values("current", ascending=False, key=abs)
    return pivot.reset_index()


def category_order(df: pd.DataFrame, by: str = "nominal_code", ledger: str = PURCHASE, limit: int = 7) -> list[str]:
    """Stable colour order: categories ranked by all-time absolute net.
    Pass the *unfiltered* ledger so filters never repaint a category."""
    sub = df[df["ledger"] == ledger]
    if sub.empty:
        return []
    totals = sub.groupby(by)["net"].sum().abs().sort_values(ascending=False)
    return list(totals.index[:limit])


@dataclass
class AlertConfig:
    threshold_pct: float = 25.0
    lookback: int = 6
    min_amount: float = 0.0       # ignore tiny totals


def run_rate_alerts(df: pd.DataFrame, *, by: str = "account_code", ledger: str = PURCHASE,
                    month: str | None = None, config: AlertConfig | None = None) -> pd.DataFrame:
    """Flag ``by`` keys whose current-month net exceeds their trailing average
    of the previous ``lookback`` months by more than ``threshold_pct`` (months
    with no spend count as zero). Keys with spend now but none before are 'New'."""
    cfg = config or AlertConfig()
    cols = [by, "name", "month", "current", "average", "increase_pct", "status"]
    sub = df[df["ledger"] == ledger]
    if sub.empty:
        return pd.DataFrame(columns=cols)
    month = month or sub["month"].max()
    prior = [shift_month(month, -i) for i in range(cfg.lookback, 0, -1)]
    pivot = sub.pivot_table(index=by, columns="month", values="net", aggfunc="sum", fill_value=0.0)
    pivot = pivot.reindex(columns=prior + [month], fill_value=0.0)
    current = pivot[month]
    average = pivot[prior].mean(axis=1)
    names = sub.groupby(by)["company_name"].agg(lambda s: s.dropna().iloc[-1] if s.notna().any() else "")
    rows = []
    for key in pivot.index:
        cur, avg = float(current[key]), float(average[key])
        if cur <= cfg.min_amount:
            continue
        if avg > 0 and cur > avg * (1 + cfg.threshold_pct / 100):
            rows.append([key, names.get(key, "") if by == "account_code" else "", month, cur, avg,
                         round((cur / avg - 1) * 100, 1), "Above run-rate"])
        elif avg == 0 and pivot.loc[key, prior].abs().sum() == 0:
            rows.append([key, names.get(key, "") if by == "account_code" else "", month, cur, 0.0, None, "New"])
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values(["status", "increase_pct"], ascending=[True, False], na_position="last") \
        .reset_index(drop=True)


def vat_summary(df: pd.DataFrame, months: Sequence[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(monthly totals, breakdown by month x VAT code).

    Output VAT = VAT on sales; input VAT = VAT on purchases; net payable =
    output - input (negative means reclaimable). Credits are already negative.
    """
    months = list(months) if months is not None else available_months(df)
    by_ledger = df.pivot_table(index="month", columns="ledger", values="vat", aggfunc="sum", fill_value=0.0)
    monthly = pd.DataFrame(index=pd.Index(months, name="month"))
    monthly["output_vat"] = by_ledger[SALES] if SALES in by_ledger else 0.0
    monthly["input_vat"] = by_ledger[PURCHASE] if PURCHASE in by_ledger else 0.0
    monthly = monthly.fillna(0.0)
    monthly["net_payable"] = monthly["output_vat"] - monthly["input_vat"]
    monthly["position"] = monthly["net_payable"].map(lambda v: "Payable" if v > 0 else ("Reclaimable" if v < 0 else "Nil"))

    if df.empty:
        breakdown = pd.DataFrame(columns=["month", "vat_code", "output_vat", "input_vat", "output_net", "input_net"])
    else:
        g = df.groupby(["month", "vat_code", "ledger"])[["vat", "net"]].sum().unstack("ledger", fill_value=0.0)
        breakdown = pd.DataFrame({
            "output_vat": g["vat"][SALES] if SALES in g["vat"] else 0.0,
            "input_vat": g["vat"][PURCHASE] if PURCHASE in g["vat"] else 0.0,
            "output_net": g["net"][SALES] if SALES in g["net"] else 0.0,
            "input_net": g["net"][PURCHASE] if PURCHASE in g["net"] else 0.0,
        }).reset_index()
    return monthly.reset_index(), breakdown


def top_entities(df: pd.DataFrame, ledger: str = PURCHASE, n: int = 10, by: str = "gross") -> pd.DataFrame:
    sub = df[df["ledger"] == ledger]
    cols = ["account_code", "company_name", "documents", "net", "gross", "share_pct"]
    if sub.empty:
        return pd.DataFrame(columns=cols)
    g = sub.groupby("account_code").agg(
        company_name=("company_name", lambda s: s.dropna().iloc[-1] if s.notna().any() else ""),
        documents=("log_id", "nunique"), net=("net", "sum"), gross=("gross", "sum"))
    total = g[by].sum()
    g["share_pct"] = (g[by] / total * 100).round(1) if total else 0.0
    return g.sort_values(by, ascending=False).head(n).reset_index()[cols]


def headline(perf: pd.DataFrame) -> dict[str, float | None]:
    """Latest-month KPIs vs the month before."""
    if perf.empty:
        return {}
    last = perf.iloc[-1]
    prev = perf.iloc[-2] if len(perf) > 1 else None

    def delta(col: str) -> float | None:
        return None if prev is None else float(last[col] - prev[col])

    return {"month": last["month"], "revenue": float(last["revenue"]), "costs": float(last["costs"]),
            "margin": float(last["margin"]), "revenue_delta": delta("revenue"),
            "costs_delta": delta("costs"), "margin_delta": delta("margin")}


# =============================================================================
# Figures (Plotly)
# =============================================================================
def _base_layout(fig, *, currency: str, horizontal: bool = False):
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency, "")
    axis = dict(tickprefix=symbol, separatethousands=True, tickformat=",.0f", zeroline=True)
    fig.update_layout(
        margin=dict(l=8, r=8, t=36, b=8), height=360, barcornerradius=4,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text=""),
        hoverlabel=dict(namelength=-1),
    )
    if horizontal:
        fig.update_xaxes(**axis)
        fig.update_yaxes(showgrid=False)
    else:
        fig.update_yaxes(**axis)
        fig.update_xaxes(showgrid=False)
    return fig


def fig_performance(perf: pd.DataFrame, *, currency: str = "GBP", dark: bool = False):
    import plotly.graph_objects as go

    colors = palette(dark)
    x = [ym_label(m) for m in perf["month"]]
    money = "%{y:,.2f}"
    fig = go.Figure()
    fig.add_bar(name="Revenue (sales)", x=x, y=perf["revenue"], marker_color=colors[0],
                hovertemplate=f"Revenue {money}<extra></extra>")
    fig.add_bar(name="Costs (purchases)", x=x, y=perf["costs"], marker_color=colors[1],
                hovertemplate=f"Costs {money}<extra></extra>")
    fig.add_scatter(name="Net margin", x=x, y=perf["margin"], mode="lines+markers",
                    line=dict(color=colors[2], width=2), marker=dict(size=8),
                    customdata=perf["margin_pct"],
                    hovertemplate=f"Margin {money} (%{{customdata}}%)<extra></extra>")
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08, hovermode="x unified")
    return _base_layout(fig, currency=currency)


def fig_stacked_by_category(df: pd.DataFrame, *, by: str = "nominal_code", ledger: str = PURCHASE,
                            order: Sequence[str] = (), months: Sequence[str] | None = None,
                            currency: str = "GBP", dark: bool = False):
    """Stacked monthly bars; categories outside ``order`` fold into 'Other'."""
    import plotly.graph_objects as go

    sub = df[df["ledger"] == ledger].copy()
    months = list(months) if months is not None else available_months(df)
    order = list(order)[:7]
    sub["category"] = sub[by].where(sub[by].isin(order), OTHER)
    pivot = sub.pivot_table(index="month", columns="category", values="net", aggfunc="sum", fill_value=0.0)
    pivot = pivot.reindex(index=months, fill_value=0.0)
    colors = palette(dark)
    fig = go.Figure()
    x = [ym_label(m) for m in months]
    for idx, cat in enumerate(order + [OTHER]):
        if cat not in pivot:
            continue
        color = (NEUTRAL_DARK if dark else NEUTRAL_LIGHT) if cat == OTHER else colors[idx]
        fig.add_bar(name=str(cat), x=x, y=pivot[cat], marker_color=color,
                    marker_line_width=1, marker_line_color="rgba(0,0,0,0)",
                    hovertemplate=f"{cat}: %{{y:,.2f}}<extra></extra>")
    fig.update_layout(barmode="relative", bargap=0.35, hovermode="x unified")
    return _base_layout(fig, currency=currency)


def fig_top_entities(top: pd.DataFrame, *, value: str = "gross", currency: str = "GBP", dark: bool = False):
    import plotly.graph_objects as go

    data = top.iloc[::-1]
    labels = [f"{n} ({c})" if n and n != c else str(c) for n, c in zip(data["company_name"], data["account_code"])]
    fig = go.Figure(go.Bar(x=data[value], y=labels, orientation="h", marker_color=palette(dark)[0],
                           customdata=data[["documents", "share_pct"]],
                           hovertemplate="%{y}<br>%{x:,.2f} · %{customdata[0]} docs · "
                                         "%{customdata[1]}% of total<extra></extra>"))
    fig.update_layout(bargap=0.35, showlegend=False, height=max(240, 32 * len(data) + 80))
    return _base_layout(fig, currency=currency, horizontal=True)


def fig_vat(monthly: pd.DataFrame, *, currency: str = "GBP", dark: bool = False):
    import plotly.graph_objects as go

    colors = palette(dark)
    x = [ym_label(m) for m in monthly["month"]]
    fig = go.Figure()
    fig.add_bar(name="Output VAT (sales)", x=x, y=monthly["output_vat"], marker_color=colors[0],
                hovertemplate="Output %{y:,.2f}<extra></extra>")
    fig.add_bar(name="Input VAT (purchases)", x=x, y=monthly["input_vat"], marker_color=colors[1],
                hovertemplate="Input %{y:,.2f}<extra></extra>")
    fig.add_scatter(name="Net payable", x=x, y=monthly["net_payable"], mode="lines+markers",
                    line=dict(color=colors[2], width=2), marker=dict(size=8),
                    hovertemplate="Net payable %{y:,.2f}<extra></extra>")
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08, hovermode="x unified")
    return _base_layout(fig, currency=currency)
