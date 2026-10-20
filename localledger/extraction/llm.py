"""Optional structured extraction with a *local* LLM.

Talks to any OpenAI-compatible ``/chat/completions`` endpoint: Ollama
(``http://localhost:11434/v1``), LM Studio, llama.cpp server, vLLM, ...
Remote endpoints are refused unless explicitly allowed, so document text
never leaves the machine by accident.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from ..models import money, normalize_invoice_number, parse_date
from .base import EngineUnavailable, ExtractedFields

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 12000

SYSTEM_PROMPT = (
    "You are an accounting data-extraction engine. Read the invoice or credit note text "
    "and return ONLY a JSON object with exactly these keys:\n"
    '  "document_type": "invoice" or "credit_note",\n'
    '  "supplier_name": issuer of the document (string),\n'
    '  "supplier_vat_number": issuer VAT registration number or null,\n'
    '  "customer_name": the billed party (string or null),\n'
    '  "invoice_number": the document number (string),\n'
    '  "invoice_date": "YYYY-MM-DD",\n'
    '  "service_period_start": "YYYY-MM-DD" or null,\n'
    '  "service_period_end": "YYYY-MM-DD" or null,\n'
    '  "currency": ISO 4217 code such as "GBP",\n'
    '  "net_total": number (total before VAT),\n'
    '  "vat_total": number,\n'
    '  "gross_total": number (total including VAT),\n'
    '  "vat_rate_percent": number or null,\n'
    '  "description": short summary of the goods or services (max 60 characters).\n'
    "Use positive numbers without currency symbols or thousands separators. "
    "Use null when a value is not present. Do not invent values."
)


class LLMError(Exception):
    pass


def is_local_endpoint(url: str) -> bool:
    """True for localhost, private-network IPs and *.local hostnames."""
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "host.docker.internal"} or host.endswith((".local", ".localhost", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


@dataclass
class LLMConfig:
    endpoint: str = "http://localhost:11434/v1"
    model: str = "llama3.1:8b"
    timeout: float = 120.0
    allow_remote: bool = False
    api_key: str = "local"          # most local servers ignore it; some require any value


def parse_json_reply(content: str) -> dict[str, Any]:
    """Parse model output, tolerating code fences and leading/trailing chatter."""
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("Model reply did not contain JSON.") from None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model reply was not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise LLMError("Model reply was not a JSON object.")
    return data


def _appears(value: str, text: str) -> bool:
    return bool(value) and normalize_invoice_number(value) in normalize_invoice_number(text)


def _amount_in_text(value: Decimal | None, text: str) -> bool:
    if value is None:
        return False
    plain = f"{abs(value):.2f}"
    with_commas = f"{abs(value):,.2f}"
    return plain in text or with_commas in text


def fields_from_llm(data: dict[str, Any], text: str, *, dayfirst: bool = True) -> ExtractedFields:
    """Convert the model's JSON into ExtractedFields, with confidences lowered
    for values that cannot be found in the source text (hallucination guard)."""
    from ..models import F

    f = ExtractedFields(source="llm")

    def s(key: str) -> str:
        value = data.get(key)
        return "" if value in (None, "null") else str(value).strip()

    f.issuer_name = s("supplier_name")
    f.recipient_name = s("customer_name")
    f.issuer_candidates = [f.issuer_name] if f.issuer_name else []
    f.recipient_candidates = [f.recipient_name] if f.recipient_name else []
    if s("supplier_vat_number"):
        f.vat_numbers = [s("supplier_vat_number")]
    f.invoice_number = s("invoice_number")
    f.invoice_date = parse_date(s("invoice_date"), dayfirst=dayfirst)
    f.service_period_start = parse_date(s("service_period_start"), dayfirst=dayfirst)
    f.service_period_end = parse_date(s("service_period_end"), dayfirst=dayfirst)
    f.currency = s("currency").upper()[:3]
    f.net, f.vat, f.gross = money(data.get("net_total")), money(data.get("vat_total")), money(data.get("gross_total"))
    rate = data.get("vat_rate_percent")
    f.vat_rate = money(rate) if rate not in (None, "") else None
    f.description = s("description")[:120]
    doc_type = s("document_type").lower()
    f.is_credit_hint = True if "credit" in doc_type else (False if doc_type else None)

    if None not in (f.net, f.vat, f.gross):
        f.amounts_reconciled = abs(f.net + f.vat - f.gross) <= Decimal("0.02")  # type: ignore[operator]

    def grounded(ok: bool) -> float:
        return 0.85 if ok else 0.5

    f.confidence = {
        F.COMPANY_NAME: grounded(bool(f.issuer_name) and f.issuer_name.lower()[:12] in text.lower()),
        F.INVOICE_NUMBER: grounded(_appears(f.invoice_number, text)),
        F.INVOICE_DATE: 0.8 if f.invoice_date else 0.0,
        F.CURRENCY: 0.8 if f.currency else 0.0,
        F.DESCRIPTION: 0.7 if f.description else 0.0,
    }
    for name, value in ((F.NET, f.net), (F.VAT, f.vat), (F.GROSS, f.gross)):
        f.confidence[name] = (0.9 if f.amounts_reconciled else 0.6) * (1.0 if _amount_in_text(value, text) else 0.7)
    return f


class LocalLLMClient:
    def __init__(self, config: LLMConfig, *, session: Any = None) -> None:
        if not config.allow_remote and not is_local_endpoint(config.endpoint):
            raise EngineUnavailable(
                f"LLM endpoint {config.endpoint} is not on this machine or local network. "
                "Enable 'Allow remote LLM endpoint' in Settings if you really want to send document text there.")
        self.config = config
        if session is None:
            import requests

            session = requests.Session()
        self.session = session

    @property
    def _base(self) -> str:
        return self.config.endpoint.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    def available(self) -> bool:
        try:
            response = self.session.get(f"{self._base}/models", headers=self._headers(), timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        response = self.session.get(f"{self._base}/models", headers=self._headers(), timeout=5)
        response.raise_for_status()
        return [m.get("id", "") for m in response.json().get("data", [])]

    def extract(self, text: str, *, dayfirst: bool = True) -> ExtractedFields:
        if not text.strip():
            raise LLMError("No text to send to the LLM.")
        prompt_text = text[:MAX_PROMPT_CHARS]
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Document text:\n<<<\n{prompt_text}\n>>>"},
            ],
        }
        try:
            response = self.session.post(f"{self._base}/chat/completions", json=payload,
                                         headers=self._headers(), timeout=self.config.timeout)
        except Exception as exc:
            raise EngineUnavailable(f"Could not reach local LLM at {self.config.endpoint}: {exc}") from exc
        if response.status_code != 200:
            raise LLMError(f"LLM returned HTTP {response.status_code}: {str(response.text)[:200]}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {exc}") from exc
        return fields_from_llm(parse_json_reply(content), text, dayfirst=dayfirst)
