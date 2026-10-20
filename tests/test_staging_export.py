from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from conftest import PURCHASE_INVOICE, make_pdf

from localledger.db import Database
from localledger.exporter import ExportBlocked, ExportOptions, export_batch, plan_export
from localledger.models import DocType, StagingRow
from localledger.staging import (
    SplitError,
    StagingStore,
    apply_changes,
    make_manual_row,
    split_document,
    unsplit_document,
)
from localledger.validator import dismiss_flags, validate_batch

D = Decimal
TODAY = date(2026, 10, 20)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data" / "ll.db")
    database.import_entities(pd.DataFrame([
        {"Type": "Supplier", "Account Reference": "ACME001", "Name": "ACME Property Services Ltd",
         "Default Nominal": "7100", "Tax Code": "T1"},
        {"Type": "Client", "Account Reference": "CONT01", "Name": "Contoso Retail plc",
         "Default Nominal": "4000", "Tax Code": "T1"}]))
    database.set_settings({"archive_dir": str(tmp_path / "Archive")})
    return database


def staged(store: StagingStore, name="acme.pdf", **kw) -> StagingRow:
    data = make_pdf(PURCHASE_INVOICE + [name])           # unique bytes per file
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    path = store.store_upload(name, data, digest)
    base = dict(doc_type="PI", account_code="ACME001", entity_match_method="manual", nominal_code="7100",
                vat_code="T1", invoice_number="INV-1", invoice_date=date(2026, 10, 3), raw_description="Rent",
                description="Oct 2026 - Rent", net="1000", vat="200", gross="1200", source_file=name,
                file_path=str(path), file_hash=digest, extraction_method="native")
    base.update(kw)
    return StagingRow(**base)


# ------------------------------------------------------------------ store
def test_store_roundtrip_and_inbox_cleanup(db):
    store = StagingStore(db)
    a, b = staged(store, "a.pdf"), staged(store, "b.pdf", invoice_number="INV-2")
    a.dismissed_flags = {"T2:batch:x"}
    store.save([a, b])
    loaded = store.load()
    assert [r.row_id for r in loaded] == [a.row_id, b.row_id]
    assert loaded[0].net == D("1000.00") and loaded[0].dismissed_flags == {"T2:batch:x"}
    assert Path(a.file_path).parent == store.inbox_dir
    assert store.discard_unreferenced([a], [b]) == 1 and not Path(a.file_path).exists()


# ------------------------------------------------------------------ split
def test_split_and_merge(db):
    store = StagingStore(db)
    rows = [staged(store)]
    gid = rows[0].group_id
    with pytest.raises(SplitError):
        split_document(rows, gid, [{"nominal_code": "7100", "vat_code": "T1", "net": 600, "vat": 120},
                                   {"nominal_code": "7200", "vat_code": "T1", "net": 300, "vat": 60}])
    new = split_document(rows, gid, [{"nominal_code": "7100", "vat_code": "T1", "net": 600, "vat": 120},
                                     {"nominal_code": "7200", "vat_code": "T0", "net": 480, "vat": 0}])
    assert len(rows) == 2 and {r.group_id for r in rows} == {gid} and [r.line_no for r in rows] == [1, 2]
    assert all(r.doc_gross == D("1200.00") for r in new)
    validate_batch(rows, db, today=TODAY)
    assert all(r.confidence_band == "HIGH" for r in rows), [r.issues for r in rows]
    merged = unsplit_document(rows, gid)
    assert len(rows) == 1 and merged.net == D("1080.00") and merged.vat == D("120.00") and merged.doc_gross is None


def test_apply_changes_relinks_entity(db):
    row = StagingRow(doc_type="SI", company_name="Contoso", net="10", vat="2", gross="12")
    changed = apply_changes(row, {"account_code": "cont01", "net": 10.0}, resolver=db.entity_resolver())
    assert set(changed) == {"account_code"}
    assert (row.entity_name, row.nominal_code, row.vat_code, row.entity_match_method) == \
        ("Contoso Retail plc", "4000", "T1", "manual")
    with pytest.raises(KeyError):
        apply_changes(row, {"file_hash": "x"})


