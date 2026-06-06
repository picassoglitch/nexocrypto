"""FastAPI dependency injection.

Auth: stub. Real impl verifies a Supabase JWT against SUPABASE_JWT_SECRET and pulls
user_id from the `sub` claim. For now we accept an X-User-Id header — the dashboard /
Telegram bot will set it server-side after auth. Tests use a fixed UUID.

CLAUDE.md rule 7: secrets never returned to the client; never logged.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Header, HTTPException, status

from .store import ApiStore, InMemoryStore


# Module-level singleton for the in-memory store. Swap for a Supabase store in prod.
_STORE: ApiStore = InMemoryStore()


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
