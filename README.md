# hyperliquid

A collection of tools for trading, monitoring, and analysis on the Hyperliquid perpetuals exchange.

## Tools

| Folder | Description |
|--------|-------------|
| [hl_data_api](./hl_data_api/) | API wrapper for a hosted Hyperliquid data layer exposing liquidations, whale trades, large positions, and real-time blockchain events. |
| [hl_engine](./hl_engine/) | NautilusTrader-based Hyperliquid trading engine with data collection, backtesting, live orchestration, strategy containers, risk controls, and multiple strategies including APEX. |
| [liquidation_tracker](./liquidation_tracker/) | Async service that captures real-time Hyperliquid liquidation events, stores them as JSONL, and maintains rolling aggregates across multiple time windows. |
| [risk_manager](./risk_manager/) | Monitors open positions via the Hyperliquid info endpoint and sends Telegram alerts when positions approach their liquidation distance or exceed risk thresholds. |
| [freqtrade_lab](./freqtrade_lab/) | Freqtrade workspace for testing and running Hyperliquid crypto strategies. Currently includes a streak-reversal strategy with ATR stops, take-profit, and risk-based position sizing. |
| [wallet_tracker](./wallet_tracker/) | Tracks large trades and open positions from curated high-value wallets on Hyperliquid, displaying a live rich terminal dashboard. |
