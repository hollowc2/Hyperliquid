# Examples

14 example scripts demonstrating HyperliquidPublicAPI features.

## Running Examples

python examples/01_liquidations.py

No API key or .env file required.

## Example Index

| # | File | Feature | Status |
|---|------|---------|--------|
| 01 | 01_liquidations.py | HL liquidations by timeframe | Working |
| 02 | 02_positions.py | Large positions from known wallets | Working |
| 03 | 03_whales.py | Whale tracker | Stub — needs wallet list |
| 04 | 04_events.py | Blockchain events | Stub — needs indexer |
| 05 | 05_contracts.py | Contract registry | Stub — needs metadata source |
| 06 | 06_ticks.py | Live prices + OHLCV candles | Working |
| 07 | 07_orderflow.py | Buy/sell pressure by timeframe | Working |
| 08 | 08_trades.py | Recent trades snapshot | Working |
| 09 | 09_smart_money.py | Smart money signals | Stub — needs tracking pipeline |
| 10 | 10_user_positions.py | Positions for any wallet address | Working |
| 11 | 11_user_fills.py | Fill history for any wallet address | Working |
| 12 | 12_hlp_positions.py | HLP vault positions + net delta | Working |
| 13 | 13_binance_liquidations.py | Binance liquidations | Dead — endpoint requires auth |
| 14 | 14_multi_liquidations.py | Multi-exchange liquidation comparison | Partial — HL only |

## Quick Import

from api import HyperliquidPublicAPI
api = HyperliquidPublicAPI()

# Prices
prices = api.get_tick_latest()

# Order flow (5m/15m/1h/4h windows)
flow = api.get_orderflow("1h")
signal_5m = flow["windows"]["5m"]   # buy_pressure, dominant_side, cumulative_delta

# HLP vault positions
hlp = api.get_hlp_positions()

# Any wallet
positions = api.get_user_positions("0x...")
fills = api.get_user_fills("0x...", limit=100)
