"""API routes per BUILD_PLAN.

Permission model: every route requires `X-User-Id` (auth stub; Supabase JWT lands later).
Rejected/refused signals always carry the reason — CLAUDE.md "keep the UI honest".
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from nexocrypto_shared import FeeSchedule, Mode, RiskProfile

from .deps import get_current_user_id, get_store
from .store import ApiStore


router = APIRouter(prefix="/api")


# ── signals ────────────────────────────────────────────────────────────────


@router.get("/signals")
async def list_signals(
    status_filter: str | None = Query(default=None, alias="status"),
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    """Status can be one of: parsed | validated | rejected. None = all."""
    return await store.list_signals(user_id=user_id, status=status_filter)


# ── approvals (semi-auto queue) ────────────────────────────────────────────


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str  # approve | reject | continue | close | breakeven | protect
    reason: str | None = None


_ALLOWED_APPROVAL_ACTIONS = {"approve", "reject", "continue", "close", "breakeven", "protect"}


@router.get("/approvals")
async def list_approvals(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    return await store.list_approvals(user_id=user_id)


@router.post("/approvals/{approval_id}/decision")
async def post_approval_decision(
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    if body.action not in _ALLOWED_APPROVAL_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown action {body.action!r}; allowed: {sorted(_ALLOWED_APPROVAL_ACTIONS)}",
        )
    result = await store.resolve_approval(
        user_id=user_id, approval_id=approval_id, action=body.action, reason=body.reason
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    return result


# ── execution ──────────────────────────────────────────────────────────────


@router.get("/positions")
async def list_positions(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    return await store.list_positions(user_id=user_id)


@router.get("/trades")
async def list_trades(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    return await store.list_trades(user_id=user_id)


# ── mode (paper-gate enforced; full_auto disabled in MVP) ──────────────────


class ModePutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Mode


@router.get("/mode")
async def get_mode(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    return await store.get_mode(user_id=user_id)


@router.put("/mode")
async def put_mode(
    body: ModePutRequest,
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    try:
        return await store.set_mode(user_id=user_id, mode=body.mode)
    except PermissionError as e:
        # CLAUDE.md rule 5: paper-before-live enforced in code + DB.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        ) from e


# ── risk profiles ──────────────────────────────────────────────────────────


@router.get("/risk-profiles")
async def get_risk_profile_(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict | None:
    p = await store.get_risk_profile(user_id=user_id)
    return p.model_dump(mode="json") if p else None


@router.put("/risk-profiles")
async def put_risk_profile_(
    profile: RiskProfile,
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    saved = await store.put_risk_profile(user_id=user_id, profile=profile)
    return saved.model_dump(mode="json")


# ── fees (global; CLAUDE.md rule 6: source of truth is the table, never hardcoded) ──


@router.get("/fee-schedules")
async def list_fee_schedules(
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    schedules = await store.list_fee_schedules()
    return [s.model_dump(mode="json") for s in schedules]


@router.put("/fee-schedules")
async def put_fee_schedules(
    schedules: list[FeeSchedule],
    store: ApiStore = Depends(get_store),
    _user_id: UUID = Depends(get_current_user_id),
) -> list[dict]:
    saved = await store.put_fee_schedules(schedules=schedules)
    return [s.model_dump(mode="json") for s in saved]


# ── strategies ─────────────────────────────────────────────────────────────


@router.get("/strategies")
async def list_strategies(
    store: ApiStore = Depends(get_store),
    _user_id: UUID = Depends(get_current_user_id),
) -> list[dict]:
    return await store.list_strategies()


# ── backtests (queue stub; runner integration in Phase 4 backtester) ──────


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: str
    pair: str
    timeframe: str = "5m"
    bars: int = 1500


@router.post("/backtests", status_code=status.HTTP_202_ACCEPTED)
async def queue_backtest(
    body: BacktestRequest,
    _user_id: UUID = Depends(get_current_user_id),
) -> dict:
    # The actual job is enqueued onto Celery in Phase 6 worker integration.
    # For now we return the job description so the dashboard can show 'queued'.
    return {
        "status": "queued",
        "strategy": body.strategy,
        "pair": body.pair,
        "timeframe": body.timeframe,
        "bars": body.bars,
        "optimistic": True,  # CLAUDE.md: backtests are always labelled OPTIMISTIC
    }


# ── SSE telemetry (skeleton — real subscription is wired in Phase 6 worker) ──


@router.get("/klines/{pair}")
async def proxy_klines(
    pair: str,
    interval: str = Query(default="5m"),
    bars: int = Query(default=200, ge=10, le=1500),
    _user_id: UUID = Depends(get_current_user_id),
) -> list[dict]:
    """Proxy Bitunix public klines so the dashboard chart doesn't need CORS or keys.

    Returns rows shaped {time, open, high, low, close} ready for Lightweight Charts.
    """
    from nexocrypto_connectors.bitunix import BitunixConnector  # local to keep startup light

    venue = BitunixConnector()
    try:
        rows = await venue.klines(pair, interval, limit=bars)
    finally:
        await venue.aclose()
    return [
        {
            "time": int(k.open_time.timestamp()),
            "open": float(k.open),
            "high": float(k.high),
            "low": float(k.low),
            "close": float(k.close),
        }
        for k in rows
    ]


@router.get("/stream")
async def stream(
    _user_id: UUID = Depends(get_current_user_id),
) -> StreamingResponse:
    """SSE skeleton — yields a single 'connected' event then idles. Real fan-out from
    the worker happens via Redis pub/sub in the next iteration."""

    async def gen():
        yield b"event: ready\ndata: {\"ok\": true}\n\n"
        # The route exits immediately for now; real impl loops on the Redis sub.
        await asyncio.sleep(0)

    return StreamingResponse(gen(), media_type="text/event-stream")
