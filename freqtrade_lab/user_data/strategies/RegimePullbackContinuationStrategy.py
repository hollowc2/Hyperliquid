"""
RegimePullbackContinuationStrategy

Trade trend continuation after a controlled 5m pullback, only when the 1h
regime is aligned and active.
"""

import os

import numpy as np
import pandas as pd
import pandas_ta as pta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, merge_informative_pair

from context_data import add_optional_context


EMA_FAST = int(os.environ.get("RPC_EMA_FAST", 21))
EMA_MID = int(os.environ.get("RPC_EMA_MID", 55))
RSI_LEN = int(os.environ.get("RPC_RSI_LEN", 14))
ATR_LEN = int(os.environ.get("RPC_ATR_LEN", 14))
ADX_LEN = int(os.environ.get("RPC_ADX_LEN", 14))
VOLUME_Z_WINDOW = int(os.environ.get("RPC_VOLUME_Z_WINDOW", 48))
PULLBACK_LOOKBACK = int(os.environ.get("RPC_PULLBACK_LOOKBACK", 12))
EMA_MID_TOL = float(os.environ.get("RPC_EMA_MID_TOL", 0.003))

HTF_EMA_FAST = int(os.environ.get("RPC_HTF_EMA_FAST", 50))
HTF_EMA_SLOW = int(os.environ.get("RPC_HTF_EMA_SLOW", 200))
HTF_SLOPE_LOOKBACK = int(os.environ.get("RPC_HTF_SLOPE_LOOKBACK", 3))
HTF_ADX_MIN = float(os.environ.get("RPC_HTF_ADX_MIN", os.environ.get("RPC_ADX_MIN", 18.0)))
HTF_ATR_PCT_MIN = float(os.environ.get("RPC_HTF_ATR_PCT_MIN", 0.0010))
HTF_ATR_PCT_MAX = float(os.environ.get("RPC_HTF_ATR_PCT_MAX", 0.0500))

