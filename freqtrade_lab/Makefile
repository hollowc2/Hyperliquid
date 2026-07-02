DATA_DIR := user_data/data/hyperliquid
CONFIG := user_data/config/config.json
BACKTEST_CONFIG := user_data/config/config.backtest.json
STRATEGY := StreakReversalStrategy
-include .env

COIN ?= ETH
TIMEFRAME ?= 5m
SUPPORT_TIMEFRAME ?= 1h
HL_CATALOG ?= ../hl_engine/data/catalog
STREAK_TRIGGER ?= 6
EMA_PERIOD ?= 100
RISK_PCT ?= 0.01
STOP_ATR_MULT ?= 1.0
TP_ATR_MULT ?= 1.5
MAX_POSITION_PCT ?= 0.20
LEVERAGE ?= 1

.PHONY: export-data backtest sweep trade

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

backtest:
	docker compose run --rm \
		-e STREAK_TRIGGER=$(STREAK_TRIGGER) \
		-e EMA_PERIOD=$(EMA_PERIOD) \
		-e RISK_PCT=$(RISK_PCT) \
		-e STOP_ATR_MULT=$(STOP_ATR_MULT) \
		-e TP_ATR_MULT=$(TP_ATR_MULT) \
		-e MAX_POSITION_PCT=$(MAX_POSITION_PCT) \
		-e LEVERAGE=$(LEVERAGE) \
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
