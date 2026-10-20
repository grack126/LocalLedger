"""Shared fixtures: build small PDFs in memory with PyMuPDF."""
from __future__ import annotations

import pytest

from localledger.extraction._pymupdf import fitz


def make_pdf(lines: list[str], *, extra_pages: list[list[str]] | None = None) -> bytes:
    doc = fitz.open()
    for page_lines in [lines, *(extra_pages or [])]:
        page = doc.new_page(width=595, height=842)
        y = 50
        for line in page_lines:
            page.insert_text((50, y), line, fontsize=10)
            y += 14
    return doc.tobytes()


def make_scanned_pdf(lines: list[str]) -> bytes:
    """Image-only PDF (no text layer) — simulates a scanned paper invoice."""
    source = fitz.open(stream=make_pdf(lines), filetype="pdf")
    pix = source[0].get_pixmap(matrix=fitz.Matrix(2, 2))
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, pixmap=pix)
    return doc.tobytes()


PURCHASE_INVOICE = [
    "ACME Property Services Ltd", "12 High Street", "London EC1A 1BB", "VAT Reg No: GB 123 4567 89",
    "TAX INVOICE", "Invoice No: INV-10452", "Invoice Date: 03/10/2026", "Due Date: 02/11/2026",
    "Bill To:", "Northwind Trading Ltd", "5 Market Square",
    "Description Qty Unit Price Amount",
    "Office rent September 2026 1 1,000.00 1,000.00",
    "Service charge 1 250.00 250.00",
    "Billing period: 01/09/2026 - 30/09/2026",
    "Subtotal £1,250.00", "VAT @ 20% £250.00", "Total Due £1,500.00",
    "ACME Property Services Ltd. Registered in England No 01234567",
]

SALES_INVOICE = [
    "Northwind Trading Ltd", "5 Market Square, Leeds LS1 4AB", "VAT No: GB 987 6543 21",
    "INVOICE", "Invoice Number: SI-2026-118", "Date: 12 October 2026",
    "Invoice To:", "Contoso Retail plc", "1 Commerce Way",
    "Description Qty Rate Amount", "Consultancy October 2026 4 500.00 2,000.00",
    "Net Total 2,000.00", "VAT 20% 400.00", "Invoice Total 2,400.00",
]

USD_CREDIT_NOTE = [
    "Globex Corporation Inc", "500 Market Street, San Francisco",
    "CREDIT NOTE", "Credit Note No: CN-0042", "Date: 15/09/2026",
    "Bill To: Northwind Trading Ltd",
    "Description Amount", "Refund for returned licences USD 400.00",
    "Subtotal USD 400.00", "VAT 0% 0.00", "Total Credit USD 400.00",
]

MATH_ERROR_INVOICE = [
    "Bolt & Nut Supplies Limited", "Unit 4 Industrial Estate", "INVOICE",
    "Invoice No: BN-7781", "Invoice Date: 05/10/2026",
    "Description Qty Price Total", "M8 bolts box 10 50.00 500.00",
    "Subtotal 500.00", "VAT 20% 100.00", "Total 650.00",
]


@pytest.fixture
def pdf():
    return make_pdf


@pytest.fixture
def scanned_pdf():
    return make_scanned_pdf
