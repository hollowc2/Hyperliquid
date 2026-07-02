"""
EthMomentumBreakoutStrategy - bounded ETH/USDC:USDC trend breakout test.

Idea:
  - Trade only when the 5m trend agrees with EMA direction.
  - Enter on a fresh Donchian channel breakout with above-average volume.
  - Exit on momentum failure back through EMA fast, an opposite channel break,
    or ATR-based profit extension.
"""

import os

import pandas as pd
import pandas_ta as pta
from freqtrade.strategy import IStrategy

from context_data import add_optional_context


BREAKOUT_WINDOW: int = int(os.environ.get("BREAKOUT_WINDOW", 36))
EXIT_WINDOW: int = int(os.environ.get("EXIT_WINDOW", 18))
EMA_FAST: int = int(os.environ.get("EMA_FAST", 50))
EMA_SLOW: int = int(os.environ.get("EMA_SLOW", 200))
ATR_PERIOD: int = int(os.environ.get("ATR_PERIOD", 14))
ATR_TP_MULT: float = float(os.environ.get("ATR_TP_MULT", 2.2))
VOLUME_WINDOW: int = int(os.environ.get("VOLUME_WINDOW", 24))
VOLUME_MULT: float = float(os.environ.get("VOLUME_MULT", 1.05))
LEVERAGE_VALUE: int = int(os.environ.get("LEVERAGE", 1))


class EthMomentumBreakoutStrategy(IStrategy):
    """
    Momentum breakout strategy for Hyperliquid ETH perpetual data.

    This is intentionally compact so parameter sweeps can be driven through
    environment variables without changing config files.
    """

    INTERFACE_VERSION = 3
    can_short = True

    timeframe = "5m"
    startup_candle_count: int = 260

    minimal_roi = {
        "0": 100,
    }
    stoploss = -0.035
    trailing_stop = False

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe = add_optional_context(dataframe, metadata.get("pair"), self.timeframe)
        dataframe["ema_fast"] = pta.ema(dataframe["close"], length=EMA_FAST)
        dataframe["ema_slow"] = pta.ema(dataframe["close"], length=EMA_SLOW)
        dataframe["atr"] = pta.atr(
            dataframe["high"],
            dataframe["low"],
            dataframe["close"],
            length=ATR_PERIOD,
        )
        dataframe["volume_sma"] = dataframe["volume"].rolling(VOLUME_WINDOW).mean()

        dataframe["breakout_high"] = (
            dataframe["high"].shift(1).rolling(BREAKOUT_WINDOW).max()
        )
        dataframe["breakout_low"] = (
            dataframe["low"].shift(1).rolling(BREAKOUT_WINDOW).min()
        )
        dataframe["exit_high"] = dataframe["high"].shift(1).rolling(EXIT_WINDOW).max()
        dataframe["exit_low"] = dataframe["low"].shift(1).rolling(EXIT_WINDOW).min()

        dataframe["atr_tp_long"] = dataframe["close"] + ATR_TP_MULT * dataframe["atr"]
        dataframe["atr_tp_short"] = dataframe["close"] - ATR_TP_MULT * dataframe["atr"]

        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        volume_confirmed = dataframe["volume"] > dataframe["volume_sma"] * VOLUME_MULT
        long_regime = dataframe["ema_fast"] > dataframe["ema_slow"]
        short_regime = dataframe["ema_fast"] < dataframe["ema_slow"]

        fresh_long_breakout = (
            (dataframe["close"] > dataframe["breakout_high"])
            & (dataframe["close"].shift(1) <= dataframe["breakout_high"].shift(1))
        )
        fresh_short_breakout = (
            (dataframe["close"] < dataframe["breakout_low"])
            & (dataframe["close"].shift(1) >= dataframe["breakout_low"].shift(1))
        )

        long_mask = (
            long_regime
            & fresh_long_breakout
            & volume_confirmed
            & dataframe["atr"].notna()
            & dataframe["ctx_risk_on_ok"]
            & dataframe["ctx_funding_neutral"]
        )
        short_mask = (
            short_regime
            & fresh_short_breakout
            & volume_confirmed
            & dataframe["atr"].notna()
            & dataframe["ctx_risk_off_ok"]
            & dataframe["ctx_funding_neutral"]
        )

        dataframe.loc[long_mask, "enter_long"] = 1
        dataframe.loc[long_mask, "enter_tag"] = "ema_donchian_volume_long"
        dataframe.loc[long_mask & (dataframe["ctx_loaded"] > 0), "enter_tag"] = (
            "ema_donchian_volume_long_ctx"
        )
        dataframe.loc[short_mask, "enter_short"] = 1
        dataframe.loc[short_mask, "enter_tag"] = "ema_donchian_volume_short"
        dataframe.loc[short_mask & (dataframe["ctx_loaded"] > 0), "enter_tag"] = (
            "ema_donchian_volume_short_ctx"
        )

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""

        long_momentum_fail = dataframe["close"] < dataframe["ema_fast"]
        short_momentum_fail = dataframe["close"] > dataframe["ema_fast"]

        long_channel_fail = dataframe["close"] < dataframe["exit_low"]
        short_channel_fail = dataframe["close"] > dataframe["exit_high"]

        long_atr_extension = dataframe["close"] >= dataframe["atr_tp_long"].shift(1)
        short_atr_extension = dataframe["close"] <= dataframe["atr_tp_short"].shift(1)

        dataframe.loc[long_momentum_fail | long_channel_fail, "exit_long"] = 1
        dataframe.loc[long_momentum_fail, "exit_tag"] = "ema_fast_loss"
        dataframe.loc[long_channel_fail, "exit_tag"] = "channel_fail"
        dataframe.loc[
            long_atr_extension & (dataframe["exit_long"] == 0),
            ["exit_long", "exit_tag"],
        ] = (1, "atr_extension")

        dataframe.loc[short_momentum_fail | short_channel_fail, "exit_short"] = 1
        dataframe.loc[short_momentum_fail, "exit_tag"] = "ema_fast_loss"
        dataframe.loc[short_channel_fail, "exit_tag"] = "channel_fail"
        dataframe.loc[
            short_atr_extension & (dataframe["exit_short"] == 0),
            ["exit_short", "exit_tag"],
        ] = (1, "atr_extension")

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
