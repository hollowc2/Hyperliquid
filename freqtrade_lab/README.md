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

## Optional Free Context Data

Strategies can consume optional external context from local files without
breaking offline backtests. Set `FT_CONTEXT_ENABLED=true` and write CSV, JSON, or
Parquet files into `FT_CONTEXT_DIR` (default `user_data/data/context`). Files are
merged onto Freqtrade candles by `date` with `merge_asof`; missing files produce
neutral `ctx_*` columns.

Practical free/no-key sources:

1. Hyperliquid public metadata: funding and perp context already aligned to the
   traded venue when exported locally.
2. Binance public klines: BTC/ETH spot or perp context such as BTC 1h/1d returns.
3. Coinbase Exchange public candles: alternate USD spot reference candles.
4. CoinGecko public market data: total crypto market cap and ETH/BTC context.
5. Alternative.me Fear & Greed: sentiment regime.
6. FRED/Stooq/Yahoo-style macro mirrors: DXY, VIX, liquidity series snapshots.
7. DefiLlama public endpoints: stablecoin market cap or chain TVL changes.

The loader accepts these filenames when present:

```text
binance.csv
coinbase.csv
coingecko.csv
alternative_me.csv
fred.csv
yahoo.csv
defillama.csv
hyperliquid.csv
context.csv
```

JSON and Parquet variants with the same base names also work. Each file needs a
`date`, `datetime`, `timestamp`, or `time` column. Known value columns are mapped
to strategy-safe fields:

```text
fear_greed -> ctx_fear_greed
btc_close / btcusdt_close -> ctx_btc_ret_1h, ctx_btc_ret_1d
eth_btc / ethbtc -> ctx_eth_btc_ret_1d
total_crypto_mcap / market_cap -> ctx_total_crypto_mcap_ret_1d
stablecoin_mcap -> ctx_stablecoin_mcap_ret_1d
defillama_tvl / tvl -> ctx_defillama_tvl_ret_1d
dxy -> ctx_dxy_ret_1d
vix -> ctx_vix_ret_1d
fred_liquidity / walcl -> ctx_fred_liquidity_z
funding_rate -> ctx_funding_rate
```

Current strategy examples use the context conservatively:

```text
EthMomentumBreakoutStrategy and VolatilityBreakoutSharpeStrategy:
  require ctx_risk_on_ok for longs, ctx_risk_off_ok for shorts, and neutral funding.

StreakReversalStrategy and LiquidationWickReversionStrategy:
  skip new entries during ctx_stress_block and require neutral funding.
```

With `FT_CONTEXT_ENABLED=false`, or with no context files present, all of those
flags default to pass-through values and historical OHLCV-only backtests are
unchanged.

Run dry/live trading with `user_data/config/config.json`:

```bash
cd ../freqtrade_lab
make trade
```

The default pair is `ETH/USDC:USDC`, which maps to
`user_data/data/hyperliquid/futures/ETH_USDC_USDC-5m-futures.json`.
