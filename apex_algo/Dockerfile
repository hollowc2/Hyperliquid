# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------
# APEX Algo — multi-stage Docker image using uv
#
# Stages:
#   builder  — installs dependencies into /app/.venv via uv
#   runtime  — copies only the venv + source, no build tools
#
# Build:
#   docker build -t apex-algo .
#
# Run recorder:
#   docker run --env-file .env -v ./data:/app/data apex-algo
# -----------------------------------------------------------------------

# ---- Stage 1: builder ------------------------------------------------
FROM python:3.12-slim AS builder

# Install uv from the official distroless image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# System libs needed to compile some wheels (e.g. cryptographic deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uv settings: compile .pyc files, use copy mode (no hardlinks across layers)
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Copy dependency manifest and lockfile first → better layer caching.
# Only these files need to change to bust the dep-install layer.
COPY pyproject.toml uv.lock* ./

# Install all dependencies into /app/.venv.
# --no-install-project: don't install apex_algo itself yet (source not copied).
# --frozen: fail if uv.lock is out of sync with pyproject.toml.
#           Falls back to uv sync (no --frozen) when uv.lock doesn't exist yet.
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-install-project; \
    else \
        uv sync --no-install-project; \
    fi

# Copy project source
COPY apex_trader/ ./apex_trader/
COPY record_live_data.py build_historical_catalog.py ./

# Install the apex_trader package itself into the venv
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen; \
    else \
        uv sync; \
    fi


# ---- Stage 2: runtime ------------------------------------------------
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only the installed venv and application source — no uv, no build tools
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/apex_trader ./apex_trader/
COPY --from=builder /app/record_live_data.py /app/build_historical_catalog.py ./

# Put the venv on PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=0

# Catalog data lives here — mount a host volume so it persists
VOLUME ["/app/data"]

# Default: run the live recorder.
# Override CMD in docker-compose.yml or with `docker run` to run the
# historical loader or backtest instead.
CMD ["python", "record_live_data.py"]
