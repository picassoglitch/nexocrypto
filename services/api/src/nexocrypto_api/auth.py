"""Supabase JWT verification.

Supabase signs user JWTs with HS256 against the project's JWT secret (the same value
exposed as `SUPABASE_JWT_SECRET` in their dashboard). Verifying that signature gives
us the user's UUID from the `sub` claim — no callback to Supabase needed per request.

Two modes:
  - `jwt`  : verify a Bearer token (production)
  - `stub` : trust an `X-User-Id` header (local dev, tests, the dashboard demo)

Selected by `NEXOCRYPTO_AUTH` env var; default `stub` so the existing local flow
keeps working until you flip it.

CLAUDE.md rule 7: the secret never leaves the server and never appears in logs.
"""

from __future__ import annotations

import os
from uuid import UUID

import jwt
from fastapi import Cookie, Header, HTTPException, status


_DEFAULT_MODE = "stub"
_JWT_ALG = "HS256"
_JWT_AUDIENCE = "authenticated"  # Supabase default audience for user tokens


def _mode() -> str:
    return (os.environ.get("NEXOCRYPTO_AUTH") or _DEFAULT_MODE).strip().lower()


def _jwt_secret() -> str:
    secret = os.environ.get("NEXOCRYPTO_SUPABASE_JWT_SECRET")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="NEXOCRYPTO_AUTH=jwt but NEXOCRYPTO_SUPABASE_JWT_SECRET is unset",
        )
    return secret


def _verify_bearer(token: str) -> UUID:
    """Verify a Supabase HS256 JWT. Raise HTTPException 401 on any failure (expired,
    bad signature, missing sub, wrong audience). NEVER raises into the route handler
    with details that leak the secret."""
    secret = _jwt_secret()
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_JWT_ALG],
            audience=_JWT_AUDIENCE,
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="token expired") from e
    except jwt.InvalidAudienceError as e:
        raise HTTPException(status_code=401, detail="wrong audience") from e
    except jwt.InvalidTokenError as e:
        # Bucket every other JWT error under one detail so we don't leak which check failed.
        raise HTTPException(status_code=401, detail="invalid token") from e
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="missing sub")
    try:
        return UUID(str(sub))
    except ValueError as e:
        raise HTTPException(status_code=401, detail="sub is not a uuid") from e


async def current_user_id(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    nxc_session: str | None = Cookie(default=None),
) -> UUID:
    """FastAPI dependency. Resolves user_id from JWT, session cookie, or stub.

    In `jwt` mode (production):
      1. Try nxc_session cookie (minted by /auth/sso). Single hop from a Nexo AI
         "Abrir NexoCrypto" click — best UX, no Bearer juggling on every
         dashboard request.
      2. Fall back to Authorization: Bearer for direct-API callers (CLI, tests).
    In `stub` mode (local dev): X-User-Id header.
    """
    mode = _mode()
    if mode == "jwt":
        if nxc_session:
            # Local import keeps the route layer's import graph small.
            from .sso import verify_session_jwt

            claims = verify_session_jwt(nxc_session)
            sub = claims.get("sub")
            try:
                return UUID(str(sub))
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=401, detail="session sub is not a uuid") from e
        if authorization is None or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail="missing Bearer token or session cookie",
            )
        return _verify_bearer(authorization[7:].strip())
    # stub mode: keep existing X-User-Id behaviour
    if x_user_id is None:
        raise HTTPException(
            status_code=401,
            detail="missing X-User-Id (auth stub; set NEXOCRYPTO_AUTH=jwt in prod)",
        )
    try:
        return UUID(x_user_id)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"X-User-Id must be a uuid: {e}",
        ) from e
