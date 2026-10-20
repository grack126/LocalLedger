"""Historical exchange rates for foreign-currency invoices.

Source: Frankfurter (https://frankfurter.dev), a free, key-less API serving
European Central Bank reference rates. Rate convention matches the rest of the
app: **units of document currency per 1 unit of base currency** (GBP->USD
1.32 means 1 GBP = 1.32 USD), so base amount = document amount / rate.

Lookup order for (currency, invoice date):
    1. a manual rate the user entered for that date
    2. a cached API rate for that date
    3. the API (cached afterwards; weekends/holidays return the prior business day)
    4. offline fallback: the nearest stored rate within ``max_stale_days``
    5. nothing -> the validator flags "FX rate missing" for manual entry
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Iterable

from .models import StagingRow, parse_date

if TYPE_CHECKING:
    from .db import Database

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.frankfurter.dev/v1"
SERIES_THRESHOLD = 3       # fetch a date range in one call when a batch needs >= this many dates


class FxError(Exception):
    pass


@dataclass(frozen=True)
class FxQuote:
    currency: str
    base: str
    requested_date: date
    rate_date: date             # date the published rate applies to
    rate: Decimal               # currency units per 1 base unit
    source: str                 # base | manual | cache | api | stale
    note: str = ""

    @property
    def is_stale(self) -> bool:
        return self.source == "stale"

    def describe(self) -> str:
        text = f"1 {self.base} = {self.rate} {self.currency} ({self.source}, {self.rate_date:%d/%m/%Y})"
        return f"{text} — {self.note}" if self.note else text


def _to_rate(value: Any) -> Decimal:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FxError(f"Invalid rate {value!r}") from exc
    if not rate.is_finite() or rate <= 0:
        raise FxError(f"Invalid rate {value!r}")
    return rate


class _MemoryStore:
    """Cache used when no database is supplied (tests, scripts)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def get_fx_rate(self, base: str, currency: str, on: date) -> dict[str, Any] | None:
        for source in ("manual", "api"):
            row = self.rows.get((base, currency, on.isoformat(), source))
            if row:
                return row
        return None

    def nearest_fx_rate(self, base: str, currency: str, on: date, max_days: int) -> dict[str, Any] | None:
        best = None
        for (b, c, _d, source), row in self.rows.items():
            if (b, c) != (base, currency):
                continue
            gap = abs((date.fromisoformat(row["effective_date"]) - on).days)
            key = (gap, row["effective_date"] > on.isoformat(), source != "manual")
            if gap <= max_days and (best is None or key < best[0]):
                best = (key, row)
        return best[1] if best else None

    def save_fx_rate(self, base: str, currency: str, rate_date: date, rate: Decimal, *,
                     effective_date: date | None = None, source: str = "api") -> None:
        self.rows[(base, currency, rate_date.isoformat(), source)] = {
            "base_currency": base, "currency": currency, "rate_date": rate_date.isoformat(),
            "effective_date": (effective_date or rate_date).isoformat(), "rate": str(rate), "source": source}