RSI_LONG_RECOVER = float(os.environ.get("RPC_RSI_LONG_RECOVER", 50.0))
RSI_SHORT_RECOVER = float(os.environ.get("RPC_RSI_SHORT_RECOVER", 50.0))
VOLUME_Z_MIN = float(os.environ.get("RPC_VOLUME_Z_MIN", -0.25))
TP_ATR_MULT = float(os.environ.get("RPC_TP_ATR_MULT", os.environ.get("RPC_ATR_TP", 2.0)))
STOP_ATR_MULT = float(os.environ.get("RPC_STOP_ATR_MULT", os.environ.get("RPC_ATR_STOP", 1.2)))
MAX_HOLD_CANDLES = int(os.environ.get("RPC_MAX_HOLD_CANDLES", os.environ.get("RPC_MAX_HOLD", 48)))
LEVERAGE_VALUE = int(os.environ.get("RPC_LEVERAGE", os.environ.get("LEVERAGE", 1)))
PAIR_ALLOWLIST = {
    item.strip()
    for item in os.environ.get("RPC_PAIR_ALLOWLIST", "").split(",")
    if item.strip()
}
ENABLE_LONGS = os.environ.get("RPC_ENABLE_LONGS", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ENABLE_SHORTS = os.environ.get("RPC_ENABLE_SHORTS", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
USE_EMA_EXIT = os.environ.get("RPC_USE_EMA_EXIT", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
USE_REGIME_EXIT = os.environ.get("RPC_USE_REGIME_EXIT", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


class RegimePullbackContinuationStrategy(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True

    timeframe = "5m"
    informative_timeframe = "1h"
    startup_candle_count = 600

    stoploss = -0.04
    minimal_roi = {"0": 100}
    trailing_stop = False

    def informative_pairs(self):
        if not self.dp:
            return []
        return [
            (pair, self.informative_timeframe)
            for pair in self.dp.current_whitelist()
        ]

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe = add_optional_context(dataframe, metadata.get("pair"), self.timeframe)
        dataframe["ema_fast"] = pta.ema(dataframe["close"], length=EMA_FAST)
        dataframe["ema_mid"] = pta.ema(dataframe["close"], length=EMA_MID)
        dataframe["rsi"] = pta.rsi(dataframe["close"], length=RSI_LEN)
        dataframe["atr"] = pta.atr(
            dataframe["high"], dataframe["low"], dataframe["close"], length=ATR_LEN
        )
        adx = pta.adx(dataframe["high"], dataframe["low"], dataframe["close"], length=ADX_LEN)
        dataframe["adx"] = adx[f"ADX_{ADX_LEN}"]

        volume_mean = dataframe["volume"].rolling(VOLUME_Z_WINDOW).mean()
        volume_std = dataframe["volume"].rolling(VOLUME_Z_WINDOW).std(ddof=0).replace(0, np.nan)
        dataframe["volume_z"] = (dataframe["volume"] - volume_mean) / volume_std

        if self.dp:
            informative = self.dp.get_pair_dataframe(
                pair=metadata["pair"],
                timeframe=self.informative_timeframe,
            )
            informative = self._populate_informative_indicators(informative)
            dataframe = merge_informative_pair(
                dataframe,
                informative,
                self.timeframe,
                self.informative_timeframe,
                ffill=True,
            )

        dataframe = self._ensure_informative_columns(dataframe)
        dataframe["long_tp"] = dataframe["close"] + TP_ATR_MULT * dataframe["atr"]
        dataframe["short_tp"] = dataframe["close"] - TP_ATR_MULT * dataframe["atr"]
        dataframe["long_sl"] = dataframe["close"] - STOP_ATR_MULT * dataframe["atr"]
        dataframe["short_sl"] = dataframe["close"] + STOP_ATR_MULT * dataframe["atr"]
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        htf_long_regime = (
            (dataframe["close_1h"] > dataframe["ema_fast_1h"])
            & (dataframe["ema_fast_1h"] > dataframe["ema_slow_1h"])
            & (dataframe["adx_1h"] >= HTF_ADX_MIN)
            & (dataframe["ema_slope_1h"] > 0)
        )
        htf_short_regime = (
            (dataframe["close_1h"] < dataframe["ema_fast_1h"])
            & (dataframe["ema_fast_1h"] < dataframe["ema_slow_1h"])
            & (dataframe["adx_1h"] >= HTF_ADX_MIN)
            & (dataframe["ema_slope_1h"] < 0)
        )
        htf_atr_ok = dataframe["atr_pct_1h"].between(HTF_ATR_PCT_MIN, HTF_ATR_PCT_MAX)

        prior_below_fast = (
            (dataframe["low"] < dataframe["ema_fast"]).shift(1).fillna(False).astype(int)
        )
        prior_above_fast = (
            (dataframe["high"] > dataframe["ema_fast"]).shift(1).fillna(False).astype(int)
        )
        recent_long_pullback = (
            prior_below_fast.rolling(PULLBACK_LOOKBACK).max().fillna(0).astype(bool)
            & (
                dataframe["low"].shift(1).rolling(PULLBACK_LOOKBACK).min()
                >= dataframe["ema_mid"] * (1 - EMA_MID_TOL)
            )
        )
        recent_short_pullback = (
            prior_above_fast.rolling(PULLBACK_LOOKBACK).max().fillna(0).astype(bool)
            & (
                dataframe["high"].shift(1).rolling(PULLBACK_LOOKBACK).max()
                <= dataframe["ema_mid"] * (1 + EMA_MID_TOL)
            )
        )

        reclaim_fast = (
            (dataframe["close"] > dataframe["ema_fast"])
            & (dataframe["close"].shift(1) <= dataframe["ema_fast"].shift(1))
        )
        reject_fast = (
            (dataframe["close"] < dataframe["ema_fast"])
            & (dataframe["close"].shift(1) >= dataframe["ema_fast"].shift(1))
        )
        rsi_long_recover = (
            (dataframe["rsi"] >= RSI_LONG_RECOVER)
            & (dataframe["rsi"].shift(1) < RSI_LONG_RECOVER)
        )
        rsi_short_recover = (
            (dataframe["rsi"] <= RSI_SHORT_RECOVER)
            & (dataframe["rsi"].shift(1) > RSI_SHORT_RECOVER)
        )
        volume_ok = dataframe["volume_z"] >= VOLUME_Z_MIN

        long_mask = (
            htf_long_regime
            & htf_atr_ok
            & recent_long_pullback
            & reclaim_fast
            & rsi_long_recover
            & volume_ok
            & dataframe["ctx_risk_on_ok"]
            & dataframe["ctx_funding_neutral"]
        )
        short_mask = (
            htf_short_regime
            & htf_atr_ok
            & recent_short_pullback
            & reject_fast
            & rsi_short_recover
            & volume_ok
            & dataframe["ctx_risk_off_ok"]
            & dataframe["ctx_funding_neutral"]
        )
        if PAIR_ALLOWLIST and metadata.get("pair") not in PAIR_ALLOWLIST:
            long_mask = long_mask & False
            short_mask = short_mask & False
        if not ENABLE_LONGS:
            long_mask = long_mask & False
        if not ENABLE_SHORTS:
            short_mask = short_mask & False

        dataframe.loc[long_mask, "enter_long"] = 1
        dataframe.loc[long_mask, "enter_tag"] = "regime_pullback_long"
        dataframe.loc[long_mask & (dataframe["ctx_loaded"] > 0), "enter_tag"] = (
            "regime_pullback_long_ctx"
        )
        dataframe.loc[short_mask, "enter_short"] = 1
        dataframe.loc[short_mask, "enter_tag"] = "regime_pullback_short"
        dataframe.loc[short_mask & (dataframe["ctx_loaded"] > 0), "enter_tag"] = (
            "regime_pullback_short_ctx"
        )
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""

        long_atr_tp = dataframe["close"] >= dataframe["long_tp"].shift(1)
        short_atr_tp = dataframe["close"] <= dataframe["short_tp"].shift(1)
        long_atr_sl = dataframe["close"] <= dataframe["long_sl"].shift(1)
        short_atr_sl = dataframe["close"] >= dataframe["short_sl"].shift(1)

        long_ema_mid_fail = dataframe["close"] < dataframe["ema_mid"]
        short_ema_mid_fail = dataframe["close"] > dataframe["ema_mid"]
        long_regime_fail = (
            (dataframe["close_1h"] < dataframe["ema_fast_1h"])
            | (dataframe["ema_fast_1h"] < dataframe["ema_slow_1h"])
            | (dataframe["ema_slope_1h"] <= 0)
        )
        short_regime_fail = (
            (dataframe["close_1h"] > dataframe["ema_fast_1h"])
            | (dataframe["ema_fast_1h"] > dataframe["ema_slow_1h"])
            | (dataframe["ema_slope_1h"] >= 0)
        )

        dataframe.loc[long_atr_tp, ["exit_long", "exit_tag"]] = (1, "atr_tp")
        dataframe.loc[long_atr_sl & (dataframe["exit_long"] == 0), ["exit_long", "exit_tag"]] = (
            1,
            "atr_sl",
        )
        if USE_EMA_EXIT:
            dataframe.loc[
                long_ema_mid_fail & (dataframe["exit_long"] == 0),
                ["exit_long", "exit_tag"],
            ] = (1, "ema_mid_fail")
        if USE_REGIME_EXIT:
            dataframe.loc[
                long_regime_fail & (dataframe["exit_long"] == 0),
                ["exit_long", "exit_tag"],
            ] = (1, "regime_fail")

        dataframe.loc[short_atr_tp, ["exit_short", "exit_tag"]] = (1, "atr_tp")
        dataframe.loc[
            short_atr_sl & (dataframe["exit_short"] == 0),
            ["exit_short", "exit_tag"],
        ] = (1, "atr_sl")
        if USE_EMA_EXIT:
            dataframe.loc[
                short_ema_mid_fail & (dataframe["exit_short"] == 0),
                ["exit_short", "exit_tag"],
            ] = (1, "ema_mid_fail")
        if USE_REGIME_EXIT:
            dataframe.loc[
                short_regime_fail & (dataframe["exit_short"] == 0),
                ["exit_short", "exit_tag"],
            ] = (1, "regime_fail")
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        age = current_time - trade.open_date_utc
        if age.total_seconds() >= MAX_HOLD_CANDLES * self._timeframe_minutes() * 60:
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

    @staticmethod
    def _populate_informative_indicators(dataframe: pd.DataFrame) -> pd.DataFrame:
        dataframe["ema_50"] = pta.ema(dataframe["close"], length=HTF_EMA_FAST)
        dataframe["ema_200"] = pta.ema(dataframe["close"], length=HTF_EMA_SLOW)
        adx = pta.adx(dataframe["high"], dataframe["low"], dataframe["close"], length=ADX_LEN)
        dataframe["adx"] = adx[f"ADX_{ADX_LEN}"]
        atr = pta.atr(dataframe["high"], dataframe["low"], dataframe["close"], length=ATR_LEN)
        dataframe["atr_pct"] = atr / dataframe["close"]
        dataframe["ema_slope"] = dataframe["ema_50"] - dataframe["ema_50"].shift(
            HTF_SLOPE_LOOKBACK
        )
        return dataframe

    @staticmethod
    def _ensure_informative_columns(dataframe: pd.DataFrame) -> pd.DataFrame:
        defaults = {
            "close_1h": np.nan,
            "ema_50_1h": np.nan,
            "ema_200_1h": np.nan,
            "adx_1h": np.nan,
            "atr_pct_1h": np.nan,
            "ema_slope_1h": np.nan,
        }
        for column, default in defaults.items():
            if column not in dataframe.columns:
                dataframe[column] = default
        dataframe["ema_fast_1h"] = dataframe["ema_50_1h"]
        dataframe["ema_slow_1h"] = dataframe["ema_200_1h"]
        return dataframe

    @staticmethod
    def _timeframe_minutes() -> int:
        unit = RegimePullbackContinuationStrategy.timeframe[-1]
        value = int(RegimePullbackContinuationStrategy.timeframe[:-1])
        if unit == "m":
            return value
        if unit == "h":
            return value * 60
        if unit == "d":
            return value * 60 * 24
        return 5
