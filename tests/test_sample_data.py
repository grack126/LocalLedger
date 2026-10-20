"""The generated sample PDFs must behave as advertised when run through the real pipeline."""
from datetime import date

import pytest

pytest.importorskip("reportlab")

import generate_sample_data as gsd  # noqa: E402
from launcher import free_port  # noqa: E402
from localledger import analytics as an  # noqa: E402
from localledger.db import Database, map_entity_columns, read_master_file  # noqa: E402
from localledger.extraction import ExtractionPipeline  # noqa: E402
from localledger.models import DocType, F  # noqa: E402
from localledger.validator import validate_batch  # noqa: E402


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("samples")
    return out, gsd.generate(out)


@pytest.fixture
def db(tmp_path, samples):
    out, _ = samples
    database = Database(tmp_path / "s.db")
    report = database.import_entities(read_master_file(out / "master_list.csv"))
    assert report.inserted == len(gsd.MASTER_LIST) and not report.errors
    database.set_settings({"own_company_name": gsd.OWN_COMPANY, "own_vat_number": gsd.OWN_VAT,
                           "fx_enabled": False})
    return database


def test_files_created_and_duplicate_is_identical(samples):
    out, files = samples
    assert all(p.is_file() and p.stat().st_size > 1000 for p in files.values())
    assert files["clean"].read_bytes() == files["duplicate"].read_bytes()
    again = gsd.generate(out)                                       # deterministic output
    assert again["clean"].read_bytes() == files["clean"].read_bytes()


def test_sage_export_columns_are_recognised(samples):
    out, _ = samples
    frame = read_master_file(out / "sage_supplier_export.csv")
    assert set(map_entity_columns(frame.columns)) == {"account_code", "company_name", "vat_reg_no",
                                                      "default_nominal_code", "default_vat_code"}


def test_samples_through_pipeline(db, samples):
    _, files = samples
    pipeline = ExtractionPipeline.from_db(db, fx=None, ocr_engine=None)
    results = {k: pipeline.process_file(p) for k, p in files.items()}
    rows = [r.row for r in results.values()]
    validate_batch(rows, db)
    clean, messy, credit, dup = (results[k].row for k in ("clean", "messy", "credit", "duplicate"))

    assert (clean.account_code, clean.entity_match_method, clean.confidence_band) == ("ACME001", "vat", "HIGH")
    assert clean.gross == 1500 and clean.vat_code == "T1" and clean.service_period_start is not None
    assert clean.description.endswith("ACME Property Services Ltd")

    assert messy.account_code == "BOLT01" and messy.invoice_number == "BN-7781"
    assert messy.confidence_band == "LOW" and F.GROSS in messy.error_fields and messy.gross == 650

    assert credit.doc_type == DocType.PC and credit.currency == "USD" and credit.account_code == "GLOBEX"
    assert credit.gross == 400 and credit.invoice_number == "CN-0042"

    assert [f.tier for f in dup.duplicate_flags] == [1] and [f.tier for f in clean.duplicate_flags] == [1]


def test_demo_history_feeds_analytics(tmp_path):
    path = tmp_path / "demo.db"
    count = gsd.load_demo(str(path), today=date(2026, 10, 20))
    db = Database(path)
    df = an.load_ledger(db)
    assert count == 37 and set(df["month"]) == {f"2026-{m:02d}" for m in range(4, 10)}
    alerts = an.run_rate_alerts(df, by="account_code", month="2026-09")
    assert "POWER1" in set(alerts["account_code"])
    assert gsd.remove_demo(str(path)) == count and an.load_ledger(db).empty
    assert db.find_entity_by_account("Supplier", "ACME001") is not None      # master list kept


def test_free_port_skips_busy_port():
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert free_port(port) != port
