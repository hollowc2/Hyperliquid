# HyperliquidPublicAPI

Free, no-auth Python client for Hyperliquid perpetuals market data. No API key required.

Data sources:
- Hyperliquid public REST: `https://api.hyperliquid.xyz/info`
- Binance Futures public REST: `https://fapi.binance.com/fapi/v1` (liquidation endpoint now requires auth — stubs out cleanly)

---

## Installation

```bash
pip install requests rich
```

Or install as an editable package so it's importable from any project:

```bash
pip install -e /path/to/data_api
```

---

## Quick Start

```python
from api import HyperliquidPublicAPI

api = HyperliquidPublicAPI()

# Live prices
prices = api.get_tick_latest()

# Order flow — 5m/15m/1h/4h directional signals
flow = api.get_orderflow("1h")
signal_5m  = flow["windows"]["5m"]   # buy_pressure, dominant_side, cumulative_delta
signal_15m = flow["windows"]["15m"]
by_coin    = flow["by_coin"]          # per-coin breakdown for BTC/ETH/HYPE/SOL/XRP

# HLP vault — HL's market-making book
hlp = api.get_hlp_positions()

# Any wallet
positions = api.get_user_positions("0x...")
fills     = api.get_user_fills("0x...", limit=100)

# Liquidations
liqs = api.get_liquidations("1h")    # count, volume, long/short split, by coin, largest events
```

---

## Methods

### Prices & Candles

| Method | Returns | Notes |
|--------|---------|-------|
| `get_tick_latest()` | Current mid prices | BTC, ETH, HYPE, SOL, XRP |
| `get_ticks(symbol, tf)` | OHLCV candles | Any HL-listed coin; tf: `5m` → `30d` |
| `get_tick_stats()` | OI, volume, funding per coin | From `metaAndAssetCtxs` |

### Order Flow

| Method | Returns | Notes |
|--------|---------|-------|
| `get_orderflow(tf)` | Buy/sell pressure by window + coin | Windows: 5m, 15m, 1h, 4h. **Approximation** — OHLCV candle proxy, not live CVD |
| `get_orderflow_stats()` | 24h volume + directional split | Volume from HL stats; split approximated from 1h candles |
| `get_imbalance(tf)` | Aggregate buy/sell ratio | Same candle proxy |
| `get_trades()` | ~50 recent trades | 10 trades/coin × 5 coins snapshot |
| `get_large_trades()` | Trades >$100K | Filtered from `get_trades()` — same small sample |

### Liquidations

| Method | Returns | Notes |
|--------|---------|-------|
| `get_liquidations(tf)` | Count, volume, long/short, by coin, largest | HLP Strategy A fills only; **effective lookback ~2h** regardless of tf due to API page cap |
| `get_liquidation_stats()` | Aggregated across 10m/1h/4h/12h/24h | Same limitations |

### Positions

| Method | Returns | Notes |
|--------|---------|-------|
| `get_positions()` | Open positions ≥$200K | Scans known HLP wallet addresses only |
| `get_user_positions(address)` | All open perp positions for any wallet | Full detail: size, entry, mark, PnL, leverage |
| `get_user_positions_api(address)` | Same as above | Alias |
| `get_user_fills(address, limit)` | Trade fill history | Up to 2,000 records; use `limit=-1` for all |

### HLP Vault

| Method | Returns | Notes |
|--------|---------|-------|
| `get_hlp_positions(include_strategies)` | Combined positions + PnL across HLP addresses | $121M+ AUM, 189 positions, net delta visible |
| `get_hlp_trades(limit)` | Merged fill history across HLP addresses | |
| `get_hlp_trade_stats()` | Volume, fees by strategy | |
| `get_hlp_liquidators()` | Liquidator address activation status | Only Strategy A currently confirmed active |
| `get_hlp_deltas(hours)` | Net exposure snapshot | Single snapshot; no true historical series |
| `get_hlp_position_history(hours)` | Current snapshot | REST limitation — no actual history |

### Stubs (raise `NotImplementedError`)

These require infrastructure not available from public REST:

| Method | Reason |
|--------|--------|
| `get_whales()` | Needs curated whale address database |
| `get_whale_addresses()` | Same |
| `get_events()` | Needs persistent chain event indexer |
| `get_contracts()` | Needs contract metadata registry |
| `get_smart_money_rankings()` | Needs sustained multi-wallet PnL tracking |
| `get_smart_money_leaderboard()` | Same |
| `get_smart_money_signals(tf)` | Needs wallet classification pipeline |
| `get_binance_liquidations(tf)` | Binance `/fapi/v1/forceOrders` now requires API key |
| `get_binance_liquidation_stats()` | Same |

---

## Timeframes

Supported `tf` values: `5m`, `10m`, `15m`, `1h`, `4h`, `12h`, `24h`, `2d`, `7d`, `14d`, `30d`

Note: HL has no 10m candle resolution — `10m` maps to `15m` internally.

---

## Tracked Coins

Order flow, prices, and trade methods cover: `BTC`, `ETH`, `HYPE`, `SOL`, `XRP`

`get_ticks()` and user data methods work with any HL-listed symbol.

---

## Caching

All methods use an in-memory TTL cache. No external cache required.

| Data Type | TTL |
|-----------|-----|
| Prices (`allMids`) | 15s |
| Market stats | 30s |
| Candles / order flow | 60s |
| HLP aggregate | 60s |
| Liquidations | 60s |
| Binance liquidations | 120s |
| User fills | not cached |

---

## Key Limitations

**Order flow is a candle proxy, not CVD.** Green candle = all volume classified as buy; red = sell. Misses intracandle reversals. Most reliable on 5m/15m; noisier on 4h.

**Liquidation lookback is ~2h regardless of the requested window.** HLP Strategy A trades ~200 fills/minute. The API page cap (2,000 records) is hit within ~10 minutes; pagination extends this to ~2h maximum. `4h` and `24h` windows return the same data with a cap warning displayed.

**Only 5 coins tracked** for order flow and price snapshots. User data and candle endpoints work for any symbol.

**HLP vault is the default wallet** in position/fill examples. It's HL's own market-making book — 189 positions, all at 20x, trading ~200 fills/minute. Not a directional trader.

---

## Examples

```bash
python examples/01_liquidations.py    # liquidation dashboard
python examples/07_orderflow.py       # order flow signals
python examples/10_user_positions.py  # positions for any wallet
python examples/12_hlp_positions.py   # HLP vault deep dive
```

See [examples/README.md](examples/README.md) for the full index.
