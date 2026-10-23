# rag_ingestion_service's sync/container path, as a container image - the
# counterpart to Dockerfile.lambda (the async path, already deployed/live).
#
# Build it from the REPOSITORY ROOT, not from anywhere else:
#
#     docker build -t rag-ingestion-service:local .
#
# Two stages, same shape as hrb_api_gateway_service's own Dockerfile. No
# spacy/presidio model download - this repo's own pipeline never imports
# guardrails code (that stayed retrieval-side, per CLAUDE.md).

# ---------------------------------------------------------------------------
# Stage 1 - build the virtual environment
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# git is needed here only because requirements.txt pins hrb-common-libs via
# a git+https URL, not a published PyPI package.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 - the image that actually runs
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8300

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 hrbingest

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=hrbingest:hrbingest src ./src

USER hrbingest

EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8300/ping || exit 1

# --app-dir src, not `src.rag_ingestion.main:app` - this repo's own
# src/rag_ingestion/ modules import each other as bare `from rag_ingestion
# import ...` (confirmed by reading main.py directly, same convention
# pytest.ini's own `pythonpath = src` already relies on locally).
CMD ["uvicorn", "rag_ingestion.main:app", "--host", "0.0.0.0", "--port", "8300", "--app-dir", "src"]
