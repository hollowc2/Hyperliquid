"""
All config dataclasses for APEX Trader.
Uses msgspec.Struct (frozen, kw_only) for fast serialization and immutability.
NautilusTrader StrategyConfig is a Pydantic BaseModel — ApexStrategyConfig inherits from it.
"""

import os
from typing import Optional

import msgspec
from nautilus_trader.config import StrategyConfig


class HyperliquidConfig(msgspec.Struct, frozen=True, kw_only=True):
    """Connection and wallet settings for Hyperliquid."""

    wallet_address: str
    private_key: str
    base_url: str = "https://api.hyperliquid.xyz"
    ws_url: str = "wss://api.hyperliquid.xyz/ws"
    testnet: bool = False

    @classmethod
    def from_env(cls) -> "HyperliquidConfig":
        testnet = os.getenv("HL_TESTNET", "false").lower() == "true"
        base_url = (
            os.getenv("HL_BASE_URL", "https://api.hyperliquid-testnet.xyz")
            if testnet
            else os.getenv("HL_BASE_URL", "https://api.hyperliquid.xyz")
        )
        ws_url = (
            os.getenv("HL_WS_URL", "wss://api.hyperliquid-testnet.xyz/ws")
            if testnet
            else os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws")
        )
        return cls(
            wallet_address=os.environ.get("HL_WALLET_ADDRESS", ""),
            private_key=os.environ.get("HL_PRIVATE_KEY", ""),
            base_url=base_url,
            ws_url=ws_url,
            testnet=testnet,
        )


class FeatureConfig(msgspec.Struct, frozen=True, kw_only=True):
    """Feature computation windows and parameters."""

    # Trade flow imbalance window (nanoseconds) — default 60 seconds
    tfi_window_ns: int = 60_000_000_000
    # Number of bars for short vol window
    vol_short_window: int = 20
    # Number of bars for long vol window
    vol_long_window: int = 100
    # Order book depth levels for OBI
    obi_depth: int = 5
    # USD book depth levels
    book_depth_levels: int = 10


class ModelConfig(msgspec.Struct, frozen=True, kw_only=True):
    """Hawkes process and Bayesian model parameters."""

    # Hawkes process
    hawkes_mu: float = 0.1
    hawkes_alpha: float = 0.3
    hawkes_beta: float = 1.0

    # Bayesian edge weights (should sum to ~1.0)
    w1_obi: float = 0.30
    w2_tfi: float = 0.30
    w3_mp_drift: float = 0.20
    w4_hawkes: float = 0.10
    w5_cascade: float = 0.05
    w6_funding: float = 0.05

    # Regime thresholds
    regime_vol_ratio_threshold: float = 1.5
    regime_trend_threshold: float = 2.0
    regime_min_liquidity_usd: float = 50_000.0

    # Funding history length (168 = 1 week of 8h funding)
    funding_history_len: int = 168


class RiskConfig(msgspec.Struct, frozen=True, kw_only=True):
    """Position sizing and risk limits."""

    max_position_usd: float = 10_000.0
    max_leverage: float = 5.0
    drawdown_limit: float = 0.15          # 15% max drawdown
    drawdown_reduce_threshold: float = 0.10  # force reduce-only at 10%
    kelly_fraction: float = 0.25          # fractional Kelly multiplier
    max_kelly_fraction: float = 0.20      # cap on Kelly output
    inventory_penalty_scale: float = 0.5  # inventory skew dampener


class ExecutionConfig(msgspec.Struct, frozen=True, kw_only=True):
    """Order execution thresholds."""

    min_edge_threshold: float = 0.002     # minimum edge to trade (0.2%)
    cascade_threshold: float = 1.5        # cascade_score threshold
    min_queue_prob: float = 0.3           # min fill probability for limit order
    market_slippage_buffer: float = 0.05  # 5% IOC slippage buffer
    signal_throttle_ms: int = 100         # min ms between signal evaluations


class ApexConfig(msgspec.Struct, frozen=True, kw_only=True):
    """Root config composing all sub-configs."""

    hyperliquid: HyperliquidConfig
    feature: FeatureConfig = FeatureConfig()
    model: ModelConfig = ModelConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()

    @classmethod
    def from_env(cls) -> "ApexConfig":
        from dotenv import load_dotenv
        load_dotenv()
        return cls(hyperliquid=HyperliquidConfig.from_env())


class ApexStrategyConfig(StrategyConfig):
    """
    NautilusTrader-compatible strategy config.
    Pydantic BaseModel — holds instrument_id and the full ApexConfig.
    """

    instrument_id: str = "BTC-USD.HYPERLIQUID"
    apex_config: Optional[ApexConfig] = None
