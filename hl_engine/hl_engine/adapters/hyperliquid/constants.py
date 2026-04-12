"""
Hyperliquid adapter constants: venue, URLs, WebSocket subscription type strings.
"""

from nautilus_trader.model.identifiers import Venue

HYPERLIQUID_VENUE = Venue("HYPERLIQUID")

HL_BASE_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"

HL_TESTNET_BASE_URL = "https://api.hyperliquid-testnet.xyz"
HL_TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"

HL_INFO_ENDPOINT = "/info"
HL_EXCHANGE_ENDPOINT = "/exchange"

HL_PING_INTERVAL_SECS = 50

# WebSocket channel type strings
WS_TYPE_L2_BOOK = "l2Book"
WS_TYPE_TRADES = "trades"
WS_TYPE_CANDLE = "candle"
WS_TYPE_ACTIVE_ASSET_CTX = "activeAssetCtx"
WS_TYPE_USER_FILLS = "userFills"
WS_TYPE_ORDER_UPDATES = "orderUpdates"
WS_TYPE_WEB_DATA2 = "webData2"

# Bar interval mappings: NautilusTrader step → Hyperliquid interval string
BAR_INTERVAL_MAP = {
    1: "1m",
    3: "3m",
    5: "5m",
    15: "15m",
    30: "30m",
    60: "1h",
    240: "4h",
    1440: "1d",
}
