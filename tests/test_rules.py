from datetime import date
from decimal import Decimal

import pytest

from localledger.models import DocType, F, StagingRow, to_decimal
from localledger.rules import (
    BUILTIN_TEMPLATES,
    SAMPLE_ROW,
    DescriptionRule,
    apply_credit_detection,
    apply_rules,
    archive_subfolder,
    build_description,
    detect_credit_note,
    detect_service_period,
    map_rows,
    preview_description,
    render_filename,
    validate_description_template,
)


def tpl(name):
    return next(t for t in BUILTIN_TEMPLATES if t.name.startswith(name))


def row(**kw):
    base = dict(
        doc_type="PI", account_code="ACME001", nominal_code="7100", invoice_number="INV-10452",
        invoice_date=date(2026, 10, 3), raw_description="Office rent",
        company_name="ACME Property Services Ltd", net="1000.00", vat_code="T1", vat="200.00",
        gross="1200.00",
    )
    base.update(kw)
    return StagingRow(**base)


# --------------------------------------------------------------------- money
@pytest.mark.parametrize("text,expected", [
    ("£1,234.56", "1234.56"), ("(12.00)", "-12.00"), ("12.00 CR", "-12.00"),
    ("1.234,56", "1234.56"), ("-€5", "-5"), ("12,5", "12.5"), ("1,234", "1234"), ("", None),
])
def test_to_decimal(text, expected):
    result = to_decimal(text)
    assert (result is None and expected is None) or result == Decimal(expected)


# ------------------------------------------------------------ service period
def test_service_period_range():
    p = detect_service_period("Billing period: 01/09/2026 - 30/09/2026", date(2026, 10, 3))
    assert (p.start, p.end, p.source) == (date(2026, 9, 1), date(2026, 9, 30), "range")
    assert p.month_label == "Sep 2026"


def test_service_period_majority_month():
    p = detect_service_period("Service period 15 Sep 2026 to 14 Oct 2026")
    assert p.month_label == "Sep 2026"          # 16 days Sep vs 14 Oct
    assert p.period_label == "Sep 2026 - Oct 2026"


def test_service_period_month_word_and_fallback():
    assert detect_service_period("For the month of October 2026").month_label == "Oct 2026"
    fallback = detect_service_period("nothing useful", date(2026, 7, 19))
    assert fallback.source == "invoice_date" and fallback.month_label == "Jul 2026"


# --------------------------------------------------------------- description
def test_default_description_rule():
    rule = DescriptionRule(prefix="{Service_Month} - ", suffix=". {Company_Name}")
    assert preview_description(rule) == "Sep 2026 - Office rent. ACME Property Services Ltd"


def test_description_tidies_empty_tokens():
    rule = DescriptionRule(prefix="{Service_Month} - ", suffix=". {Company_Name}")
    r = row(company_name="", raw_description="Office rent")
    assert build_description(r, [rule]) == "Oct 2026 - Office rent"
    r = row(raw_description="")
    assert build_description(r, [rule]) == "Oct 2026 - ACME Property Services Ltd"


def test_description_does_not_repeat_prefix():
    rule = DescriptionRule(prefix="{Service_Month} - ")
    r = row(raw_description="Oct 2026 - Office rent")
    assert build_description(r, [rule]) == "Oct 2026 - Office rent"


def test_description_truncates_and_specific_rule_wins():
    generic = DescriptionRule(name="all", template="{Raw_Description}", max_length=20)
    specific = DescriptionRule(name="acme", template="Rent {Invoice_Number}", account_codes=["acme001"])
    r = row(raw_description="A very long description that goes on and on")
    assert build_description(r, [generic, specific]) == "Rent INV-10452"
    assert len(build_description(r, [generic])) <= 20


def test_truncation_keeps_prefix_and_suffix():
    rule = DescriptionRule(prefix="{Service_Month} - ", suffix=". {Company_Name}", max_length=60)
    r = row(raw_description="Office rent September 2026; Service charge",
            service_period_start=date(2026, 9, 1), service_period_end=date(2026, 9, 30))
    text = build_description(r, [rule])
    assert len(text) <= 60 and text.startswith("Sep 2026 - Office rent")
    assert text.endswith(". ACME Property Services Ltd")


def test_template_validation():
    assert validate_description_template("{Service_Month} - {Raw_Description}") == []
    assert validate_description_template("{Bogus} {Raw_Description")


