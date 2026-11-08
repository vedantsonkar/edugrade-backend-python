# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --only-binary=:all: --prefix=/install .

# ── Runner ───────────────────────────────────────────────────────────────────
FROM python:3.14-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    WORKERS=1

RUN useradd --uid 8888 --create-home appuser

COPY --from=builder /install /usr/local
WORKDIR /srv
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser scripts ./scripts/
COPY --chown=appuser:appuser alembic.ini ./

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8080')+'/api/v1/health', timeout=4)"

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS}"]
