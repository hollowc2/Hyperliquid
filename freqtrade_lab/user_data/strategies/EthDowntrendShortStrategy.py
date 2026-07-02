import os

import numpy as np
import pandas as pd
import pandas_ta as pta
from freqtrade.strategy import IStrategy

from context_data import add_optional_context


EMA_FAST = int(os.environ.get("DS_EMA_FAST", 144))
EMA_SLOW = int(os.environ.get("DS_EMA_SLOW", 576))
EMA_ANCHOR = int(os.environ.get("DS_EMA_ANCHOR", 2016))
ATR_LEN = int(os.environ.get("DS_ATR_LEN", 14))
ATR_PCT_MIN = float(os.environ.get("DS_ATR_PCT_MIN", 0.0015))
RSI_LEN = int(os.environ.get("DS_RSI_LEN", 14))
RSI_MAX = float(os.environ.get("DS_RSI_MAX", 48))
RSI_TAKE = float(os.environ.get("DS_RSI_TAKE", 24))
FAST_EXIT_RSI = float(os.environ.get("DS_FAST_EXIT_RSI", 52))
REGIME_EXIT_ENABLED = os.environ.get("DS_REGIME_EXIT_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BREAKDOWN_LOOKBACK = int(os.environ.get("DS_BREAKDOWN_LOOKBACK", 72))
LEVERAGE_VALUE = int(os.environ.get("LEVERAGE", 1))


class EthDowntrendShortStrategy(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True

    timeframe = "5m"
    startup_candle_count = 2200

    minimal_roi = {"0": 100}
    stoploss = -0.05
    trailing_stop = False

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe = add_optional_context(dataframe, metadata.get("pair"), self.timeframe)
        dataframe["ema_fast"] = pta.ema(dataframe["close"], length=EMA_FAST)
        dataframe["ema_slow"] = pta.ema(dataframe["close"], length=EMA_SLOW)
        dataframe["ema_anchor"] = pta.ema(dataframe["close"], length=EMA_ANCHOR)
        dataframe["atr"] = pta.atr(
            dataframe["high"],
            dataframe["low"],
            dataframe["close"],
            length=ATR_LEN,
        )
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["rsi"] = pta.rsi(dataframe["close"], length=RSI_LEN)
        dataframe["breakdown_low"] = (
            dataframe["low"].shift(1).rolling(BREAKDOWN_LOOKBACK).min()
        )
        dataframe["anchor_slope"] = dataframe["ema_anchor"] - dataframe["ema_anchor"].shift(288)
        dataframe["slow_slope"] = dataframe["ema_slow"] - dataframe["ema_slow"].shift(72)
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        downtrend = (
            (dataframe["close"] < dataframe["ema_anchor"])
            & (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["ema_slow"] < dataframe["ema_anchor"])
            & (dataframe["anchor_slope"] < 0)
            & (dataframe["slow_slope"] < 0)
        )
        momentum = (
            (dataframe["close"] < dataframe["breakdown_low"])
            | (
                (dataframe["close"] < dataframe["ema_fast"])
                & (dataframe["rsi"] < RSI_MAX)
                & (dataframe["close"].shift(1) >= dataframe["ema_fast"].shift(1))
            )
        )
        short_mask = (
            downtrend
            & momentum
            & (dataframe["atr_pct"] >= ATR_PCT_MIN)
            & dataframe["ctx_risk_off_ok"]
            & dataframe["ctx_funding_neutral"]
        )

        dataframe.loc[short_mask, "enter_short"] = 1
        dataframe.loc[short_mask, "enter_tag"] = "downtrend_short"
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""

        take_profit = dataframe["rsi"] <= RSI_TAKE
        regime_fail = (
            dataframe["close"] > dataframe["ema_slow"]
            if REGIME_EXIT_ENABLED
            else pd.Series(False, index=dataframe.index)
        )
        fast_reclaim = (
            (dataframe["close"] > dataframe["ema_fast"])
            & (dataframe["rsi"] > FAST_EXIT_RSI)
        )

        dataframe.loc[take_profit, "exit_short"] = 1
        dataframe.loc[take_profit, "exit_tag"] = "rsi_flush"
        dataframe.loc[regime_fail | fast_reclaim, "exit_short"] = 1
        dataframe.loc[regime_fail, "exit_tag"] = "regime_fail"
        dataframe.loc[fast_reclaim & ~regime_fail, "exit_tag"] = "fast_reclaim"
        return dataframe

    def leverage(
        self,
        pair: str,
        current_time,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag,
        side: str,
        **kwargs,
    ) -> float:
        return float(min(LEVERAGE_VALUE, max_leverage))
