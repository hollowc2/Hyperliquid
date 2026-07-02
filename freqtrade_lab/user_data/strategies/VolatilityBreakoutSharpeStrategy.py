"""
VolatilityBreakoutSharpeStrategy

Conservative ETH perp breakout strategy for the local Hyperliquid/Freqtrade lab.
The intent is fewer, cleaner trades: only participate when price breaks a recent
range in the direction of the EMA regime and activity is above normal.
"""

import os

import numpy as np
import pandas as pd
import pandas_ta as pta
from freqtrade.strategy import IStrategy

from context_data import add_optional_context


LOOKBACK = int(os.environ.get("VB_LOOKBACK", 36))
EMA_FAST = int(os.environ.get("VB_EMA_FAST", 48))
EMA_SLOW = int(os.environ.get("VB_EMA_SLOW", 144))
ATR_LEN = int(os.environ.get("VB_ATR_LEN", 14))
ATR_PCT_MIN = float(os.environ.get("VB_ATR_PCT_MIN", 0.0010))
VOL_Z_MIN = float(os.environ.get("VB_VOL_Z_MIN", 0.25))
TP_ATR = float(os.environ.get("VB_TP_ATR", 2.0))
STOP_ATR = float(os.environ.get("VB_STOP_ATR", 1.0))
MAX_HOLD_CANDLES = int(os.environ.get("VB_MAX_HOLD_CANDLES", 36))
LEVERAGE_VALUE = int(os.environ.get("LEVERAGE", 1))


class VolatilityBreakoutSharpeStrategy(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True

    timeframe = "5m"
    startup_candle_count = 300

    stoploss = -0.03
    minimal_roi = {"0": 100}
    trailing_stop = False

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe = add_optional_context(dataframe, metadata.get("pair"), self.timeframe)
        dataframe["ema_fast"] = pta.ema(dataframe["close"], length=EMA_FAST)
        dataframe["ema_slow"] = pta.ema(dataframe["close"], length=EMA_SLOW)
        dataframe["atr"] = pta.atr(
            dataframe["high"], dataframe["low"], dataframe["close"], length=ATR_LEN
        )
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]

        vol_mean = dataframe["volume"].rolling(LOOKBACK).mean()
        vol_std = dataframe["volume"].rolling(LOOKBACK).std(ddof=0).replace(0, np.nan)
        dataframe["vol_z"] = (dataframe["volume"] - vol_mean) / vol_std

        dataframe["range_high"] = dataframe["high"].rolling(LOOKBACK).max().shift(1)
        dataframe["range_low"] = dataframe["low"].rolling(LOOKBACK).min().shift(1)
        dataframe["trend_up"] = (
            (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["ema_slow"] > dataframe["ema_slow"].shift(12))
        )
        dataframe["trend_down"] = (
            (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["ema_slow"] < dataframe["ema_slow"].shift(12))
        )

        dataframe["long_tp"] = dataframe["close"] + TP_ATR * dataframe["atr"]
        dataframe["long_sl"] = dataframe["close"] - STOP_ATR * dataframe["atr"]
        dataframe["short_tp"] = dataframe["close"] - TP_ATR * dataframe["atr"]
        dataframe["short_sl"] = dataframe["close"] + STOP_ATR * dataframe["atr"]
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        clean_vol = (dataframe["atr_pct"] >= ATR_PCT_MIN) & (dataframe["vol_z"] >= VOL_Z_MIN)
        long_break = dataframe["close"] > dataframe["range_high"]
        short_break = dataframe["close"] < dataframe["range_low"]

        long_mask = (
            clean_vol
            & long_break
            & dataframe["trend_up"]
            & dataframe["ctx_risk_on_ok"]
            & dataframe["ctx_funding_neutral"]
        )
        short_mask = (
            clean_vol
            & short_break
            & dataframe["trend_down"]
            & dataframe["ctx_risk_off_ok"]
            & dataframe["ctx_funding_neutral"]
        )

        dataframe.loc[long_mask, "enter_long"] = 1
        dataframe.loc[long_mask, "enter_tag"] = np.where(
            dataframe.loc[long_mask, "ctx_loaded"] > 0,
            "vol_breakout_long_ctx",
            "vol_breakout_long",
        )
        dataframe.loc[short_mask, "enter_short"] = 1
        dataframe.loc[short_mask, "enter_tag"] = np.where(
            dataframe.loc[short_mask, "ctx_loaded"] > 0,
            "vol_breakout_short_ctx",
            "vol_breakout_short",
        )
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""

        prior_long_tp = dataframe["long_tp"].shift(1)
        prior_long_sl = dataframe["long_sl"].shift(1)
        prior_short_tp = dataframe["short_tp"].shift(1)
        prior_short_sl = dataframe["short_sl"].shift(1)

        long_exit = (
            (dataframe["close"] >= prior_long_tp)
            | (dataframe["close"] <= prior_long_sl)
            | (dataframe["ema_fast"] < dataframe["ema_slow"])
        )
        short_exit = (
            (dataframe["close"] <= prior_short_tp)
            | (dataframe["close"] >= prior_short_sl)
            | (dataframe["ema_fast"] > dataframe["ema_slow"])
        )

        dataframe.loc[long_exit, "exit_long"] = 1
        dataframe.loc[long_exit, "exit_tag"] = "atr_or_regime"
        dataframe.loc[short_exit, "exit_short"] = 1
        dataframe.loc[short_exit, "exit_tag"] = "atr_or_regime"
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade,
        current_time,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        age = current_time - trade.open_date_utc
        if age.total_seconds() >= MAX_HOLD_CANDLES * 5 * 60:
            return "time_stop"
        return None

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
