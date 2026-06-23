# syntax=docker/dockerfile:1
# Build context MUST be the repo root: `docker build -f support/Dockerfile .`

# ---- build stage: produce wheels from monorepo source ----
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir --upgrade pip wheel
# unified-channel library (the /tmp editable failure mode dies here — baked from source)
COPY python/ ./python/
# unified-support app (only what hatchling needs to build the wheel)
COPY support/pyproject.toml support/README.md ./support/
COPY support/support/ ./support/support/
RUN pip wheel --no-deps -w /wheels ./python ./support

# ---- runtime stage: slim, no build tools ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/vendor
WORKDIR /app
COPY --from=build /wheels /wheels
# Install the library WITH the channel extras, plus the app (pulls aiosqlite/aiohttp/httpx).
RUN UC="$(ls /wheels/unified_channel-*.whl)" \
 && pip install --no-cache-dir "${UC}[telegram,whatsapp]" /wheels/unified_support-*.whl \
 && rm -rf /wheels
# Optional ERP client (top-level module on PYTHONPATH; absent => ERP disabled, app handles it)
COPY support/vendor/ /app/vendor/
# Default knowledge base — a mounted volume can override /app/knowledge
COPY support/knowledge/ /app/knowledge/
RUN useradd -m app \
 && mkdir -p /app/config /app/data \
 && chown -R app:app /app
USER app
EXPOSE 8081 8082 8443
# Liveness = dashboard port accepting connections (auth-agnostic; /support/ returns 401 when healthy)
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(s.connect_ex(('127.0.0.1',8081)))"
ENTRYPOINT ["python", "-m", "support.app", "/app/config/config.yaml"]
