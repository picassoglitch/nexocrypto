from __future__ import annotations

from fastapi import FastAPI

from nexocrypto_shared import get_settings

from .routes import router as api_router

app = FastAPI(title="NexoCrypto API", version="0.0.1")
app.include_router(api_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    s = get_settings()
    return {"status": "ok", "env": s.app_env}
