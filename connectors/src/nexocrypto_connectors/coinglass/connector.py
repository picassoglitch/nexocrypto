"""Coinglass — DERIVATIVES CONTEXT only (ARCHITECTURE Lane B).

Use cases: open interest, aggregated/weighted funding rates, liquidation heatmaps,
long/short ratios. Cross-venue context, NOT execution-grade.

CLAUDE.md §0.3 is hard about this: Coinglass order books are L2/L3 snapshots with
≤1-min updates on lower tiers, so any book/snapshot from this connector carries
`is_native=False` — these CANNOT be used to gate fills.

Licensing reminder from ARCHITECTURE: ~$29 Hobbyist / $79 Startup / $299 Standard /
$699 Pro. Lower tiers are personal use only. Commercial distribution via nexo-ai.world
needs Standard ($299/mo+).

Endpoints used (v4):
  GET /api/futures/openInterest/ohlc-history    OI OHLC history per symbol
  GET /api/futures/fundingRate/oi-weight-ohlc-history    OI-weighted funding
  GET /api/futures/liquidation/aggregated-history        aggregated liquidations

Auth: CG-API-KEY header.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from ..base import ConnectorError


COINGLASS_BASE = "https://open-api-v4.coinglass.com"


@dataclass(frozen=True)
class OpenInterestRow:
    taken_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class FundingAggregate:
    taken_at: datetime
    weighted_rate: Decimal  # OI-weighted funding rate across exchanges


@dataclass(frozen=True)
class LiquidationBucket:
    taken_at: datetime
    long_liq_usd: Decimal
    short_liq_usd: Decimal


def _from_ms(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


class CoinglassConnector:
    source = "coinglass_context"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = COINGLASS_BASE,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self._base, timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ConnectorError("coinglass: api_key required")
        return {"CG-API-KEY": self._api_key, "Accept": "application/json"}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = await self._client.get(path, params=params, headers=self._headers())
        except httpx.HTTPError as e:
            raise ConnectorError(f"coinglass GET {path} transport error: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(
                f"coinglass GET {path} -> {r.status_code}", status=r.status_code
            )
        payload = r.json()
        code = payload.get("code")
        # Coinglass returns "0" string or 0 int on success
        if str(code) not in ("0", "None"):
            raise ConnectorError(
                f"coinglass code={code} msg={payload.get('msg')!r}", code=str(code)
            )
        return payload.get("data")

    async def open_interest_history(
        self, symbol: str, *, interval: str = "1h", exchange: str = "Binance"
    ) -> list[OpenInterestRow]:
        data = await self._get(
            "/api/futures/openInterest/ohlc-history",
            {"symbol": symbol, "interval": interval, "exchange": exchange},
        )
        out: list[OpenInterestRow] = []
        for row in data or []:
            out.append(
                OpenInterestRow(
                    taken_at=_from_ms(row.get("time", row.get("t", 0))),
                    open=Decimal(str(row.get("open", row.get("o", "0")))),
                    high=Decimal(str(row.get("high", row.get("h", "0")))),
                    low=Decimal(str(row.get("low", row.get("l", "0")))),
                    close=Decimal(str(row.get("close", row.get("c", "0")))),
                )
            )
        return out

    async def funding_oi_weighted_history(
        self, symbol: str, *, interval: str = "1h"
    ) -> list[FundingAggregate]:
        data = await self._get(
            "/api/futures/fundingRate/oi-weight-ohlc-history",
            {"symbol": symbol, "interval": interval},
        )
        out: list[FundingAggregate] = []
        for row in data or []:
            out.append(
                FundingAggregate(
                    taken_at=_from_ms(row.get("time", row.get("t", 0))),
                    weighted_rate=Decimal(str(row.get("close", row.get("c", "0")))),
                )
            )
        return out

    async def liquidations_aggregated_history(
        self, symbol: str, *, interval: str = "1h"
    ) -> list[LiquidationBucket]:
        data = await self._get(
            "/api/futures/liquidation/aggregated-history",
            {"symbol": symbol, "interval": interval},
        )
        out: list[LiquidationBucket] = []
        for row in data or []:
            out.append(
                LiquidationBucket(
                    taken_at=_from_ms(row.get("time", row.get("t", 0))),
                    long_liq_usd=Decimal(str(row.get("longLiquidationUsd", "0"))),
                    short_liq_usd=Decimal(str(row.get("shortLiquidationUsd", "0"))),
                )
            )
        return out
