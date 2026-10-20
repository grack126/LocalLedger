#!/usr/bin/env python3
"""Generate sample data so LocalLedger OCR can be tried straight after cloning.

Creates, under ``sample_data/``:

    master_list.csv              clients + suppliers in the app's standard schema
    sage_supplier_export.csv     suppliers in a Sage-style export layout (tests column mapping)
    invoices/
        01_ACME_clean_invoice.pdf          tidy purchase invoice -> HIGH confidence
        02_BOLT_messy_math_error.pdf       messy layout, totals don't add up -> LOW, red cells
        03_GLOBEX_credit_note_USD.pdf      USD credit note -> PC type + exchange rate
        04_ACME_invoice_resent.pdf         byte-identical copy of 01 -> Tier 1 duplicate flag

Optional:
    --load-demo     also import the master list and 6 months of demo history into
                    the app database, so the Analytics page has something to show
    --remove-demo   delete that demo history again (master-list records are kept)

Usage:
    python generate_sample_data.py [--out sample_data] [--load-demo | --remove-demo] [--db PATH]
"""
from __future__ import annotations

import argparse
import csv
import random
import shutil
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OWN_COMPANY = "Northwind Trading Ltd"
OWN_VAT = "GB 987 6543 21"
DEMO_BATCH_PREFIX = "DEMO-"

MASTER_LIST = [
    # Entity Type, Company Name, Account Code, VAT Registration Number, Default Nominal Code, Default VAT Code
    ("Supplier", "ACME Property Services Ltd", "ACME001", "GB123456789", "7100", "T1"),
    ("Supplier", "Bolt & Nut Supplies Ltd", "BOLT01", "", "5000", "T1"),
    ("Supplier", "Globex Corporation", "GLOBEX", "", "7500", ""),
    ("Supplier", "Northern Power Ltd", "POWER1", "GB555666777", "7200", "T5"),
    ("Supplier", "Swift Couriers Ltd", "SWIFT1", "", "7400", "T1"),
    ("Client", "Contoso Retail plc", "CONT01", "GB222333444", "4000", "T1"),
    ("Client", "Fabrikam Industries Ltd", "FABR01", "", "4010", "T1"),
]


