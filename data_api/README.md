# HyperliquidPublicAPI

Free, no-auth Python client for Hyperliquid and Binance Futures public data.

No API key required.

## Installation

pip install requests rich

Or install as editable package:

pip install -e /path/to/data_api

## Quick Start

from api import HyperliquidPublicAPI
api = HyperliquidPublicAPI()

## Available Methods

TICK DATA:
- get_tick_latest()                     - Current prices for TRACKED_COINS
- get_ticks(symbol, timeframe)          - Historical OHLCV candles
- get_tick_stats()                      - Synthetic stats from metaAndAssetCtxs

ORDER FLOW & TRADES:
- get_trades()                          - Recent ~250 trades (5 coins × ~50)
- get_large_trades()                    - Trades >$100k filtered from get_trades()
- get_orderflow(tf)                     - Buy/sell pressure proxy from candles [APPROX]
- get_orderflow_stats()                 - 24h volume from metaAndAssetCtxs
- get_imbalance(tf)                     - Buy/sell imbalance for a timeframe [APPROX]

LIQUIDATIONS (Hyperliquid):
- get_liquidations(tf)                  - HLP liquidator fills in window [LIMITED]
- get_liquidation_stats()               - Aggregated across timeframes

POSITIONS:
- get_positions()                       - Large positions from known HLP wallets [LIMITED]

USER DATA:
- get_user_positions(address)           - Open positions for any HL wallet
- get_user_positions_api(address)       - Delegates to get_user_positions()
- get_user_fills(address, limit)        - Historical fills for any HL wallet

HLP (HYPERLIQUIDITY PROVIDER):
- get_hlp_positions(include_strategies) - Positions across known HLP addresses
- get_hlp_trades(limit)                 - Fills from HLP addresses
- get_hlp_trade_stats()                 - Aggregated HLP trade stats
- get_hlp_liquidators()                 - Liquidator activation status
- get_hlp_deltas(hours)                 - Net exposure snapshot
- get_hlp_position_history(hours)       - Single current snapshot

## Examples

Run any example from the examples/ directory:

python examples/01_liquidations.py
python examples/07_orderflow.py
python examples/12_hlp_positions.py

## Data Sources

- Hyperliquid public REST: https://api.hyperliquid.xyz/info
- Binance Futures public REST: https://fapi.binance.com/fapi/v1 (liquidation endpoint now requires auth)

## Limitations

- Order flow / imbalance: OHLCV candle proxy, not live CVD
- Liquidations: HLP Strategy A address only; ~2h effective lookback due to API page cap
- get_trades(): ~10 trades per coin snapshot only
- Binance liquidation endpoints now require API key authentication
- Whale/smart-money/event/contract features are stubs — require sustained tracking infrastructure