def test_manual_row_fills_gross_and_defaults(db):
    entity = db.find_entity_by_account("Supplier", "ACME001")
    row = make_manual_row(doc_type="PI", invoice_date="05/10/2026", net="50", vat="10", entity=entity,
                          description="Parking receipt")
    assert row.gross == D("60.00") and row.nominal_code == "7100" and row.is_manual
    validate_batch([row], db, today=TODAY)
    assert row.confidence_band in ("HIGH", "MEDIUM") and not row.error_fields


# ------------------------------------------------------------------ export
def test_export_archives_logs_and_cleans_inbox(db, tmp_path):
    store = StagingStore(db)
    rows = [staged(store, "a.pdf"), staged(store, "b.pdf", invoice_number="INV-2", gross="1300")]   # b: math error
    options = ExportOptions.from_db(db)
    plan = plan_export(rows, db, options, today=TODAY)
    assert len(plan.exportable) == 1 and rows[1].group_id in plan.blocked

    result = export_batch(rows, db, options, today=TODAY)
    target = tmp_path / "Archive" / "2026-10" / "ACME001_2026-10-03_INV-1.pdf"
    assert result.archived == {rows[0].group_id: str(target)} and target.is_file()
    assert not Path(rows[0].file_path).exists()                     # inbox copy removed
    assert Path(rows[1].file_path).exists()                         # blocked doc untouched
    assert result.data.decode("cp1252").splitlines()[1].startswith("PI,ACME001,7100,,03/10/2026,INV-1")
    assert Path(result.saved_path).is_file()
    assert db.list_invoice_logs()[0]["archived_path"] == str(target)


def test_export_requires_dismissed_duplicates(db):
    store = StagingStore(db)
    a = staged(store, "a.pdf")
    b = staged(store, "b.pdf", invoice_number="INV-1")              # same account + invoice number
    rows = [a, b]
    options = ExportOptions.from_db(db)
    with pytest.raises(ExportBlocked):
        export_batch(rows, db, options, today=TODAY)
    dismiss_flags(rows, a.group_id)
    dismiss_flags(rows, b.group_id)
    result = export_batch(rows, db, options, today=TODAY)
    assert len(result.log_ids) == 2
    names = sorted(Path(p).name for p in result.archived.values())
    assert names == ["ACME001_2026-10-03_INV-1.pdf", "ACME001_2026-10-03_INV-1_2.pdf"]   # no overwrite


def test_export_template_exclusion_and_move_mode(db, tmp_path):
    folder = tmp_path / "incoming"
    folder.mkdir()
    original = folder / "scan.pdf"
    original.write_bytes(make_pdf(PURCHASE_INVOICE))
    pi = StagingRow(doc_type="PI", account_code="ACME001", entity_match_method="manual", nominal_code="7100",
                    vat_code="T1", invoice_number="INV-9", invoice_date=date(2026, 10, 3), raw_description="Rent",
                    net="100", vat="20", gross="120", file_path=str(original), file_hash="f" * 64,
                    source_file="scan.pdf", extraction_method="native")
    si = StagingRow(doc_type="SI", account_code="CONT01", entity_match_method="manual", nominal_code="4000",
                    vat_code="T1", invoice_number="SI-1", invoice_date=date(2026, 10, 4), raw_description="Fees",
                    net="100", vat="20", gross="120", extraction_method="manual")
    xero_bills = db.get_template(name="Xero – Bills (Purchases)")
    options = ExportOptions.from_db(db, xero_bills, archive_mode="move")
    result = export_batch([pi, si], db, options, today=TODAY)
    assert result.plan.excluded and si.group_id in result.plan.excluded
    assert not original.exists() and Path(result.archived[pi.group_id]).is_file()


def test_export_rolls_back_archive_when_logging_fails(db, tmp_path, monkeypatch):
    store = StagingStore(db)
    rows = [staged(store, "a.pdf")]

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "log_exported_rows", boom)
    with pytest.raises(RuntimeError):
        export_batch(rows, db, ExportOptions.from_db(db), today=TODAY)
    assert not any((tmp_path / "Archive").rglob("*.pdf"))
    assert Path(rows[0].file_path).exists()
