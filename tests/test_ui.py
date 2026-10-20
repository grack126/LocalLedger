"""Headless Streamlit tests (streamlit.testing.v1.AppTest) — every page renders,
and the main review / memory / export flow works end to end."""
from datetime import date

import pandas as pd
import pytest
from conftest import PURCHASE_INVOICE, make_pdf

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from localledger.db import Database  # noqa: E402
from localledger.models import StagingRow  # noqa: E402
from localledger.staging import StagingStore  # noqa: E402

PAGES = ["ui/staging.py", "ui/history.py", "ui/analytics.py", "ui/entities.py", "ui/templates.py",
         "ui/rules_editor.py", "ui/settings.py"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "data" / "ui.db"
    monkeypatch.setenv("LOCALLEDGER_DB", str(path))
    db = Database(path)
    db.import_entities(pd.DataFrame([{"Type": "Supplier", "Account Reference": "ACME001",
                                      "Name": "ACME Property Services Ltd", "Default Nominal": "7100",
                                      "Tax Code": "T1"}]))
    db.set_settings({"archive_dir": str(tmp_path / "Archive"), "fx_enabled": False})
    return db


def stage_row(db: Database, **kw) -> StagingRow:
    store = StagingStore(db)
    data = make_pdf(PURCHASE_INVOICE)
    path = store.store_upload("acme.pdf", data, "a" * 64)
    base = dict(doc_type="PI", company_name="ACME Property Services Ltd", account_code="ACME001",
                entity_match_method="exact", entity_match_score=100.0, nominal_code="7100", vat_code="T1",
                invoice_number="INV-10452", invoice_date=date.today().replace(day=1),
                raw_description="Office rent", description="Office rent", net="1250", vat="250", gross="1500",
                source_file="acme.pdf", file_path=str(path), file_hash="a" * 64, extraction_method="native",
                raw_text="\n".join(PURCHASE_INVOICE))
    base.update(kw)
    row = StagingRow(**base)
    store.save([row])
    return row


ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def run(page: str, **state) -> AppTest:
    at = AppTest.from_file(str(ROOT / page), default_timeout=60)
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_empty(env, page):
    at = run(page)
    assert not at.exception, at.exception


def test_review_edit_memory_and_export(env):
    row = stage_row(env)
    at = run("ui/staging.py", selected_row_id=row.row_id)
    assert not at.exception, at.exception
    assert at.dataframe, "staging grid missing"
    assert any("Save changes" in b.label for b in at.button)

    nominal = next(t for t in at.text_input if t.label.endswith("Nominal code"))
    nominal.set_value("7300")
    next(b for b in at.button if b.label == "Save changes").click()
    at.run()
    assert not at.exception, at.exception
    assert any("Save as default" in b.label for b in at.button), "vendor-memory prompt missing"
    next(b for b in at.button if b.label == "Save as default").click()
    at.run()
    assert env.find_entity_by_account("Supplier", "ACME001").default_nominal_code == "7300"

    export = next(b for b in at.button if b.label.startswith("Export 1"))
    export.click()
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["last_export"]["documents"] == 1
    assert len(env.list_invoice_logs()) == 1 and StagingStore(env).load() == []


def test_duplicate_blocks_until_dismissed(env):
    row = stage_row(env)
    env.log_exported_rows([StagingRow(doc_type="PI", account_code="ACME001", invoice_number="INV-10452",
                                      invoice_date=date(2026, 1, 1), net="1", vat="0", gross="1")])
    at = run("ui/staging.py", selected_row_id=row.row_id)
    assert not at.exception, at.exception
    export = next(b for b in at.button if b.label.startswith("Export"))
    assert export.disabled
    next(b for b in at.button if b.label == "Dismiss flag").click()
    at.run()
    assert next(b for b in at.button if b.label.startswith("Export")).disabled is False


def test_analytics_with_history(env):
    rows = [StagingRow(doc_type=t, account_code="ACME001", invoice_number=f"X{i}", invoice_date=date(2026, m, 3),
                       net="100", vat="20", gross="120", nominal_code="7100", vat_code="T1")
            for i, (t, m) in enumerate([("PI", 8), ("PI", 9), ("SI", 9), ("PI", 10), ("SC", 10)])]
    env.log_exported_rows(rows)
    at = run("ui/analytics.py")
    assert not at.exception, at.exception
    assert len(at.metric) >= 4
