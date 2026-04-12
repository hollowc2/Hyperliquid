"""
ApexStrategy — Central orchestrator for the APEX trading system.

Inherits from NautilusTrader Strategy and coordinates:
  - Data subscriptions and feature updates
  - Signal generation via BayesianEdgeModel
  - Risk checks via ExposureManager
  - Order routing via OrderRouter
  - Position management

Signal evaluation is throttled to at most once per 100ms to prevent
CPU overload from high-frequency order book updates.
"""

from typing import Optional

from nautilus_trader.model.data import Bar, BarType, OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import (
    BarAggregation,
    BookType,
    OrderSide,
    OrderType,
    PriceType,
    TimeInForce,
)
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import LimitOrder, MarketOrder
from nautilus_trader.trading.strategy import Strategy

from hl_engine.config.apex_config import ApexStrategyConfig
from hl_engine.data.types import FundingRateData, LiquidationData, OpenInterestData
from hl_engine.execution.order_router import OrderRouter
from hl_engine.execution.slippage_model import SlippageModel
from hl_engine.features.orderbook_features import OrderBookFeatures
from hl_engine.features.trade_features import TradeFlowFeatures
from hl_engine.features.volatility_features import VolatilityFeatures
from hl_engine.models.bayesian_model import BayesianEdgeModel, FeatureVector
from hl_engine.models.cascade_model import LiquidationCascadeModel
from hl_engine.models.funding_model import FundingPressureModel
from hl_engine.models.hawkes_model import HawkesProcess
from hl_engine.models.regime_detector import RegimeDetector
from hl_engine.risk.exposure_manager import ExposureManager
from hl_engine.risk.kelly_sizing import KellySizer


