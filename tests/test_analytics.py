from datetime import date

import pytest

from localledger import analytics as an
from localledger.db import Database
from localledger.models import StagingRow


def doc(doc_type, account, inv, d, net, vat, nominal, vat_code="T1", svc=None, name=None):
    gross = net + vat
    return StagingRow(doc_type=doc_type, account_code=account, invoice_number=inv, invoice_date=d,
                      net=str(net), vat=str(vat), gross=str(gross), nominal_code=nominal, vat_code=vat_code,
                      entity_name=name or account, service_period_start=svc,
                      service_period_end=svc.replace(day=28) if svc else None)


@pytest.fixture
def ledger(tmp_path):
    db = Database(tmp_path / "a.db")
    rows = []
    # steady supplier spend Apr-Sep (100/month), spike in Oct (200)
    for i, month in enumerate(range(4, 11)):
        net = 200 if month == 10 else 100
        rows.append(doc("PI", "ACME", f"A{i}", date(2026, month, 5), net, net * 0.2, "7100", name="ACME Ltd"))
    rows.append(doc("SI", "CUST", "S1", date(2026, 9, 30), 1000, 200, "4000",
                    svc=date(2026, 9, 1)))
    rows.append(doc("SI", "CUST", "S2", date(2026, 10, 2), 500, 100, "4000", svc=date(2026, 9, 1)))   # accrues to Sep
    rows.append(doc("SC", "CUST", "C1", date(2026, 10, 10), 100, 20, "4000"))
    rows.append(doc("PI", "NEWCO", "N1", date(2026, 10, 12), 50, 0, "7500", vat_code="T0"))
    db.log_exported_rows(rows)
    return db


def test_monthly_performance_and_credits(ledger):
    df = an.load_ledger(ledger, "invoice")
    perf = an.monthly_performance(df).set_index("month")
    assert perf.loc["2026-10", "revenue"] == pytest.approx(400.0)          # 500 sale - 100 credit
    assert perf.loc["2026-10", "costs"] == pytest.approx(250.0)
    assert perf.loc["2026-09", "margin"] == pytest.approx(900.0)
    assert perf.loc["2026-09", "margin_pct"] == pytest.approx(90.0)


def test_service_basis_moves_revenue(ledger):
    perf = an.monthly_performance(an.load_ledger(ledger, "service")).set_index("month")
    assert perf.loc["2026-09", "revenue"] == pytest.approx(1500.0)
    assert perf.loc["2026-10", "revenue"] == pytest.approx(-100.0)


def test_variance_and_alerts(ledger):
    df = an.load_ledger(ledger)
    var = an.variance_table(df, "nominal_code", "Purchase").set_index("nominal_code")
    assert var.loc["7100", "change"] == pytest.approx(100.0) and var.loc["7100", "change_pct"] == pytest.approx(100.0)
    alerts = an.run_rate_alerts(df, by="account_code", month="2026-10").set_index("account_code")
    assert alerts.loc["ACME", "status"] == "Above run-rate" and alerts.loc["ACME", "increase_pct"] == pytest.approx(100.0)
    assert alerts.loc["NEWCO", "status"] == "New"
    quiet = an.run_rate_alerts(df, month="2026-09")
    assert quiet.empty


def test_vat_summary(ledger):
    monthly, breakdown = an.vat_summary(an.load_ledger(ledger))
    oct_ = monthly.set_index("month").loc["2026-10"]
    assert oct_["output_vat"] == pytest.approx(80.0) and oct_["input_vat"] == pytest.approx(40.0)
    assert oct_["net_payable"] == pytest.approx(40.0) and oct_["position"] == "Payable"
    assert set(breakdown[breakdown["month"] == "2026-10"]["vat_code"]) == {"T0", "T1"}


def test_top_entities_and_figures(ledger):
    df = an.load_ledger(ledger)
    top = an.top_entities(df, "Purchase")
    assert list(top["account_code"]) == ["ACME", "NEWCO"] and top.loc[0, "documents"] == 7
    perf = an.monthly_performance(df)
    figs = [an.fig_performance(perf), an.fig_vat(an.vat_summary(df)[0], dark=True),
            an.fig_top_entities(top), an.fig_stacked_by_category(df, order=an.category_order(df))]
    assert all(len(f.data) > 0 for f in figs)
    assert figs[0].data[0].marker.color == an.PALETTE_LIGHT[0]
    assert an.ym_label("2026-10") == "Oct 2026"


def test_empty_ledger(tmp_path):
    df = an.load_ledger(Database(tmp_path / "e.db"))
    assert df.empty and an.monthly_performance(df).empty and an.run_rate_alerts(df).empty