# =============================================================================
# Dates relative to today, so validation never flags them as stale
# =============================================================================
def month_bounds(d: date) -> tuple[date, date]:
    start = d.replace(day=1)
    nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def previous_month(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


def business_day(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


@dataclass
class Dates:
    clean: date
    messy: date
    credit: date
    period_start: date
    period_end: date

    @classmethod
    def from_today(cls, today: date | None = None) -> Dates:
        today = today or date.today()
        clean = business_day(today - timedelta(days=10))
        period_start, period_end = month_bounds(previous_month(clean))
        return cls(clean, business_day(today - timedelta(days=8)), business_day(today - timedelta(days=21)),
                   period_start, period_end)


def d(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def money(value: Decimal | float, symbol: str = "£") -> str:
    return f"{symbol}{Decimal(str(value)):,.2f}"


# =============================================================================
# PDF helpers (reportlab)
# =============================================================================
def _canvas(path: Path, title: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)   # invariant -> reproducible bytes
    c.setTitle(title)
    c.setAuthor("LocalLedger OCR sample generator")
    return c, A4


def _text(c, x, y, text, size=10, font="Helvetica", align="left", color=(0, 0, 0)):
    c.setFont(font, size)
    c.setFillColorRGB(*color)
    if align == "right":
        c.drawRightString(x, y, text)
    elif align == "center":
        c.drawCentredString(x, y, text)
    else:
        c.drawString(x, y, text)


def clean_invoice(path: Path, dates: Dates) -> None:
    """A tidy, professionally laid-out purchase invoice."""
    from reportlab.lib.units import mm

    c, (w, h) = _canvas(path, "Invoice INV-10452")
    accent = (0.16, 0.47, 0.84)
    c.setFillColorRGB(*accent)
    c.rect(0, h - 12 * mm, w, 12 * mm, stroke=0, fill=1)
    _text(c, 20 * mm, h - 30 * mm, "ACME Property Services Ltd", 16, "Helvetica-Bold")
    _text(c, w - 20 * mm, h - 30 * mm, "TAX INVOICE", 18, "Helvetica-Bold", "right", accent)
    for i, line in enumerate(["12 High Street", "London EC1A 1BB", "VAT Reg No: GB 123 4567 89"]):
        _text(c, 20 * mm, h - (38 + 5 * i) * mm, line, 9, color=(0.3, 0.3, 0.3))
    meta = [("Invoice No:", "INV-10452"), ("Invoice Date:", d(dates.clean)),
            ("Due Date:", d(dates.clean + timedelta(days=30))), ("Account:", "NWT-001")]
    for i, (label, value) in enumerate(meta):
        _text(c, w - 70 * mm, h - (40 + 5 * i) * mm, label, 9, "Helvetica-Bold")
        _text(c, w - 20 * mm, h - (40 + 5 * i) * mm, value, 9, align="right")

    _text(c, 20 * mm, h - 66 * mm, "Bill To:", 9, "Helvetica-Bold")
    for i, line in enumerate([OWN_COMPANY, "5 Market Square", "Leeds LS1 4AB"]):
        _text(c, 20 * mm, h - (71 + 5 * i) * mm, line, 10 if i == 0 else 9)

    top = h - 100 * mm
    c.setFillColorRGB(0.94, 0.95, 0.97)
    c.rect(20 * mm, top - 2 * mm, w - 40 * mm, 8 * mm, stroke=0, fill=1)
    cols = [(22 * mm, "Description", "left"), (125 * mm, "Qty", "right"), (155 * mm, "Unit Price", "right"),
            (w - 22 * mm, "Amount", "right")]
    for x, label, align in cols:
        _text(c, x, top, label, 9, "Helvetica-Bold", align)
    month = dates.period_start.strftime("%B %Y")
    items = [(f"Office rent {month}", 1, Decimal("1000.00")), ("Service charge", 1, Decimal("250.00"))]
    y = top - 10 * mm
    for desc, qty, price in items:
        _text(c, 22 * mm, y, desc, 10)
        _text(c, 125 * mm, y, str(qty), 10, align="right")
        _text(c, 155 * mm, y, f"{price:,.2f}", 10, align="right")
        _text(c, w - 22 * mm, y, f"{qty * price:,.2f}", 10, align="right")
        y -= 7 * mm
    _text(c, 22 * mm, y - 2 * mm, f"Billing period: {d(dates.period_start)} - {d(dates.period_end)}", 9,
          "Helvetica-Oblique", color=(0.35, 0.35, 0.35))

    net = sum(q * p for _, q, p in items)
    vat = (net * Decimal("0.20")).quantize(Decimal("0.01"))
    y -= 18 * mm
    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.line(120 * mm, y + 6 * mm, w - 20 * mm, y + 6 * mm)
    for label, value, bold in (("Subtotal", net, False), ("VAT @ 20%", vat, False), ("Total Due", net + vat, True)):
        font = "Helvetica-Bold" if bold else "Helvetica"
        _text(c, 125 * mm, y, label, 10, font)
        _text(c, w - 22 * mm, y, money(value), 10, font, "right")
        y -= 7 * mm

    _text(c, 20 * mm, 30 * mm, "Payment by BACS: Barclays Bank, sort code 20-00-00, account 12345678", 8,
          color=(0.4, 0.4, 0.4))
    _text(c, 20 * mm, 25 * mm, "ACME Property Services Ltd. Registered in England No 01234567", 8,
          color=(0.4, 0.4, 0.4))
    c.showPage()
    c.save()


def messy_invoice(path: Path, dates: Dates) -> None:
    """Untidy layout with a supplier arithmetic error (Total should be 600.00, says 650.00)."""
    c, (w, h) = _canvas(path, "inv BN-7781")
    rnd = random.Random(7781)

    def jitter(v: float) -> float:
        return v + rnd.uniform(-2.5, 2.5)

    _text(c, jitter(40), h - 60, "Bolt & Nut Supplies Limited", 15, "Times-Bold")
    _text(c, jitter(40), h - 76, "Unit 4 Industrial Estate, Sheffield S9 2RX", 8, "Times-Roman")
    _text(c, jitter(40), h - 87, "tel 0114 496 0000   orders@boltnut.example", 8, "Courier")
    _text(c, w - 60, h - 64, "INVOICE", 22, "Times-Bold", "right", (0.55, 0.1, 0.1))

    _text(c, jitter(330), h - 120, "Invoice No: BN-7781", 9, "Courier")
    _text(c, jitter(330), h - 133, f"Invoice Date: {d(dates.messy)}", 9, "Courier")
    _text(c, jitter(330), h - 146, "Terms: 14 days", 9, "Courier")
    _text(c, jitter(40), h - 125, "Invoice To:", 9, "Times-Bold")
    _text(c, jitter(40), h - 138, OWN_COMPANY, 9, "Times-Roman")
    _text(c, jitter(40), h - 150, "5 Market Square, Leeds", 9, "Times-Roman")

    y = h - 200
    _text(c, 40, y, "Description Qty Price Total", 9, "Helvetica-Bold")
    lines = [("M8 hex bolts (box of 100)", 10, "25.00", "250.00"),
             ("Washers zinc M8", 500, "0.30", "150.00"),
             ("Delivery", 1, "100.00", "100.00")]
    for desc, qty, price, total in lines:
        y -= 16
        _text(c, jitter(40), y, f"{desc} {qty} {price} {total}", 9, rnd.choice(["Helvetica", "Courier", "Times-Roman"]))

    y -= 40
    for label, value in (("Sub-total", "500.00"), ("VAT 20%", "100.00"), ("TOTAL", "650.00")):
        _text(c, jitter(320), y, f"{label} {value}", 10, "Courier-Bold" if label == "TOTAL" else "Courier")
        y -= 15

    _text(c, 60, 140, "pls pay asap - thanks, Dave", 11, "Times-Italic", color=(0.1, 0.2, 0.6))
    c.saveState()                     # rotated "COPY" stamp
    c.translate(w - 170, 210)
    c.rotate(18)
    c.setStrokeColorRGB(0.8, 0.2, 0.2)
    c.setLineWidth(2)
    c.rect(0, 0, 110, 34)
    _text(c, 55, 10, "COPY", 20, "Helvetica-Bold", "center", (0.8, 0.2, 0.2))
    c.restoreState()
    c.showPage()
    c.save()


def credit_note(path: Path, dates: Dates) -> None:
    """A US-dollar credit note from an overseas supplier."""
    from reportlab.lib.units import mm

    c, (w, h) = _canvas(path, "Credit Note CN-0042")
    _text(c, 20 * mm, h - 25 * mm, "Globex Corporation Inc", 16, "Helvetica-Bold")
    _text(c, 20 * mm, h - 31 * mm, "500 Market Street, San Francisco, CA 94105, USA", 9, color=(0.3, 0.3, 0.3))
    _text(c, w - 20 * mm, h - 25 * mm, "CREDIT NOTE", 18, "Helvetica-Bold", "right", (0.85, 0.35, 0.15))
    meta = [("Credit Note No:", "CN-0042"), ("Date:", d(dates.credit)), ("Original invoice:", "GX-2291")]
    for i, (label, value) in enumerate(meta):
        _text(c, w - 75 * mm, h - (40 + 5 * i) * mm, label, 9, "Helvetica-Bold")
        _text(c, w - 20 * mm, h - (40 + 5 * i) * mm, value, 9, align="right")
    _text(c, 20 * mm, h - 45 * mm, f"Bill To: {OWN_COMPANY}", 10)
    _text(c, 20 * mm, h - 50 * mm, "5 Market Square, Leeds LS1 4AB, United Kingdom", 9)

    top = h - 75 * mm
    _text(c, 22 * mm, top, "Description", 9, "Helvetica-Bold")
    _text(c, w - 22 * mm, top, "Amount (USD)", 9, "Helvetica-Bold", "right")
    _text(c, 22 * mm, top - 9 * mm, "Refund for returned analytics licences (2 seats)", 10)
    _text(c, w - 22 * mm, top - 9 * mm, "$400.00", 10, align="right")
    y = top - 30 * mm
    for label, value, bold in (("Subtotal", "USD 400.00", False), ("VAT 0%", "USD 0.00", False),
                               ("Total Credit", "USD 400.00", True)):
        font = "Helvetica-Bold" if bold else "Helvetica"
        _text(c, 125 * mm, y, label, 10, font)
        _text(c, w - 22 * mm, y, value, 10, font, "right")
        y -= 7 * mm
    _text(c, 20 * mm, 30 * mm, "This credit will be offset against your next invoice. Amounts in US dollars.", 8,
          color=(0.4, 0.4, 0.4))
    c.showPage()
    c.save()


# =============================================================================
# CSVs
# =============================================================================
def write_master_lists(out: Path) -> None:
    with (out / "master_list.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Entity Type", "Company Name", "Account Code", "VAT Registration Number",
                         "Default Nominal Code", "Default VAT Code"])
        writer.writerows(MASTER_LIST)
    with (out / "sage_supplier_export.csv").open("w", newline="", encoding="cp1252") as fh:
        writer = csv.writer(fh)
        writer.writerow(["A/C", "Name", "VAT Reg No", "Def Nom Code", "Tax Code"])
        writer.writerows([(code, name, vat, nom, tax) for kind, name, code, vat, nom, tax in MASTER_LIST
                          if kind == "Supplier"])


def generate(out: Path, today: date | None = None) -> dict[str, Path]:
    out = Path(out)
    invoices = out / "invoices"
    invoices.mkdir(parents=True, exist_ok=True)
    dates = Dates.from_today(today)
    files = {
        "clean": invoices / "01_ACME_clean_invoice.pdf",
        "messy": invoices / "02_BOLT_messy_math_error.pdf",
        "credit": invoices / "03_GLOBEX_credit_note_USD.pdf",
        "duplicate": invoices / "04_ACME_invoice_resent.pdf",
    }
    clean_invoice(files["clean"], dates)
    messy_invoice(files["messy"], dates)
    credit_note(files["credit"], dates)
    shutil.copyfile(files["clean"], files["duplicate"])          # identical bytes, different name
    write_master_lists(out)
    (out / "README.txt").write_text(
        "LocalLedger OCR sample data\n"
        "===========================\n"
        "1. Clients & Suppliers > Import: choose master_list.csv\n"
        "2. Settings > Business: set your company to 'Northwind Trading Ltd'\n"
        "3. Upload & Review: drop the four PDFs from invoices/\n\n"
        "What you should see:\n"
        "  01 clean invoice      HIGH confidence, matched to ACME001 by VAT number\n"
        "  02 messy invoice      LOW confidence: 500.00 + 100.00 is not 650.00 (red cells)\n"
        "  03 credit note        flipped to PC (purchase credit), USD with an exchange rate\n"
        "  04 resent invoice     flagged as a duplicate of 01 (same file, different name)\n",
        encoding="utf-8")
    return files


# =============================================================================
# Demo history for the Analytics page
# =============================================================================
def load_demo(db_path: str | None = None, today: date | None = None) -> int:
    import pandas as pd

    from localledger.db import Database
    from localledger.models import StagingRow

    db = Database(db_path) if db_path else Database()
    db.import_entities(pd.DataFrame(MASTER_LIST, columns=["Entity Type", "Company Name", "Account Code",
                                                          "VAT Registration Number", "Default Nominal Code",
                                                          "Default VAT Code"]))
    if not db.get_setting("own_company_name"):
        db.set_settings({"own_company_name": OWN_COMPANY, "own_vat_number": OWN_VAT})

    rnd = random.Random(2026)
    today = today or date.today()
    last = previous_month(today)
    months = []
    cursor = last
    for _ in range(6):
        months.append(cursor.replace(day=1))
        cursor = previous_month(cursor)
    months.reverse()
    rates = {"T1": Decimal("0.20"), "T5": Decimal("0.05")}
    plan = [  # doc type, account, name, nominal, vat code, base net, prefix
        ("PI", "ACME001", "ACME Property Services Ltd", "7100", "T1", 1250, "INV-"),
        ("PI", "BOLT01", "Bolt & Nut Supplies Ltd", "5000", "T1", 450, "BN-"),
        ("PI", "POWER1", "Northern Power Ltd", "7200", "T5", 320, "NP-"),
        ("PI", "SWIFT1", "Swift Couriers Ltd", "7400", "T1", 180, "SC-"),
        ("SI", "CONT01", "Contoso Retail plc", "4000", "T1", 3600, "SI-"),
        ("SI", "FABR01", "Fabrikam Industries Ltd", "4010", "T1", 1700, "FB-"),
    ]
    rows = []
    for i, month in enumerate(months):
        for doc_type, account, name, nominal, vat_code, base, prefix in plan:
            net = Decimal(base) * Decimal(str(round(rnd.uniform(0.85, 1.2), 2)))
            if account == "POWER1" and month == months[-1]:
                net *= Decimal("2.4")                                   # run-rate alert demo
            net = net.quantize(Decimal("0.01"))
            vat = (net * rates[vat_code]).quantize(Decimal("0.01"))
            day = business_day(month.replace(day=min(28, 3 + 4 * plan.index((doc_type, account, name, nominal,
                                                                                vat_code, base, prefix)))))
            svc_start, svc_end = month_bounds(previous_month(month) if account == "ACME001" else month)
            rows.append(StagingRow(doc_type=doc_type, account_code=account, entity_name=name,
                                   invoice_number=f"{prefix}{month:%y%m}{i}", invoice_date=day, net=net, vat=vat,
                                   gross=net + vat, nominal_code=nominal, vat_code=vat_code,
                                   service_period_start=svc_start, service_period_end=svc_end,
                                   raw_description=f"{name} {month:%b %Y}", extraction_method="manual"))
    rows.append(StagingRow(doc_type="SC", account_code="CONT01", entity_name="Contoso Retail plc",
                           invoice_number="CN-DEMO-1", invoice_date=business_day(months[-1].replace(day=20)),
                           net="400", vat="80", gross="480", nominal_code="4000", vat_code="T1",
                           raw_description="Returned stock", extraction_method="manual"))
    db.log_exported_rows(rows, batch_id=f"{DEMO_BATCH_PREFIX}{today:%Y%m%d}")
    return len(rows)


def remove_demo(db_path: str | None = None) -> int:
    from localledger.db import Database

    db = Database(db_path) if db_path else Database()
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM invoice_logs WHERE batch_id LIKE ?", (f"{DEMO_BATCH_PREFIX}%",))
        return cur.rowcount


# =============================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(ROOT / "sample_data"), help="output folder (default: sample_data)")
    parser.add_argument("--load-demo", action="store_true", help="import the master list + 6 months of demo history")
    parser.add_argument("--remove-demo", action="store_true", help="delete previously loaded demo history")
    parser.add_argument("--db", help="database path (default: data/localledger.db or $LOCALLEDGER_DB)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))

    if args.remove_demo:
        print(f"Removed {remove_demo(args.db)} demo document(s) from history.")
        return 0
    try:
        files = generate(Path(args.out))
    except ImportError:
        print("reportlab is not installed: pip install -r requirements.txt", file=sys.stderr)
        return 1
    print(f"Sample data written to {Path(args.out).resolve()}")
    for path in files.values():
        print(f"  {path.relative_to(Path(args.out))}")
    print("  master_list.csv, sage_supplier_export.csv")
    if args.load_demo:
        print(f"Loaded master list and {load_demo(args.db)} demo history document(s) into the app database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
