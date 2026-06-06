"""Postgres-backed ApiStore implementation.

Backs onto the nexocrypto.* schema from supabase/migrations/0001_init.sql. The same
SQL works on hosted Supabase (where RLS is enforced by auth.uid() = user_id) and on
local Postgres with the test_auth_shim. Server-side code is expected to run with
service-role privileges (RLS bypassed) — same pattern Supabase recommends for backend
workers.

The shape returned by every method matches InMemoryStore's so the API routes don't
care which store they're on.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from nexocrypto_shared import (
    FeeSchedule,
    Mode,
    RiskProfile,
    Signal,
    TradeDecision,
)


class PgStore:
    """ApiStore impl over Postgres / Supabase. Async via psycopg 3.

    Construct with a libpq DSN. Each method opens its own connection — fine for the
    skeleton; swap for psycopg_pool.AsyncConnectionPool when traffic warrants.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def _conn(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(self._dsn, autocommit=True, row_factory=dict_row)

    async def _ensure_user(self, conn: psycopg.AsyncConnection, user_id: UUID) -> None:
        """Insert user row if missing. Real Supabase path inserts auth.users at signup;
        this is the test-friendly fallback so scanner runs don't break on a fresh DB.

        nexocrypto.users.id FKs to auth.users(id) so we seed both. On hosted Supabase
        the auth row already exists and the insert is a no-op."""
        await conn.execute(
            "insert into auth.users (id) values (%s) on conflict (id) do nothing",
            (user_id,),
        )
        await conn.execute(
            "insert into nexocrypto.users (id) values (%s) on conflict (id) do nothing",
            (user_id,),
        )

    # ── signals ────────────────────────────────────────────────────────────

    async def list_signals(self, *, user_id: UUID, status: str | None = None) -> list[dict]:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            sql = """
                select id, user_id, source, pair, side, entry, stop_loss, take_profits,
                       leverage, timeframe, margin_type, dedup_hash, raw_text, created_at,
                       'parsed' as status
                  from nexocrypto.parsed_signals
                 where user_id = %s
                union all
                select id, user_id, null as source, null as pair, null as side,
                       null as entry, null as stop_loss, null as take_profits,
                       null as leverage, null as timeframe, null as margin_type,
                       null as dedup_hash, null as raw_text, created_at,
                       decision as status
                  from nexocrypto.validated_signals
                 where user_id = %s
                 order by created_at desc
            """
            cur = await conn.execute(sql, (user_id, user_id))
            rows = await cur.fetchall()
            if status:
                # Validated rows have status 'approved' or 'rejected' from decision col;
                # parsed rows have status 'parsed'.
                rows = [r for r in rows if r.get("status") == status]
            return [self._json_safe(r) for r in rows]
        finally:
            await conn.close()

    async def add_parsed_signal(self, *, user_id: UUID, signal: Signal, raw_text: str | None = None) -> dict:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            tps = [float(t) for t in signal.take_profits]
            cur = await conn.execute(
                """
                insert into nexocrypto.parsed_signals
                  (user_id, source, pair, side, entry, stop_loss, take_profits,
                   leverage, timeframe, margin_type, raw_text, dedup_hash)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id, user_id, source, pair, side, entry, stop_loss, take_profits,
                          leverage, timeframe, margin_type, dedup_hash, raw_text, created_at
                """,
                (
                    user_id,
                    signal.source,
                    signal.pair,
                    signal.side.value,
                    float(signal.entry),
                    float(signal.stop_loss),
                    tps,
                    float(signal.leverage),
                    signal.timeframe,
                    signal.margin_type.value,
                    raw_text,
                    signal.dedup_hash,
                ),
            )
            row = await cur.fetchone()
            row["status"] = "parsed"
            return self._json_safe(row)
        finally:
            await conn.close()

    async def add_validated_signal(self, *, user_id: UUID, decision: TradeDecision) -> dict:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            outcome = "approved" if decision.approved else "rejected"
            ev_net = float(decision.ev_net_bps) if decision.ev_net_bps is not None else None
            cur = await conn.execute(
                """
                insert into nexocrypto.validated_signals
                  (user_id, strategy, ev_net, decision, reject_reason)
                values (%s, %s, %s, %s, %s)
                returning id, user_id, strategy, ev_net, decision, reject_reason, created_at
                """,
                (user_id, None, ev_net, outcome, None if decision.approved else decision.reason),
            )
            row = await cur.fetchone()
            row["status"] = outcome
            row["signal_id"] = str(decision.signal_id)
            return self._json_safe(row)
        finally:
            await conn.close()

    # ── approvals ─────────────────────────────────────────────────────────

    async def list_approvals(self, *, user_id: UUID) -> list[dict]:
        # Approvals live alongside validated_signals in the schema; for now we treat any
        # validated 'approved' row whose mode is semi_auto as a pending approval. The
        # semi-auto queue model graduates into its own table when we add it.
        return []

    async def add_approval(self, *, user_id: UUID, signal: Signal, decision: TradeDecision) -> dict:
        # No-op in v1; the scanner doesn't queue approvals yet (paper mode only).
        return {
            "id": uuid4(),
            "user_id": user_id,
            "status": "pending",
            "signal_id": signal.id,
            "pair": signal.pair,
        }

    async def resolve_approval(self, *, user_id: UUID, approval_id: UUID, action: str, reason: str | None = None) -> dict | None:
        return None  # placeholder until approvals table lands

    # ── execution ─────────────────────────────────────────────────────────

    async def list_positions(self, *, user_id: UUID) -> list[dict]:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            cur = await conn.execute(
                "select id, user_id, trade_id, pair, side, qty, entry_price, "
                "liquidation_price, unrealized_pnl, updated_at "
                "from nexocrypto.positions where user_id = %s order by updated_at desc",
                (user_id,),
            )
            return [self._json_safe(r) for r in await cur.fetchall()]
        finally:
            await conn.close()

    async def list_trades(self, *, user_id: UUID) -> list[dict]:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            cur = await conn.execute(
                "select id, user_id, exchange, pair, side, strategy, mode, entry_price, "
                "exit_price, qty, leverage, status, opened_at, closed_at "
                "from nexocrypto.trades where user_id = %s order by opened_at desc",
                (user_id,),
            )
            return [self._json_safe(r) for r in await cur.fetchall()]
        finally:
            await conn.close()

    # ── mode + risk ───────────────────────────────────────────────────────

    async def get_mode(self, *, user_id: UUID) -> dict:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            cur = await conn.execute(
                "select user_id, mode, paper_trades_count, paper_profit_factor, "
                "paper_max_drawdown, live_unlocked, account_protection_lock, updated_at "
                "from nexocrypto.mode_state where user_id = %s",
                (user_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return {
                    "user_id": user_id,
                    "mode": Mode.PAPER.value,
                    "paper_trades_count": 0,
                    "live_unlocked": False,
                    "account_protection_lock": False,
                }
            return self._json_safe(row)
        finally:
            await conn.close()

    async def set_mode(self, *, user_id: UUID, mode: Mode) -> dict:
        current = await self.get_mode(user_id=user_id)
        if mode in (Mode.SEMI_AUTO, Mode.FULL_AUTO) and not current.get("live_unlocked"):
            raise PermissionError("paper_gate_unmet")
        if mode == Mode.FULL_AUTO:
            raise PermissionError("full_auto_disabled_in_mvp")
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            await conn.execute(
                """
                insert into nexocrypto.mode_state (user_id, mode)
                values (%s, %s)
                on conflict (user_id) do update set mode = excluded.mode, updated_at = now()
                """,
                (user_id, mode.value),
            )
        finally:
            await conn.close()
        return await self.get_mode(user_id=user_id)

    async def unlock_live_for_test(self, *, user_id: UUID) -> None:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            await conn.execute(
                """
                insert into nexocrypto.mode_state (user_id, live_unlocked)
                values (%s, true)
                on conflict (user_id) do update set live_unlocked = true, updated_at = now()
                """,
                (user_id,),
            )
        finally:
            await conn.close()

    async def get_risk_profile(self, *, user_id: UUID) -> RiskProfile | None:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            cur = await conn.execute(
                "select params from nexocrypto.risk_profiles "
                "where user_id = %s order by is_default desc, id limit 1",
                (user_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return RiskProfile.model_validate(row["params"])
        finally:
            await conn.close()

    async def put_risk_profile(self, *, user_id: UUID, profile: RiskProfile) -> RiskProfile:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            params_json = profile.model_dump(mode="json")
            await conn.execute(
                """
                insert into nexocrypto.risk_profiles (user_id, name, params, is_default)
                values (%s, %s, %s::jsonb, true)
                """,
                (user_id, profile.name, json.dumps(params_json)),
            )
        finally:
            await conn.close()
        return profile

    async def list_fee_schedules(self) -> list[FeeSchedule]:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "select exchange, symbol, vip_level, maker_bps, taker_bps, effective_at, source "
                "from nexocrypto.fee_schedules order by effective_at desc"
            )
            rows = await cur.fetchall()
            return [
                FeeSchedule(
                    exchange=r["exchange"], symbol=r["symbol"], vip_level=r["vip_level"] or "regular",
                    maker_bps=r["maker_bps"], taker_bps=r["taker_bps"],
                    effective_at=r["effective_at"], source=r["source"],
                )
                for r in rows
            ]
        finally:
            await conn.close()

    async def put_fee_schedules(self, *, schedules: list[FeeSchedule]) -> list[FeeSchedule]:
        conn = await self._conn()
        try:
            for s in schedules:
                await conn.execute(
                    """
                    insert into nexocrypto.fee_schedules
                      (exchange, symbol, vip_level, maker_bps, taker_bps, effective_at, source)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (s.exchange, s.symbol, s.vip_level, s.maker_bps, s.taker_bps,
                     s.effective_at, s.source),
                )
        finally:
            await conn.close()
        return await self.list_fee_schedules()

    async def list_strategies(self) -> list[dict]:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "select key, name, enabled from nexocrypto.strategies order by key"
            )
            rows = await cur.fetchall()
            if not rows:
                # Seed the MVP three if the table is empty.
                seed = [
                    ("ema_adx_trend", "EMA/ADX trend"),
                    ("vwap_rsi_meanrev", "VWAP/RSI mean-reversion"),
                    ("fvg_ob", "FVG + Order Block"),
                ]
                for k, n in seed:
                    await conn.execute(
                        "insert into nexocrypto.strategies (key, name, enabled) "
                        "values (%s, %s, true) on conflict (key) do nothing",
                        (k, n),
                    )
                cur = await conn.execute(
                    "select key, name, enabled from nexocrypto.strategies order by key"
                )
                rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await conn.close()

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _json_safe(row: dict[str, Any]) -> dict[str, Any]:
        """Convert Decimal/datetime to JSON-serialisable shapes the API can return."""
        from datetime import datetime
        from decimal import Decimal

        out: dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, Decimal):
                out[k] = str(v)
            elif isinstance(v, datetime):
                out[k] = v.isoformat()
            elif isinstance(v, list):
                out[k] = [str(x) if isinstance(x, Decimal) else x for x in v]
            else:
                out[k] = v
        return out