# ----------------------------------------------------------------- credit notes
def test_credit_note_keyword_in_header():
    d = detect_credit_note("ACME Ltd\nCREDIT NOTE\nCredit note no: CN-001\nTotal 120.00")
    assert d.is_credit


def test_invoice_boilerplate_is_not_credit():
    text = "TAX INVOICE\nInvoice INV-1\nWe accept credit cards.\nRefund policy: 30 days\nTotal £120.00"
    assert not detect_credit_note(text).is_credit


def test_negative_total_is_credit():
    assert detect_credit_note("Statement\nTotal due: -£120.00").is_credit
    assert detect_credit_note("Document\nGrand Total (£55.20)").is_credit


def test_apply_credit_flips_type_and_signs():
    r = row(net="-100", vat="-20", gross="-120")
    changes = apply_credit_detection(r, detect_credit_note("", gross=r.gross))
    assert r.doc_type == DocType.PC and r.gross == Decimal("120.00") and changes


def test_apply_rules_pipeline():
    r = row(raw_text="CREDIT NOTE\nBilling period: 01/09/2026 - 30/09/2026", raw_description="Rent refund")
    apply_rules(r, description_rules=[DescriptionRule(prefix="{Service_Month} - ")])
    assert r.doc_type == DocType.PC
    assert r.service_period_start == date(2026, 9, 1)
    assert r.description == "Sep 2026 - Rent refund"


# ------------------------------------------------------------------ templates
def test_builtin_templates_are_valid():
    for t in BUILTIN_TEMPLATES:
        assert t.validate() == [], t.name


def test_sage_mapping():
    r = row(description="Sep 2026 - Office rent", service_period_start=date(2026, 9, 1),
            service_period_end=date(2026, 9, 30))
    credit = row(doc_type="PC", invoice_number="CN-1", net="50", vat="10", gross="60")
    result = map_rows([r, credit], tpl("Sage 50"))
    text = result.to_csv_bytes().decode("cp1252").splitlines()
    assert text[0].startswith("Type,Account Reference,Nominal A/C Ref")
    assert text[1].split(",")[:5] == ["PI", "ACME001", "7100", "", "03/10/2026"]
    assert "Sep 2026" in text[1]
    assert text[2].split(",")[0] == "PC" and ",50.00," in text[2]   # Sage keeps credits positive


def test_xero_mapping_signs_tax_map_and_exclusions():
    sales = row(doc_type="SI")
    credit = row(doc_type="PC", net="50", vat="10", gross="60")
    result = map_rows([row(), credit, sales], tpl("Xero – Bills"))
    assert len(result.records) == 2 and len(result.excluded) == 1
    df = result.to_dataframe()
    assert df.loc[0, "*TaxType"] == "20% (VAT on Expenses)"
    assert df.loc[1, "*UnitAmount"] == "-50.00"
    assert df.loc[0, "*ContactName"] == "ACME Property Services Ltd"


def test_truncation_warning_and_excel_output():
    r = row(description="x" * 80)
    result = map_rows([r], tpl("Sage 50"))
    assert any("truncated" in w for w in result.warnings)
    assert result.to_excel_bytes()[:2] == b"PK"


def test_freeagent_iso_dates():
    df = map_rows([row()], tpl("FreeAgent")).to_dataframe()
    assert df.loc[0, "Dated On"] == "2026-10-03" and df.loc[0, "Sales Tax Rate"] == "20"


def test_template_roundtrip_json():
    t = tpl("Xero – Bills")
    clone = type(t).from_json(t.to_json())
    assert clone.to_dict() == t.to_dict()


# ------------------------------------------------------------- archive naming
def test_render_filename():
    assert render_filename("[AccountCode]_[InvoiceDate]_[InvoiceNumber].pdf", row()) == \
        "ACME001_2026-10-03_INV-10452.pdf"
    weird = row(invoice_number="INV/10:452", account_code="")
    assert render_filename("[AccountCode]_[InvoiceDate]_[InvoiceNumber].pdf", weird) == \
        "2026-10-03_INV-10-452.pdf"


def test_archive_subfolder():
    assert archive_subfolder("{YYYY}-{MM}", row()) == "2026-10"
    assert archive_subfolder("{Ledger}/{YYYY}/../{MM}", row()) == "Purchases/2026/10"


def test_sample_row_fields():
    assert SAMPLE_ROW.doc_type == DocType.PI and F.NET == "Net Amount"
