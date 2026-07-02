"""
LiquidationWickReversionStrategy

OHLCV-only mean-reversion strategy for futures backtests.

Idea:
  - Use volume z-score, ATR/range expansion, and rejection wicks as a local
    liquidation-proxy signal.
  - Fade downside flushes with long entries and upside squeezes with shorts.
  - Exit on reversion to the short EMA, exhaustion normalization, or age.
"""

from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy


class LiquidationWickReversionStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"
    startup_candle_count = 240
    can_short = True

    stoploss = -0.035
    minimal_roi = {"0": 100}
    trailing_stop = False
    use_custom_stoploss = False

    process_only_new_candles = True
    ignore_roi_if_entry_signal = False

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        close = dataframe["close"]
        high = dataframe["high"]
        low = dataframe["low"]
        open_ = dataframe["open"]
        volume = dataframe["volume"]

        prev_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        dataframe["atr"] = true_range.rolling(14, min_periods=14).mean()
        dataframe["ema_fast"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
        dataframe["ema_slow"] = close.ewm(span=80, adjust=False, min_periods=80).mean()

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
        rs = gain / loss.replace(0, np.nan)
        dataframe["rsi"] = 100 - (100 / (1 + rs))

        volume_mean = volume.rolling(96, min_periods=48).mean()
        volume_std = volume.rolling(96, min_periods=48).std()
        dataframe["volume_z"] = (volume - volume_mean) / volume_std.replace(0, np.nan)

        body_top = pd.concat([open_, close], axis=1).max(axis=1)
        body_bottom = pd.concat([open_, close], axis=1).min(axis=1)
        candle_range = (high - low).replace(0, np.nan)
        dataframe["lower_wick_ratio"] = (body_bottom - low) / candle_range
        dataframe["upper_wick_ratio"] = (high - body_top) / candle_range
        dataframe["range_atr"] = (high - low) / dataframe["atr"].replace(0, np.nan)
        dataframe["ema_distance"] = (close - dataframe["ema_fast"]) / dataframe["atr"].replace(0, np.nan)

        dataframe["trend_bias"] = np.where(close >= dataframe["ema_slow"], 1, -1)

        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        common_flush = (
            (dataframe["volume_z"] >= 1.8)
            & (dataframe["range_atr"] >= 1.4)
            & dataframe["atr"].notna()
            & dataframe["ema_slow"].notna()
            & (dataframe["volume"] > 0)
        )

        long_flush = (
            common_flush
            & (dataframe["close"] < dataframe["open"])
            & (dataframe["lower_wick_ratio"] >= 0.42)
            & (dataframe["ema_distance"] <= -0.65)
            & (dataframe["rsi"] <= 38)
            & (dataframe["trend_bias"] <= 0)
        )

        short_flush = (
            common_flush
            & (dataframe["close"] > dataframe["open"])
            & (dataframe["upper_wick_ratio"] >= 0.42)
            & (dataframe["ema_distance"] >= 0.65)
            & (dataframe["rsi"] >= 62)
            & (dataframe["trend_bias"] >= 0)
        )

        dataframe.loc[long_flush, "enter_long"] = 1
        dataframe.loc[long_flush, "enter_tag"] = "liq_wick_long"
        dataframe.loc[short_flush, "enter_short"] = 1
        dataframe.loc[short_flush, "enter_tag"] = "liq_wick_short"

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""

        long_mean_reversion = (dataframe["close"] >= dataframe["ema_fast"]) | (
            (dataframe["rsi"] >= 52) & (dataframe["ema_distance"] >= -0.1)
        )
        short_mean_reversion = (dataframe["close"] <= dataframe["ema_fast"]) | (
            (dataframe["rsi"] <= 48) & (dataframe["ema_distance"] <= 0.1)
        )

        dataframe.loc[long_mean_reversion, "exit_long"] = 1
        dataframe.loc[long_mean_reversion, "exit_tag"] = "mean_reversion"
        dataframe.loc[short_mean_reversion, "exit_short"] = 1
        dataframe.loc[short_mean_reversion, "exit_tag"] = "mean_reversion"

        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:
        if current_profit >= 0.018:
            return "profit_cap"

        if (current_time - trade.open_date_utc).total_seconds() >= 18 * 5 * 60:
            return "time_stop"

        return None
