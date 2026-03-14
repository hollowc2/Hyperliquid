# Plan: Hyperliquid Streak Reversal Bot — Standalone Project

## Context

Port the Streak Reversal strategy (proven ~57-60% win rate on 15m ETH from
Polymarket backtests) to a standalone perpetual futures bot on Hyperliquid.

This is a NEW standalone Python project — extracted from the polymarket_auto_trader
monorepo, keeping only what's needed, restructured for simplicity.

Key differences from Polymarket:
- Both long AND short signals (doubles trade count)
- Explicit exit logic required (stops/targets + counter-signal override)
- Positions persist until manually closed — no binary resolution
- 1x leverage to start; parameterized for easy increase

Confirmed design decisions:
- Asset: ETH only (extend to BTC after paper validation)
- Leverage: 1x
- Counter-signal: close to flat, wait for next clean bar (no flip)

---

## Standalone Project Structure

```
hyperliquid-streak-bot/
├── pyproject.toml          # uv project, single package
├── .env.example            # all required env vars documented
├── .env                    # gitignored
├── src/
│   ├── strategy.py         # StreakReversal signal + TrendFilter gate (copied + simplified)
│   ├── client.py           # HyperliquidClient (REST + order placement via SDK)
│   ├── trader.py           # HLTrade, HLTradingState, HLPaperTrader, HLLiveTrader
│   ├── resilience.py       # CircuitBreaker, RateLimiter, with_retry (copied verbatim)
│   └── data.py             # fetch_live_candles() from Binance (copied + simplified)
├── bot.py                  # Main entry point (CLI)
├── state/                  # gitignored — runtime state files
│   └── trades.json
├── Dockerfile
├── docker-compose.yml
└── hyperliquid_streak_bot_plan.md   # THIS plan document (written on first implementation step)
```

---

## Dependencies (pyproject.toml)

```toml
[project]
name = "hyperliquid-streak-bot"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "hyperliquid-python-sdk",
    "eth-account",
    "pandas",
    "numpy",
    "requests",
    "websockets",
    "python-dotenv",
]
```

---

## src/strategy.py — Signal Generation

Copy + simplify from packages/strategies/ + packages/indicators/.
No plugin system, no protocol overhead — just the functions needed.

```python
def count_streaks(candles: pd.DataFrame) -> pd.Series:
    direction = (candles["close"].diff() > 0).map({True: 1, False: -1}).fillna(0)
    return direction.groupby((direction != direction.shift()).cumsum()).cumcount() + 1

def streak_signal(candles: pd.DataFrame, trigger: int = 6) -> int:
    direction = (candles["close"].diff() > 0).map({True: 1, False: -1}).fillna(0)
    streak = count_streaks(candles)
    last_dir = direction.iloc[-1]
    last_streak = streak.iloc[-1]
    if last_streak >= trigger:
        return -1 if last_dir == 1 else 1  # reversal
    return 0

def apply_trend_filter(signal: int, candles: pd.DataFrame, ema_period: int = 100) -> int:
    # Veto signal if it's WITH the trend (not against it)
    ema = candles["close"].ewm(span=ema_period).mean().iloc[-1]
    price = candles["close"].iloc[-1]
    trending_up = price > ema
    if signal == 1 and trending_up:   return 0   # don't buy into uptrend
    if signal == -1 and not trending_up: return 0 # don't short into downtrend
    return signal
```

---

## src/data.py — Binance Candles

Copy `fetch_live_candles()` from scripts/streak_bot.py.
Fetch last N 15m candles from Binance public API (no auth required).
Returns pd.DataFrame with columns: open, high, low, close, volume, timestamp.

```python
def fetch_candles(symbol: str = "ETHUSDT", interval: str = "15m", n: int = 250) -> pd.DataFrame:
    ...
```

---

## src/client.py — HyperliquidClient

```python
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from eth_account import Account

class HyperliquidClient:
    def __init__(self, private_key: str, wallet: str, testnet: bool = False):
        self.info = Info(base_url)
        self.exchange = Exchange(Account.from_key(private_key), base_url, ...)

    def get_price(self, coin: str = "ETH") -> float
    def get_position(self, coin: str = "ETH") -> dict | None
    def get_account_value(self) -> float                # USDC balance
    def set_leverage(self, coin: str, leverage: int)   # call once on init

    # Orders (size in base asset, e.g. ETH)
    def place_market_long(self, coin, sz_base) -> str   # returns order_id
    def place_market_short(self, coin, sz_base) -> str
    def place_stop_loss(self, coin, trigger_px, is_long) -> str  # reduce_only
    def place_take_profit(self, coin, trigger_px, is_long) -> str # reduce_only
    def cancel_order(self, coin, oid)
    def close_position(self, coin)                      # market reduce_only
    def get_open_orders(self, coin) -> list[dict]
```

Key detail: Hyperliquid orders use base asset size (ETH), not USD.
Convert: `sz_base = size_usd / current_price`

Stop/TP order type:
```python
{"trigger": {"triggerPx": price, "isMarket": True, "tpsl": "sl"}}  # stop
{"trigger": {"triggerPx": price, "isMarket": True, "tpsl": "tp"}}  # target
```

---

## src/trader.py — Trade Lifecycle

