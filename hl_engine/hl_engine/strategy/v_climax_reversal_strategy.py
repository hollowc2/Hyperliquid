"""V-climax reversal strategy for Hyperliquid perpetuals."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import floor
from typing import Optional

from nautilus_trader.model.data import Bar, BarType, OrderBookDeltas
from nautilus_trader.model.enums import BookType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from hl_engine.config.v_climax_reversal_config import VClimaxReversalConfig


class ClimaxPhase(Enum):
    """Internal state machine phases."""

    SEARCHING = "SEARCHING"
    PENDING_ENTRY = "PENDING_ENTRY"
    ENTERING = "ENTERING"
    IN_POSITION_PHASE_1 = "IN_POSITION_PHASE_1"
    IN_POSITION_PHASE_2 = "IN_POSITION_PHASE_2"
    EXITING = "EXITING"


@dataclass(frozen=True)
class StrategyBar:
    """Small OHLCV bar used for deterministic strategy logic."""

    open: float
    high: float
    low: float
    close: float
    volume: float
    ts_event: int


@dataclass(frozen=True)
class ClimaxEvent:
    """Detected exhaustion event and its initial risk level."""

    high: float
    low: float
    atr: float
    initial_stop: float
    ts_event: int
    expires_after_bar_count: int


class VClimaxReversalStrategy(Strategy):
    """
    Long-only waterfall + volume climax reversal strategy.

    The strategy subscribes to 1-minute bars, aggregates closed 2-minute bars
    internally, and uses L2 book updates for live entry/exit triggers.
    """

    def __init__(self, config: VClimaxReversalConfig) -> None:
        super().__init__(config=config)
        self._config = config
        self._instrument_id: Optional[InstrumentId] = None
        self._instrument: Optional[Instrument] = None

        self._phase = ClimaxPhase.SEARCHING
        self._source_bucket: list[Bar] = []
        self._bars: deque[StrategyBar] = deque(
            maxlen=max(config.lookback_bars, config.atr_period) + 2
        )
        self._climax: Optional[ClimaxEvent] = None
        self._bars_since_climax = 0

        self._active_entry_order_id = None
        self._active_exit_order_id = None
        self._entry_price: Optional[float] = None
        self._entry_qty: Optional[float] = None
        self._active_stop: Optional[float] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        self._instrument_id = InstrumentId.from_str(self._config.instrument_id)
        self._instrument = self.cache.instrument(self._instrument_id)
        if self._instrument is None:
            self.log.error(f"Instrument not found in cache: {self._instrument_id}")
            return

        self.subscribe_order_book_deltas(
            instrument_id=self._instrument_id,
            book_type=BookType.L2_MBP,
        )

        source_bar_type = BarType.from_str(
            f"{self._config.instrument_id}-"
            f"{self._config.source_bar_minutes}-MINUTE-LAST-EXTERNAL"
        )
        self.subscribe_bars(source_bar_type)
        self.request_bars(
            source_bar_type,
            start=datetime(2020, 1, 1, tzinfo=timezone.utc),
            limit=self._warmup_source_bar_count(),
        )

        self.log.info(
            "VClimaxReversalStrategy started | "
            f"instrument={self._instrument_id} bar_minutes={self._config.bar_minutes}"
        )

    def on_stop(self) -> None:
        if self._instrument_id:
            self.cancel_all_orders(self._instrument_id)
        self.log.info("VClimaxReversalStrategy stopped")

    # ------------------------------------------------------------------
    # Data handlers
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        closed_bar = self._add_source_bar(bar)
        if closed_bar is None:
            return

        self._on_strategy_bar(closed_bar)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        if self._instrument_id is None:
            return
        book = self.cache.order_book(self._instrument_id)
        if book is None:
            return

        best_ask = book.best_ask_price()
        best_bid = book.best_bid_price()
        if best_ask is not None:
            self._maybe_enter(float(best_ask))
        if best_bid is not None:
            self._maybe_exit(float(best_bid))

    # ------------------------------------------------------------------
    # Order events
    # ------------------------------------------------------------------

    def on_order_filled(self, event) -> None:
        if self._active_entry_order_id and event.client_order_id == self._active_entry_order_id:
            self._active_entry_order_id = None
            self._entry_price = float(event.last_px)
            self._entry_qty = float(event.last_qty)
            if self._climax is not None:
                self._active_stop = self._climax.initial_stop
            self._phase = ClimaxPhase.IN_POSITION_PHASE_1
            self.log.info(
                f"Climax entry filled qty={event.last_qty} px={event.last_px} "
                f"stop={self._active_stop}"
            )
            return

        if self._active_exit_order_id and event.client_order_id == self._active_exit_order_id:
            self._active_exit_order_id = None
            self._reset_after_exit()
            self.log.info(f"Climax exit filled qty={event.last_qty} px={event.last_px}")

    def on_order_canceled(self, event) -> None:
        if self._active_entry_order_id and event.client_order_id == self._active_entry_order_id:
            self._active_entry_order_id = None
            self._phase = ClimaxPhase.PENDING_ENTRY if self._climax else ClimaxPhase.SEARCHING
        elif self._active_exit_order_id and event.client_order_id == self._active_exit_order_id:
            self._active_exit_order_id = None
            if self._entry_price is not None:
                self._phase = ClimaxPhase.IN_POSITION_PHASE_2

    def on_order_rejected(self, event) -> None:
        if self._active_entry_order_id and event.client_order_id == self._active_entry_order_id:
            self._active_entry_order_id = None
            self._phase = ClimaxPhase.PENDING_ENTRY if self._climax else ClimaxPhase.SEARCHING
        elif self._active_exit_order_id and event.client_order_id == self._active_exit_order_id:
            self._active_exit_order_id = None
            if self._entry_price is not None:
                self._phase = ClimaxPhase.IN_POSITION_PHASE_2
        self.log.warning(f"Order rejected: {event.reason}")

    # ------------------------------------------------------------------
    # Strategy logic
    # ------------------------------------------------------------------

    def _on_strategy_bar(self, bar: StrategyBar) -> None:
        previous_bar = self._bars[-1] if self._bars else None
        self._bars.append(bar)

        if self._phase == ClimaxPhase.PENDING_ENTRY:
            self._bars_since_climax += 1
            if self._climax and self._bars_since_climax > self._climax.expires_after_bar_count:
                self._clear_climax()

        if self._phase in (ClimaxPhase.IN_POSITION_PHASE_1, ClimaxPhase.IN_POSITION_PHASE_2):
            self._maybe_activate_trailing(bar.close)
            if self._phase == ClimaxPhase.IN_POSITION_PHASE_2 and previous_bar is not None:
                self._raise_stop(previous_bar.low)
            self._maybe_exit(bar.low)
            return

        if self._phase != ClimaxPhase.SEARCHING:
            self._maybe_enter(bar.close)
            return

        climax = self._detect_climax()
        if climax is not None:
            self._climax = climax
            self._bars_since_climax = 0
            self._phase = ClimaxPhase.PENDING_ENTRY
            self.log.info(
                f"Climax detected high={climax.high:.2f} low={climax.low:.2f} "
                f"stop={climax.initial_stop:.2f}"
            )

    def _detect_climax(self) -> Optional[ClimaxEvent]:
        cfg = self._config
        required = max(cfg.lookback_bars + 1, cfg.atr_period + 1)
        if len(self._bars) < required:
            return None

        current = self._bars[-1]
        window = list(self._bars)[-cfg.lookback_bars:]
        prior_volume_window = list(self._bars)[-(cfg.lookback_bars + 1):-1]
        atr_window = list(self._bars)[-(cfg.atr_period + 1):]

        window_high = max(b.high for b in window)
        window_low = min(b.low for b in window)
        if window_high <= 0.0:
            return None

        waterfall = (window_high - window_low) / window_high
        if waterfall <= cfg.waterfall_drop_pct:
            return None
        if current.low > window_low:
            return None

        avg_volume = self._sma_volume(prior_volume_window)
        if avg_volume <= 0.0 or current.volume < cfg.volume_multiple * avg_volume:
            return None

        atr = self._atr(atr_window)
        initial_stop = self._initial_stop(
            entry_ref=current.high,
            climax_low=current.low,
            atr=atr,
            atr_stop_multiple=cfg.atr_stop_multiple,
            min_stop_distance_pct=cfg.min_stop_distance_pct,
        )
        return ClimaxEvent(
            high=current.high,
            low=current.low,
            atr=atr,
            initial_stop=initial_stop,
            ts_event=current.ts_event,
            expires_after_bar_count=cfg.pending_entry_ttl_bars,
        )

    def _maybe_enter(self, ask_price: float) -> None:
        if self._phase != ClimaxPhase.PENDING_ENTRY:
            return
        if self._active_entry_order_id is not None or self._climax is None:
            return
        if ask_price < self._climax.high:
            return
        max_ask = self._climax.high * (1.0 + self._config.entry_slippage_cap_pct)
        if ask_price > max_ask:
            return

        qty = self._compute_order_quantity(ask_price, self._climax.initial_stop)
        if qty <= 0.0:
            self.log.warning("Climax entry skipped: computed quantity is zero")
            return

        order = self.order_factory.market(
            instrument_id=self._instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity(qty, self._instrument.size_precision),
            time_in_force=TimeInForce.IOC,
        )
        self._active_entry_order_id = order.client_order_id
        self._phase = ClimaxPhase.ENTERING
        self.submit_order(order)
        self.log.info(f"Submitted v-climax BUY qty={qty} ask={ask_price:.2f}")

    def _maybe_activate_trailing(self, mark_price: float) -> None:
        if self._phase != ClimaxPhase.IN_POSITION_PHASE_1 or self._entry_price is None:
            return
        if self._is_net_profitable(mark_price, self._entry_price, self._config.round_trip_taker_fee_pct):
            self._phase = ClimaxPhase.IN_POSITION_PHASE_2

    def _maybe_exit(self, bid_or_mark_price: float) -> None:
        if self._phase not in (ClimaxPhase.IN_POSITION_PHASE_1, ClimaxPhase.IN_POSITION_PHASE_2):
            return
        if self._active_exit_order_id is not None or self._active_stop is None:
            return
        if bid_or_mark_price > self._active_stop:
            return

        qty = self._position_quantity()
        if qty <= 0.0:
            qty = self._entry_qty or 0.0
        if qty <= 0.0:
            return

        order = self.order_factory.market(
            instrument_id=self._instrument_id,
            order_side=OrderSide.SELL,
            quantity=Quantity(qty, self._instrument.size_precision),
            time_in_force=TimeInForce.IOC,
        )
        self._active_exit_order_id = order.client_order_id
        self._phase = ClimaxPhase.EXITING
        self.submit_order(order)
        self.log.info(f"Submitted v-climax SELL stop={self._active_stop:.2f} px={bid_or_mark_price:.2f}")

    def _compute_order_quantity(self, entry_price: float, stop_price: float) -> float:
        if self._instrument is None or entry_price <= stop_price:
            return 0.0
        equity = self._account_equity()
        risk_amount = equity * self._config.risk_fraction
        raw_qty = risk_amount / (entry_price - stop_price)
        return self._round_quantity_down(raw_qty, self._instrument)

    def _account_equity(self) -> float:
        if self._instrument_id is not None:
            account = self.portfolio.account(self._instrument_id.venue)
            if account:
                try:
                    return float(account.balance_total().as_double())
                except Exception:
                    pass
        return self._config.fallback_account_equity

    def _position_quantity(self) -> float:
        if self._instrument_id is None:
            return 0.0
        open_positions = self.cache.positions_open(instrument_id=self._instrument_id)
        position = open_positions[0] if open_positions else None
        if position is None or not position.is_long:
            return 0.0
        return float(position.quantity)

    def _raise_stop(self, candidate_stop: float) -> None:
        if self._active_stop is None:
            self._active_stop = candidate_stop
        else:
            self._active_stop = max(self._active_stop, candidate_stop)

    def _clear_climax(self) -> None:
        self._climax = None
        self._bars_since_climax = 0
        self._phase = ClimaxPhase.SEARCHING

    def _reset_after_exit(self) -> None:
        self._phase = ClimaxPhase.SEARCHING
        self._climax = None
        self._bars_since_climax = 0
        self._entry_price = None
        self._entry_qty = None
        self._active_stop = None

    def _add_source_bar(self, bar: Bar) -> Optional[StrategyBar]:
        self._source_bucket.append(bar)
        ratio = self._config.bar_minutes // self._config.source_bar_minutes
        if ratio <= 1:
            self._source_bucket.clear()
            return self._to_strategy_bar([bar])
        if len(self._source_bucket) < ratio:
            return None
        source_bars = self._source_bucket[:ratio]
        del self._source_bucket[:ratio]
        return self._to_strategy_bar(source_bars)

    def _warmup_source_bar_count(self) -> int:
        strategy_bars = max(self._config.lookback_bars, self._config.atr_period) + 2
        return strategy_bars * max(1, self._config.bar_minutes // self._config.source_bar_minutes)

    @staticmethod
    def _to_strategy_bar(bars: list[Bar]) -> StrategyBar:
        return StrategyBar(
            open=float(bars[0].open),
            high=max(float(b.high) for b in bars),
            low=min(float(b.low) for b in bars),
            close=float(bars[-1].close),
            volume=sum(float(b.volume) for b in bars),
            ts_event=bars[-1].ts_event,
        )

    @staticmethod
    def _sma_volume(bars: list[StrategyBar]) -> float:
        if not bars:
            return 0.0
        return sum(b.volume for b in bars) / len(bars)

    @staticmethod
    def _atr(bars: list[StrategyBar]) -> float:
        if len(bars) < 2:
            return 0.0
        true_ranges = []
        for previous, current in zip(bars, bars[1:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        return sum(true_ranges) / len(true_ranges)

    @staticmethod
    def _initial_stop(
        entry_ref: float,
        climax_low: float,
        atr: float,
        atr_stop_multiple: float,
        min_stop_distance_pct: float,
    ) -> float:
        atr_stop = climax_low - (atr_stop_multiple * atr)
        min_distance_stop = entry_ref * (1.0 - min_stop_distance_pct)
        return min(atr_stop, min_distance_stop)

    @staticmethod
    def _round_quantity_down(raw_qty: float, instrument: Instrument) -> float:
        if raw_qty <= 0.0:
            return 0.0
        increment = float(instrument.size_increment)
        precision = instrument.size_precision
        if increment <= 0.0:
            return round(raw_qty, precision)
        rounded = floor(raw_qty / increment) * increment
        min_quantity = float(instrument.min_quantity) if instrument.min_quantity else 0.0
        if rounded < min_quantity:
            return 0.0
        return round(rounded, precision)

    @staticmethod
    def _is_net_profitable(mark_price: float, entry_price: float, round_trip_fee_pct: float) -> bool:
        return mark_price >= entry_price * (1.0 + round_trip_fee_pct)
