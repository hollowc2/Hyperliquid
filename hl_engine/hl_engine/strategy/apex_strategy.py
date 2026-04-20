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
        self._last_bar_close: float = 0.0  # fallback mid when no book

        # --- Monitoring state ---
        self._last_edge: float = 0.0
        self._last_order_summary: dict = {}
        self._trade_count: int = 0
        self._total_commission: float = 0.0
        self._session_realized_pnl: float = 0.0
        self._last_state_dump_ts: int = 0
        self._state_dump_interval_ns: int = 1_000_000_000  # 1 second

        import os
        from pathlib import Path
        state_dir = Path(os.getenv("HL_CATALOG_PATH", "data/catalog")).parent
        state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = str(state_dir / "apex_state.json")

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
        from nautilus_trader.model.identifiers import ClientId
        hl_client_id = ClientId("HYPERLIQUID")
        self.subscribe_data(
            data_type=DataType(FundingRateData, metadata={"instrument_id": self._instrument_id}),
            client_id=hl_client_id,
        )
        self.subscribe_data(
            data_type=DataType(LiquidationData, metadata={"instrument_id": self._instrument_id}),
            client_id=hl_client_id,
        )
        self.subscribe_data(
            data_type=DataType(OpenInterestData, metadata={"instrument_id": self._instrument_id}),
            client_id=hl_client_id,
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

        # Heartbeat: write state file every 5s even when no market data arrives
        # (ensures the monitor shows "DISCONNECTED" rather than going permanently stale)
        from datetime import timedelta
        self.clock.set_timer(
            name="state_heartbeat",
            interval=timedelta(seconds=5),
            callback=self._on_heartbeat,
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

        # Dump state to file at most once per second
        if (now - self._last_state_dump_ts) >= self._state_dump_interval_ns:
            self._write_state_file(book)
            self._last_state_dump_ts = now

    def on_trade_tick(self, tick: TradeTick) -> None:
        """Update trade flow features and Hawkes intensity."""
        self._trade_features.update(tick)
        self._latest_tfi = self._trade_features.compute_tfi()
        self._latest_hawkes = self._hawkes.update(tick.ts_event)

    def on_bar(self, bar: Bar) -> None:
        """Update volatility features and price momentum."""
        self._vol_features.update(bar)

        # Price momentum: log return of most recent bar
        o, c = float(bar.open), float(bar.close)
        if o > 0.0 and c > 0.0:
            import math
            self._price_momentum = math.log(c / o)
        if c > 0.0:
            self._last_bar_close = c

        # Fallback signal evaluation when no order book data is available.
        # on_order_book_deltas won't fire without OB data in catalog, so bars
        # serve as the evaluation clock (1m cadence, throttle still applies).
        # Also triggers when book exists but has no quotes (empty, post-subscribe).
        book = self.cache.order_book(self._instrument_id)
        book_has_quotes = (
            book is not None
            and book.best_bid_price() is not None
            and book.best_ask_price() is not None
        )
        if not book_has_quotes:
            now = self.clock.timestamp_ns()
            if (now - self._last_signal_ts) >= self._signal_throttle_ns:
                self._maybe_evaluate_signal(None)
                self._last_signal_ts = now

    def on_data(self, data) -> None:
        """Dispatch custom data types to the appropriate model."""
        from nautilus_trader.model.data import CustomData
        if isinstance(data, CustomData):
            data = data.data  # unwrap CustomData wrapper

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

        self._trade_count += 1
        self._total_commission += float(event.commission.as_double()) if hasattr(event.commission, "as_double") else float(str(event.commission).split()[0])

        # Update exposure manager with current equity and actual position notional.
        # This also unpins a pin_at_limit() if the position has been reduced below max.
        account = self.portfolio.account(self._instrument_id.venue)
        if account:
            from nautilus_trader.model.currencies import USDC
            bal = account.balance_total(USDC)
            if bal is not None:
                self._exposure_manager.update_equity(float(bal.as_double()))

        if self._exposure_manager is not None:
            open_positions = self.cache.positions_open(instrument_id=self._instrument_id)
            if open_positions:
                pos = open_positions[0]
                book = self.cache.order_book(self._instrument_id)
                best_bid = book.best_bid_price() if book else None
                mid = float(best_bid) if best_bid else float(pos.avg_px_open)
                actual_notional = float(pos.quantity) * mid
            else:
                actual_notional = 0.0
            self._exposure_manager.update_notional(actual_notional)

    def on_order_canceled(self, event) -> None:
        """Clear active order tracker on cancel."""
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None

    def on_order_rejected(self, event) -> None:
        """Clear active order tracker on rejection."""
        if self._active_order_id and event.client_order_id == self._active_order_id:
            self._active_order_id = None
            self.log.warning(f"Order rejected: {event.reason}")
            # Pin the local notional at the limit so the ExposureManager blocks
            # all further new-order attempts until a fill reduces the position.
            # This prevents a tight retry loop when the orchestrator is rejecting
            # because the real position on Hyperliquid already exceeds the limit
            # but the local NautilusTrader cache is stale.
            if self._exposure_manager and "notional" in (event.reason or "").lower():
                self._exposure_manager.pin_at_limit()
                self.log.warning(
                    "Notional limit hit — halting new orders until position is reduced"
                )

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
        # Requires a live book; skip filter (treat as 0) when book unavailable.
        toxicity = self._trade_features.compute_toxicity_score(book) if book is not None else 0.0
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
            book_depth_usd=self._latest_book_depth if book is not None else None,
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
        self.log.debug(
            f"edge={edge:.4f} obi={self._latest_obi:.3f} tfi={self._latest_tfi:.3f} "
            f"mp_drift={self._latest_mp_drift:.6f} hawkes={hawkes_norm:.3f}"
        )
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
        portfolio_value = 10_000.0
        if account:
            from nautilus_trader.model.currencies import USDC
            bal = account.balance_total(USDC)
            if bal is not None:
                portfolio_value = float(bal.as_double())

        # Reference price: use book mid when available, fall back to last bar close.
        if book is not None:
            best_bid = book.best_bid_price()
            best_ask = book.best_ask_price()
            if best_bid is None or best_ask is None:
                return
            ref_px = (float(best_bid) + float(best_ask)) / 2.0
        else:
            ref_px = self._last_bar_close
        if ref_px == 0.0:
            return

        # Current position for inventory penalty
        open_positions = self.cache.positions_open(instrument_id=self._instrument_id)
        position = open_positions[0] if open_positions else None
        current_position_usd = 0.0
        if position:
            current_position_usd = float(position.quantity) * ref_px * (
                1 if position.is_long else -1
            )

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

        # Update notional estimate. Take the max of what the position cache reports
        # and what we're already tracking — this prevents the tracked value from
        # being silently reset downward when the NautilusTrader cache is stale
        # (e.g. on container restart before reconciliation completes).
        seeded_notional = max(abs(current_position_usd), self._exposure_manager.tracked_notional)
        self._exposure_manager.update_notional(seeded_notional + order_notional)

        # Route the order. Without a live book, use MARKET IOC directly.
        if book is not None:
            decision = self._order_router.route(
                book=book,
                instrument=self._instrument,
                quantity=size,
                is_buy=is_buy,
                is_cascade_mode=cascade_mode,
                slippage_model=self._slippage_model,
            )
        else:
            decision = self._order_router.route_no_book(
                is_buy=is_buy,
                is_cascade_mode=cascade_mode,
                ref_px=ref_px,
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
        self._last_edge = edge
        self._last_order_summary = {
            "side": order_side.name,
            "qty": size,
            "price": decision.price,
            "edge": round(edge, 4),
            "regime": self._regime_detector.state.value,
        }
        self.submit_order(order)

        self.log.info(
            f"Submitted {order_side.name} {size} @ "
            f"{decision.price or 'MARKET'} | edge={edge:.4f} | "
            f"regime={self._regime_detector.state.value}"
        )

    # ------------------------------------------------------------------
    # State export for live monitoring dashboard
    # ------------------------------------------------------------------

    def _on_heartbeat(self, event) -> None:
        """Write state on timer tick so monitor stays alive during data gaps."""
        book = self.cache.order_book(self._instrument_id) if self._instrument_id else None
        self._write_state_file(book)

    def _write_state_file(self, book) -> None:
        """Write a JSON snapshot of strategy state to disk for the monitor UI."""
        import json
        from datetime import datetime, timezone

        # Position info
        pos_info: dict = {}
        open_positions = self.cache.positions_open(instrument_id=self._instrument_id)
        if open_positions:
            pos = open_positions[0]
            best_bid = book.best_bid_price() if book else None
            best_ask = book.best_ask_price() if book else None
            mid = 0.0
            if best_bid and best_ask:
                mid = (float(best_bid) + float(best_ask)) / 2.0
            unreal_pnl = float(pos.quantity) * (mid - float(pos.avg_px_open)) * (1 if pos.is_long else -1)
            pos_info = {
                "side": pos.entry.name if hasattr(pos.entry, "name") else str(pos.entry),
                "qty": round(float(pos.quantity), 5),
                "avg_px": round(float(pos.avg_px_open), 2),
                "unrealized_pnl": round(unreal_pnl, 4),
                "realized_pnl": round(float(pos.realized_pnl.as_double()) if hasattr(pos.realized_pnl, "as_double") else 0.0, 4),
                "duration_s": round((self.clock.timestamp_ns() - pos.ts_opened) / 1e9, 1),
            }

        # Account balance
        balance = 0.0
        account = self.portfolio.account(self._instrument_id.venue)
        if account:
            from nautilus_trader.model.currencies import USDC
            bal = account.balance_total(USDC)
            if bal is not None:
                balance = round(float(bal.as_double()), 2)

        # Mid price
        best_bid = book.best_bid_price() if book else None
        best_ask = book.best_ask_price() if book else None
        mid_px = 0.0
        if best_bid and best_ask:
            mid_px = round((float(best_bid) + float(best_ask)) / 2.0, 2)

        state = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "instrument": str(self._instrument_id) if self._instrument_id else "",
            "mid_px": mid_px,
            "regime": self._regime_detector.state.value,
            "balance": balance,
            "position": pos_info,
            "features": {
                "obi": round(self._latest_obi, 4),
                "tfi": round(self._latest_tfi, 4),
                "mp_drift": round(self._latest_mp_drift, 8),
                "hawkes": round(self._latest_hawkes, 4),
                "cascade": round(self._cascade_model.compute_cascade_score(), 4),
                "funding": round(self._funding_pressure, 4),
                "spread": round(self._latest_spread, 4),
                "vol_short": round(self._vol_features.realized_vol_short(), 6),
            },
            "last_edge": round(self._last_edge, 4),
            "last_order": self._last_order_summary,
            "active_order": str(self._active_order_id) if self._active_order_id else None,
            "trade_count": self._trade_count,
            "total_commission": round(self._total_commission, 4),
        }

        try:
            with open(self._state_file, "w") as f:
                json.dump(state, f)
        except Exception:
            pass  # Never let monitoring I/O crash the strategy
