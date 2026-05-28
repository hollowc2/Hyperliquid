"""Multi-timeframe BTC trend-following strategy."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from math import floor
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

from hl_engine.config.trend_follow_config import TrendFollowConfig


class TrendRegime(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    MIXED = "MIXED"
    WARMING_UP = "WARMING_UP"


@dataclass(frozen=True)
class TrendBar:
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts_event: int


@dataclass
class TimeframeState:
    minutes: int
    bucket: list[Bar]
    bars: deque[TrendBar]
    fast_ema: Optional[float] = None
    slow_ema: Optional[float] = None
    atr: Optional[float] = None
    regime: TrendRegime = TrendRegime.WARMING_UP


class TrendFollowStrategy(Strategy):
    """
    Closed-1h EMA trend-following strategy with 4h/1d confirmation.

    The live feed provides configurable source bars. This strategy aggregates
    closed 15m, 1h, 4h, and 1d bars internally, evaluates entries only on a
    closed 1h bar, and manages exits with an ATR trailing stop plus regime
    invalidation.
    """

    TIMEFRAMES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}

    def __init__(self, config: TrendFollowConfig) -> None:
        super().__init__(config=config)
        self._config = config
        self._instrument_id: Optional[InstrumentId] = None
        self._instrument: Optional[Instrument] = None
        self._timeframes = self._build_timeframe_states(config)
        self._active_entry_order_id = None
        self._active_exit_order_id = None
        self._active_side = "FLAT"
        self._entry_price: Optional[float] = None
        self._entry_qty: Optional[float] = None
        self._stop_price: Optional[float] = None
        self._last_signal_reason = "warming_up"
        self._last_state_push_ns = 0
        self._live_started_ns = 0
        self._skip_historical_warmup_orders = False

    def on_start(self) -> None:
        self._instrument_id = InstrumentId.from_str(self._config.instrument_id)
        self._instrument = self.cache.instrument(self._instrument_id)
        if self._instrument is None:
            self.log.error(f"Instrument not found in cache: {self._instrument_id}")
            return

        self._live_started_ns = time.time_ns()
        self._skip_historical_warmup_orders = bool(os.getenv("ORCHESTRATOR_REST_URL"))
        self._sync_position_from_orchestrator()
        bar_type = BarType.from_str(
            f"{self._config.instrument_id}-{self._config.source_bar_minutes}-MINUTE-LAST-EXTERNAL"
        )
        self.subscribe_bars(bar_type)
        self.request_bars(
            bar_type=bar_type,
            start=datetime(2020, 1, 1, tzinfo=timezone.utc),
            limit=self._warmup_source_bar_count(),
        )
        self.log.info(
            "TrendFollowStrategy started | "
            f"instrument={self._instrument_id} trade_bar={self._config.trade_bar_minutes}m "
            f"confirm={self._config.confirmation_timeframes}"
        )
        self._push_state_snapshot()

    def on_stop(self) -> None:
        if self._instrument_id:
            self.cancel_all_orders(self._instrument_id)
        self.log.info("TrendFollowStrategy stopped")

    def on_bar(self, bar: Bar) -> None:
        is_historical_warmup = self._is_historical_warmup_bar(bar)
        closed = self._add_source_bar_to_timeframes(bar)
        if not is_historical_warmup:
            self._check_intrabar_stop(bar)
        for name, trend_bar in closed:
            self._update_timeframe_indicators(name)
            if name == self._trade_timeframe_name() and not is_historical_warmup:
                self._on_trade_bar(trend_bar)
        if closed and not is_historical_warmup:
            self._push_state_snapshot(min_interval_secs=5.0)

    def on_order_filled(self, event) -> None:
        if self._active_entry_order_id and event.client_order_id == self._active_entry_order_id:
            self._active_entry_order_id = None
            self._entry_price = float(event.last_px)
            self._entry_qty = float(event.last_qty)
            self._active_side = "LONG" if event.order_side == OrderSide.BUY else "SHORT"
            self.log.info(
                f"Trend entry filled side={self._active_side} qty={event.last_qty} "
                f"px={event.last_px} stop={self._stop_price}"
            )
            self._push_state_snapshot()
            return

        if self._active_exit_order_id and event.client_order_id == self._active_exit_order_id:
            self._active_exit_order_id = None
            self._reset_position_state()
            self.log.info(f"Trend exit filled qty={event.last_qty} px={event.last_px}")
            self._push_state_snapshot()

    def on_order_canceled(self, event) -> None:
        if self._active_entry_order_id and event.client_order_id == self._active_entry_order_id:
            self._active_entry_order_id = None
        if self._active_exit_order_id and event.client_order_id == self._active_exit_order_id:
            self._active_exit_order_id = None
        self._push_state_snapshot()

    def on_order_rejected(self, event) -> None:
        if self._active_entry_order_id and event.client_order_id == self._active_entry_order_id:
            self._active_entry_order_id = None
            self._reset_position_state()
        if self._active_exit_order_id and event.client_order_id == self._active_exit_order_id:
            self._active_exit_order_id = None
        self.log.warning(f"Order rejected: {event.reason}")
        self._push_state_snapshot()

    def _on_trade_bar(self, bar: TrendBar) -> None:
        self._trail_stop(bar)
        signal = self._aligned_signal()
        if self._active_side != "FLAT":
            if self._position_invalidated(signal):
                self._submit_exit("regime_invalidated")
            else:
                self._check_bar_stop(bar)
            return
        if self._active_entry_order_id is not None or self._active_exit_order_id is not None:
            return
        if signal not in {"LONG", "SHORT"}:
            self._last_signal_reason = signal.lower()
            return
        if signal == "LONG" and not self._config.allow_long:
            self._last_signal_reason = "long_disabled"
            return
        if signal == "SHORT" and not self._config.allow_short:
            self._last_signal_reason = "short_disabled"
            return
        if not self._entry_filter_allows(signal):
            self._last_signal_reason = "entry_filter_blocked"
            return

        stop = self._initial_stop(signal, bar.close)
        qty = self._compute_order_quantity(bar.close, stop)
        if qty <= 0.0:
            self._last_signal_reason = "zero_qty_or_invalid_atr"
            return

        self._stop_price = stop
        self._submit_entry(signal, qty)
        self._last_signal_reason = f"{signal.lower()}_entry"

    def _submit_entry(self, side: str, qty: float) -> None:
        order_side = OrderSide.BUY if side == "LONG" else OrderSide.SELL
        order = self.order_factory.market(
            instrument_id=self._instrument_id,
            order_side=order_side,
            quantity=Quantity(qty, self._instrument.size_precision),
            time_in_force=TimeInForce.IOC,
        )
        self._active_entry_order_id = order.client_order_id
        self.submit_order(order)
        self.log.info(f"Submitted trend {side} entry qty={qty} stop={self._stop_price}")

    def _submit_exit(self, reason: str) -> None:
        if self._active_exit_order_id is not None or self._active_side == "FLAT":
            return
        qty = self._position_quantity()
        if qty <= 0.0:
            qty = self._entry_qty or 0.0
        if qty <= 0.0:
            return
        order_side = OrderSide.SELL if self._active_side == "LONG" else OrderSide.BUY
        order = self.order_factory.market(
            instrument_id=self._instrument_id,
            order_side=order_side,
            quantity=Quantity(qty, self._instrument.size_precision),
            time_in_force=TimeInForce.IOC,
        )
        self._active_exit_order_id = order.client_order_id
        self.submit_order(order)
        self._last_signal_reason = reason
        self.log.info(f"Submitted trend exit reason={reason} side={self._active_side}")

    def _aligned_signal(self) -> str:
        trade_tf = self._timeframes[self._trade_timeframe_name()]
        regimes = [trade_tf.regime]
        for name in self._config.confirmation_timeframes:
            tf = self._timeframes.get(name)
            if tf is not None:
                regimes.append(tf.regime)
        return self.classify_alignment(regimes, self._config.strict_confirmation)

    def _entry_filter_allows(self, side: str) -> bool:
        if not self._config.use_entry_filter:
            return True
        tf = self._timeframes.get(self._config.entry_filter_timeframe)
        if tf is None:
            return True
        if tf.regime == TrendRegime.WARMING_UP:
            return True
        expected = TrendRegime.BULLISH if side == "LONG" else TrendRegime.BEARISH
        return tf.regime == expected

    def _position_invalidated(self, signal: str) -> bool:
        if self._active_side == "LONG":
            return signal in {"SHORT", "MIXED"}
        if self._active_side == "SHORT":
            return signal in {"LONG", "MIXED"}
        return False

    def _trail_stop(self, bar: TrendBar) -> None:
        if self._active_side == "FLAT" or self._stop_price is None:
            return
        atr = self._timeframes[self._trade_timeframe_name()].atr
        if atr is None or atr <= 0.0:
            return
        distance = self._config.atr_stop_multiple * atr
        if self._active_side == "LONG":
            self._stop_price = max(self._stop_price, bar.close - distance)
        elif self._active_side == "SHORT":
            self._stop_price = min(self._stop_price, bar.close + distance)

    def _check_intrabar_stop(self, bar: Bar) -> None:
        if self._active_side == "FLAT" or self._stop_price is None:
            return
        trend_bar = self._to_trend_bar([bar])
        self._check_bar_stop(trend_bar)

    def _check_bar_stop(self, bar: TrendBar) -> None:
        if self._active_side == "LONG" and bar.low <= self._stop_price:
            self._submit_exit("atr_stop")
        elif self._active_side == "SHORT" and bar.high >= self._stop_price:
            self._submit_exit("atr_stop")

    def _initial_stop(self, side: str, entry_price: float) -> float:
        atr = self._timeframes[self._trade_timeframe_name()].atr
        if atr is None or atr <= 0.0:
            return entry_price
        distance = max(
            self._config.atr_stop_multiple * atr,
            entry_price * self._config.min_stop_distance_pct,
        )
        if side == "LONG":
            return entry_price - distance
        return entry_price + distance

    def _compute_order_quantity(self, entry_price: float, stop_price: float) -> float:
        if self._instrument is None:
            return 0.0
        distance = abs(entry_price - stop_price)
        if entry_price <= 0.0 or distance <= 0.0:
            return 0.0
        return self.capped_risk_sized_quantity(
            equity=self._account_equity(),
            risk_fraction=self._config.risk_fraction,
            entry_price=entry_price,
            stop_price=stop_price,
            max_position_usd=self._config.max_position_usd,
            instrument=self._instrument,
        )

    def _sync_position_from_orchestrator(self) -> None:
        base_url = os.getenv("ORCHESTRATOR_REST_URL", "").rstrip("/")
        strategy_id = os.getenv("STRATEGY_ID", "trend-follow-btc")
        if not base_url:
            return

        try:
            with urlopen(f"{base_url}/account/{strategy_id}", timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            self.log.warning(f"Could not sync starting position from orchestrator: {exc}")
            return

        try:
            position = self._extract_orchestrator_position(payload.get("account_state", {}))
        except (TypeError, ValueError) as exc:
            self.log.warning(f"Invalid orchestrator account position: {exc}")
            return

        signed_qty = position["signed_qty"]
        if abs(signed_qty) < 1e-12:
            return
        self._active_side = "LONG" if signed_qty > 0 else "SHORT"
        self._entry_qty = abs(signed_qty)
        self._entry_price = position.get("avg_px")
        self.log.info(
            f"Restored starting trend position from orchestrator: "
            f"side={self._active_side} qty={self._entry_qty:.8f} avg_px={self._entry_price}"
        )

    def _extract_orchestrator_position(self, account_state: dict) -> dict:
        paper_state = account_state.get("paper") or {}
        if "position_qty" in paper_state:
            qty = float(paper_state["position_qty"])
            avg_px = float(paper_state.get("avg_price") or 0.0)
            return {"signed_qty": qty, "avg_px": avg_px}

        for item in account_state.get("assetPositions") or []:
            position = item.get("position") or {}
            if position.get("coin") not in (None, "BTC"):
                continue
            qty = float(position.get("szi", 0.0))
            avg_px = float(position.get("entryPx") or 0.0)
            return {"signed_qty": qty, "avg_px": avg_px}

        return {"signed_qty": 0.0, "avg_px": 0.0}

    def _account_equity(self) -> float:
        if self._instrument_id is not None:
            account = self.portfolio.account(self._instrument_id.venue)
            if account:
                try:
                    return float(account.balance_total().as_double())
                except Exception:
                    pass
        return self._config.initial_balance_usdc

    def _position_quantity(self) -> float:
        if self._instrument_id is None:
            return 0.0
        open_positions = self.cache.positions_open(instrument_id=self._instrument_id)
        if not open_positions:
            return 0.0
        return float(open_positions[0].quantity)

    def _position_state(self) -> dict:
        if self._instrument_id is None:
            return {"side": self._active_side, "qty": self._entry_qty or 0.0}
        open_positions = self.cache.positions_open(instrument_id=self._instrument_id)
        if not open_positions:
            return {"side": self._active_side, "qty": self._entry_qty or 0.0}
        position = open_positions[0]
        signed_qty = float(position.quantity) * (1.0 if position.is_long else -1.0)
        return {
            "side": "LONG" if position.is_long else "SHORT",
            "qty": abs(signed_qty),
            "signed_qty": signed_qty,
            "avg_px": float(position.avg_px_open),
        }

    def _push_state_snapshot(self, min_interval_secs: float = 0.0) -> None:
        base_url = os.getenv("ORCHESTRATOR_REST_URL", "")
        strategy_id = os.getenv("STRATEGY_ID", "trend-follow-btc")
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
        trade_tf = self._timeframes[self._trade_timeframe_name()]
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "instrument": str(self._instrument_id) if self._instrument_id else self._config.instrument_id,
            "currency": "USDC",
            "position": self._position_state(),
            "trend": {
                "phase": self._active_side if self._active_side != "FLAT" else self._aligned_signal(),
                "timeframe_regimes": {
                    name: state.regime.value for name, state in self._timeframes.items()
                },
                "active_side": self._active_side,
                "entry_price": self._entry_price,
                "stop_price": self._stop_price,
                "atr": trade_tf.atr,
                "position_qty": self._entry_qty or 0.0,
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

    def _reset_position_state(self) -> None:
        self._active_side = "FLAT"
        self._entry_price = None
        self._entry_qty = None
        self._stop_price = None

    def _add_source_bar_to_timeframes(self, bar: Bar) -> list[tuple[str, TrendBar]]:
        closed: list[tuple[str, TrendBar]] = []
        for name, state in self._timeframes.items():
            state.bucket.append(bar)
            ratio = state.minutes // self._config.source_bar_minutes
            if len(state.bucket) < ratio:
                continue
            source_bars = state.bucket[:ratio]
            del state.bucket[:ratio]
            trend_bar = self._to_trend_bar(source_bars)
            state.bars.append(trend_bar)
            closed.append((name, trend_bar))
        return closed

    def _update_timeframe_indicators(self, name: str) -> None:
        state = self._timeframes[name]
        closes = [b.close for b in state.bars]
        state.fast_ema = self.ema(closes, self._config.fast_ema_period)
        state.slow_ema = self.ema(closes, self._config.slow_ema_period)
        state.atr = self.atr(list(state.bars)[-(self._config.atr_period + 1):])
        state.regime = self.classify_ema_regime(
            closes,
            self._config.fast_ema_period,
            self._config.slow_ema_period,
        )

    def _warmup_source_bar_count(self) -> int:
        max_minutes = max(state.minutes for state in self._timeframes.values())
        bars_needed = max(self._config.slow_ema_period, self._config.atr_period + 1) + 2
        return bars_needed * max(1, max_minutes // self._config.source_bar_minutes)

    def _trade_timeframe_name(self) -> str:
        return self.timeframe_name_for_minutes(self._config.trade_bar_minutes)

    def _is_historical_warmup_bar(self, bar: Bar) -> bool:
        if not self._skip_historical_warmup_orders or self._live_started_ns <= 0:
            return False
        # REST warmup bars are injected with ts_init near their historical event
        # time. Live ZMQ bars use the orchestrator wall clock as ts_init.
        return int(bar.ts_init) < self._live_started_ns - 5_000_000_000

    @classmethod
    def timeframe_name_for_minutes(cls, minutes: int) -> str:
        for name, timeframe_minutes in cls.TIMEFRAMES.items():
            if timeframe_minutes == minutes:
                return name
        raise ValueError(f"Unsupported trade_bar_minutes: {minutes}")

    @classmethod
    def _build_timeframe_states(cls, config: TrendFollowConfig) -> dict[str, TimeframeState]:
        names = {cls.timeframe_name_for_minutes(config.trade_bar_minutes), *config.confirmation_timeframes}
        if config.entry_filter_timeframe:
            names.add(config.entry_filter_timeframe)
        for name in names:
            if name not in cls.TIMEFRAMES:
                raise ValueError(f"Unsupported timeframe: {name}")
        maxlen = max(config.slow_ema_period, config.atr_period + 1) + 5
        return {
            name: TimeframeState(minutes=cls.TIMEFRAMES[name], bucket=[], bars=deque(maxlen=maxlen))
            for name in sorted(names, key=lambda item: cls.TIMEFRAMES[item])
        }

    @staticmethod
    def _to_trend_bar(bars: list[Bar]) -> TrendBar:
        return TrendBar(
            open=float(bars[0].open),
            high=max(float(b.high) for b in bars),
            low=min(float(b.low) for b in bars),
            close=float(bars[-1].close),
            volume=sum(float(b.volume) for b in bars),
            ts_event=bars[-1].ts_event,
        )

    @staticmethod
    def ema(values: list[float], period: int) -> Optional[float]:
        if period <= 0 or len(values) < period:
            return None
        alpha = 2.0 / (period + 1.0)
        ema_value = sum(values[:period]) / period
        for value in values[period:]:
            ema_value = value * alpha + ema_value * (1.0 - alpha)
        return ema_value

    @classmethod
    def classify_ema_regime(cls, closes: list[float], fast_period: int, slow_period: int) -> TrendRegime:
        fast = cls.ema(closes, fast_period)
        slow = cls.ema(closes, slow_period)
        if fast is None or slow is None:
            return TrendRegime.WARMING_UP
        if fast > slow:
            return TrendRegime.BULLISH
        if fast < slow:
            return TrendRegime.BEARISH
        return TrendRegime.MIXED

    @staticmethod
    def classify_alignment(regimes: list[TrendRegime], strict: bool = True) -> str:
        if any(regime == TrendRegime.WARMING_UP for regime in regimes):
            return "WARMING_UP"
        if strict and all(regime == TrendRegime.BULLISH for regime in regimes):
            return "LONG"
        if strict and all(regime == TrendRegime.BEARISH for regime in regimes):
            return "SHORT"
        if not strict:
            bullish = sum(regime == TrendRegime.BULLISH for regime in regimes)
            bearish = sum(regime == TrendRegime.BEARISH for regime in regimes)
            if bullish > bearish:
                return "LONG"
            if bearish > bullish:
                return "SHORT"
        return "MIXED"

    @staticmethod
    def atr(bars: list[TrendBar]) -> Optional[float]:
        if len(bars) < 2:
            return None
        true_ranges = []
        for previous, current in zip(bars, bars[1:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        return sum(true_ranges) / len(true_ranges) if true_ranges else None

    @staticmethod
    def stop_price(side: str, entry_price: float, atr: float, multiple: float, min_distance_pct: float) -> float:
        if entry_price <= 0.0 or atr <= 0.0:
            return entry_price
        distance = max(atr * multiple, entry_price * min_distance_pct)
        return entry_price - distance if side == "LONG" else entry_price + distance

    @staticmethod
    def risk_sized_quantity(
        equity: float,
        risk_fraction: float,
        entry_price: float,
        stop_price: float,
        instrument: Instrument,
    ) -> float:
        distance = abs(entry_price - stop_price)
        if equity <= 0.0 or risk_fraction <= 0.0 or distance <= 0.0:
            return 0.0
        return TrendFollowStrategy._round_quantity_down(equity * risk_fraction / distance, instrument)

    @staticmethod
    def capped_risk_sized_quantity(
        equity: float,
        risk_fraction: float,
        entry_price: float,
        stop_price: float,
        max_position_usd: float,
        instrument: Instrument,
    ) -> float:
        distance = abs(entry_price - stop_price)
        if equity <= 0.0 or risk_fraction <= 0.0 or entry_price <= 0.0 or distance <= 0.0:
            return 0.0
        risk_qty = equity * risk_fraction / distance
        max_qty = max_position_usd / entry_price if max_position_usd > 0.0 else risk_qty
        return TrendFollowStrategy._round_quantity_down(min(risk_qty, max_qty), instrument)

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
