"""
Custom NautilusTrader data types for Hyperliquid-specific market data.

Register with the DataEngine before use:
    engine.register_serializable_type(FundingRateData, ...)

Note: NautilusTrader's Data base class is a Cython extension type.
Subclasses must:
  1. Pass ts_event/ts_init to Data.__new__(cls, ts_event, ts_init)
  2. Override ts_event and ts_init as @property (abstract in Cython base)
"""

from nautilus_trader.core.data import Data
from nautilus_trader.model.identifiers import InstrumentId


class FundingRateData(Data):
    """
    Hyperliquid perpetual funding rate snapshot.

    Published via activeAssetCtx WebSocket channel.
    """

    def __new__(cls, instrument_id, rate, next_funding_time, open_interest, ts_event, ts_init):
        return Data.__new__(cls, ts_event, ts_init)

    def __init__(
        self,
        instrument_id: InstrumentId,
        rate: float,
        next_funding_time: int,
        open_interest: float,
        ts_event: int,
        ts_init: int,
    ) -> None:
        self._ts_event = ts_event
        self._ts_init = ts_init
        self.instrument_id = instrument_id
        self.rate = rate
        self.next_funding_time = next_funding_time
        self.open_interest = open_interest

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

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

    def __new__(cls, instrument_id, side, quantity, price, usd_value, ts_event, ts_init):
        return Data.__new__(cls, ts_event, ts_init)

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
        self._ts_event = ts_event
        self._ts_init = ts_init
        self.instrument_id = instrument_id
        self.side = side  # "LONG" or "SHORT"
        self.quantity = quantity
        self.price = price
        self.usd_value = usd_value

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

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

    def __new__(cls, instrument_id, open_interest, open_interest_usd, ts_event, ts_init):
        return Data.__new__(cls, ts_event, ts_init)

    def __init__(
        self,
        instrument_id: InstrumentId,
        open_interest: float,
        open_interest_usd: float,
        ts_event: int,
        ts_init: int,
    ) -> None:
        self._ts_event = ts_event
        self._ts_init = ts_init
        self.instrument_id = instrument_id
        self.open_interest = open_interest
        self.open_interest_usd = open_interest_usd

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

    def __repr__(self) -> str:
        return (
            f"OpenInterestData("
            f"instrument_id={self.instrument_id}, "
            f"open_interest={self.open_interest:.4f}, "
            f"open_interest_usd={self.open_interest_usd:.2f})"
        )
