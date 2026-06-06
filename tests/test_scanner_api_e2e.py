"""End-to-end: scanner writes signals through PgStore, API reads them back."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from nexocrypto_api.deps import set_store_for_tests
from nexocrypto_api.main import app
from nexocrypto_api.pg_store import PgStore
from nexocrypto_engine.strategy import EmaAdxTrendParams, EmaAdxTrendStrategy
from nexocrypto_worker import scan_once
from nexocrypto_shared import Kline

from tests.strategy._helpers import flat_series, pullback_then_resume_uptrend


NOW = datetime(2026, 6, 6, 16, 0, tzinfo=timezone.utc)


@dataclass
class _FakeFunding:
    mark_price: Decimal = Decimal("60000")
    funding_rate: Decimal = Decimal("0.0001")


class _FakeSource:
    def __init__(self, series: list[Kline]) -> None:
        self._series = series

    async def klines(self, pair, interval, *, limit=100):
        return self._series[-limit:]

    async def funding(self, pair):
        return _FakeFunding(mark_price=self._series[-1].close)


@pytest.fixture
def pg_store(db_dsn) -> PgStore:
    return PgStore(db_dsn)


@pytest.fixture
def client(pg_store):
    set_store_for_tests(pg_store)
    with TestClient(app) as c:
        yield c


async def test_scanner_writes_through_pgstore_visible_via_api(client, pg_store):
    user = UUID("22222222-2222-2222-2222-222222222222")
    src = _FakeSource(pullback_then_resume_uptrend())

    # Scanner runs end-to-end with persistence enabled.
    result = await scan_once(
        src,
        "BTCUSDT",
        interval="5m",
        bars=300,
        strategies=[(EmaAdxTrendStrategy(), EmaAdxTrendParams(adx_threshold=Decimal("15")))],
        now=NOW,
        store=pg_store,
        user_id=user,
    )
    assert any(o.fired for o in result.outcomes)

    # API now sees what the scanner wrote.
    r = client.get("/api/signals", headers={"X-User-Id": str(user)})
    assert r.status_code == 200
    rows = r.json()
    # parsed signal + validated decision = 2 rows
    statuses = sorted(r["status"] for r in rows)
    assert "parsed" in statuses
    assert ("approved" in statuses) or ("rejected" in statuses)


async def test_scanner_no_writes_when_store_is_none(client, pg_store):
    """Scanner used without a store should NOT persist."""
    user = UUID("33333333-3333-3333-3333-333333333333")
    src = _FakeSource(flat_series(220, price=100))
    await scan_once(src, "BTCUSDT", interval="5m", bars=220, now=NOW)
    r = client.get("/api/signals", headers={"X-User-Id": str(user)})
    assert r.status_code == 200
    assert r.json() == []