### HLTrade (dataclass)
```python
@dataclass
class HLTrade:
    id: str
    coin: str
    is_long: bool
    entry_price: float
    entry_time: datetime
    size_usd: float
    size_base: float
    stop_px: float
    target_px: float
    stop_oid: str | None
    target_oid: str | None
    atr_at_entry: float
    leverage: int
    paper: bool
    # Filled on close:
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: str | None = None   # "stop" | "target" | "signal" | "manual"
    pnl_usd: float | None = None
    fee_usd: float | None = None
```

### HLTradingState
- Persists to `state/trades.json`
- Tracks `open_trade: HLTrade | None` + `closed_trades: list[HLTrade]`
- Methods: `save()`, `load()`, `record_entry()`, `record_exit()`, `stats()`

### HLPaperTrader
- Simulates entry at `close * (1 + 0.0005)` (taker slippage)
- Stop/TP checked against subsequent bar OHLC (no lookahead: H/L of NEXT bar)
- Paper stop: if is_long and low < stop_px → stopped out at stop_px
- Paper TP: if is_long and high > target_px → filled at target_px
- No real orders placed

### HLLiveTrader
- Uses HyperliquidClient for real order placement
- Entry → immediately place stop + tp bracket
- Check open_orders() each loop iteration to detect sl/tp fills
- On counter-signal: cancel_order(sl), cancel_order(tp), close_position()

---

## Position Sizing

Risk-based (replaces Kelly — we have defined stops now):

```python
atr = candles["high"].sub(candles["low"]).rolling(14).mean().iloc[-1]
stop_dist_pct = atr / current_price
risk_usd = account_value * RISK_PCT              # default 0.01 (1%)
size_usd = risk_usd / stop_dist_pct
size_usd = min(size_usd, account_value * MAX_POS_PCT)  # cap at 20%
size_base = size_usd / current_price
```

Stop/target levels:
```python
if is_long:
    stop_px   = entry_px * (1 - atr_mult_stop / entry_px * atr)
    target_px = entry_px * (1 + atr_mult_tp   / entry_px * atr)
```
(Simplified: stop = entry - STOP_ATR_MULT × ATR, target = entry + TP_ATR_MULT × ATR)

---

## bot.py — Main Loop

```
while True:
    sleep to next 15m bar close + 5s

    candles = fetch_candles("ETHUSDT", "15m", n=250)
    signal = streak_signal(candles, trigger=TRIGGER)
    if USE_TREND_FILTER:
        signal = apply_trend_filter(signal, candles, EMA_PERIOD)

    price = client.get_price("ETH")
    trade = state.open_trade

    if trade:
        if not paper:
            check_if_sl_tp_hit(trade)   # poll open orders
        else:
            simulate_sl_tp(trade, candles)

        # Counter-signal: opposite direction → close flat
        if (signal == 1 and not trade.is_long) or (signal == -1 and trade.is_long):
            close_position_and_record(trade, reason="signal")
            state.open_trade = None
            # Do NOT re-enter — wait for next clean bar

    elif signal != 0:
        enter_new_position(signal, candles, price)
```

CLI:
```
python bot.py --paper              # required for paper mode
python bot.py --live               # requires HL_PRIVATE_KEY in .env
python bot.py --trigger 6          # consecutive bars (default 6)
python bot.py --gate trend         # apply TrendFilter(ema=100)
python bot.py --ema-period 100
python bot.py --risk-pct 0.01
python bot.py --stop-atr 1.0
python bot.py --tp-atr 1.5
python bot.py --leverage 1         # default 1, bump after paper validation
```

---

## Environment Variables (.env)

```
HL_PRIVATE_KEY=0x...
HL_WALLET_ADDRESS=0x...
HL_TESTNET=false

RISK_PCT=0.01
STOP_ATR_MULT=1.0
TP_ATR_MULT=1.5
MAX_POSITION_PCT=0.20
LEVERAGE=1
TRIGGER=6
USE_TREND_FILTER=true
EMA_PERIOD=100
MAX_DAILY_TRADES=10
MAX_DAILY_LOSS_USD=100.0
```

---

## Build Sequence

1. Write `hyperliquid_streak_bot_plan.md` to project root (this document)
2. Scaffold project: `uv init hyperliquid-streak-bot`, create `src/` layout
3. `src/resilience.py` — copy CircuitBreaker, RateLimiter, with_retry verbatim
4. `src/data.py` — copy + simplify fetch_live_candles() from streak_bot.py
5. `src/strategy.py` — implement streak_signal() + apply_trend_filter()
6. `src/client.py` — HyperliquidClient wrapping SDK
7. `src/trader.py` — HLTrade, HLTradingState, HLPaperTrader
8. `bot.py` — main loop with argparse CLI
9. Paper trade for 1-2 weeks, compare vs Polymarket 15m baseline (~60% win rate)
10. `src/trader.py` — add HLLiveTrader (real orders)
11. `Dockerfile` + `docker-compose.yml` for deployment
12. `.env.example` documenting all config vars

---

## Verification

```bash
# Paper trade (runs in real-time, 15m intervals)
uv run python bot.py --paper --gate trend --trigger 6

# Check state
cat state/trades.json | python -m json.tool

# Expected behavior:
# - Signals fire after 6 consecutive same-direction 15m closes
# - TrendFilter vetoes signals going with the trend
# - Stops at 1x ATR, targets at 1.5x ATR
# - No position flip on counter-signal — goes flat and waits
# - Win rate should approximate Polymarket 15m baseline (~60%)
```
