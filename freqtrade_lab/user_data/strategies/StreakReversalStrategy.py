"""
StreakReversalStrategy — freqtrade IStrategy implementation.

Signal logic:
  - Detect consecutive same-direction candle streaks.
  - Go long after STREAK_TRIGGER consecutive down bars (mean-reversion).
  - Go short after STREAK_TRIGGER consecutive up bars.
  - Trend filter: veto long if close < EMA; veto short if close > EMA.
  - Stop-loss: ATR-based (custom_stoploss).
  - Take-profit: ATR-based (custom_exit + vectorized exit column for backtesting).
  - Position sizing: risk-based (custom_stake_amount).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as pta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute
from freqtrade.exchange import timeframe_to_prev_date

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy-level constants read from environment (with defaults)
# ---------------------------------------------------------------------------
STREAK_TRIGGER: int = int(os.environ.get("STREAK_TRIGGER", 6))
EMA_PERIOD: int = int(os.environ.get("EMA_PERIOD", 100))
RISK_PCT: float = float(os.environ.get("RISK_PCT", 0.01))
STOP_ATR_MULT: float = float(os.environ.get("STOP_ATR_MULT", 1.0))
TP_ATR_MULT: float = float(os.environ.get("TP_ATR_MULT", 1.5))
MAX_POSITION_PCT: float = float(os.environ.get("MAX_POSITION_PCT", 0.20))
LEVERAGE_VALUE: int = int(os.environ.get("LEVERAGE", 1))


class StreakReversalStrategy(IStrategy):
    """
    Mean-reversion strategy that fades extended candle streaks on ETH perpetuals.
    Designed for Hyperliquid (live) and Binance ETH/USDT (backtesting).
    """

    # ------------------------------------------------------------------
    # freqtrade class-level settings
    # ------------------------------------------------------------------
    INTERFACE_VERSION = 3
    can_short = True
    use_custom_stoploss = True

    timeframe = "5m"
    startup_candle_count: int = 400  # 5× EMA_PERIOD for warmup stability

    # Safety floor — real stop managed via custom_stoploss()
    stoploss = -0.05

    # Effectively disabled; ATR TP handled in custom_exit() / exit columns
    minimal_roi = {"0": 100}

    # No trailing stop — ATR stop is dynamic via custom_stoploss
    trailing_stop = False

    # ------------------------------------------------------------------
    # Internal state: stash ATR/SL/TP levels at entry so custom_stoploss
    # and custom_exit can retrieve them without re-analysing the dataframe.
    # Key: (pair, normalized_candle_open_timestamp_as_datetime)
    # Wiped on restart → fallback in custom_stoploss() handles that.
    # ------------------------------------------------------------------
    _trade_entry_data: dict = {}

    # ------------------------------------------------------------------
    # populate_indicators
    # ------------------------------------------------------------------
    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # --- ATR (14-period) and trend EMA ---
        dataframe["atr"] = pta.atr(
            dataframe["high"], dataframe["low"], dataframe["close"], length=14
        )
        dataframe["ema"] = pta.ema(dataframe["close"], length=EMA_PERIOD)

        # --- Per-bar direction: +1 up, -1 down (ties → -1, matches original) ---
        diff = dataframe["close"].diff()
        direction_arr = np.where(diff > 0, 1, -1)

        # --- Streak count (vectorized, 1-indexed) ---
        # Mark where direction changes relative to previous bar
        direction_s = pd.Series(direction_arr, index=dataframe.index)
        changed = direction_s != direction_s.shift(1)
        # First bar always "changes" (no prior), so treat it as streak length 1
        changed.iloc[0] = True
        run_id = changed.cumsum()
        streak_s = direction_s.groupby(run_id).cumcount() + 1

        dataframe["direction"] = direction_arr
        dataframe["streak"] = streak_s.values

        # --- Raw signal (pre-trend-filter) ---
        # Long: enough consecutive down bars (mean-reversion long)
        # Short: enough consecutive up bars (mean-reversion short)
        dataframe["raw_signal"] = 0
        dataframe.loc[
            (dataframe["streak"] >= STREAK_TRIGGER) & (dataframe["direction"] == -1),
            "raw_signal",
        ] = 1
        dataframe.loc[
            (dataframe["streak"] >= STREAK_TRIGGER) & (dataframe["direction"] == 1),
            "raw_signal",
        ] = -1

        # --- Trend filter ---
        # Veto long if price is above EMA (already in uptrend, don't fade)
        # Veto short if price is below EMA (already in downtrend, don't fade)
        trending_up = dataframe["close"] > dataframe["ema"]
        dataframe["signal"] = dataframe["raw_signal"].copy()
        dataframe.loc[(dataframe["signal"] == 1) & trending_up, "signal"] = 0
        dataframe.loc[(dataframe["signal"] == -1) & ~trending_up, "signal"] = 0

        # --- Pre-compute per-bar SL/TP levels (bar close reference) ---
        dataframe["sl_long"] = dataframe["close"] - STOP_ATR_MULT * dataframe["atr"]
        dataframe["sl_short"] = dataframe["close"] + STOP_ATR_MULT * dataframe["atr"]
        dataframe["tp_long"] = dataframe["close"] + TP_ATR_MULT * dataframe["atr"]
        dataframe["tp_short"] = dataframe["close"] - TP_ATR_MULT * dataframe["atr"]

        return dataframe

    # ------------------------------------------------------------------
    # populate_entry_trend
    # ------------------------------------------------------------------
    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        long_mask = (dataframe["signal"] == 1) & dataframe["atr"].notna()
        short_mask = (dataframe["signal"] == -1) & dataframe["atr"].notna()

        # No-flip guarantee: don't enter on the same bar that fires a counter-signal exit.
        # A long bar (signal==1) would exit any short; a short bar (signal==-1) would exit any long.
        # mask entry on that bar so position goes flat rather than flipping.
        long_mask = long_mask & (dataframe["signal"] != -1)
        short_mask = short_mask & (dataframe["signal"] != 1)

        dataframe.loc[long_mask, "enter_long"] = 1
        dataframe.loc[long_mask, "enter_tag"] = "streak_reversal_long"
        dataframe.loc[short_mask, "enter_short"] = 1
        dataframe.loc[short_mask, "enter_tag"] = "streak_reversal_short"

        return dataframe

    # ------------------------------------------------------------------
    # populate_exit_trend
    # ------------------------------------------------------------------
    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""

        # --- Counter-signal exits ---
        # If a new opposing streak fires, exit immediately (go flat; no flip).
        counter_long_exit = dataframe["signal"] == -1   # short signal → exit long
        counter_short_exit = dataframe["signal"] == 1   # long signal → exit short

        dataframe.loc[counter_long_exit, "exit_long"] = 1
        dataframe.loc[counter_long_exit, "exit_tag"] = "counter_signal"
        dataframe.loc[counter_short_exit, "exit_short"] = 1
        dataframe.loc[counter_short_exit, "exit_tag"] = "counter_signal"

        # --- ATR take-profit (vectorized approximation for backtesting) ---
        # Use prior bar's TP level as reference to avoid lookahead bias.
        # If current close crosses the prior-bar TP level, mark exit.
        # In live/dry-run, custom_exit() provides the exact check.
        tp_long_prev = dataframe["tp_long"].shift(1)
        tp_short_prev = dataframe["tp_short"].shift(1)

        atr_tp_long_exit = (
            dataframe["close"] >= tp_long_prev
        ) & tp_long_prev.notna()
        atr_tp_short_exit = (
            dataframe["close"] <= tp_short_prev
        ) & tp_short_prev.notna()

        # Only set if not already set by counter-signal
        dataframe.loc[atr_tp_long_exit & (dataframe["exit_long"] == 0), "exit_long"] = 1
        dataframe.loc[atr_tp_long_exit & (dataframe["exit_tag"] == ""), "exit_tag"] = "atr_tp"
        dataframe.loc[atr_tp_short_exit & (dataframe["exit_short"] == 0), "exit_short"] = 1
        dataframe.loc[atr_tp_short_exit & (dataframe["exit_tag"] == ""), "exit_tag"] = "atr_tp"

        return dataframe

    # ------------------------------------------------------------------
    # custom_stoploss
    # ------------------------------------------------------------------
    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float:
        """
        Return ATR-based stop-loss as a fraction relative to current_rate.
        Falls back to the safety floor (-0.05) if no entry data is available.
        """
        open_date_normalized = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
        stash_key = (pair, open_date_normalized)

        entry_data = self._trade_entry_data.get(stash_key)

        if entry_data is None:
            # Post-restart fallback: try to re-derive SL from analyzed dataframe.
            entry_data = self._recover_entry_data(pair, trade)

        if entry_data is None:
            logger.warning(
                "custom_stoploss: no entry data for %s opened %s — using safety floor",
                pair,
                trade.open_date_utc,
            )
            return self.stoploss  # safety floor

        sl_price = entry_data["sl_short"] if trade.is_short else entry_data["sl_long"]
        return stoploss_from_absolute(sl_price, current_rate, is_short=trade.is_short)

    # ------------------------------------------------------------------
    # custom_exit
    # ------------------------------------------------------------------
    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:
        """
        Exact ATR take-profit check for live / dry-run.
        Complements the shifted-column backtesting approximation in populate_exit_trend.
        """
        open_date_normalized = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
        stash_key = (pair, open_date_normalized)

        entry_data = self._trade_entry_data.get(stash_key)
        if entry_data is None:
            entry_data = self._recover_entry_data(pair, trade)

        if entry_data is None:
            return None

        if trade.is_short:
            if current_rate <= entry_data["tp_short"]:
                return "atr_tp"
        else:
            if current_rate >= entry_data["tp_long"]:
                return "atr_tp"

        return None

    # ------------------------------------------------------------------
    # confirm_trade_entry
    # ------------------------------------------------------------------
    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> bool:
        """
        Stash ATR-based SL/TP levels keyed to this trade's candle boundary.
        Uses actual fill rate (not bar close) for precision.
        Always returns True (never vetoes the trade here).
        """
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is None or dataframe.empty:
                logger.warning("confirm_trade_entry: no dataframe for %s", pair)
                return True

            last = dataframe.iloc[-1]
            atr = last["atr"]
            if pd.isna(atr) or atr <= 0:
                logger.warning("confirm_trade_entry: ATR invalid for %s", pair)
                return True

            # Compute SL/TP from actual fill rate for maximum precision
            sl_long = rate - STOP_ATR_MULT * atr
            sl_short = rate + STOP_ATR_MULT * atr
            tp_long = rate + TP_ATR_MULT * atr
            tp_short = rate - TP_ATR_MULT * atr

            # Normalize current_time to timeframe candle boundary so the key
            # matches what custom_stoploss / custom_exit will look up.
            candle_time = timeframe_to_prev_date(self.timeframe, current_time)
            stash_key = (pair, candle_time)

            self._trade_entry_data[stash_key] = {
                "sl_long": sl_long,
                "sl_short": sl_short,
                "tp_long": tp_long,
                "tp_short": tp_short,
                "atr": atr,
                "rate": rate,
                "side": side,
            }
            logger.debug(
                "Stashed entry data for %s @ %s: sl_long=%.4f sl_short=%.4f tp_long=%.4f tp_short=%.4f",
                pair, candle_time, sl_long, sl_short, tp_long, tp_short,
            )
        except Exception as exc:
            logger.error("confirm_trade_entry: unexpected error for %s: %s", pair, exc)

        return True

    # ------------------------------------------------------------------
    # custom_stake_amount
    # ------------------------------------------------------------------
    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: Optional[float],
        max_stake: float,
        leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """
        Risk-based position sizing:
          risk_usd  = equity * RISK_PCT
          stop_pct  = (STOP_ATR_MULT * ATR) / entry_rate
          size_usd  = risk_usd / stop_pct
          clamped to MAX_POSITION_PCT of equity
        freqtrade auto-clamps to [min_stake, max_stake].
        """
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is None or dataframe.empty:
                return proposed_stake

            atr = dataframe.iloc[-1]["atr"]
            if pd.isna(atr) or atr <= 0 or current_rate <= 0:
                return proposed_stake

            # At leverage=1, max_stake == equity. At higher leverage, equity = max_stake / leverage.
            equity = max_stake / leverage

            risk_usd = equity * RISK_PCT
            stop_pct = (STOP_ATR_MULT * atr) / current_rate
            if stop_pct <= 0:
                return max_stake

            size_usd = risk_usd / stop_pct
            max_by_pct = equity * MAX_POSITION_PCT

            stake = min(size_usd, max_by_pct)
            if min_stake is not None:
                stake = max(stake, min_stake)
            stake = min(stake, max_stake)
            logger.debug(
                "custom_stake_amount: atr=%.4f stop_pct=%.4f risk_usd=%.2f size_usd=%.2f max_pct=%.2f → stake=%.2f",
                atr, stop_pct, risk_usd, size_usd, max_by_pct, stake,
            )
            return stake

        except Exception as exc:
            logger.error("custom_stake_amount: unexpected error: %s", exc)
            return proposed_stake

    # ------------------------------------------------------------------
    # leverage callback
    # ------------------------------------------------------------------
    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """Request configured leverage, capped by exchange maximum."""
        return float(min(LEVERAGE_VALUE, max_leverage))

    # ------------------------------------------------------------------
    # Internal helper: post-restart SL/TP recovery
    # ------------------------------------------------------------------
    def _recover_entry_data(self, pair: str, trade: Trade) -> Optional[dict]:
        """
        After a bot restart _trade_entry_data is empty.
        Try to re-derive SL/TP from the analyzed dataframe at the entry candle.
        Falls back to None if the candle is no longer in the dataframe window.
        """
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is None or dataframe.empty:
                return None

            # Find the candle that matches the trade open date.
            # Use timeframe_to_prev_date to align to candle boundary consistently.
            open_ts = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
            mask = dataframe["date"] == open_ts
            if not mask.any():
                logger.debug(
                    "_recover_entry_data: entry candle %s not in dataframe for %s",
                    open_ts, pair,
                )
                return None

            row = dataframe.loc[mask].iloc[0]
            atr = row["atr"]
            if pd.isna(atr) or atr <= 0:
                return None

            rate = trade.open_rate
            recovered = {
                "sl_long": rate - STOP_ATR_MULT * atr,
                "sl_short": rate + STOP_ATR_MULT * atr,
                "tp_long": rate + TP_ATR_MULT * atr,
                "tp_short": rate - TP_ATR_MULT * atr,
                "atr": atr,
                "rate": rate,
                "side": "short" if trade.is_short else "long",
            }

            # Re-stash so subsequent calls hit the cache
            candle_time = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
            self._trade_entry_data[(pair, candle_time)] = recovered
            logger.info(
                "_recover_entry_data: recovered entry data for %s @ %s", pair, open_ts
            )
            return recovered

        except Exception as exc:
            logger.error("_recover_entry_data: error for %s: %s", pair, exc)
            return None
