from datetime import date

import pandas as pd
import pytest

from localledger.db import Database, memory_changes
from localledger.models import StagingRow
from localledger.rules import BUILTIN_TEMPLATES
from localledger.validator import validate_batch

MASTER = pd.DataFrame([
    {"Type": "Supplier", "Account Reference": "ACME001", "Name": "ACME Property Services Ltd",
     "VAT Number": "GB 123 4567 89", "Default Nominal": "7100", "Tax Code": "T1"},
    {"Type": "Supplier", "Account Reference": "BOLT01", "Name": "Bolt & Nut Supplies Limited",
     "VAT Number": "", "Default Nominal": "5000", "Tax Code": "T1"},
    {"Type": "Customer", "Account Reference": "CUST01", "Name": "Northwind Traders",
     "VAT Number": "", "Default Nominal": "4000", "Tax Code": "T1"},
])


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    report = database.import_entities(MASTER)
    assert (report.inserted, report.errors) == (3, [])
    return database


def staged(**kw):
    base = dict(doc_type="PI", company_name="Acme Property Services Limited", invoice_number="INV-001",
                invoice_date=date(2026, 10, 3), raw_description="Rent", net="1000", vat="200",
                gross="1200", extraction_method="native", file_hash="h1", source_file="a.pdf")
    base.update(kw)
    return StagingRow(**base)


def test_seeds_and_reopen(tmp_path):
    first = Database(tmp_path / "x.db")
    assert len(first.list_templates()) == len(BUILTIN_TEMPLATES)
    assert first.vat_code_map()["T1"].rate == 20
    again = Database(tmp_path / "x.db")              # idempotent init
    assert len(again.list_templates()) == len(BUILTIN_TEMPLATES)
    assert again.get_setting("base_currency") == "GBP"


def test_settings_roundtrip(db):
    db.set_setting("duplicate_window_days", 5)
    assert db.get_settings()["duplicate_window_days"] == 5


def test_import_merge_keeps_learned_defaults(db):
    db.save_vendor_memory(entity_type="Supplier", account_code="BOLT01", nominal_code="5100")
    update = MASTER.copy()
    update.loc[1, "Default Nominal"] = ""
    report = db.import_entities(update)
    assert report.updated == 3 and report.inserted == 0
    assert db.find_entity_by_account("Supplier", "bolt01").default_nominal_code == "5100"


def test_resolver_vat_fuzzy_and_type_filter(db):
    resolver = db.entity_resolver()
    by_vat = resolver.resolve("Totally different name", "GB123456789", "Supplier")
    assert by_vat.method == "vat" and by_vat.entity.account_code == "ACME001"
    fuzzy = resolver.resolve("Bolt and Nut Supplies", None, "Supplier")
    assert fuzzy.entity.account_code == "BOLT01" and fuzzy.auto_assigned
    assert resolver.resolve("Northwind Traders", None, "Supplier").entity is None   # client only
    r = staged()
    match = resolver.apply(r)
    assert match.auto_assigned and r.account_code == "ACME001"
    assert (r.nominal_code, r.vat_code) == ("7100", "T1")


def test_vendor_memory_alias_and_learned_entity(db):
    r = staged(company_name="ACME Facilities (UK)")
    assert db.entity_resolver().resolve(r.company_name, None, "Supplier").auto_assigned is False
    edited = staged(company_name="ACME Facilities (UK)", account_code="ACME001", nominal_code="7300")
    assert set(memory_changes(r, edited)) == {"Account Code", "Nominal Code"}
    db.remember_from_row(edited, ["Account Code", "Nominal Code"])
    match = db.entity_resolver().resolve("ACME Facilities (UK)", None, "Supplier")
    assert match.method == "alias" and match.entity.default_nominal_code == "7300"

    learned = db.save_vendor_memory(entity_type="Supplier", account_code="NEW01",
                                    extracted_name="Brand New Co", vat_code="t0")
    assert learned.source == "learned" and learned.default_vat_code == "T0"
    with pytest.raises(ValueError):
        db.save_vendor_memory(entity_type="Supplier", account_code="NEW01", vat_code="ZZ")


def test_templates_builtin_protection_and_clone(db):
    sage = db.get_template(name="Sage 50 – Audit Trail Import")
    with pytest.raises(ValueError):
        db.save_template(sage)
    with pytest.raises(ValueError):
        db.delete_template(sage.id)
    mine = sage.clone("My Sage")
    mine.date_format = "YYYY-MM-DD"
    tid = db.save_template(mine)
    assert db.get_template(tid).date_format == "YYYY-MM-DD"
    db.delete_template(tid)
    assert db.get_template(tid) is None


def test_log_history_duplicates_and_analytics(db):
    resolver = db.entity_resolver()
    inv = staged()
    resolver.apply(inv)
    a = staged(doc_type="PC", invoice_number="CN-9", net="100", vat="20", gross="120",
               file_hash="h2", group_id="G2", nominal_code="7100", vat_code="T1",
               account_code="ACME001", entity_match_method="manual", doc_gross="120", line_no=1)
    validate_batch([inv, a], db, today=date(2026, 10, 20))
    assert inv.confidence_band == "HIGH", inv.issues
    ids = db.log_exported_rows([inv, a], template_id=1)
    assert len(ids) == 2

    again = staged(source_file="renamed.pdf", invoice_number="INV-001-dup")
    resolver.apply(again)
    validate_batch([again], db, today=date(2026, 10, 20))
    tiers = sorted(f.tier for f in again.duplicate_flags)
    assert tiers == [1] and again.duplicate_flags[0].source == "history"

    frame = db.ledger_lines_frame()
    assert sorted(frame["gross"].tolist()) == [-120.0, 1200.0]
    db.void_invoice_log(ids[0], "test")
    assert len(db.ledger_lines_frame()) == 1


def test_backup(db, tmp_path):
    dest = db.backup_to(tmp_path / "backup" / "copy.db")
    assert Database(dest).find_entity_by_account("Client", "CUST01") is not None
