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
        """Insert user row if missing.

        Two modes, switched on NEXOCRYPTO_MANAGE_USERS (default 'true' for local/test):

          true:  Insert into BOTH auth.users and nexocrypto.users (on conflict no-op).
                 Required for local Postgres + tests where the auth shim is applied.

          false: ONLY upsert nexocrypto.users. Supabase production owns auth.users via
                 the managed auth service; app code MUST NOT write there. The user row
                 in auth.users already exists from signup; this method just makes sure
                 the mirror row in nexocrypto.users is there.
        """
        import os  # local import keeps the constructor fast-path uncomplicated

        manage_users = (
            os.environ.get("NEXOCRYPTO_MANAGE_USERS", "true").strip().lower()
            in ("1", "true", "yes", "on")
        )
        if manage_users:
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
    # Real implementation against nexocrypto.approvals (migration 0003).

    _APPROVAL_ACTION_STATUS = {
        "approve": "approved",
        "reject": "rejected",
        "continue": "continued",
        "close": "closed",
        "breakeven": "breakeven",
        "protect": "protected",
    }

    async def list_approvals(self, *, user_id: UUID) -> list[dict]:
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            cur = await conn.execute(
                """
                select id, user_id, signal_id, pair, side, entry, stop_loss, take_profits,
                       leverage, qty, ev_net_bps, liquidation_distance_bps, strategy,
                       idempotency_key, status, resolved_at, resolved_by, resolution_reason,
                       created_at
                  from nexocrypto.approvals
                 where user_id = %s and status = 'pending'
                 order by created_at desc
                """,
                (user_id,),
            )
            return [self._json_safe(r) for r in await cur.fetchall()]
        finally:
            await conn.close()

    async def add_approval(
        self, *, user_id: UUID, signal: Signal, decision: TradeDecision
    ) -> dict:
        """Insert a pending approval. Idempotent on the decision's idempotency_key —
        if the scanner re-runs the same tick, the second call returns the existing row
        instead of raising."""
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            tps = [float(t) for t in signal.take_profits]
            try:
                cur = await conn.execute(
                    """
                    insert into nexocrypto.approvals
                      (user_id, signal_id, pair, side, entry, stop_loss, take_profits,
                       leverage, qty, ev_net_bps, liquidation_distance_bps, strategy,
                       idempotency_key, status)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                    returning id, user_id, signal_id, pair, side, entry, stop_loss,
                              take_profits, leverage, qty, ev_net_bps,
                              liquidation_distance_bps, strategy, idempotency_key, status,
                              created_at
                    """,
                    (
                        user_id,
                        signal.id,
                        signal.pair,
                        signal.side.value,
                        float(signal.entry),
                        float(signal.stop_loss),
                        tps,
                        float(decision.intended_leverage) if decision.intended_leverage else float(signal.leverage),
                        float(decision.intended_qty) if decision.intended_qty else None,
                        float(decision.ev_net_bps) if decision.ev_net_bps is not None else None,
                        float(decision.liquidation_distance_bps) if decision.liquidation_distance_bps is not None else None,
                        signal.strategy,
                        decision.idempotency_key,
                    ),
                )
            except psycopg.errors.UniqueViolation:
                # Dedup on idempotency_key — return the existing row.
                await conn.execute("rollback")  # in case autocommit didn't already
                cur = await conn.execute(
                    """
                    select id, user_id, signal_id, pair, side, entry, stop_loss,
                           take_profits, leverage, qty, ev_net_bps,
                           liquidation_distance_bps, strategy, idempotency_key, status,
                           created_at
                      from nexocrypto.approvals
                     where idempotency_key = %s
                    """,
                    (decision.idempotency_key,),
                )
            row = await cur.fetchone()
            return self._json_safe(row)
        finally:
            await conn.close()

    async def resolve_approval(
        self, *, user_id: UUID, approval_id: UUID, action: str, reason: str | None = None
    ) -> dict | None:
        new_status = self._APPROVAL_ACTION_STATUS.get(action)
        if new_status is None:
            return None
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            cur = await conn.execute(
                """
                update nexocrypto.approvals
                   set status = %s,
                       resolved_at = now(),
                       resolved_by = coalesce(%s, 'human'),
                       resolution_reason = %s
                 where id = %s and user_id = %s and status = 'pending'
                 returning id, user_id, signal_id, pair, side, entry, stop_loss,
                           take_profits, leverage, qty, ev_net_bps, idempotency_key,
                           status, resolved_at, resolved_by, resolution_reason, created_at
                """,
                (new_status, None, reason, approval_id, user_id),
            )
            row = await cur.fetchone()
            return self._json_safe(row) if row else None
        finally:
            await conn.close()

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

    # ── tenants (Nexo AI integration) ─────────────────────────────────────

    async def provision_tenant(
        self,
        *,
        external_user_id: str,
        email: str,
        display_name: str | None,
        tier: str,
    ) -> tuple[dict, bool]:
        """Insert a tenant or return the existing row. Idempotent on
        external_user_id (unique). Returns (tenant, created)."""
        import secrets
        conn = await self._conn()
        try:
            # Check first to know if we created or found.
            cur = await conn.execute(
                """
                select id, external_user_id, email, display_name, tier, status,
                       api_token, created_at, updated_at
                  from nexocrypto.tenants where external_user_id = %s
                """,
                (external_user_id,),
            )
            existing = await cur.fetchone()
            if existing is not None:
                return (self._json_safe(existing), False)
            token = secrets.token_urlsafe(32)
            cur = await conn.execute(
                """
                insert into nexocrypto.tenants
                  (external_user_id, email, display_name, tier, api_token)
                values (%s, %s, %s, %s, %s)
                returning id, external_user_id, email, display_name, tier, status,
                          api_token, created_at, updated_at
                """,
                (external_user_id, email, display_name, tier, token),
            )
            row = await cur.fetchone()
            return (self._json_safe(row), True)
        finally:
            await conn.close()

    async def get_tenant_by_id(self, *, tenant_id: UUID) -> dict | None:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                """
                select id, external_user_id, email, display_name, tier, status,
                       api_token, created_at, updated_at
                  from nexocrypto.tenants where id = %s
                """,
                (tenant_id,),
            )
            row = await cur.fetchone()
            return self._json_safe(row) if row else None
        finally:
            await conn.close()

    async def set_tenant_status(self, *, tenant_id: UUID, status: str) -> dict | None:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                """
                update nexocrypto.tenants
                   set status = %s, updated_at = now()
                 where id = %s
                 returning id, external_user_id, email, display_name, tier, status,
                           api_token, created_at, updated_at
                """,
                (status, tenant_id),
            )
            row = await cur.fetchone()
            return self._json_safe(row) if row else None
        finally:
            await conn.close()

    # ── exchange connections (encrypted at rest) ──────────────────────────

    async def add_exchange_connection(
        self,
        *,
        user_id: UUID,
        exchange: str,
        api_key_enc: bytes,
        api_secret_enc: bytes,
        ip_allowlist: list[str] | None = None,
    ) -> dict:
        """Insert an encrypted exchange connection. The encrypted blobs land in
        api_key_enc / api_secret_enc bytea columns; the response NEVER includes them
        (CLAUDE.md rule 7)."""
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            cur = await conn.execute(
                """
                insert into nexocrypto.exchange_connections
                  (user_id, exchange, api_key_enc, api_secret_enc, ip_allowlist, status)
                values (%s, %s, %s, %s, %s, 'untested')
                returning id, user_id, exchange, ip_allowlist, status, last_tested_at,
                          created_at
                """,
                (user_id, exchange, bytes(api_key_enc), bytes(api_secret_enc), ip_allowlist),
            )
            row = await cur.fetchone()
            return self._json_safe(row)
        finally:
            await conn.close()

    async def list_exchange_connections(self, *, user_id: UUID) -> list[dict]:
        """List connections WITHOUT the encrypted secrets. Same response shape as
        InMemoryStore so the API contract holds. NEVER select api_key_enc here."""
        conn = await self._conn()
        try:
            await self._ensure_user(conn, user_id)
            cur = await conn.execute(
                """
                select id, user_id, exchange, ip_allowlist, status, last_tested_at,
                       created_at
                  from nexocrypto.exchange_connections
                 where user_id = %s
                 order by created_at desc
                """,
                (user_id,),
            )
            return [self._json_safe(r) for r in await cur.fetchall()]
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
