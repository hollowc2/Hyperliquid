DATA_DIR := user_data/data/hyperliquid
CONFIG := user_data/config/config.json
BACKTEST_CONFIG := user_data/config/config.backtest.json
COINBASE_MULTI_CONFIG := user_data/config/config.backtest.coinbase.multi.json
HLPROXY_MULTI_CONFIG := user_data/config/config.backtest.hlproxy.multi.json
STRATEGY := StreakReversalStrategy
-include .env

COIN ?= ETH
SYMBOLS ?= BTC ETH SOL LINK HYPE XRP
PAIRS ?= BTC/USD ETH/USD SOL/USD LINK/USD HYPE/USD XRP/USD
TIMEFRAME ?= 5m
SUPPORT_TIMEFRAME ?= 1h
TIMEFRAMES ?= 5m 1h
TIMERANGE ?= 20250702-20260702
HL_CATALOG ?= ../hl_engine/data/catalog
COINBASE_DATA_DIR ?= user_data/data/coinbase
HLPROXY_DATA_DIR ?= user_data/data/coinbase_hlproxy
STREAK_TRIGGER ?= 6
EMA_PERIOD ?= 100
RISK_PCT ?= 0.01
STOP_ATR_MULT ?= 1.0
TP_ATR_MULT ?= 1.5
MAX_POSITION_PCT ?= 0.20
LEVERAGE ?= 1
FT_CONTEXT_ENABLED ?= false
FT_CONTEXT_DIR ?= user_data/data/context

.PHONY: export-data download-coinbase-data proxy-coinbase-data long-data backtest sweep trade

export-data:
	cd ../hl_engine && uv run python export_freqtrade_ohlcv.py \
		--catalog $(HL_CATALOG) \
		--coin $(COIN) \
		--timeframe $(TIMEFRAME) \
		--output ../freqtrade_lab/$(DATA_DIR)
	cd ../hl_engine && uv run python export_freqtrade_ohlcv.py \
		--catalog $(HL_CATALOG) \
		--coin $(COIN) \
		--source-timeframe $(TIMEFRAME) \
		--timeframe $(SUPPORT_TIMEFRAME) \
		--output ../freqtrade_lab/$(DATA_DIR)

download-coinbase-data:
	docker compose run --rm freqtrade download-data \
		--config /freqtrade/$(COINBASE_MULTI_CONFIG) \
		--datadir /freqtrade/$(COINBASE_DATA_DIR) \
		--pairs $(PAIRS) \
		--timeframes $(TIMEFRAMES) \
		--timerange $(TIMERANGE) \
		--trading-mode spot \
		--erase

proxy-coinbase-data:
	python scripts/proxy_spot_to_hl_futures.py \
		--source-dir $(COINBASE_DATA_DIR) \
		--dest-dir $(HLPROXY_DATA_DIR) \
		--symbols $(SYMBOLS) \
		--timeframes $(TIMEFRAMES)

long-data: download-coinbase-data proxy-coinbase-data

backtest:
	docker compose run --rm \
		-e STREAK_TRIGGER=$(STREAK_TRIGGER) \
		-e EMA_PERIOD=$(EMA_PERIOD) \
		-e RISK_PCT=$(RISK_PCT) \
		-e STOP_ATR_MULT=$(STOP_ATR_MULT) \
		-e TP_ATR_MULT=$(TP_ATR_MULT) \
		-e MAX_POSITION_PCT=$(MAX_POSITION_PCT) \
		-e LEVERAGE=$(LEVERAGE) \
		-e FT_CONTEXT_ENABLED=$(FT_CONTEXT_ENABLED) \
		-e FT_CONTEXT_DIR=$(FT_CONTEXT_DIR) \
		freqtrade backtesting \
		--config /freqtrade/$(BACKTEST_CONFIG) \
		--datadir /freqtrade/$(DATA_DIR) \
		--strategy $(STRATEGY) \
		--cache none

sweep:
	for trigger in 4 5 6 7 8; do \
		for stop in 0.8 1.0 1.2; do \
			for tp in 1.2 1.5 2.0; do \
				$(MAKE) backtest STREAK_TRIGGER=$$trigger STOP_ATR_MULT=$$stop TP_ATR_MULT=$$tp; \
			done; \
		done; \
	done

trade:
	docker compose up freqtrade
