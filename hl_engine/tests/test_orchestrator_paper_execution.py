import sqlite3
import pytest

from hl_engine.orchestrator import app as app_module
from hl_engine.orchestrator.app import OrderRequest
from hl_engine.orchestrator.global_risk import GlobalRiskManager
from hl_engine.orchestrator.paper_execution import PAPER_TAKER_FEE, PaperExecutionEngine
from hl_engine.orchestrator.persistence import PersistenceStore
from hl_engine.orchestrator.rate_limiter import RateLimiter
from hl_engine.orchestrator.strategy_registry import StrategyRegistry


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send_multipart(self, frames):
        self.messages.append(frames)


class FakeDataFeed:
    def get_snapshot(self, coin):
        assert coin == "BTC"
        return {
            "coin": "BTC",
            "bids": [[100.0, 2.0]],
            "asks": [[101.0, 2.0]],
            "ts_ns": 1,
        }


@pytest.mark.asyncio
async def test_paper_account_buy_sell_persists_and_reconciles(tmp_path):
    store = PersistenceStore(tmp_path / "orchestrator.db")
    await store.init()
    risk = GlobalRiskManager(global_ceiling_usd=10_000)
    risk.configure_strategy("vclimax-btc", 1000)
    socket = FakeSocket()
    paper = PaperExecutionEngine(store, risk, socket)

    buy = await paper.execute_order(
        oid=1,
        strategy_id="vclimax-btc",
        client_order_id="buy-1",
        instrument_id="BTC-USD.HYPERLIQUID",
        side="BUY",
        qty=1.0,
        fill_px=100.0,
        initial_balance=1000.0,
    )
    assert buy.balance == 1000.0 - 100.0 * PAPER_TAKER_FEE
    assert buy.position_qty == 1.0
    assert buy.avg_price == 100.0

    sell = await paper.execute_order(
        oid=2,
        strategy_id="vclimax-btc",
        client_order_id="sell-1",
        instrument_id="BTC-USD.HYPERLIQUID",
        side="SELL",
        qty=1.0,
        fill_px=110.0,
        initial_balance=1000.0,
    )
    assert sell.realized_pnl == 10.0
    assert sell.position_qty == 0.0
    expected_fees = 100.0 * PAPER_TAKER_FEE + 110.0 * PAPER_TAKER_FEE
    assert sell.balance == 1000.0 + 10.0 - expected_fees

    await store.flush()
    restored = PaperExecutionEngine(store, risk, socket)
    await restored.restore_from_db()
    state = restored.account_state("vclimax-btc", 1000.0)
    assert float(state["marginSummary"]["accountValue"]) == pytest.approx(sell.balance)
    assert state["paper"]["position_qty"] == 0.0

    with sqlite3.connect(tmp_path / "orchestrator.db") as db:
        row = db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()
    assert row[0] == 2
    await store.close()


@pytest.mark.asyncio
async def test_paper_account_marks_open_position_to_mid_price(tmp_path):
    store = PersistenceStore(tmp_path / "orchestrator.db")
    await store.init()
    risk = GlobalRiskManager(global_ceiling_usd=10_000)
    risk.configure_strategy("trend-follow-btc", 1000)
    socket = FakeSocket()
    paper = PaperExecutionEngine(store, risk, socket)
    paper.set_mark_price_provider(lambda instrument_id: 110.0)

    await paper.execute_order(
        oid=1,
        strategy_id="trend-follow-btc",
        client_order_id="buy-1",
        instrument_id="BTC-USD.HYPERLIQUID",
        side="BUY",
        qty=1.0,
        fill_px=100.0,
        initial_balance=1000.0,
    )

    state = paper.account_state("trend-follow-btc", 1000.0, "BTC-USD.HYPERLIQUID")
    expected_balance = 1000.0 - 100.0 * PAPER_TAKER_FEE

    assert state["paper"]["balance"] == pytest.approx(expected_balance)
    assert state["paper"]["unrealized_pnl"] == pytest.approx(10.0)
    assert state["paper"]["equity"] == pytest.approx(expected_balance + 10.0)
    assert float(state["marginSummary"]["accountValue"]) == pytest.approx(expected_balance + 10.0)
    assert float(state["assetPositions"][0]["position"]["unrealizedPnl"]) == pytest.approx(10.0)
    await store.close()


