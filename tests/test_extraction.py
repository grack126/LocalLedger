import json
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from conftest import MATH_ERROR_INVOICE, PURCHASE_INVOICE, SALES_INVOICE, USD_CREDIT_NOTE

from localledger.db import Database
from localledger.extraction import (
    EngineUnavailable,
    ExtractionError,
    ExtractionPipeline,
    extract_native,
    infer_vat_code,
    page_count,
    parse_text,
    render_page_png,
)
from localledger.extraction.llm import LLMConfig, LocalLLMClient, fields_from_llm, is_local_endpoint
from localledger.extraction.parsers import classify_line, find_amounts, parse_amounts
from localledger.fx import FxService
from localledger.models import DocType, F
from localledger.validator import validate_batch

D = Decimal
TODAY = date(2026, 10, 20)

MASTER = pd.DataFrame([
    {"Type": "Supplier", "Account Reference": "ACME001", "Name": "ACME Property Services Ltd",
     "VAT Number": "GB123456789", "Default Nominal": "7100", "Tax Code": "T1"},
    {"Type": "Supplier", "Account Reference": "BOLT01", "Name": "Bolt & Nut Supplies Ltd",
     "VAT Number": "", "Default Nominal": "5000", "Tax Code": "T1"},
    {"Type": "Supplier", "Account Reference": "GLOBEX", "Name": "Globex Corporation",
     "VAT Number": "", "Default Nominal": "7500", "Tax Code": ""},
    {"Type": "Client", "Account Reference": "CONT01", "Name": "Contoso Retail plc",
     "VAT Number": "", "Default Nominal": "4000", "Tax Code": "T1"},
])


# ----------------------------------------------------------------- fakes
class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        return self._payload


class FakeFxSession:
    """Frankfurter stand-in: GBP->USD 1.3201 on every business day."""

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        path = url.rsplit("/", 1)[-1]
        if ".." in path:
            start, end = (date.fromisoformat(p) for p in path.split(".."))
            rates, day = {}, start
            while day <= end:
                if day.weekday() < 5:
                    rates[day.isoformat()] = {"USD": 1.3201}
                day = date.fromordinal(day.toordinal() + 1)
            return FakeResponse(200, {"base": "GBP", "rates": rates})
        return FakeResponse(200, {"base": "GBP", "date": "2026-09-15" if path != "latest" else "2026-10-20",
                                  "rates": {"USD": 1.3201}})


class FakeOcr:
    name = "fake-ocr"

    def __init__(self, lines):
        self.lines = lines

    def available(self):
        return True

    def recognize(self, png):
        assert png[:4] == b"\x89PNG"
        return "\n".join(self.lines), 0.9


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def extract(self, text, dayfirst=True):
        self.calls += 1
        return fields_from_llm(self.reply, text, dayfirst=dayfirst)

    def available(self):
        return True


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "x.db")
    database.import_entities(MASTER)
    database.set_settings({"own_company_name": "Northwind Trading Ltd", "own_vat_number": "GB 987 6543 21"})
    return database


@pytest.fixture
def pipeline(db):
    fx = FxService(db, session=FakeFxSession(), today=TODAY)
    return ExtractionPipeline.from_db(db, fx=fx, ocr_engine=None)


# ------------------------------------------------------------ native text
def test_native_text_and_rendering(pdf):
    data = pdf(PURCHASE_INVOICE, extra_pages=[["Page two terms"]])
    doc = extract_native(data)
    assert doc.page_count == 2 and doc.method == "native" and doc.quality > 0.9
    assert "Invoice No: INV-10452" in doc.text
    assert page_count(data) == 2
    assert render_page_png(data, 1)[:4] == b"\x89PNG"
    with pytest.raises(ExtractionError):
        extract_native(b"not a pdf")


# ------------------------------------------------------------------ parsers
@pytest.mark.parametrize("line,expected", [
    ("Total Due £1,500.00", [D("1500.00")]), ("Grand Total (55.20)", [D("-55.20")]),
    ("Deposit £1,200", [D("1200")]), ("VAT @ 20% 250.00", [D("250.00")]),
    ("Period 01.09.2026", []), ("Total 1.234,56", [D("1234.56")]), ("Balance 12.00 CR", [D("-12.00")]),
])
def test_find_amounts(line, expected):
    assert find_amounts(line) == expected


