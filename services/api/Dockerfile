FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Workspace layout — the api package depends on shared + connectors + engine
# at runtime (routes.py imports the Bitunix client for the /api/klines proxy
# and the engine's RSI/MACD/ADX indicators for /api/indicators), so the build
# needs all four in the context. Build context is the repo root — enforced by
# railway.json's dockerfilePath = services/api/Dockerfile.
COPY packages/shared/ /app/packages/shared/
COPY connectors/     /app/connectors/
COPY services/engine/ /app/services/engine/
COPY services/api/   /app/services/api/

RUN pip install \
      /app/packages/shared \
      /app/connectors \
      /app/services/engine \
      /app/services/api

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status==200 else 1)"

CMD ["uvicorn", "nexocrypto_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
