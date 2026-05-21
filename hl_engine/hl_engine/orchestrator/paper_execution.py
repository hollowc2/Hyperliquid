"""
Orchestrator-side paper execution.

This is used when HL_PAPER_TRADE=true or no private key is configured. It
creates immediate synthetic taker fills from the orchestrator's live top of book
and persists paper account state so strategy restarts can reconcile normally.
"""

import logging
import time
import uuid
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

    async def restore_from_db(self) -> None:
        rows = await self._persistence.load_paper_accounts()
        for strategy_id, row in rows.items():
            account = PaperAccount(**row)
            self._accounts[strategy_id] = account
            await self._risk_manager.set_notional(strategy_id, abs(account.position_qty * account.avg_price))
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
        return account

    def order_reduces_position(self, strategy_id: str, is_buy: bool, qty: float) -> bool:
        account = self._accounts.get(strategy_id)
        if account is None or account.position_qty == 0.0:
            return False
        signed_qty = qty if is_buy else -qty
        return (account.position_qty > 0) != (signed_qty > 0)

    def account_state(self, strategy_id: str, initial_balance: float) -> dict:
        account = self.ensure_account(strategy_id, initial_balance)
        return {
            "marginSummary": {
                "accountValue": str(account.balance),
                "totalNtlPos": str(abs(account.position_qty * account.avg_price)),
            },
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": str(account.position_qty),
                        "entryPx": str(account.avg_price),
                        "returnOnEquity": "0",
                        "unrealizedPnl": "0",
                    }
                }
            ] if account.position_qty else [],
            "paper": {
                "initial_balance": account.initial_balance,
                "balance": account.balance,
                "realized_pnl": account.realized_pnl,
                "position_qty": account.position_qty,
                "avg_price": account.avg_price,
                "cumulative_fees": account.cumulative_fees,
            },
        }

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

        await self._risk_manager.set_notional(strategy_id, abs(account.position_qty * fill_px))
        metrics.fills_total.labels(strategy=strategy_id, side="buy" if is_buy else "sell").inc()

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
