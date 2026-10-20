import sqlite3
from datetime import date
from decimal import Decimal

from test_extraction import FakeFxSession, FakeResponse

from localledger.db import SCHEMA_VERSION, Database
from localledger.fx import FxService
from localledger.models import StagingRow

TODAY = date(2026, 10, 20)


class DownSession:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        raise ConnectionError("offline")


def test_base_currency_is_one():
    q = FxService(session=DownSession(), today=TODAY).quote("GBP", date(2026, 10, 1))
    assert q.rate == 1 and q.source == "base"


def test_api_then_cache(tmp_path):
    session = FakeFxSession()
    fx = FxService(Database(tmp_path / "a.db"), session=session, today=TODAY)
    first = fx.quote("usd", date(2026, 9, 15))
    second = fx.quote("USD", date(2026, 9, 15))
    assert (first.source, second.source) == ("api", "cache")
    assert first.rate == second.rate == Decimal("1.3201") and len(session.calls) == 1


def test_manual_rate_wins_and_survives_offline():
    fx = FxService(session=DownSession(), today=TODAY)
    fx.set_manual_rate("EUR", date(2026, 10, 1), "1.18")
    assert fx.quote("EUR", date(2026, 10, 1)).source == "manual"
    stale = fx.quote("EUR", date(2026, 10, 5))           # offline, 4 days from a stored rate
    assert stale.source == "stale" and stale.rate == Decimal("1.18") and "offline" in stale.note
    assert fx.quote("EUR", date(2026, 10, 30)) is None    # too far from any stored rate


def test_future_invoice_uses_latest():
    session = FakeFxSession()
    q = FxService(session=session, today=TODAY).quote("USD", date(2026, 12, 25))
    assert session.calls[-1].endswith("/latest") and "future" in q.note


def test_http_errors_reported():
    class NotFound:
        def get(self, *a, **k):
            return FakeResponse(404, {"message": "not found"})

    fx = FxService(session=NotFound(), today=TODAY)
    assert fx.quote("XYZ", date(2026, 9, 1)) is None and "No published rate" in fx.last_error


def test_prefetch_series_covers_weekends():
    session = FakeFxSession()
    fx = FxService(session=session, today=TODAY)
    pairs = [("USD", date(2026, 9, 12)), ("USD", date(2026, 9, 14)), ("USD", date(2026, 9, 16))]  # 12th = Saturday
    assert fx.prefetch(pairs) == 3 and len(session.calls) == 1 and ".." in session.calls[0]
    saturday = fx.quote("USD", date(2026, 9, 12))
    assert saturday.source == "cache" and saturday.rate_date == date(2026, 9, 11)   # Friday's rate


def test_apply_to_row_respects_existing_rate():
    fx = FxService(session=FakeFxSession(), today=TODAY)
    row = StagingRow(currency="USD", invoice_date=date(2026, 9, 15))
    assert fx.apply_to_row(row).rate == Decimal("1.3201") and row.exchange_rate == Decimal("1.3201")
    row.exchange_rate = Decimal("1.25")
    assert fx.apply_to_row(row) is None and row.exchange_rate == Decimal("1.25")


def test_disabled_lookup_uses_stored_rates_only(tmp_path):
    db = Database(tmp_path / "d.db")
    db.set_setting("fx_enabled", False)
    session = DownSession()
    fx = FxService.from_db(db, session=session, today=TODAY)
    assert fx.quote("USD", date(2026, 9, 15)) is None and session.calls == 0


def test_migration_from_v1_adds_fx_table(tmp_path):
    path = tmp_path / "old.db"
    Database(path)
    with sqlite3.connect(path) as conn:                    # simulate a database from step 1
        conn.execute("DROP TABLE fx_rates")
        conn.execute("PRAGMA user_version = 1")
    db = Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    db.save_fx_rate("GBP", "USD", date(2026, 9, 15), Decimal("1.3"))
    assert db.get_fx_rate("GBP", "USD", date(2026, 9, 15))["rate"] == "1.3"
