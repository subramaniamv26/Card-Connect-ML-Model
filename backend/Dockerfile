# syntax=docker/dockerfile:1.7
# One image, two runtime targets: `api` (FastAPI) and `worker` (scheduler, training, imports, bulk jobs).
FROM python:3.12.10-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    MODEL_DIR=/models
# ca-certificates: python-oracledb thin mode verifies the database's TLS certificate (TCPS) against them.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data /models && chown app:app /data /models
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
USER app

FROM base AS api
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "app.healthcheck", "api"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM base AS worker
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 CMD ["python", "-m", "app.healthcheck", "worker"]
CMD ["python", "-m", "app.worker"]
