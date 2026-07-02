# Freqtrade Lab

Freqtrade workspace for Hyperliquid perpetual strategy testing.

## Data Flow

`hl_engine` owns Hyperliquid ingestion. It writes NautilusTrader catalog data under
`hl_engine/data/catalog`. This lab consumes exported Freqtrade OHLCV JSON under
`freqtrade_lab/user_data/data/hyperliquid`.

Build or update the catalog:

```bash
cd ../hl_engine
HL_RECORD_COINS=ETH HL_INTERVAL=5m uv run python build_historical_catalog.py
```

Export the catalog into Freqtrade format:

```bash
cd ../freqtrade_lab
make export-data COIN=ETH TIMEFRAME=5m
```

This writes the requested strategy timeframe plus a 1h support file used by
Freqtrade futures backtesting.

Run a backtest against the exported Hyperliquid data:

```bash
cd ../freqtrade_lab
make backtest
```

Run dry/live trading with `user_data/config/config.json`:

```bash
cd ../freqtrade_lab
make trade
```

The default pair is `ETH/USDC:USDC`, which maps to
`user_data/data/hyperliquid/futures/ETH_USDC_USDC-5m-futures.json`.
