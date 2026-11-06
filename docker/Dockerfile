# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --prefix=/install .

# ── Runner ───────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    WORKERS=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 8888 --create-home appuser

COPY --from=builder /install /usr/local
WORKDIR /srv
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini scripts ./scripts
COPY --chown=appuser:appuser alembic.ini ./

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS "http://localhost:${PORT}/api/v1/health" || exit 1

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS}"]