class ApexStrategy(Strategy):
    """
    Main APEX trading strategy.

    Architecture:
      on_order_book_deltas → throttled signal eval (100ms)
      on_trade_tick → update trade features + Hawkes process
      on_bar → update volatility features, price momentum
      on_data → dispatch funding/liquidation/OI custom data
      _maybe_evaluate_signal → build FeatureVector → edge → act
      _act_on_edge → Kelly size → exposure check → route → submit
    """

    def __init__(self, config: ApexStrategyConfig) -> None:
        super().__init__(config=config)

        self._config = config
        apex_cfg = config.apex_config

        # --- Feature extractors ---
        self._ob_features = OrderBookFeatures()
        self._trade_features = TradeFlowFeatures(
            window_ns=apex_cfg.feature.tfi_window_ns if apex_cfg else 60_000_000_000
        )
        self._vol_features = VolatilityFeatures(
            short_window=apex_cfg.feature.vol_short_window if apex_cfg else 20,
            long_window=apex_cfg.feature.vol_long_window if apex_cfg else 100,
        )

        # --- Models ---
        model_cfg = apex_cfg.model if apex_cfg else None
        self._hawkes = HawkesProcess(
            mu=model_cfg.hawkes_mu if model_cfg else 0.1,
            alpha=model_cfg.hawkes_alpha if model_cfg else 0.3,
            beta=model_cfg.hawkes_beta if model_cfg else 1.0,
        )
        self._cascade_model = LiquidationCascadeModel(
            cascade_threshold=(
                apex_cfg.execution.cascade_threshold if apex_cfg else 1.5
            )
        )
        self._funding_model = FundingPressureModel(
            history_len=model_cfg.funding_history_len if model_cfg else 168
        )
        self._regime_detector = RegimeDetector(
            vol_ratio_threshold=model_cfg.regime_vol_ratio_threshold if model_cfg else 1.5,
            trend_threshold=model_cfg.regime_trend_threshold if model_cfg else 2.0,
            min_liquidity_usd=model_cfg.regime_min_liquidity_usd if model_cfg else 50_000.0,
        )
        self._edge_model = BayesianEdgeModel(
            w1=model_cfg.w1_obi if model_cfg else 0.30,
            w2=model_cfg.w2_tfi if model_cfg else 0.30,
            w3=model_cfg.w3_mp_drift if model_cfg else 0.20,
            w4=model_cfg.w4_hawkes if model_cfg else 0.10,
            w5=model_cfg.w5_cascade if model_cfg else 0.05,
            w6=model_cfg.w6_funding if model_cfg else 0.05,
        )

        # --- Risk ---
        risk_cfg = apex_cfg.risk if apex_cfg else None
        self._kelly_sizer = KellySizer(
            kelly_fraction=risk_cfg.kelly_fraction if risk_cfg else 0.25,
            max_kelly_fraction=risk_cfg.max_kelly_fraction if risk_cfg else 0.20,
            inventory_penalty_scale=risk_cfg.inventory_penalty_scale if risk_cfg else 0.5,
            max_position_usd=risk_cfg.max_position_usd if risk_cfg else 10_000.0,
        )
        # ExposureManager initialized in on_start() with portfolio data
        self._exposure_manager: Optional[ExposureManager] = None

        # --- Execution ---
        self._slippage_model = SlippageModel()
        exec_cfg = apex_cfg.execution if apex_cfg else None
        self._order_router = OrderRouter(
            min_queue_prob=exec_cfg.min_queue_prob if exec_cfg else 0.3
        )
        self._min_edge = exec_cfg.min_edge_threshold if exec_cfg else 0.002
        self._signal_throttle_ns = (
            (exec_cfg.signal_throttle_ms if exec_cfg else 100) * 1_000_000
        )

        # --- State variables ---
        self._instrument_id: Optional[InstrumentId] = None
        self._instrument: Optional[Instrument] = None
        self._active_order_id = None
        self._last_signal_ts: int = 0

        # Latest feature values (updated incrementally)
        self._latest_obi: float = 0.0
        self._latest_tfi: float = 0.0
        self._latest_mp_drift: float = 0.0
        self._latest_spread: float = 0.0
        self._latest_book_depth: float = 0.0
        self._latest_hawkes: float = 0.0
        self._funding_pressure: float = 0.0
        self._price_momentum: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to data feeds and initialize risk manager."""
        instrument_id_str = self._config.instrument_id
        self._instrument_id = InstrumentId.from_str(instrument_id_str)
        self._instrument = self.cache.instrument(self._instrument_id)

        if self._instrument is None:
            self.log.error(f"Instrument not found in cache: {self._instrument_id}")
            return

        apex_cfg = self._config.apex_config
        risk_cfg = apex_cfg.risk if apex_cfg else None

        self._exposure_manager = ExposureManager(
            max_position_usd=risk_cfg.max_position_usd if risk_cfg else 10_000.0,
            max_leverage=risk_cfg.max_leverage if risk_cfg else 5.0,
            drawdown_limit=risk_cfg.drawdown_limit if risk_cfg else 0.15,
            drawdown_reduce_threshold=risk_cfg.drawdown_reduce_threshold if risk_cfg else 0.10,
        )

        # Subscribe to order book (L2 MBP)
        self.subscribe_order_book_deltas(
            instrument_id=self._instrument_id,
            book_type=BookType.L2_MBP,
        )

        # Subscribe to trade ticks
        self.subscribe_trade_ticks(self._instrument_id)

        # Subscribe to 1-minute bars
        bar_type = BarType.from_str(f"{instrument_id_str}-1-MINUTE-LAST-EXTERNAL")
        self.subscribe_bars(bar_type)

        # Subscribe to custom data types
        from nautilus_trader.model.data import DataType
        self.subscribe_data(
            data_type=DataType(FundingRateData, metadata={"instrument_id": self._instrument_id}),
            client_id=None,
        )
        self.subscribe_data(
            data_type=DataType(LiquidationData),
            client_id=None,
        )
        self.subscribe_data(
            data_type=DataType(OpenInterestData, metadata={"instrument_id": self._instrument_id}),
            client_id=None,
        )

        # Request historical bars for model warmup (200 bars)
        # Use a start far enough back to cover warmup; limit caps the result.
        from datetime import datetime, timezone
        warmup_start = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.request_bars(
            bar_type=bar_type,
            start=warmup_start,
            limit=200,
        )

        self.log.info(f"ApexStrategy started for {self._instrument_id}")

    def on_stop(self) -> None:
        """Cancel all open orders on stop."""
        if self._instrument_id:
            self.cancel_all_orders(self._instrument_id)
        self.log.info("ApexStrategy stopped")

    # ------------------------------------------------------------------
    # Data event handlers
    # ------------------------------------------------------------------

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        """Update book features and throttle signal evaluation."""
        book = self.cache.order_book(self._instrument_id)
        if book is None:
            return

        # Compute order book features
        self._latest_obi = self._ob_features.compute_obi(book)
        _, mp_drift = self._ob_features.compute_microprice(book)
        self._latest_spread = self._ob_features.compute_spread(book)
        bid_usd, ask_usd = self._ob_features.compute_book_depth_usd(book)
        self._latest_book_depth = bid_usd + ask_usd

        # Normalize microprice drift by spread (or mid)
        best_bid = book.best_bid_price()
        best_ask = book.best_ask_price()
        if best_bid and best_ask:
            mid = (float(best_bid) + float(best_ask)) / 2.0
            self._latest_mp_drift = mp_drift / mid if mid > 0 else 0.0
        else:
            self._latest_mp_drift = 0.0

        # Throttle signal evaluation to 100ms
        now = self.clock.timestamp_ns()
        if (now - self._last_signal_ts) >= self._signal_throttle_ns:
            self._maybe_evaluate_signal(book)
            self._last_signal_ts = now

    def on_trade_tick(self, tick: TradeTick) -> None:
        """Update trade flow features and Hawkes intensity."""
        self._trade_features.update(tick)
        self._latest_tfi = self._trade_features.compute_tfi()
        self._latest_hawkes = self._hawkes.update(tick.ts_event)

    def on_bar(self, bar: Bar) -> None:
        """Update volatility features and price momentum."""
        self._vol_features.update(bar)

        # Price momentum: log return of most recent bar
        if self._vol_features._last_close and float(bar.open) > 0:
            import math
            self._price_momentum = math.log(
                float(bar.close) / float(bar.open)
            )

    def on_data(self, data) -> None:
        """Dispatch custom data types to the appropriate model."""
        if isinstance(data, FundingRateData):
            self._funding_model.update_funding(data.rate)
            self._funding_pressure = self._funding_model.compute_pressure(
                self._price_momentum
            )

        elif isinstance(data, LiquidationData):
            self._cascade_model.update_liquidation(data)

        elif isinstance(data, OpenInterestData):
            self._cascade_model.update_oi(data)
            self._funding_model.update_oi(data.open_interest)

    # ------------------------------------------------------------------
    # Order event handlers
    # ------------------------------------------------------------------

    def on_order_filled(self, event) -> None:
        """Clear active order tracker on fill."""
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None
            self.log.info(
                f"Order filled: {event.last_qty} @ {event.last_px} "
                f"| commission: {event.commission}"
            )

        # Update exposure manager with current equity
        account = self.portfolio.account(self._instrument_id.venue)
        if account:
            self._exposure_manager.update_equity(float(account.balance_total().as_double()))

    def on_order_canceled(self, event) -> None:
        """Clear active order tracker on cancel."""
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None

    def on_order_rejected(self, event) -> None:
        """Clear active order tracker on rejection."""
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None
            self.log.warning(f"Order rejected: {event.reason}")

    # ------------------------------------------------------------------
    # Signal evaluation and execution
    # ------------------------------------------------------------------

    def _maybe_evaluate_signal(self, book) -> None:
        """
        Build feature vector and compute edge. Act if edge exceeds threshold.
        Skip if there's already an active order.
        """
        if self._active_order_id is not None:
            return

        if self._instrument is None or self._exposure_manager is None:
            return

        # Toxicity filter — skip if recent trades are too toxic (adverse selection)
        toxicity = self._trade_features.compute_toxicity_score(book)
        if toxicity > 0.002:  # 0.2% toxicity threshold
            return

        # Compute cascade score
        cascade_score = self._cascade_model.compute_cascade_score()
        cascade_mode = self._cascade_model.is_cascade_mode()

        # Update regime
        vol_short = self._vol_features.realized_vol_short()
        vol_long = self._vol_features.realized_vol_long()
        trend_str = self._vol_features.trend_strength()
        self._regime_detector.update(
            vol_short=vol_short,
            vol_long=vol_long,
            trend_strength=trend_str,
            book_depth_usd=self._latest_book_depth,
        )

        # Skip untradeable regimes unless in cascade mode
        if not self._regime_detector.is_tradeable() and not cascade_mode:
            return

        # Normalize Hawkes intensity
        hawkes_norm = self._hawkes.normalized_intensity(self.clock.timestamp_ns())

        # Build feature vector
        features = FeatureVector(
            obi=self._latest_obi,
            tfi=self._latest_tfi,
            mp_drift_norm=self._latest_mp_drift,
            hawkes_norm=hawkes_norm,
            cascade_score=cascade_score,
            funding_pressure=self._funding_pressure,
            spread=self._latest_spread,
        )

        # Compute edge
        edge = self._edge_model.compute_edge(features)

        # Minimum edge threshold check
        if abs(edge) < self._min_edge:
            return

        self._act_on_edge(edge, book, cascade_mode)

    def _act_on_edge(self, edge: float, book, cascade_mode: bool) -> None:
        """
        Given a detected edge, compute position size, run risk checks,
        route the order, and submit.
        """
        is_buy = edge > 0
        direction = 1 if is_buy else -1

        # Kelly position sizing
        variance = self._kelly_sizer.estimate_variance(self._vol_features)
        f_kelly = self._kelly_sizer.compute_kelly_fraction(abs(edge), variance)

        # Portfolio value for sizing
        account = self.portfolio.account(self._instrument_id.venue)
        portfolio_value = (
            float(account.balance_total().as_double())
            if account
            else 10_000.0
        )

        # Current position for inventory penalty
        position = self.cache.position_for_instrument(self._instrument_id)
        current_position_usd = 0.0
        if position:
            best_bid = book.best_bid_price()
            mid = float(best_bid) if best_bid else 0.0
            current_position_usd = float(position.quantity) * mid * (
                1 if position.is_long else -1
            )

        # Reference price for sizing
        best_bid = book.best_bid_price()
        best_ask = book.best_ask_price()
        if best_bid is None or best_ask is None:
            return
        ref_px = (float(best_bid) + float(best_ask)) / 2.0
        if ref_px == 0.0:
            return

        size = self._kelly_sizer.compute_position_size(
            f_kelly=f_kelly,
            portfolio_value=portfolio_value,
            price=ref_px,
            instrument=self._instrument,
            current_position_usd=current_position_usd,
            direction=direction,
        )

        if size <= 0.0:
            return

        order_notional = size * ref_px

        # Reduce-only check
        reduce_only = self._exposure_manager.check_reduce_only()
        if reduce_only and current_position_usd * direction > 0:
            # In reduce-only mode, skip trades that increase exposure
            return

        # Exposure check
        is_reduce = (current_position_usd * direction < 0)
        allowed, reason = self._exposure_manager.check_new_order(
            order_notional_usd=order_notional,
            is_reduce=is_reduce,
        )
        if not allowed:
            self.log.debug(f"Order blocked by risk: {reason}")
            return

        # Update notional estimate
        self._exposure_manager.update_notional(
            abs(current_position_usd) + order_notional
        )

        # Route the order
        decision = self._order_router.route(
            book=book,
            instrument=self._instrument,
            quantity=size,
            is_buy=is_buy,
            is_cascade_mode=cascade_mode,
            slippage_model=self._slippage_model,
        )

        # Build and submit the NautilusTrader order
        order_side = OrderSide.BUY if is_buy else OrderSide.SELL
        quantity = Quantity(size, self._instrument.size_precision)

        if decision.order_type == OrderType.LIMIT and decision.price is not None:
            order = self.order_factory.limit(
                instrument_id=self._instrument_id,
                order_side=order_side,
                quantity=quantity,
                price=Price(decision.price, self._instrument.price_precision),
                time_in_force=decision.time_in_force,
                post_only=decision.post_only,
            )
        else:
            order = self.order_factory.market(
                instrument_id=self._instrument_id,
                order_side=order_side,
                quantity=quantity,
                time_in_force=TimeInForce.IOC,
            )

        self._active_order_id = order.client_order_id
        self.submit_order(order)

        self.log.info(
            f"Submitted {order_side.name} {size} @ "
            f"{decision.price or 'MARKET'} | edge={edge:.4f} | "
            f"regime={self._regime_detector.state.value}"
        )
