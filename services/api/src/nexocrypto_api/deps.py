"""FastAPI dependency injection.

Auth: stub. Real impl verifies a Supabase JWT against SUPABASE_JWT_SECRET and pulls
user_id from the `sub` claim. For now we accept an X-User-Id header — the dashboard /
Telegram bot will set it server-side after auth. Tests use a fixed UUID.

Store selection: NEXOCRYPTO_STORE=memory|pg picks the backend.
  - memory (default): in-process InMemoryStore — good for the dashboard demo
  - pg: PgStore against NEXOCRYPTO_DATABASE_URL (libpq DSN)

CLAUDE.md rule 7: secrets never returned to the client; never logged.
"""

from __future__ import annotations

import os
from uuid import UUID

from fastapi import Header, HTTPException, status

from .store import ApiStore, InMemoryStore


def _build_store() -> ApiStore:
    kind = os.environ.get("NEXOCRYPTO_STORE", "memory").strip().lower()
    if kind == "pg":
        dsn = os.environ.get("NEXOCRYPTO_DATABASE_URL")
        if not dsn:
            raise RuntimeError("NEXOCRYPTO_STORE=pg but NEXOCRYPTO_DATABASE_URL is unset")
        # Imported lazily so the memory path doesn't need psycopg installed.
        from .pg_store import PgStore  # noqa: WPS433
        return PgStore(dsn)
    return InMemoryStore()


_STORE: ApiStore = _build_store()


def get_store() -> ApiStore:
    return _STORE


def set_store_for_tests(store: ApiStore) -> None:
    """Test helper — point the API at a fresh store per test."""
    global _STORE
    _STORE = store


async def get_current_user_id(x_user_id: str | None = Header(default=None)) -> UUID:
    if x_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-User-Id (auth stub; Supabase JWT lands later)",
        )
    try:
        return UUID(x_user_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"X-User-Id must be a uuid: {e}",
        ) from e