class FxService:
    def __init__(
        self,
        db: Database | None = None,
        *,
        base_currency: str = "GBP",
        api_url: str = DEFAULT_API_URL,
        enabled: bool = True,
        timeout: float = 8.0,
        max_stale_days: int = 7,
        session: Any = None,
        today: date | None = None,
    ) -> None:
        self.store = db if db is not None else _MemoryStore()
        self.base = base_currency.upper()
        self.api_url = api_url.rstrip("/")
        self.enabled = enabled
        self.timeout = timeout
        self.max_stale_days = max_stale_days
        self._session = session
        self._today = today
        self.last_error: str = ""

    @classmethod
    def from_db(cls, db: Database, **overrides: Any) -> FxService:
        s = db.get_settings()
        kwargs = dict(base_currency=s["base_currency"], api_url=s["fx_api_url"], enabled=s["fx_enabled"],
                      max_stale_days=int(s["fx_max_stale_days"]))
        kwargs.update(overrides)
        return cls(db, **kwargs)

    # ----------------------------------------------------------------- utils
    @property
    def today(self) -> date:
        return self._today or date.today()

    @property
    def session(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.api_url}/{path}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except Exception as exc:
            raise FxError(f"Exchange-rate service unreachable ({exc.__class__.__name__}).") from exc
        if response.status_code == 404:
            raise FxError("No published rate for that date/currency.")
        if response.status_code != 200:
            raise FxError(f"Exchange-rate service returned HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise FxError("Exchange-rate service returned invalid JSON.") from exc

    # ----------------------------------------------------------------- API
    def fetch(self, currency: str, on: date) -> tuple[date, Decimal]:
        """Single-date API call -> (effective date, rate)."""
        currency = currency.upper()
        path = "latest" if on >= self.today else on.isoformat()
        data = self._get(path, {"base": self.base, "symbols": currency})
        rates = data.get("rates") or {}
        if currency not in rates:
            raise FxError(f"{currency} is not published by the ECB reference feed.")
        effective = parse_date(data.get("date")) or on
        return effective, _to_rate(rates[currency])

    def fetch_series(self, currencies: Iterable[str], start: date, end: date) -> dict[str, dict[date, Decimal]]:
        """Time-series call -> {currency: {business_date: rate}}."""
        symbols = ",".join(sorted({c.upper() for c in currencies}))
        # start a week early so a weekend/holiday first date still has a prior business day
        data = self._get(f"{(start - timedelta(days=7)).isoformat()}..{min(end, self.today).isoformat()}",
                         {"base": self.base, "symbols": symbols})
        out: dict[str, dict[date, Decimal]] = {}
        for day, rates in (data.get("rates") or {}).items():
            day_date = parse_date(day)
            if not day_date:
                continue
            for cur, value in rates.items():
                out.setdefault(cur.upper(), {})[day_date] = _to_rate(value)
        return out

    # ----------------------------------------------------------------- lookup
    def quote(self, currency: str, on: date) -> FxQuote | None:
        currency = (currency or "").upper()
        if not currency or currency == self.base:
            return FxQuote(currency or self.base, self.base, on, on, Decimal(1), "base")
        lookup_date = min(on, self.today)

        cached = self.store.get_fx_rate(self.base, currency, on)
        if cached:
            source = "manual" if cached["source"] == "manual" else "cache"
            return FxQuote(currency, self.base, on, date.fromisoformat(cached["effective_date"]),
                           Decimal(cached["rate"]), source)

        note = ""
        if self.enabled:
            try:
                effective, rate = self.fetch(currency, lookup_date)
                self.store.save_fx_rate(self.base, currency, on, rate, effective_date=effective, source="api")
                note = "latest available rate (invoice dated in the future)" if on > self.today else ""
                return FxQuote(currency, self.base, on, effective, rate, "api", note)
            except FxError as exc:
                self.last_error = str(exc)
                note = str(exc)
                log.info("FX lookup failed for %s %s: %s", currency, on, exc)
        else:
            note = "online lookup disabled"

        nearest = self.store.nearest_fx_rate(self.base, currency, on, self.max_stale_days)
        if nearest:
            effective = date.fromisoformat(nearest["effective_date"])
            return FxQuote(currency, self.base, on, effective, Decimal(nearest["rate"]), "stale",
                           f"offline fallback: nearest stored rate ({(on - effective).days:+d} days)"
                           + (f"; {note}" if note else ""))
        return None

    def prefetch(self, pairs: Iterable[tuple[str, date]]) -> int:
        """Warm the cache for many (currency, date) pairs with one series call.
        Returns how many pairs were cached. Errors are swallowed (quote() retries)."""
        needed = sorted({(c.upper(), d) for c, d in pairs
                         if c and d and c.upper() != self.base
                         and not self.store.get_fx_rate(self.base, c.upper(), d)}, key=lambda p: p[1])
        if not self.enabled or len({d for _, d in needed}) < SERIES_THRESHOLD:
            return 0
        try:
            series = self.fetch_series({c for c, _ in needed}, needed[0][1], needed[-1][1])
        except FxError as exc:
            self.last_error = str(exc)
            return 0
        stored = 0
        for currency, on in needed:
            days = series.get(currency) or {}
            prior = [d for d in days if d <= on]
            if not prior:
                continue
            effective = max(prior)
            if (on - effective).days > 7:
                continue
            self.store.save_fx_rate(self.base, currency, on, days[effective], effective_date=effective, source="api")
            stored += 1
        return stored

    def set_manual_rate(self, currency: str, on: date, rate: Decimal | float | str) -> None:
        self.store.save_fx_rate(self.base, currency.upper(), on, _to_rate(rate), source="manual")

    def apply_to_row(self, row: StagingRow, *, overwrite: bool = False) -> FxQuote | None:
        """Fill ``row.exchange_rate`` for a foreign-currency row."""
        if row.currency.upper() == self.base or row.invoice_date is None:
            return None
        if row.exchange_rate is not None and not overwrite:
            return None
        quote = self.quote(row.currency, row.invoice_date)
        if quote:
            row.exchange_rate = quote.rate
        return quote

    def apply_to_rows(self, rows: Iterable[StagingRow]) -> dict[str, FxQuote | None]:
        rows = list(rows)
        self.prefetch((r.currency, r.invoice_date) for r in rows
                      if r.invoice_date and r.exchange_rate is None)
        return {r.row_id: self.apply_to_row(r) for r in rows}
