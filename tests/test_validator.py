from datetime import date
from decimal import Decimal

from localledger.db import HistoricalInvoice, VatCode
from localledger.models import F, StagingRow
from localledger.validator import (
    HIGH,
    LOW,
    ValidationSettings,
    check_math,
    detect_duplicates,
    dismiss_flags,
    export_blockers,
    infer_missing_amounts,
    validate_batch,
    validate_row,
)

TODAY = date(2026, 10, 20)
VAT = {
    "T0": VatCode("T0", "Zero", Decimal("0")),
    "T1": VatCode("T1", "Standard", Decimal("20")),
    "T5": VatCode("T5", "Reduced", Decimal("5")),
    "T8": VatCode("T8", "EU reverse charge", Decimal("20"), is_reverse_charge=True),
}
KNOWN = {"Supplier": {"ACME001", "BOLT01"}, "Client": {"CUST01"}}


def row(**kw):
    base = dict(
        doc_type="PI", account_code="ACME001", nominal_code="7100", invoice_number="INV-001",
        invoice_date=date(2026, 10, 3), raw_description="Office rent", net="1000.00",
        vat_code="T1", vat="200.00", gross="1200.00", extraction_method="native",
        entity_match_method="exact", entity_match_score=100.0, file_hash="",
    )
    base.update(kw)
    return StagingRow(**base)


def d(x):
    return Decimal(x)


# ---------------------------------------------------------------------- math
def test_math_passes_within_tolerance():
    assert check_math(d("100.00"), d("20.01"), d("120.01"), "T1", VAT).passed
    assert check_math(d("100.00"), d("20.00"), d("120.02"), "T1", VAT).passed


def test_math_sum_mismatch():
    m = check_math(d("100.00"), d("20.00"), d("130.00"), "T1", VAT)
    assert m.failed and m.sum_ok is False and m.difference == d("-10.00")


def test_math_vat_rate_mismatch_and_reverse_charge():
    assert check_math(d("100"), d("15"), d("115"), "T1", VAT).vat_rate_ok is False
    assert check_math(d("100"), d("5"), d("105"), "T5", VAT).passed
    assert check_math(d("100"), d("0"), d("100"), "T8", VAT).passed       # reverse charge
    assert check_math(d("100"), d("20"), d("120"), "T0", VAT).failed      # VAT on zero-rated


def test_math_missing_amount_is_failure():
    m = check_math(d("100"), None, d("120"), "T1", VAT)
    assert m.failed and m.issues[0].code == "amount_missing"


def test_infer_missing_amounts():
    assert infer_missing_amounts(d("100"), d("20"), None)[2] == d("120.00")
    net, vat, gross, inferred = infer_missing_amounts(None, None, d("120"), d("20"))
    assert (net, vat, inferred) == (d("100.00"), d("20.00"), [F.NET, F.VAT])


# ------------------------------------------------------------------ confidence
def test_clean_row_is_high():
    r = row()
    res = validate_row(r, vat_codes=VAT, known_accounts=KNOWN, today=TODAY)
    assert res.band == HIGH and r.confidence >= 85 and not res.issues


def test_math_error_forces_low_and_marks_cells():
    r = row(gross="1300.00")
    res = validate_row(r, vat_codes=VAT, known_accounts=KNOWN, today=TODAY)
    assert res.band == LOW and res.score < 50
    assert {F.NET, F.VAT, F.GROSS} <= set(r.error_fields)


def test_missing_nominal_caps_below_high():
    res = validate_row(row(nominal_code=""), vat_codes=VAT, known_accounts=KNOWN, today=TODAY)
    assert res.band != HIGH and F.NOMINAL_CODE in res.error_fields


def test_fuzzy_entity_and_ocr_lower_score():
    exact = validate_row(row(), vat_codes=VAT, known_accounts=KNOWN, today=TODAY).score
    fuzzy = validate_row(row(entity_match_method="fuzzy", entity_match_score=72.0, extraction_method="ocr",
                             extraction_quality=0.6), vat_codes=VAT, known_accounts=KNOWN, today=TODAY).score
    assert fuzzy < exact


def test_foreign_currency_without_rate_warns():
    r = row(currency="USD")
    validate_row(r, vat_codes=VAT, known_accounts=KNOWN, today=TODAY)
    assert any(i.code == "fx_missing" for i in r.issues)


def test_split_lines_must_sum_to_document_total():
    a = row(net="600", vat="120", gross="720", doc_gross="1200", group_id="G1", line_no=1)
    b = row(net="400", vat="80", gross="470", doc_gross="1200", group_id="G1", line_no=2,
            nominal_code="7200")
    report = validate_batch([a, b], settings={}, vat_codes=VAT, known_accounts=KNOWN, history=[], today=TODAY)
    assert a.confidence_band == LOW and b.confidence_band == LOW
    assert not report.exportable
    b.vat, b.gross = Decimal("80.00"), Decimal("480.00")
    validate_batch([a, b], settings={}, vat_codes=VAT, known_accounts=KNOWN, history=[], today=TODAY)
    assert a.confidence_band == HIGH and not a.duplicate_flags      # split lines don't flag each other


# ------------------------------------------------------------------ duplicates
def test_tier1_same_file_renamed():
    a, b = row(file_hash="abc", source_file="a.pdf"), row(file_hash="abc", source_file="copy.pdf",
                                                           invoice_number="X9")
    flags = detect_duplicates([a, b])
    assert flags[a.row_id][0].tier == 1 and flags[b.row_id][0].tier == 1


def test_tier2_normalised_invoice_number():
    a, b = row(invoice_number="INV-001"), row(invoice_number="inv 001", gross="5", net="5", vat="0")
    assert detect_duplicates([a, b])[a.row_id][0].tier == 2


def test_tier3_amount_and_date_window():
    a = row(invoice_number="INV-1001", invoice_date=date(2026, 10, 1))
    b = row(invoice_number="INV-1O01", invoice_date=date(2026, 10, 3))     # OCR read O for 0
    c = row(invoice_number="INV-2000", invoice_date=date(2026, 10, 9))
    flags = detect_duplicates([a, b, c], window_days=3)
    assert [f.tier for f in flags[a.row_id]] == [3]
    assert flags[c.row_id] == []


def test_credit_note_does_not_flag_matching_invoice():
    inv = row(invoice_number="INV-1")
    cn = row(doc_type="PC", invoice_number="CN-1")
    assert detect_duplicates([inv, cn])[inv.row_id] == []


def test_history_duplicates_and_dismissal():
    hist = [HistoricalInvoice(7, "PI", "ACME001", "ACME", "INV-001", "INV001", date(2026, 9, 1),
                              120000, "zzz", "old.pdf", "2026-09-05T10:00:00Z")]
    r = row()
    report = validate_batch([r], settings={}, vat_codes=VAT, known_accounts=KNOWN, history=hist, today=TODAY)
    assert r.duplicate_flags[0].source == "history" and r.duplicate_flags[0].matched_ref == "7"
    assert export_blockers(r) and not report.exportable
    dismiss_flags([r], r.group_id)
    assert not export_blockers(r)
    validate_batch([r], settings={}, vat_codes=VAT, known_accounts=KNOWN, history=hist, today=TODAY)
    assert not r.open_duplicate_flags            # dismissal survives re-validation


def test_settings_thresholds():
    vs = ValidationSettings.from_settings({"confidence_high": 90, "math_tolerance": 0.05})
    assert vs.high_threshold == 90 and vs.math_tolerance == Decimal("0.05")
