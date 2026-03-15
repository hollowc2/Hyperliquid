# top_wallets

Terminal dashboard for monitoring the top 10 Hyperliquid leaderboard wallets in real time.

## Features

- **Summary table** — rank, username/address, all-time vol, 30d PnL, account value, lifetime PnL, perp summary, spot holdings
- **Perp detail panel** — full position breakdown for the selected wallet: leverage, entry price, stop, TP, liquidation price, distance to stop/liq (color-coded), notional, uPnL, ROE
- **Live WebSocket alerts** — toast notifications for large trades (>$100K) by top-10 wallets
- **Caching** — enriched wallet data persists to `wallet_cache.json`; on restart the table renders instantly from cache while refreshing in the background

## Usage

```bash
pip install -r requirements.txt
python hyperliquid_tracker.py
```

## Keybindings

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate wallets — perp panel updates instantly |
| `p` | Toggle perp detail panel |
| `r` | Force refresh |
| `q` | Quit |

## Perp Detail Columns

| Column | Description |
|--------|-------------|
| Dir | Long (L) / Short (S) |
| Coin | Asset |
| Lev | Leverage × type (C=cross, I=isolated) |
| Entry | Average entry price |
| Stop | Closest stop-loss trigger price |
| Dist Stop | % distance from mark to stop — 🔴 <3% 🟡 <10% 🟢 safe |
| TP | Closest take-profit trigger price |
| Liq | Liquidation price |
| Dist Liq | % distance from mark to liquidation — 🔴 <5% 🟡 <15% 🟢 safe |
| Notional | Position size in USD |
| uPnL | Unrealized PnL |
| ROE | Return on equity |

## Config

Constants at the top of `hyperliquid_tracker.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `REFRESH_INTERVAL` | `60` | Seconds between full data refreshes |
| `LARGE_TRADE_THRESHOLD` | `$100K` | Minimum trade size for WS alerts |
| `MIN_PERP_NOTIONAL` | `$50K` | Hide positions smaller than this |
