"""
MaCrossStrategy — Simple moving-average crossover strategy.

Used as a smoke test for the BacktestEngine pipeline.
Trades 0.001 BTC on every fast/slow SMA crossover using market IOC orders.
"""

from collections import deque
import asyncio
from datetime import datetime, timezone
import json
import os
import time
from typing import Optional
from urllib.request import urlopen

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from hl_engine.config.ma_config import MaCrossConfig

FIXED_SIZE = 0.001  # BTC per trade


def _extract_signed_position_qty(account_state: dict, instrument_id: str) -> float:
    paper_state = account_state.get("paper", {})
    if "position_qty" in paper_state:
        return float(paper_state["position_qty"])

    coin = instrument_id.split("-", 1)[0]
    for item in account_state.get("assetPositions", []):
        position = item.get("position", {})
        if position.get("coin") == coin:
            return float(position.get("szi", 0.0))

    return 0.0


class MaCrossStrategy(Strategy):
    """
    MA crossover strategy: buys on fast-crosses-above-slow, sells on fast-crosses-below-slow.

    Requires slow_period + 1 bars before generating any signal.
    Only one active order at a time; skips signal if _active_order_id is set.
    """

    def __init__(self, config: MaCrossConfig) -> None:
        super().__init__(config=config)
        self._config = config
        # Keep one extra bar so we can compare previous vs current SMAs
        self._closes: deque[float] = deque(maxlen=config.slow_period + 1)
        self._instrument_id: Optional[InstrumentId] = None
        self._instrument: Optional[Instrument] = None
        self._active_order_id = None
        self._notional_limit_halted = False
        self._signed_position_qty = 0.0
        self._bars_since_position_change = 0
        self._exit_cooldown_bars_remaining = 0
        self._last_signal_reason = "warming_up"
        self._last_state_push_ns = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        self._instrument_id = InstrumentId.from_str(self._config.instrument_id)
        self._instrument = self.cache.instrument(self._instrument_id)

        if self._instrument is None:
            self.log.error(f"Instrument not found in cache: {self._instrument_id}")
            return

        self._sync_position_from_orchestrator()

        bar_type = BarType.from_str(
            f"{self._config.instrument_id}-{self._config.bar_minutes}-MINUTE-LAST-EXTERNAL"
        )
        self.subscribe_bars(bar_type)

        self.request_bars(
            bar_type=bar_type,
            start=datetime(2020, 1, 1, tzinfo=timezone.utc),
            limit=self._config.slow_period + 10,
        )

        self.log.info(
            f"MaCrossStrategy started | instrument={self._instrument_id} "
            f"fast={self._config.fast_period} slow={self._config.slow_period}"
        )
        self._push_state_snapshot()

    def on_stop(self) -> None:
        if self._instrument_id:
            self.cancel_all_orders(self._instrument_id)
        self.log.info("MaCrossStrategy stopped")

    def _sync_position_from_orchestrator(self) -> None:
        base_url = os.getenv("ORCHESTRATOR_REST_URL", "").rstrip("/")
        strategy_id = os.getenv("STRATEGY_ID", "ma-cross-btc")
        if not base_url:
            return

        try:
            with urlopen(f"{base_url}/account/{strategy_id}", timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            self.log.warning(f"Could not sync starting position from orchestrator: {exc}")
            return

        try:
            qty = _extract_signed_position_qty(
                payload.get("account_state", {}),
                self._config.instrument_id,
            )
        except (TypeError, ValueError) as exc:
            self.log.warning(f"Invalid orchestrator account position: {exc}")
            return

        self._signed_position_qty = 0.0 if abs(qty) < 1e-12 else qty
        if self._signed_position_qty != 0.0:
            self.log.info(
                f"Restored starting position from orchestrator: {self._signed_position_qty:.8f}"
            )

    # ------------------------------------------------------------------
    # Data handlers
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        self._closes.append(float(bar.close))
        self._bars_since_position_change += 1

        # Need slow_period + 1 bars: slow_period for current SMA + 1 for previous SMA
        if len(self._closes) < self._config.slow_period + 1:
            self._last_signal_reason = "warming_up"
            self._push_state_snapshot(min_interval_secs=30.0)
            return

        if self._active_order_id is not None:
            return

        closes = list(self._closes)
        fp = self._config.fast_period
        sp = self._config.slow_period

        fast_now  = sum(closes[-fp:]) / fp
        slow_now  = sum(closes[-sp:]) / sp
        fast_prev = sum(closes[-fp - 1:-1]) / fp
        slow_prev = sum(closes[-sp - 1:-1]) / sp

        crossed_up   = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if not (crossed_up or crossed_down):
            self._last_signal_reason = "no_crossover"
            self._push_state_snapshot(min_interval_secs=30.0)
            return

        if self._signal_spread_bps(fast_now, slow_now) < self._config.min_signal_spread_bps:
            self._last_signal_reason = "spread_too_small"
            self._push_state_snapshot(min_interval_secs=30.0)
            return

        side = OrderSide.BUY if crossed_up else OrderSide.SELL
        if self._exit_cooldown_bars_remaining > 0 and not self._side_reduces_position(side):
            self._exit_cooldown_bars_remaining -= 1
            self._last_signal_reason = "exit_cooldown"
            self._push_state_snapshot(min_interval_secs=30.0)
            return
        if self._side_reduces_position(side) and self._bars_since_position_change < self._config.min_hold_bars:
            self._last_signal_reason = "min_hold"
            self._push_state_snapshot(min_interval_secs=30.0)
            return
        if self._side_increases_position(side):
            self._last_signal_reason = "already_positioned"
            self._push_state_snapshot(min_interval_secs=30.0)
            return

        if self._notional_limit_halted and not self._side_reduces_position(side):
            self._last_signal_reason = "notional_limit_halted"
            self._push_state_snapshot(min_interval_secs=30.0)
            return

        qty  = Quantity(FIXED_SIZE, self._instrument.size_precision)

        order = self.order_factory.market(
            instrument_id=self._instrument_id,
            order_side=side,
            quantity=qty,
            time_in_force=TimeInForce.IOC,
        )
        self._active_order_id = order.client_order_id
        self.submit_order(order)

        self.log.info(
            f"MA crossover {'BUY' if crossed_up else 'SELL'} | "
            f"fast={fast_now:.2f} slow={slow_now:.2f}"
        )
        self._last_signal_reason = "buy_crossover" if crossed_up else "sell_crossover"
        self._push_state_snapshot()

    # ------------------------------------------------------------------
    # Order event handlers
    # ------------------------------------------------------------------

    def on_order_filled(self, event) -> None:
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None
        signed_qty = float(event.last_qty) if event.order_side == OrderSide.BUY else -float(event.last_qty)
        was_positioned = self._signed_position_qty != 0.0
        updated_qty = self._signed_position_qty + signed_qty
        self._signed_position_qty = 0.0 if abs(updated_qty) < 1e-12 else updated_qty
        self._bars_since_position_change = 0
        if was_positioned and self._signed_position_qty == 0.0:
            self._exit_cooldown_bars_remaining = self._config.cooldown_bars_after_exit
        self._push_state_snapshot()

    def on_order_canceled(self, event) -> None:
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None
        self._push_state_snapshot()

    def on_order_rejected(self, event) -> None:
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None
            self.log.warning(f"Order rejected: {event.reason}")
            if "notional" in (event.reason or "").lower():
                self._notional_limit_halted = True
                self.log.warning(
                    "Notional limit hit — halting new entry orders until position is reduced"
                )
        self._push_state_snapshot()

    def _push_state_snapshot(self, min_interval_secs: float = 0.0) -> None:
        base_url = os.getenv("ORCHESTRATOR_REST_URL", "")
        strategy_id = os.getenv("STRATEGY_ID", "ma-cross-btc")
        if not base_url:
            return
        now_ns = time.time_ns()
        if min_interval_secs > 0.0 and now_ns - self._last_state_push_ns < min_interval_secs * 1_000_000_000:
            return
        self._last_state_push_ns = now_ns
        try:
            asyncio.get_running_loop().create_task(
                self._push_state_to_orchestrator(
                    base_url.rstrip("/"),
                    strategy_id,
                    self._build_state_snapshot(),
                )
            )
        except RuntimeError:
            pass

    def _build_state_snapshot(self) -> dict:
        closes = list(self._closes)
        fast = None
        slow = None
        if len(closes) >= self._config.fast_period:
            fast = sum(closes[-self._config.fast_period:]) / self._config.fast_period
        if len(closes) >= self._config.slow_period:
            slow = sum(closes[-self._config.slow_period:]) / self._config.slow_period
        side = "FLAT"
        if self._signed_position_qty > 0.0:
            side = "LONG"
        elif self._signed_position_qty < 0.0:
            side = "SHORT"
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "instrument": str(self._instrument_id) if self._instrument_id else self._config.instrument_id,
            "currency": "USDC",
            "position": {
                "side": side,
                "qty": abs(self._signed_position_qty),
                "signed_qty": self._signed_position_qty,
            },
            "ma_cross": {
                "fast_period": self._config.fast_period,
                "slow_period": self._config.slow_period,
                "bar_minutes": self._config.bar_minutes,
                "fast_ma": fast,
                "slow_ma": slow,
                "min_signal_spread_bps": self._config.min_signal_spread_bps,
                "bars_since_position_change": self._bars_since_position_change,
                "exit_cooldown_bars_remaining": self._exit_cooldown_bars_remaining,
                "notional_limit_halted": self._notional_limit_halted,
                "last_signal_reason": self._last_signal_reason,
            },
        }

    async def _push_state_to_orchestrator(self, base_url: str, strategy_id: str, state: dict) -> None:
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base_url}/strategies/{strategy_id}/state",
                    json=state,
                    timeout=aiohttp.ClientTimeout(total=2.0),
                ) as resp:
                    await resp.read()
        except Exception as exc:
            self.log.warning(f"Orchestrator state push failed: {exc}")

    def _side_reduces_position(self, side: OrderSide) -> bool:
        if self._signed_position_qty != 0.0:
            is_buy = side == OrderSide.BUY
            return (self._signed_position_qty > 0.0) != is_buy

        if self._instrument_id is None:
            return False

        open_positions = self.cache.positions_open(instrument_id=self._instrument_id)
        if not open_positions:
            return False

        position = open_positions[0]
        is_buy = side == OrderSide.BUY
        return (position.is_long and not is_buy) or (not position.is_long and is_buy)

    def _side_increases_position(self, side: OrderSide) -> bool:
        if self._signed_position_qty != 0.0:
            is_buy = side == OrderSide.BUY
            return (self._signed_position_qty > 0.0) == is_buy

        if self._instrument_id is None:
            return False

        open_positions = self.cache.positions_open(instrument_id=self._instrument_id)
        if not open_positions:
            return False

        position = open_positions[0]
        is_buy = side == OrderSide.BUY
        return (position.is_long and is_buy) or (not position.is_long and not is_buy)

    @staticmethod
    def _signal_spread_bps(fast: float, slow: float) -> float:
        if slow == 0.0:
            return 0.0
        return abs(fast - slow) / abs(slow) * 10_000.0
