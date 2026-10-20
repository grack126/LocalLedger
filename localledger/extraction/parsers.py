"""Regex/heuristic field extraction from invoice text.

The approach is label-driven with an accounting cross-check:
* every line holding a money amount is classified as a net / VAT / gross
  summary line by its label ("Subtotal", "VAT @ 20%", "Amount due" ...);
* the combination of candidates where Net + VAT = Gross wins, which
  disambiguates documents that print several "Total" lines;
* anything still missing is inferred from the other amounts and the VAT rate,
  with a lower field confidence.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..models import CENT, F, ISO_CURRENCIES, normalize_vat_number, parse_date, to_decimal
from ..rules import DATE_PATTERN
from .base import ExtractedFields

TOLERANCE = Decimal("0.02")

# =============================================================================
# Money
# =============================================================================
_NUM = r"(?:\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2}|\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})"
_MONEY_RE = re.compile(
    r"(?<![\w.,/\-])(?P<tok>"
    rf"\(\s?[£$€]?\s?{_NUM}\s?\)"                       # (1,234.56)
    rf"|-\s?[£$€]?\s?{_NUM}(?:\s?CR\b)?"                # -£12.00
    rf"|[£$€]\s?-?\s?{_NUM}(?:\s?CR\b)?"                # £-12.00 / £12.00 CR
    rf"|{_NUM}(?:\s?CR\b)?"                             # 12.00
    r"|[£$€]\s?\d{1,3}(?:,\d{3})*(?![.,]?\d)"           # £1,200 (symbol required without decimals)
    r")(?![.,]?\d)(?!\s?%)"
)


def find_amounts(line: str) -> list[Decimal]:
    out = []
    for match in _MONEY_RE.finditer(line):
        value = to_decimal(match.group("tok"))
        if value is not None:
            out.append(value.quantize(CENT))
    return out


# =============================================================================
# Summary-line labels
# =============================================================================
_NET_LABEL = re.compile(
    r"\b(?:sub[\s\-]?total|net(?:\s+(?:total|amount|value|invoice\s+total))?|total\s+net|goods\s+(?:total|value)|"
    r"(?:total|amount)\s+(?:excl?\.?|excluding|exclusive\s+of|before|ex\.?)\s*(?:vat|tax|gst))\b",
    re.I)
_VAT_LABEL = re.compile(r"\b(?:total\s+)?(?:vat|v\.a\.t\.?|tax|gst|sales\s+tax)\b", re.I)
_VAT_EXCLUDE = re.compile(
    r"\b(?:incl?\.?|including|inclusive\s+of|excl?\.?|excluding|exclusive\s+of|before|ex\.?)\s*(?:vat|tax)\b|"
    r"\b(?:vat|tax)\s*(?:reg(?:istration|istered)?|no\b|number|id\b|#|exempt\s+no)", re.I)
_GROSS_LABEL = re.compile(
    r"\b(?:grand\s+total|invoice\s+total|total\s+(?:due|payable|amount|to\s+pay|incl?\.?|including|inc\.?|gbp|eur|usd)|"
    r"amount\s+(?:due|payable|to\s+pay|credited)|balance\s+(?:due|outstanding|to\s+pay)|total\s+credit|credit\s+total|total)\b",
    re.I)
_PAID_LABEL = re.compile(r"\b(?:amount\s+paid|paid|payments?\s+received|deposit)\b", re.I)


def classify_line(line: str) -> str | None:
    """'net' | 'vat' | 'gross' | None for a summary line."""
    if _PAID_LABEL.search(line):
        return None
    if _NET_LABEL.search(line):
        return "net"
    if _VAT_LABEL.search(line) and not _VAT_EXCLUDE.search(line):
        return "vat"
    if _GROSS_LABEL.search(line):
        return "gross"
    return None


@dataclass
class AmountResult:
    net: Decimal | None = None
    vat: Decimal | None = None
    gross: Decimal | None = None
    reconciled: bool = False
    confidence: dict[str, float] = field(default_factory=dict)
    first_summary_line: int | None = None
    notes: list[str] = field(default_factory=list)


def parse_amounts(lines: list[str], vat_rate: Decimal | None = None) -> AmountResult:
    from ..validator import infer_missing_amounts

    cands: dict[str, list[Decimal]] = {"net": [], "vat": [], "gross": []}
    first_summary: int | None = None
    all_amounts: list[Decimal] = []
    for idx, line in enumerate(lines):
        amounts = find_amounts(line)
        if not amounts:
            continue
        all_amounts += amounts
        kind = classify_line(line)
        if kind:
            cands[kind].append(amounts[-1])          # rightmost amount on the line
            if first_summary is None:
                first_summary = idx
    if any(g != 0 for g in cands["gross"]):
        cands["gross"] = [g for g in cands["gross"] if g != 0]

    res = AmountResult(first_summary_line=first_summary)

    # 1. A full triple that adds up (search bottom-up: summaries sit at the end).
    for g in reversed(cands["gross"]):
        for n in reversed(cands["net"]):
            for v in reversed(cands["vat"]):
                if abs(abs(n) + abs(v) - abs(g)) <= TOLERANCE:
                    res.net, res.vat, res.gross, res.reconciled = n, v, g, True
                    res.confidence = {F.NET: 0.95, F.VAT: 0.95, F.GROSS: 0.95}
                    return res

    # 2. Net == Gross with no VAT line (zero-rated / not VAT registered).
    if not cands["vat"]:
        for g in reversed(cands["gross"]):
            if any(abs(n - g) <= TOLERANCE for n in cands["net"]):
                res.net, res.vat, res.gross, res.reconciled = g, Decimal("0.00"), g, True
                res.confidence = {F.NET: 0.85, F.VAT: 0.7, F.GROSS: 0.85}
                res.notes.append("No VAT line found; net equals gross so VAT taken as 0.")
                return res

    # 3. Best effort: last labelled value of each, then infer the rest.
    net = cands["net"][-1] if cands["net"] else None
    vat = cands["vat"][-1] if cands["vat"] else None
    gross = cands["gross"][-1] if cands["gross"] else None
    conf = {F.NET: 0.6 if net is not None else 0.0, F.VAT: 0.6 if vat is not None else 0.0,
            F.GROSS: 0.6 if gross is not None else 0.0}
    if gross is None and net is None and all_amounts:
        gross = max(all_amounts, key=abs)
        conf[F.GROSS] = 0.4
        res.notes.append("No labelled totals; the largest amount was taken as the gross total.")
    net, vat, gross, inferred = infer_missing_amounts(net, vat, gross, vat_rate)
    for name in inferred:
        conf[name] = 0.55
    if inferred:
        res.notes.append("Inferred " + ", ".join(inferred) + " from the other amounts.")
    res.net, res.vat, res.gross = net, vat, gross
    res.reconciled = None not in (net, vat, gross) and abs(net + vat - gross) <= TOLERANCE  # type: ignore[operator]
    res.confidence = conf
    return res


# =============================================================================
# Invoice number & dates
# =============================================================================
_DATE_RE = re.compile(DATE_PATTERN, re.I)
_INV_LABEL = r"(?:invoice|inv|credit\s*note|credit\s*memo|cn|bill|document|doc|receipt)"
_INV_NO_RE = re.compile(
    rf"\b{_INV_LABEL}\.?\s*(?:no\.?|number|num\.?|nr\.?|#|ref(?:erence)?\.?|id)(?![a-z])\s*[:#.\-]?[ \t]*"
    r"(?P<v>[A-Z0-9][A-Z0-9\-/_.]*[A-Z0-9]|[A-Z0-9])?", re.I)
_INV_SIMPLE_RE = re.compile(rf"\b(?:invoice|credit\s*note)\s*[:#][ \t]*(?P<v>[A-Z0-9][A-Z0-9\-/_.]*[A-Z0-9])", re.I)
_REFERENCE_RE = re.compile(r"\breference\s*[:#][ \t]*(?P<v>[A-Z0-9][A-Z0-9\-/_.]*[A-Z0-9])", re.I)
_LABEL_WORDS = {"DATE", "DATED", "NO", "NUMBER", "PAGE", "TAX", "VAT", "TOTAL", "DUE", "REF"}
# A neighbouring label glued onto the number by column merging: "BN-7781Invoice" -> "BN-7781"
_GLUED_LABEL = re.compile(
    r"(?<=\d)(?:Invoice|Date|Dated|Due|Total|Page|Account|Ref|Reference|Terms|VAT|Order|Customer|"
    r"To|Bill|Tax|Amount|PO)$")


def _unglue(value: str) -> str:
    return _GLUED_LABEL.sub("", value)


def _valid_number(value: str | None) -> bool:
    if not value or value.upper() in _LABEL_WORDS or not any(c.isdigit() for c in value):
        return False
    if _DATE_RE.fullmatch(value) or _MONEY_RE.fullmatch(value):
        return False
    return 1 <= len(value) <= 40


def parse_invoice_number(lines: list[str]) -> tuple[str, float]:
    for idx, line in enumerate(lines[:60]):
        match = _INV_NO_RE.search(line)
        if not match:
            continue
        value = _unglue((match.group("v") or "").strip(".-/"))
        if _valid_number(value):
            return value, 0.95
        # value printed on the next line (table-style header)
        if idx + 1 < len(lines):
            for token in lines[idx + 1].split():
                token = token.strip(".,:;")
                if _valid_number(token):
                    return token, 0.75
    for regex, conf in ((_INV_SIMPLE_RE, 0.85), (_REFERENCE_RE, 0.5)):
        for line in lines[:60]:
            match = regex.search(line)
            if match and _valid_number(match.group("v")):
                return match.group("v").strip(".-/"), conf
    return "", 0.0


_DATE_LABEL_RE = re.compile(
    r"\b(?P<label>invoice\s+date|date\s+of\s+(?:issue|invoice|supply)|issue\s+date|issued(?:\s+on)?|"
    r"tax\s+point(?:\s+date)?|credit\s+note\s+date|document\s+date|date)\s*[:\-]?\s*"
    rf"(?P<d>{DATE_PATTERN})", re.I)
_DUE_RE = re.compile(rf"\b(?:due\s+date|payment\s+due|due\s+(?:on|by)?)\s*[:\-]?\s*(?P<d>{DATE_PATTERN})", re.I)
_NOT_INVOICE_DATE = re.compile(r"(?:due|payment|delivery|order|period|start|end|from|until|despatch|dispatch|valid)\W*$",
                               re.I)
_LABEL_RANK = {"invoice date": 1.0, "tax point": 0.9, "tax point date": 0.9, "date of issue": 1.0,
               "issue date": 1.0, "credit note date": 1.0, "document date": 0.9, "date of invoice": 1.0,
               "date of supply": 0.85, "issued": 0.85, "issued on": 0.85, "date": 0.75}


def parse_invoice_date(text: str, *, dayfirst: bool = True) -> tuple[date | None, float]:
    best: tuple[float, int, date] | None = None
    for match in _DATE_LABEL_RE.finditer(text):
        prefix = text[max(0, match.start() - 14):match.start()]
        if _NOT_INVOICE_DATE.search(prefix):
            continue
        parsed = parse_date(match.group("d"), dayfirst=dayfirst)
        if not parsed:
            continue
        label = re.sub(r"\s+", " ", match.group("label").lower())
        rank = _LABEL_RANK.get(label, 0.7)
        if best is None or rank > best[0]:
            best = (rank, match.start(), parsed)
    if best:
        return best[2], round(0.95 * best[0], 2)
    for line in text.splitlines()[:30]:
        if re.search(r"\b(?:due|period|from|until|delivery|order)\b", line, re.I):
            continue
        match = _DATE_RE.search(line)
        if match:
            parsed = parse_date(match.group(0), dayfirst=dayfirst)
            if parsed:
                return parsed, 0.6
    return None, 0.0


def parse_due_date(text: str, *, dayfirst: bool = True) -> date | None:
    match = _DUE_RE.search(text)
    return parse_date(match.group("d"), dayfirst=dayfirst) if match else None


# =============================================================================
# Currency, VAT rate, VAT numbers
# =============================================================================
_SYMBOLS = {"£": "GBP", "€": "EUR", "$": "USD"}
_CODE_RE = re.compile(r"\b(" + "|".join(sorted(ISO_CURRENCIES)) + r")\b")


def parse_currency(text: str, base_currency: str = "GBP") -> tuple[str, float]:
    counts: Counter[str] = Counter()
    for symbol, code in _SYMBOLS.items():
        counts[code] += text.count(symbol)
    for match in _CODE_RE.finditer(text):
        counts[match.group(1)] += 2              # explicit codes are stronger evidence
    if counts.get("USD") and any(counts.get(c) for c in ("CAD", "AUD", "NZD", "SGD", "HKD")):
        dollar = max(("CAD", "AUD", "NZD", "SGD", "HKD"), key=lambda c: counts.get(c, 0))
        counts[dollar] += counts.pop("USD")
    counts = Counter({k: v for k, v in counts.items() if v})
    if not counts:
        return base_currency.upper(), 0.5
    (top, n), *rest = counts.most_common()
    confidence = 0.95 if not rest or n >= 2 * rest[0][1] else 0.7
    return top, confidence


_RATE_RE = re.compile(
    r"\b(?:vat|tax|gst)\b[^\n%]{0,25}?(?P<r1>\d{1,2}(?:\.\d{1,2})?)\s*%|(?P<r2>\d{1,2}(?:\.\d{1,2})?)\s*%\s*(?:vat|tax|gst)\b",
    re.I)


def parse_vat_rate(text: str) -> Decimal | None:
    rates = Counter()
    for match in _RATE_RE.finditer(text):
        rate = Decimal(match.group("r1") or match.group("r2"))
        if 0 <= rate <= 30:
            rates[rate] += 1
    return rates.most_common(1)[0][0] if rates else None


_VAT_NO_RE = re.compile(
    r"\b(?:vat|tva|ust|btw|iva|moms)[\s\-]*(?:reg(?:istration|istered)?\.?\s*)?(?:no\.?|number|num|nr\.?|id|#)?"
    r"\s*[:.]?\s*(?P<v>(?:[A-Z]{2}\s?)?[0-9A-Z](?:[ ]?[0-9A-Z]){7,13})", re.I)


def parse_vat_numbers(text: str) -> list[str]:
    found: list[str] = []
    for match in _VAT_NO_RE.finditer(text):
        raw = match.group("v").strip()
        norm = normalize_vat_number(raw)
        if sum(c.isdigit() for c in norm) >= 7 and norm not in found:
            found.append(norm)
    return found


# =============================================================================
# Parties (issuer / recipient)
# =============================================================================
_RECIPIENT_LABEL = re.compile(
    r"^(?:(?:bill(?:ed)?|invoice[d]?|sold|deliver(?:y)?|ship)\s+to|customer|client|attn|attention|to\s*:)"
    r"\s*:?\s*(?P<rest>.*)$", re.I)
_ISSUER_LABEL = re.compile(r"^(?:from|supplier|seller|vendor|issued\s+by)\s*:\s*(?P<rest>.+)$", re.I)
_TITLE_ONLY = re.compile(
    r"^(?:tax\s+|vat\s+|sales\s+)?(?:invoice|credit\s+note|credit\s+memo|statement|receipt|bill|"
    r"purchase\s+order|remittance(?:\s+advice)?)\b", re.I)
_TITLE_TAIL = re.compile(
    r"(?:^|\s+)(?:tax\s+|vat\s+|sales\s+)?(?:invoice|credit\s+note|credit\s+memo|receipt|statement)\b.*$", re.I)
_NOISE = re.compile(
    r"@|www\.|https?://|\b(?:tel|telephone|phone|mob|mobile|fax|email|e-mail|vat|reg(?:istered|istration)?|"
    r"company\s+(?:no|number)|page|date|due|sort\s+code|iban|swift|bic|account|qty|quantity|description|"
    r"amount|total|terms|invoice\s+(?:no|number)|order|po\b|reference|ref\b)\b", re.I)
_ADDRESS = re.compile(
    r"\b(?:street|st\.|road|rd\.?|lane|avenue|ave\.?|house|unit|floor|suite|building|park|way|close|drive|"
    r"court|place|square|industrial|estate|po\s+box|united\s+kingdom|england|scotland|wales)\b|"
    r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b|^\d+[a-z]?\s", re.I)
_LEGAL = re.compile(r"\b(?:ltd|limited|plc|llp|llc|inc|gmbh|corp|corporation|sarl|bv|pty)\b\.?|\b& co\b", re.I)
_FOOTER_COMPANY = re.compile(
    r"^(?P<name>.{2,60}?\b(?:ltd|limited|plc|llp|llc|inc|gmbh)\b\.?)[\s,.\-]*(?:is\s+)?(?:registered|company|reg)",
    re.I)


def _clean_candidate(line: str) -> str:
    line = _TITLE_TAIL.sub("", line).strip(" ,.:;-|")
    return re.sub(r"\s+", " ", line)


def _is_candidate(line: str) -> bool:
    if not 2 <= len(line) <= 80:
        return False
    letters = sum(c.isalpha() for c in line)
    if letters < 2 or letters / max(len(line.replace(" ", "")), 1) < 0.5:
        return False
    if _NOISE.search(line) or _ADDRESS.search(line) or _TITLE_ONLY.match(line):
        return False
    if _DATE_RE.search(line) or find_amounts(line):
        return False
    return True


def _score(line: str, idx: int) -> float:
    score = 0.0
    if _LEGAL.search(line):
        score += 3.0
    score += max(0.0, 2.0 - idx * 0.25)
    words = len(line.split())
    if 1 <= words <= 6:
        score += 0.5
    if line.isupper() or line.istitle():
        score += 0.3
    return score


def parse_parties(lines: list[str]) -> tuple[list[str], list[str]]:
    """Return (issuer candidates, recipient candidates), best first."""
    recipient_start: int | None = None
    recipients: list[str] = []
    for idx, line in enumerate(lines[:40]):
        match = _RECIPIENT_LABEL.match(line)
        if match:
            recipient_start = idx
            rest = _clean_candidate(match.group("rest") or "")
            if rest and _is_candidate(rest):
                recipients.append(rest)
            for nxt in lines[idx + 1: idx + 4]:
                cand = _clean_candidate(nxt)
                if _is_candidate(cand):
                    recipients.append(cand)
                    break
            break

    scored: dict[str, float] = {}
    for idx, line in enumerate(lines[:40]):
        label = _ISSUER_LABEL.match(line)
        if label:
            cand = _clean_candidate(label.group("rest"))
            if _is_candidate(cand):
                scored[cand] = scored.get(cand, 0) + 6
            continue
        in_recipient_block = recipient_start is not None and recipient_start <= idx <= recipient_start + 6
        if in_recipient_block or idx > 15:
            continue
        cand = _clean_candidate(line)
        if cand and _is_candidate(cand) and cand not in recipients:
            scored[cand] = max(scored.get(cand, 0), _score(cand, idx))
    for line in lines[-15:]:                      # footer: "ACME Ltd. Registered in England ..."
        match = _FOOTER_COMPANY.match(line.strip())
        if match:
            cand = _clean_candidate(match.group("name"))
            if cand and cand not in recipients:
                scored[cand] = max(scored.get(cand, 0), 2.5)
    issuers = [c for c, _ in sorted(scored.items(), key=lambda kv: -kv[1])]
    return issuers[:6], recipients[:3]


# =============================================================================
# Line items / description
# =============================================================================
_ITEM_HEADER = re.compile(r"\b(?:description|item|details|particulars|service|product)s?\b", re.I)
_ITEM_HEADER_COLS = re.compile(r"\b(?:qty|quantity|amount|price|total|rate|net|unit|hrs|hours)\b", re.I)
_DESC_LABEL = re.compile(r"^(?:description|details|for|re|regarding)\s*:\s*(?P<v>.+)$", re.I)
_NUMBERISH = re.compile(r"^[£$€]?-?\(?[\d,.]+\)?%?$|^x\d+$|^@$", re.I)


def _strip_numbers(line: str) -> str:
    tokens = line.split()
    popped_plain_int = False
    while tokens:
        tok = tokens[-1]
        if find_amounts(tok) or tok.endswith("%") or tok in {"@", "x"} or tok.upper() in ISO_CURRENCIES:
            tokens.pop()
        elif _NUMBERISH.match(tok) and not popped_plain_int:
            tokens.pop()
            popped_plain_int = True
        else:
            break
    return " ".join(tokens).strip(" -:|")


def parse_line_items(lines: list[str], first_summary: int | None) -> list[str]:
    end = first_summary if first_summary is not None else len(lines)
    start = 0
    for idx, line in enumerate(lines[:end]):
        if _ITEM_HEADER.search(line) and _ITEM_HEADER_COLS.search(line):
            start = idx + 1
            break
    else:
        if first_summary is None:
            return []
    items = []
    for line in lines[start:end]:
        if not find_amounts(line) or classify_line(line):
            continue
        if _INV_NO_RE.search(line) or _DATE_LABEL_RE.search(line):
            continue
        desc = _strip_numbers(line)
        if len(desc) >= 3 and sum(c.isalpha() for c in desc) >= 3:
            items.append(desc)
    return items


def build_description(lines: list[str], items: list[str]) -> str:
    if items:
        text = "; ".join(items[:3])
        if len(items) > 3:
            text += f" (+{len(items) - 3} more)"
        return text[:160]
    for line in lines:
        match = _DESC_LABEL.match(line)
        if match:
            return match.group("v").strip()[:160]
    return ""


# =============================================================================
# Entry point
# =============================================================================
def parse_text(text: str, *, dayfirst: bool = True, base_currency: str = "GBP") -> ExtractedFields:
    """Extract all fields from document text."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    f = ExtractedFields(source="regex")
    f.vat_rate = parse_vat_rate(text)
    amounts = parse_amounts(lines, f.vat_rate)
    f.net, f.vat, f.gross, f.amounts_reconciled = amounts.net, amounts.vat, amounts.gross, amounts.reconciled
    f.notes += amounts.notes
    f.invoice_number, inv_conf = parse_invoice_number(lines)
    f.invoice_date, date_conf = parse_invoice_date(text, dayfirst=dayfirst)
    f.due_date = parse_due_date(text, dayfirst=dayfirst)
    f.currency, cur_conf = parse_currency(text, base_currency)
    f.vat_numbers = parse_vat_numbers(text)
    f.issuer_candidates, f.recipient_candidates = parse_parties(lines)
    f.issuer_name = f.issuer_candidates[0] if f.issuer_candidates else ""
    f.recipient_name = f.recipient_candidates[0] if f.recipient_candidates else ""
    f.line_items = parse_line_items(lines, amounts.first_summary_line)
    f.description = build_description(lines, f.line_items)
    f.confidence = {
        **amounts.confidence,
        F.INVOICE_NUMBER: inv_conf,
        F.INVOICE_DATE: date_conf,
        F.CURRENCY: cur_conf,
        F.COMPANY_NAME: 0.8 if f.issuer_name and _LEGAL.search(f.issuer_name) else (0.6 if f.issuer_name else 0.0),
        F.DESCRIPTION: 0.8 if f.line_items else (0.6 if f.description else 0.0),
    }
    return f


def missing_core_fields(f: ExtractedFields) -> list[str]:
    """Fields whose absence should trigger the LLM fallback."""
    missing = []
    if not f.invoice_number:
        missing.append(F.INVOICE_NUMBER)
    if not f.invoice_date:
        missing.append(F.INVOICE_DATE)
    if not f.issuer_name:
        missing.append(F.COMPANY_NAME)
    if not f.amounts_reconciled:
        missing.append("Amounts")
    return missing
