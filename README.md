# hyperliquid

A collection of tools for trading, monitoring, and analysis on the Hyperliquid perpetuals exchange.

## Tools

| Folder | Description |
|--------|-------------|
| [apex_algo](./apex_algo/) | NautilusTrader-based algorithmic strategy using a Bayesian edge model with orderbook features, regime detection, and ATR-based risk sizing. |
| [data_api](./data_api/) | API wrapper for a hosted Hyperliquid data layer exposing liquidations, whale trades, large positions, and real-time blockchain events. |
| [liquidation_tracker](./liquidation_tracker/) | Async service that captures real-time Hyperliquid liquidation events, stores them as JSONL, and maintains rolling aggregates across multiple time windows. |
| [risk_manager](./risk_manager/) | Monitors open positions via the Hyperliquid info endpoint and sends Telegram alerts when positions approach their liquidation distance or exceed risk thresholds. |
| [freqtrade_lab](./freqtrade_lab/) | Freqtrade workspace for testing and running Hyperliquid crypto strategies. Currently includes a streak-reversal strategy with ATR stops, take-profit, and risk-based position sizing. |
| [top_wallets](./top_wallets/) | Tracks large trades and open positions from a curated list of high-value wallets on Hyperliquid, displaying a live rich terminal dashboard. |
