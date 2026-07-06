"""
FundingBasisCarryStrategy

Long-only 5m strategy that trades when local Hyperliquid funding and
perp-vs-Coinbase spot basis context agree with an EMA uptrend.
"""

import logging
import os
from datetime import datetime
from typing import Optional

import pandas as pd
import pandas_ta as pta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute

from context_data import add_optional_context


logger = logging.getLogger(__name__)

EMA_FAST = int(os.environ.get("FBC_EMA_FAST", 50))
EMA_SLOW = int(os.environ.get("FBC_EMA_SLOW", 200))
EMA_SLOPE_LOOKBACK = int(os.environ.get("FBC_EMA_SLOPE_LOOKBACK", 12))
ATR_LEN = int(os.environ.get("FBC_ATR_LEN", 14))
ATR_PCT_MIN = float(os.environ.get("FBC_ATR_PCT_MIN", 0.0008))
ATR_PCT_MAX = float(os.environ.get("FBC_ATR_PCT_MAX", 0.0600))
MIN_FUNDING_8H = float(os.environ.get("FBC_MIN_FUNDING_8H", 0.0))
MIN_FUNDING_24H = float(os.environ.get("FBC_MIN_FUNDING_24H", 0.0))
MIN_BASIS_PCT = float(os.environ.get("FBC_MIN_BASIS_PCT", 0.0))
MAX_BASIS_PCT = float(os.environ.get("FBC_MAX_BASIS_PCT", 0.015))
MAX_BASIS_Z = float(os.environ.get("FBC_MAX_BASIS_Z", 2.0))
MIN_FUNDING_Z = float(os.environ.get("FBC_MIN_FUNDING_Z", -0.5))
STOP_ATR_MULT = float(os.environ.get("FBC_STOP_ATR_MULT", 1.5))
RISK_PCT = float(os.environ.get("FBC_RISK_PCT", os.environ.get("RISK_PCT", 0.01)))
MAX_POSITION_PCT = float(
    os.environ.get("FBC_MAX_POSITION_PCT", os.environ.get("MAX_POSITION_PCT", 0.20))
)
MAX_HOLD_CANDLES = int(os.environ.get("FBC_MAX_HOLD_CANDLES", 144))
LEVERAGE_VALUE = int(os.environ.get("FBC_LEVERAGE", os.environ.get("LEVERAGE", 1)))


class FundingBasisCarryStrategy(IStrategy):
    INTERFACE_VERSION = 3
    can_short = False
    use_custom_stoploss = True

    timeframe = "5m"
    startup_candle_count = 300

    stoploss = -0.05
    minimal_roi = {"0": 100}
    trailing_stop = False

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe = add_optional_context(dataframe, metadata.get("pair"), self.timeframe)
        dataframe["ema_fast"] = pta.ema(dataframe["close"], length=EMA_FAST)
        dataframe["ema_slow"] = pta.ema(dataframe["close"], length=EMA_SLOW)
        dataframe["atr"] = pta.atr(
            dataframe["high"],
            dataframe["low"],
            dataframe["close"],
            length=ATR_LEN,
        )
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["trend_up"] = (
            (dataframe["close"] > dataframe["ema_fast"])
            & (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["ema_slow"] > dataframe["ema_slow"].shift(EMA_SLOPE_LOOKBACK))
        )
        dataframe["long_sl"] = dataframe["close"] - STOP_ATR_MULT * dataframe["atr"]
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""

        funding_positive = (
            (dataframe["ctx_funding_8h_mean"] > MIN_FUNDING_8H)
            & (dataframe["ctx_funding_24h_mean"] > MIN_FUNDING_24H)
            & (dataframe["ctx_funding_z"] >= MIN_FUNDING_Z)
        )
        basis_constructive = (
            (dataframe["ctx_basis_pct"] > MIN_BASIS_PCT)
            & (dataframe["ctx_basis_pct"] <= MAX_BASIS_PCT)
            & (dataframe["ctx_basis_z"] <= MAX_BASIS_Z)
        )
        volatility_ok = dataframe["atr_pct"].between(ATR_PCT_MIN, ATR_PCT_MAX)

        long_mask = (
            (dataframe["ctx_loaded"] > 0)
            & funding_positive
            & basis_constructive
            & dataframe["trend_up"]
            & volatility_ok
            & dataframe["atr"].notna()
            & dataframe["ctx_risk_on_ok"]
        )

        dataframe.loc[long_mask, "enter_long"] = 1
        dataframe.loc[long_mask, "enter_tag"] = "funding_basis_carry_long"
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""

        funding_reversal = (
            (dataframe["ctx_funding_8h_mean"] <= MIN_FUNDING_8H)
            | (dataframe["ctx_funding_24h_mean"] <= MIN_FUNDING_24H)
        )
        basis_reversal = dataframe["ctx_basis_pct"] <= MIN_BASIS_PCT
        trend_break = (dataframe["close"] < dataframe["ema_slow"]) | (
            dataframe["ema_fast"] < dataframe["ema_slow"]
        )
        atr_stop = dataframe["close"] <= dataframe["long_sl"].shift(1)

        dataframe.loc[funding_reversal, ["exit_long", "exit_tag"]] = (
            1,
            "funding_reversal",
        )
        dataframe.loc[
            basis_reversal & (dataframe["exit_long"] == 0),
            ["exit_long", "exit_tag"],
        ] = (1, "basis_reversal")
        dataframe.loc[
            trend_break & (dataframe["exit_long"] == 0),
            ["exit_long", "exit_tag"],
        ] = (1, "trend_break")
        dataframe.loc[
            atr_stop & (dataframe["exit_long"] == 0),
            ["exit_long", "exit_tag"],
        ] = (1, "atr_stop")
        return dataframe

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
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is None or dataframe.empty:
                return self.stoploss
            atr = dataframe.iloc[-1]["atr"]
            if pd.isna(atr) or atr <= 0:
                return self.stoploss
            stop_price = current_rate - STOP_ATR_MULT * atr
            return stoploss_from_absolute(stop_price, current_rate, is_short=False)
        except Exception as exc:
            logger.warning("custom_stoploss fallback for %s: %s", pair, exc)
            return self.stoploss

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:
        age = current_time - trade.open_date_utc
        if age.total_seconds() >= MAX_HOLD_CANDLES * 5 * 60:
            return "time_stop"
        return None

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
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is None or dataframe.empty:
                return proposed_stake
            atr = dataframe.iloc[-1]["atr"]
            if pd.isna(atr) or atr <= 0 or current_rate <= 0:
                return proposed_stake

            equity = max_stake / max(leverage, 1.0)
            risk_usd = equity * RISK_PCT
            stop_pct = (STOP_ATR_MULT * atr) / current_rate
            if stop_pct <= 0:
                return proposed_stake

            stake = min(risk_usd / stop_pct, equity * MAX_POSITION_PCT, max_stake)
            if min_stake is not None:
                stake = max(stake, min_stake)
            return stake
        except Exception as exc:
            logger.warning("custom_stake_amount fallback for %s: %s", pair, exc)
            return proposed_stake

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
        return float(min(LEVERAGE_VALUE, max_leverage))
