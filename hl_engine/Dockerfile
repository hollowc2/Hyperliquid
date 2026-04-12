# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------
# HL Engine — multi-stage Docker image using uv
#
# Stages:
#   builder    — installs ALL dependencies (including nautilus-trader)
#   runtime    — full image for backtesting / historical loader
#   collector  — lightweight image for TimescaleDB live recorder only
#                (no nautilus-trader; much smaller)
#
# Build full:
#   docker build --target runtime -t hl-engine .
#
# Build collector only (faster, smaller):
#   docker build --target collector -t apex-collector .
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
# --no-install-project: don't install hl_engine itself yet (source not copied).
# --frozen: fail if uv.lock is out of sync with pyproject.toml.
#           Falls back to uv sync (no --frozen) when uv.lock doesn't exist yet.
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-install-project; \
    else \
        uv sync --no-install-project; \
    fi

# Copy project source
COPY hl_engine/ ./hl_engine/
COPY record_live_data.py build_historical_catalog.py record_live_ts.py ./

# Install the hl_engine package itself into the venv
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen; \
    else \
        uv sync; \
    fi


# ---- Stage 2: runtime (full — includes nautilus-trader for backtesting) ----
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only the installed venv and application source — no uv, no build tools
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/hl_engine ./hl_engine/
COPY --from=builder /app/record_live_data.py \
                    /app/build_historical_catalog.py \
                    /app/record_live_ts.py ./

# Put the venv on PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=0

# Catalog data lives here — mount a host volume so it persists
VOLUME ["/app/data"]

# Default: run the live recorder.
# Override CMD in docker-compose.yml or with `docker run` to run the
# historical loader, backtest, or TS recorder instead.
CMD ["python", "record_live_data.py"]


# ---- Stage 3: collector (lightweight — no nautilus-trader) ---------------
# Installs only the packages needed to stream live data into TimescaleDB.
FROM python:3.12-slim AS collector

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Minimal dependency set — skip nautilus-trader entirely
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system \
        "websockets>=12.0" \
        "aiohttp>=3.9.0" \
        "asyncpg>=0.29.0" \
        "python-dotenv>=1.0.0"

# Copy only what the TS recorder needs.
# hl_engine is not installed as a package here; we put it on PYTHONPATH
# by placing the source tree under /app and setting PYTHONPATH=/app.
COPY hl_engine/data/__init__.py        ./hl_engine/data/__init__.py
COPY hl_engine/data/timescale_sink.py  ./hl_engine/data/timescale_sink.py
COPY record_live_ts.py                   ./

# Create a minimal top-level __init__ so `from hl_engine.data...` resolves
RUN touch ./hl_engine/__init__.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=0

CMD ["python", "record_live_ts.py"]
