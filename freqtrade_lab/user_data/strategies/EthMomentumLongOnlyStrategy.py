import pandas as pd

from EthMomentumBreakoutStrategy import EthMomentumBreakoutStrategy


class EthMomentumLongOnlyStrategy(EthMomentumBreakoutStrategy):
    can_short = False

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        dataframe["enter_short"] = 0
        dataframe.loc[
            dataframe["enter_tag"].str.contains("short", na=False),
            "enter_tag",
        ] = ""
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe = super().populate_exit_trend(dataframe, metadata)
        dataframe["exit_short"] = 0
        return dataframe
