"""
Orchestrator-side paper execution.

This is used when HL_PAPER_TRADE=true or no private key is configured. It
creates immediate synthetic taker fills from the orchestrator's live top of book
and persists paper account state so strategy restarts can reconcile normally.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import zmq.asyncio

from hl_engine.orchestrator import metrics
from hl_engine.orchestrator.global_risk import GlobalRiskManager
from hl_engine.orchestrator.persistence import PersistenceStore
from hl_engine.transport.serialization import wrap_fill

log = logging.getLogger(__name__)

PAPER_TAKER_FEE = 0.0005


@dataclass
class PaperAccount:
    initial_balance: float
    balance: float
    realized_pnl: float = 0.0
    position_qty: float = 0.0
    avg_price: float = 0.0
    cumulative_fees: float = 0.0


@dataclass(frozen=True)
class PaperFill:
    oid: int
    strategy_id: str
    client_order_id: str
    instrument_id: str
    side: str
    fill_px: float
    fill_sz: float
    fee: float
    realized_pnl: float
    balance: float
    position_qty: float
    avg_price: float
    ts_event_ns: int
    trade_id: str

    @property
    def fill_payload(self) -> dict:
        return {
            "oid": self.oid,
            "client_order_id": self.client_order_id,
            "fill_px": self.fill_px,
            "fill_sz": self.fill_sz,
            "fee": self.fee,
            "hash": self.trade_id,
            "dir": f"{'Buy' if self.side.upper() == 'BUY' else 'Sell'} Open",
            "ts_event_ns": self.ts_event_ns,
            "strategy_id": self.strategy_id,
        }


class PaperExecutionEngine:
    """Immediate-fill paper execution for orchestrator-managed strategies."""

    def __init__(
        self,
        persistence: PersistenceStore,
        risk_manager: GlobalRiskManager,
        zmq_fills_pub: "zmq.asyncio.Socket",
    ) -> None:
        self._persistence = persistence
        self._risk_manager = risk_manager
        self._zmq_fills_pub = zmq_fills_pub
        self._accounts: dict[str, PaperAccount] = {}
        self._mark_price_provider: Callable[[str], float | None] | None = None

    def set_mark_price_provider(self, provider: Callable[[str], float | None]) -> None:
        """Set a best-effort live mark provider used for paper account MTM."""
        self._mark_price_provider = provider

    async def restore_from_db(self) -> None:
        rows = await self._persistence.load_paper_accounts()
        for strategy_id, row in rows.items():
            account = PaperAccount(**row)
            self._accounts[strategy_id] = account
            await self._risk_manager.set_notional(strategy_id, abs(account.position_qty * account.avg_price))
            self._update_metrics(strategy_id, account, instrument_id="")
        await self._restore_paper_metrics()
        log.info("PaperExecutionEngine restored %d accounts from DB", len(rows))

    def ensure_account(self, strategy_id: str, initial_balance: float) -> PaperAccount:
        account = self._accounts.get(strategy_id)
        if account is None:
            account = PaperAccount(
                initial_balance=initial_balance,
                balance=initial_balance,
            )
            self._accounts[strategy_id] = account
            self._save_account(strategy_id, account)
            self._update_metrics(strategy_id, account, instrument_id="")
        return account

    def order_reduces_position(self, strategy_id: str, is_buy: bool, qty: float) -> bool:
        account = self._accounts.get(strategy_id)
        if account is None or account.position_qty == 0.0:
            return False
        signed_qty = qty if is_buy else -qty
        return (account.position_qty > 0) != (signed_qty > 0)

    def account_state(
        self,
        strategy_id: str,
        initial_balance: float,
        instrument_id: str = "BTC-USD.HYPERLIQUID",
    ) -> dict:
        account = self.ensure_account(strategy_id, initial_balance)
        view = self._account_view(account, instrument_id)
        self._update_metrics(strategy_id, account, instrument_id=instrument_id)
        return {
            "marginSummary": {
                "accountValue": str(view["equity"]),
                "totalNtlPos": str(view["notional"]),
            },
            "assetPositions": [
                {
                    "position": {
                        "coin": instrument_id.split("-")[0],
                        "szi": str(account.position_qty),
                        "entryPx": str(account.avg_price),
                        "returnOnEquity": "0",
                        "unrealizedPnl": str(view["unrealized_pnl"]),
                    }
                }
            ] if account.position_qty else [],
            "paper": {
                "initial_balance": account.initial_balance,
                "balance": account.balance,
                "equity": view["equity"],
                "realized_pnl": account.realized_pnl,
                "unrealized_pnl": view["unrealized_pnl"],
                "position_qty": account.position_qty,
                "avg_price": account.avg_price,
                "mark_price": view["mark_price"],
                "cumulative_fees": account.cumulative_fees,
            },
        }

    def clear_position(self, strategy_id: str, initial_balance: float) -> PaperAccount:
        """Clear stale paper exposure while preserving realized PnL and fees."""
        account = self.ensure_account(strategy_id, initial_balance)
        account.position_qty = 0.0
        account.avg_price = 0.0
        self._save_account(strategy_id, account)
        self._update_metrics(strategy_id, account, instrument_id="")
        return account

    def reset_account(self, strategy_id: str, initial_balance: float) -> PaperAccount:
        """Reset a paper account to its configured starting balance."""
        account = PaperAccount(
            initial_balance=initial_balance,
            balance=initial_balance,
            realized_pnl=0.0,
            position_qty=0.0,
            avg_price=0.0,
            cumulative_fees=0.0,
        )
        self._accounts[strategy_id] = account
        self._save_account(strategy_id, account)
        self._update_metrics(strategy_id, account, instrument_id="")
        return account

    async def execute_order(
        self,
        *,
        oid: int,
        strategy_id: str,
        client_order_id: str,
        instrument_id: str,
        side: str,
        qty: float,
        fill_px: float,
        initial_balance: float,
    ) -> PaperFill:
        account = self.ensure_account(strategy_id, initial_balance)
        is_buy = side.upper() == "BUY"
        realized = self._apply_fill(account, is_buy=is_buy, fill_qty=qty, fill_px=fill_px)
        fee = fill_px * qty * PAPER_TAKER_FEE
        account.cumulative_fees += fee
        account.realized_pnl += realized
        account.balance = account.initial_balance + account.realized_pnl - account.cumulative_fees

        ts_event_ns = time.time_ns()
        trade_id = f"PAPER-{uuid.uuid4().hex[:16].upper()}"
        paper_fill = PaperFill(
            oid=oid,
            strategy_id=strategy_id,
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            side=side.upper(),
            fill_px=fill_px,
            fill_sz=qty,
            fee=fee,
            realized_pnl=realized,
            balance=account.balance,
            position_qty=account.position_qty,
            avg_price=account.avg_price,
            ts_event_ns=ts_event_ns,
            trade_id=trade_id,
        )

        self._persistence.save_paper_fill(
            oid=oid,
            strategy_id=strategy_id,
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            side=side.upper(),
            fill_px=fill_px,
            fill_sz=qty,
            fee=fee,
            realized_pnl=realized,
            balance=account.balance,
            position_qty=account.position_qty,
            avg_price=account.avg_price,
            ts_event_ns=ts_event_ns,
        )
        self._persistence.save_fill(
            oid=oid,
            strategy_id=strategy_id,
            fill_px=fill_px,
            fill_sz=qty,
            fee=fee,
            hash_=trade_id,
            notional_delta=abs(account.position_qty * fill_px),
            ts_event_ns=ts_event_ns,
        )
        self._persistence.mark_order_filled(client_order_id)
        self._save_account(strategy_id, account)
        self._update_metrics(strategy_id, account, instrument_id=instrument_id)

        await self._risk_manager.set_notional(strategy_id, abs(account.position_qty * fill_px))
        metrics.fills_total.labels(strategy=strategy_id, side="buy" if is_buy else "sell").inc()
        metrics.commissions_paid.labels(strategy=strategy_id, currency="USDC").inc(fee)

        topic, payload = wrap_fill(strategy_id, paper_fill.fill_payload)
        await self._zmq_fills_pub.send_multipart([topic, payload])
        log.info(
            "[PAPER] Fill dispatched: %s %s qty=%s px=%s fee=%.4f balance=%.2f pos=%s",
            strategy_id,
            side.upper(),
            qty,
            fill_px,
            fee,
            account.balance,
            account.position_qty,
        )
        return paper_fill

    def _update_metrics(self, strategy_id: str, account: PaperAccount, instrument_id: str) -> None:
        from hl_engine.orchestrator.app import update_strategy_account_metrics

        view = self._account_view(account, instrument_id)
        update_strategy_account_metrics(
            strategy_id,
            currency="USDC",
            instrument=instrument_id,
            equity=view["equity"],
            balance=account.balance,
            realized_pnl=account.realized_pnl,
            unrealized_pnl=view["unrealized_pnl"],
            net_exposure_qty=account.position_qty if instrument_id else None,
        )

    def _account_view(self, account: PaperAccount, instrument_id: str) -> dict:
        mark_price = self._mark_price(instrument_id)
        if mark_price is None or account.position_qty == 0.0:
            mark_price = account.avg_price if account.position_qty else None
        unrealized = self._unrealized_pnl(account, mark_price)
        notional_price = mark_price or account.avg_price
        return {
            "mark_price": mark_price,
            "unrealized_pnl": unrealized,
            "equity": account.balance + unrealized,
            "notional": abs(account.position_qty * notional_price),
        }

    def _mark_price(self, instrument_id: str) -> float | None:
        if not instrument_id or self._mark_price_provider is None:
            return None
        try:
            mark = self._mark_price_provider(instrument_id)
        except Exception:
            log.exception("Paper mark price provider failed for %s", instrument_id)
            return None
        return float(mark) if mark and mark > 0.0 else None

    @staticmethod
    def _unrealized_pnl(account: PaperAccount, mark_price: float | None) -> float:
        if mark_price is None or account.position_qty == 0.0 or account.avg_price <= 0.0:
            return 0.0
        return account.position_qty * (mark_price - account.avg_price)

    async def _restore_paper_metrics(self) -> None:
        aggregates = await self._persistence.load_paper_fill_aggregates()
        for strategy_id, aggregate in aggregates.items():
            for side, count in aggregate["fills"].items():
                metrics.fills_total.labels(strategy=strategy_id, side=side).inc(count)
            fees = float(aggregate["fees"])
            if fees:
                metrics.commissions_paid.labels(strategy=strategy_id, currency="USDC").inc(fees)

    def _save_account(self, strategy_id: str, account: PaperAccount) -> None:
        self._persistence.save_paper_account(
            strategy_id=strategy_id,
            initial_balance=account.initial_balance,
            balance=account.balance,
            realized_pnl=account.realized_pnl,
            position_qty=account.position_qty,
            avg_price=account.avg_price,
            cumulative_fees=account.cumulative_fees,
        )

    @staticmethod
    def _apply_fill(account: PaperAccount, *, is_buy: bool, fill_qty: float, fill_px: float) -> float:
        realized = 0.0
        signed_qty = fill_qty if is_buy else -fill_qty

        if account.position_qty == 0.0:
            account.position_qty = signed_qty
            account.avg_price = fill_px
        elif (account.position_qty > 0) == is_buy:
            new_qty = account.position_qty + signed_qty
            account.avg_price = (
                (account.avg_price * abs(account.position_qty) + fill_px * fill_qty)
                / abs(new_qty)
            )
            account.position_qty = new_qty
        else:
            close_qty = min(fill_qty, abs(account.position_qty))
            direction = 1.0 if account.position_qty > 0 else -1.0
            realized = direction * (fill_px - account.avg_price) * close_qty
            remaining = fill_qty - close_qty
            account.position_qty += signed_qty
            if abs(account.position_qty) < 1e-10:
                account.position_qty = 0.0
                account.avg_price = 0.0
            elif remaining > 1e-10:
                account.avg_price = fill_px

        return realized
