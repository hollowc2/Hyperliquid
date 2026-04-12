# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run backtest (NautilusTrader)
python3 hl_engine/run_backtest.py                  # Full APEX strategy
python3 hl_engine/run_backtest.py --strategy ma    # MA crossover smoke test
python3 hl_engine/run_backtest.py --bar-minutes 5  # Resample 1m bars to 5m

# Run live trading
python3 hl_engine/run_live.py
HL_PAPER_TRADE=true python3 hl_engine/run_live.py  # Paper trading

# Stream market data to Parquet catalog
python3 hl_engine/record_live_data.py

# Build historical catalog from REST API
python3 hl_engine/build_historical_catalog.py

# Stream to TimescaleDB
python3 hl_engine/record_live_ts.py

# Docker
docker compose run --rm backtest
docker compose build ts-recorder
```

Tests use pytest with async support (`asyncio_mode = "auto"`), placed in `tests/`:
```bash
uv run pytest tests/test_name.py
```

## Architecture

This is a crypto derivatives trading engine for Hyperliquid perpetual futures, built on **NautilusTrader** as the event-driven backtest/live framework.

### Signal & Execution Pipeline

```
Hyperliquid WebSocket
  → HyperliquidLiveMarketDataClient (asyncio, NOT the threaded SDK)
  → NautilusTrader DataEngine (L2 deltas, trade ticks, bars, custom data)
  → ApexStrategy (throttled to 100ms evaluation intervals)
  → Feature Extractors → FeatureVector → BayesianEdgeModel.compute_edge()
  → OrderRouter (LIMIT vs MARKET selection)
  → ExposureManager (hard risk checks)
  → KellySizer (fractional Kelly, 0.25x multiplier)
  → HyperliquidLiveExecClient / HyperliquidPaperExecClient
  → Hyperliquid REST API
```

### Key Components

**`hl_engine/strategy/apex_strategy.py`** — Main orchestrator. Subscribes to L2 order book deltas, trade ticks, 1m bars, and custom data types (FundingRateData, LiquidationData, OpenInterestData). Signal evaluation is throttled at 100ms.

**`hl_engine/models/bayesian_model.py`** — Edge model using log-odds: `Σ wᵢ * fᵢ` over 6 features (OBI, TFI, microprice drift, Hawkes intensity, cascade score, funding pressure). `edge = sigmoid(log_odds) - p_market`.

**`hl_engine/models/`** — Supporting models: `hawkes_model.py` (trade imbalance persistence), `cascade_model.py` (liquidation cascade scoring), `funding_model.py` (168-hour funding history), `regime_detector.py` (vol regime).

**`hl_engine/features/`** — Feature extractors: `orderbook_features.py` (OBI, microprice, spread, depth USD), `trade_features.py` (trade flow imbalance), `volatility_features.py` (Parkinson volatility).

**`hl_engine/execution/order_router.py`** — Cascade mode → MARKET IOC; good queue probability → LIMIT post-only at best bid/ask; otherwise → LIMIT one tick inside spread.

**`hl_engine/risk/exposure_manager.py`** — Pre-order checks: max notional ($10k), max leverage (5x), max drawdown (15% halt, 10% reduce-only).

**`hl_engine/config/apex_config.py`** — All configuration as frozen `msgspec.Struct` dataclasses: `HyperliquidConfig`, `FeatureConfig`, `ModelConfig`, `RiskConfig`, `ExecutionConfig`. Default Bayesian weights: OBI=0.30, TFI=0.30, microprice=0.20, Hawkes=0.10, cascade=0.05, funding=0.05.

**`hl_engine/adapters/hyperliquid/`** — Custom NautilusTrader adapters. `data_client.py` is asyncio-native (not the threaded Hyperliquid SDK). `factories.py` wires clients into the NautilusTrader node. Custom data types registered via `types.py`.

**`hl_engine/data/`** — `live_recorder.py` (WebSocket → Parquet), `historical_loader.py` (REST → Parquet), `timescale_sink.py` (optional TimescaleDB persistence).

### Environment Variables

```
HL_WALLET_ADDRESS     Hyperliquid wallet address
HL_PRIVATE_KEY        Private key for signing orders
HL_TESTNET            true/false (default false)
HL_PAPER_TRADE        true for paper trading
HL_RECORD_COINS       Comma-separated coins to record (e.g. BTC,ETH,SOL)
HL_CATALOG_PATH       Path to Parquet catalog (default data/catalog)
```

### Docker

Multi-stage Dockerfile with three targets:
- `runtime` — Full image including NautilusTrader (for live trading + backtesting)
- `collector` — Lightweight, no NautilusTrader (for data recording only)
- `builder` — Build stage for compiled dependencies