@pytest.mark.parametrize("line,kind", [
    ("Subtotal 100.00", "net"), ("Total excl. VAT 100.00", "net"), ("VAT @ 20% 20.00", "vat"),
    ("Total incl. VAT 120.00", "gross"), ("Amount due 120.00", "gross"), ("Amount paid 120.00", None),
    ("Total VAT 20.00", "vat"), ("Office rent 100.00", None),
])
def test_classify_line(line, kind):
    assert classify_line(line) == kind


def test_invoice_number_unglued_from_neighbouring_label():
    from localledger.extraction.parsers import parse_invoice_number

    assert parse_invoice_number(["Invoice No: BN-7781Invoice To:"]) == ("BN-7781", 0.95)
    assert parse_invoice_number(["Invoice No: INV-TO2026"])[0] == "INV-TO2026"     # letters before digits kept


def test_amounts_prefers_reconciled_total():
    res = parse_amounts(["Subtotal 1,000.00", "VAT 200.00", "Total 1,200.00", "Amount paid 1,200.00",
                         "Balance due 0.00"])
    assert (res.net, res.vat, res.gross, res.reconciled) == (D("1000.00"), D("200.00"), D("1200.00"), True)


def test_amounts_inferred_from_rate():
    res = parse_amounts(["Total 120.00"], vat_rate=D("20"))
    assert (res.net, res.vat, res.gross) == (D("100.00"), D("20.00"), D("120.00"))
    assert res.confidence[F.NET] < 0.6


def test_parse_sales_invoice_text(pdf):
    f = parse_text(extract_native(pdf(SALES_INVOICE)).text)
    assert f.invoice_number == "SI-2026-118" and f.invoice_date == date(2026, 10, 12)
    assert (f.net, f.vat, f.gross) == (D("2000.00"), D("400.00"), D("2400.00")) and f.amounts_reconciled
    assert f.issuer_name == "Northwind Trading Ltd" and f.recipient_name == "Contoso Retail plc"
    assert f.vat_rate == D("20")


# ------------------------------------------------------------------ pipeline
def test_purchase_invoice_end_to_end(pipeline, pdf, db):
    res = pipeline.process_bytes(pdf(PURCHASE_INVOICE), "acme.pdf")
    r = res.row
    assert res.ok and r.doc_type == DocType.PI
    assert (r.account_code, r.entity_match_method, r.nominal_code, r.vat_code) == ("ACME001", "vat", "7100", "T1")
    assert (r.net, r.vat, r.gross) == (D("1250.00"), D("250.00"), D("1500.00"))
    assert r.service_period_start == date(2026, 9, 1)
    assert r.description.startswith("Sep 2026 - Office rent")
    assert len(r.file_hash) == 64 and r.exchange_rate is None
    validate_batch([r], db, today=TODAY)
    assert r.confidence_band == "HIGH", r.issues


def test_sales_invoice_detected_from_own_company(pipeline, pdf):
    r = pipeline.process_bytes(pdf(SALES_INVOICE), "si.pdf").row
    assert r.doc_type == DocType.SI and r.account_code == "CONT01" and r.nominal_code == "4000"
    assert r.vat_reg_no == ""                    # own VAT number is not the client's


def test_usd_credit_note_with_fx(pipeline, pdf):
    res = pipeline.process_bytes(pdf(USD_CREDIT_NOTE), "globex.pdf")
    r = res.row
    assert r.doc_type == DocType.PC and r.currency == "USD"
    assert r.account_code == "GLOBEX" and r.vat_code == "T0"
    assert r.exchange_rate == D("1.3201") and res.fx_quote.source == "api"
    assert r.gross == D("400.00") and r.raw_description == "Refund for returned licences"


def test_math_error_stays_low_even_with_llm(db, pdf):
    llm = FakeLLM({"supplier_name": "Bolt & Nut Supplies Limited", "invoice_number": "BN-7781",
                   "invoice_date": "2026-10-05", "currency": "GBP", "net_total": 500, "vat_total": 100,
                   "gross_total": 600, "description": "M8 bolts"})
    pipe = ExtractionPipeline.from_db(db, fx=None, ocr_engine=None, llm_client=llm)
    pipe.options.llm_enabled = True
    res = pipe.process_bytes(pdf(MATH_ERROR_INVOICE), "bolt.pdf")
    r = res.row
    assert llm.calls == 1                         # amounts did not reconcile -> fallback ran
    assert r.gross == D("650.00")                 # ungrounded LLM "fix" rejected
    validate_batch([r], db, today=TODAY)
    assert r.confidence_band == "LOW" and F.GROSS in r.error_fields


