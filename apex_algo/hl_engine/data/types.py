"""
Custom NautilusTrader data types for Hyperliquid-specific market data.

Register with the DataEngine before use:
    engine.register_serializable_type(FundingRateData, ...)
"""

from nautilus_trader.core.data import Data
from nautilus_trader.model.identifiers import InstrumentId


class FundingRateData(Data):
    """
    Hyperliquid perpetual funding rate snapshot.

    Published via activeAssetCtx WebSocket channel.
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        rate: float,
        next_funding_time: int,
        open_interest: float,
        ts_event: int,
        ts_init: int,
    ) -> None:
        super().__init__(ts_event=ts_event, ts_init=ts_init)
        self.instrument_id = instrument_id
        self.rate = rate
        self.next_funding_time = next_funding_time
        self.open_interest = open_interest

    def __repr__(self) -> str:
        return (
            f"FundingRateData("
            f"instrument_id={self.instrument_id}, "
            f"rate={self.rate:.6f}, "
            f"next_funding_time={self.next_funding_time}, "
            f"open_interest={self.open_interest:.2f})"
        )


class LiquidationData(Data):
    """
    Hyperliquid liquidation event.

    Published via webData2 WebSocket channel.
    side: "LONG" (long position liquidated) or "SHORT" (short position liquidated).
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        side: str,
        quantity: float,
        price: float,
        usd_value: float,
        ts_event: int,
        ts_init: int,
    ) -> None:
        super().__init__(ts_event=ts_event, ts_init=ts_init)
        self.instrument_id = instrument_id
        self.side = side  # "LONG" or "SHORT"
        self.quantity = quantity
        self.price = price
        self.usd_value = usd_value

    def __repr__(self) -> str:
        return (
            f"LiquidationData("
            f"instrument_id={self.instrument_id}, "
            f"side={self.side}, "
            f"quantity={self.quantity}, "
            f"price={self.price}, "
            f"usd_value={self.usd_value:.2f})"
        )


class OpenInterestData(Data):
    """
    Hyperliquid open interest snapshot.

    Published via activeAssetCtx WebSocket channel alongside FundingRateData.
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        open_interest: float,
        open_interest_usd: float,
        ts_event: int,
        ts_init: int,
    ) -> None:
        super().__init__(ts_event=ts_event, ts_init=ts_init)
        self.instrument_id = instrument_id
        self.open_interest = open_interest
        self.open_interest_usd = open_interest_usd

    def __repr__(self) -> str:
        return (
            f"OpenInterestData("
            f"instrument_id={self.instrument_id}, "
            f"open_interest={self.open_interest:.4f}, "
            f"open_interest_usd={self.open_interest_usd:.2f})"
        )