@pytest.mark.asyncio
async def test_reset_paper_account_zeroes_pnl_fees_and_exposure(tmp_path):
    store = PersistenceStore(tmp_path / "orchestrator.db")
    await store.init()
    risk = GlobalRiskManager(global_ceiling_usd=10_000)
    risk.configure_strategy("trend-follow-btc", 1000)
    socket = FakeSocket()
    paper = PaperExecutionEngine(store, risk, socket)

    await paper.execute_order(
        oid=1,
        strategy_id="trend-follow-btc",
        client_order_id="buy-1",
        instrument_id="BTC-USD.HYPERLIQUID",
        side="BUY",
        qty=1.0,
        fill_px=100.0,
        initial_balance=1000.0,
    )
    account = paper.reset_account("trend-follow-btc", 1000.0)

    assert account.initial_balance == 1000.0
    assert account.balance == 1000.0
    assert account.realized_pnl == 0.0
    assert account.position_qty == 0.0
    assert account.avg_price == 0.0
    assert account.cumulative_fees == 0.0

    await store.flush()
    restored = PaperExecutionEngine(store, risk, socket)
    await restored.restore_from_db()
    state = restored.account_state("trend-follow-btc", 1000.0)
    assert state["paper"]["balance"] == 1000.0
    assert state["paper"]["realized_pnl"] == 0.0
    assert state["paper"]["cumulative_fees"] == 0.0
    await store.close()


@pytest.mark.asyncio
async def test_submit_order_paper_path_publishes_fill_and_sets_risk(tmp_path):
    store = PersistenceStore(tmp_path / "orchestrator.db")
    await store.init()
    risk = GlobalRiskManager(global_ceiling_usd=10_000)
    risk.configure_strategy("vclimax-btc", 1000)
    rate_limiter = RateLimiter(global_max_ops=100)
    rate_limiter.configure_strategy("vclimax-btc", 100)
    registry = StrategyRegistry("strategies")
    registry.load()
    socket = FakeSocket()
    paper = PaperExecutionEngine(store, risk, socket)

    old_globals = {
        "persistence": app_module.persistence,
        "rate_limiter": app_module.rate_limiter,
        "risk_manager": app_module.risk_manager,
        "order_gateway": app_module.order_gateway,
        "fill_dispatcher": app_module.fill_dispatcher,
        "paper_execution": app_module.paper_execution,
        "strategy_registry": app_module.strategy_registry,
        "data_feed": app_module.data_feed,
    }
    try:
        app_module.persistence = store
        app_module.rate_limiter = rate_limiter
        app_module.risk_manager = risk
        app_module.order_gateway = None
        app_module.fill_dispatcher = None
        app_module.paper_execution = paper
        app_module.strategy_registry = registry
        app_module.data_feed = FakeDataFeed()

        result = await app_module.submit_order(
            OrderRequest(
                strategy_id="vclimax-btc",
                client_order_id="paper-buy-1",
                instrument_id="BTC-USD.HYPERLIQUID",
                side="BUY",
                order_type="MARKET",
                quantity=1.0,
                price=None,
            )
        )
    finally:
        for name, value in old_globals.items():
            setattr(app_module, name, value)

    assert result["status"] == "submitted"
    assert result["client_order_id"] == "paper-buy-1"
    assert len(socket.messages) == 1
    frame = socket.messages[0]
    assert frame[0] == b"fills.vclimax-btc"

    summary = risk.get_summary()
    assert summary["strategies"]["vclimax-btc"]["notional_usd"] == 101.0

    await store.flush()
    with sqlite3.connect(tmp_path / "orchestrator.db") as db:
        row = db.execute(
            "SELECT client_order_id, fill_px, fill_sz, fee FROM paper_fills"
        ).fetchone()
    assert row[0] == "paper-buy-1"
    assert row[1] == 101.0
    assert row[2] == 1.0
    assert row[3] == pytest.approx(101.0 * PAPER_TAKER_FEE)
    await store.close()