def test_llm_fills_missing_invoice_number(db, pdf):
    lines = [ln for ln in PURCHASE_INVOICE if not ln.startswith("Invoice No")] + ["Our ref ZX-99812"]
    llm = FakeLLM({"supplier_name": "ACME Property Services Ltd", "invoice_number": "ZX-99812",
                   "invoice_date": "2026-10-03", "currency": "GBP", "net_total": 1250, "vat_total": 250,
                   "gross_total": 1500})
    pipe = ExtractionPipeline.from_db(db, fx=None, ocr_engine=None, llm_client=llm)
    pipe.options.llm_enabled = True
    res = pipe.process_bytes(pdf(lines), "acme2.pdf")
    assert res.row.invoice_number == "ZX-99812" and res.row.field_confidence[F.INVOICE_NUMBER] == 0.85
    assert any("LLM" in entry for entry in res.log)


def test_llm_not_called_when_regex_complete(db, pdf):
    llm = FakeLLM({})
    pipe = ExtractionPipeline.from_db(db, fx=None, ocr_engine=None, llm_client=llm)
    pipe.options.llm_enabled = True
    pipe.process_bytes(pdf(PURCHASE_INVOICE), "acme.pdf")
    assert llm.calls == 0


def test_scanned_pdf_without_and_with_ocr(db, scanned_pdf):
    data = scanned_pdf(PURCHASE_INVOICE)
    no_ocr = ExtractionPipeline.from_db(db, fx=None, ocr_engine=None)
    no_ocr.options.ocr_engine = "none"
    res = no_ocr.process_bytes(data, "scan.pdf")
    assert res.ok and res.row.gross is None and any("scanned" in w for w in res.warnings)

    with_ocr = ExtractionPipeline.from_db(db, fx=None, ocr_engine=FakeOcr(PURCHASE_INVOICE))
    res = with_ocr.process_bytes(data, "scan.pdf")
    r = res.row
    assert r.extraction_method == "ocr" and r.extraction_quality == 0.9
    assert r.account_code == "ACME001" and r.gross == D("1500.00")


def test_process_many_survives_bad_files(pipeline, pdf):
    results = pipeline.process_many([("a.pdf", pdf(PURCHASE_INVOICE)), ("broken.pdf", b"%PDF-1.4 garbage"),
                                     ("c.pdf", pdf(USD_CREDIT_NOTE))])
    assert [r.ok for r in results] == [True, False, True]
    assert results[2].row.exchange_rate == D("1.3201")


# ------------------------------------------------------------------ VAT codes
def test_infer_vat_code(db):
    codes = db.vat_code_map()
    assert infer_vat_code(codes, printed_rate=None, net=D("100"), vat=D("20"), current="T1")[0] == "T1"
    code, why = infer_vat_code(codes, printed_rate=None, net=D("100"), vat=D("5"), current="T1")
    assert code == "T5" and "does not match" in why
    assert infer_vat_code(codes, printed_rate=None, net=D("100"), vat=D("0"))[0] == "T0"
    assert infer_vat_code(codes, printed_rate=None, net=D("100"), vat=D("0"), current="T8")[0] == "T8"


# ------------------------------------------------------------------ LLM client
def test_llm_endpoint_locality():
    assert is_local_endpoint("http://localhost:11434/v1")
    assert is_local_endpoint("http://192.168.1.20:1234/v1")
    assert not is_local_endpoint("https://api.example.com/v1")
    with pytest.raises(EngineUnavailable):
        LocalLLMClient(LLMConfig(endpoint="https://api.example.com/v1"), session=object())


def test_llm_client_parses_fenced_json():
    reply = {"choices": [{"message": {"content": "Sure!\n```json\n" + json.dumps(
        {"document_type": "credit_note", "supplier_name": "ACME", "invoice_number": "CN-1",
         "invoice_date": "2026-10-01", "net_total": 10, "vat_total": 2, "gross_total": 12}) + "\n```"}}]}

    class Session:
        def post(self, url, json=None, headers=None, timeout=None):
            assert url.endswith("/chat/completions") and json["temperature"] == 0
            return FakeResponse(200, reply)

    fields = LocalLLMClient(LLMConfig(), session=Session()).extract("ACME credit note CN-1 total 12.00")
    assert fields.is_credit_hint and fields.invoice_number == "CN-1" and fields.amounts_reconciled
    assert fields.confidence[F.GROSS] > fields.confidence[F.NET]     # 12.00 is in the text, 10.00 is not
