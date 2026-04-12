"""
MaCrossStrategy — Simple moving-average crossover strategy.

Used as a smoke test for the BacktestEngine pipeline.
Trades 0.001 BTC on every fast/slow SMA crossover using market IOC orders.
"""

from collections import deque
from datetime import datetime, timezone
from typing import Optional

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from hl_engine.config.ma_config import MaCrossConfig

FIXED_SIZE = 0.001  # BTC per trade


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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        self._instrument_id = InstrumentId.from_str(self._config.instrument_id)
        self._instrument = self.cache.instrument(self._instrument_id)

        if self._instrument is None:
            self.log.error(f"Instrument not found in cache: {self._instrument_id}")
            return

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

    def on_stop(self) -> None:
        if self._instrument_id:
            self.cancel_all_orders(self._instrument_id)
        self.log.info("MaCrossStrategy stopped")

    # ------------------------------------------------------------------
    # Data handlers
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        self._closes.append(float(bar.close))

        # Need slow_period + 1 bars: slow_period for current SMA + 1 for previous SMA
        if len(self._closes) < self._config.slow_period + 1:
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
            return

        side = OrderSide.BUY if crossed_up else OrderSide.SELL
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

    # ------------------------------------------------------------------
    # Order event handlers
    # ------------------------------------------------------------------

    def on_order_filled(self, event) -> None:
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None

    def on_order_canceled(self, event) -> None:
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None

    def on_order_rejected(self, event) -> None:
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None
            self.log.warning(f"Order rejected: {event.reason}")
