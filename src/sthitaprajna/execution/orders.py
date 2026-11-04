"""Order model, state machine, Algo-ID tagging, OPS limiter and freeze-quantity slicing (A11).

Idempotent client order ids; every order is Algo-ID tagged; the OPS limiter keeps the system far below
SEBI's 10 orders/second; exits jump the queue (a stop is never delayed behind an entry).
"""
from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sthitaprajna.domain import Instrument, Segment, Side, new_id
from sthitaprajna.risk import ceilings


class OrderStatus(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"              # resting (e.g. broker-side SL)
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
ALLOWED = {
    OrderStatus.NEW: {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.REJECTED,
                            OrderStatus.CANCELLED},
    OrderStatus.OPEN: {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.OPEN},
    OrderStatus.PARTIAL: {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED},
}


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL_M = "SL-M"


EXIT_PURPOSES = {"EXIT", "HARVEST", "STOP", "BROKER_STOP", "KILL", "MANUAL_CLOSE", "MANUAL_REDUCE", "EOD",
                 "TIME_STOP", "FLATTEN", "FILL_OUTSIDE_PLAN", "CONFIRM_TIMEOUT_SCRATCH", "TARGET", "EXTERNAL"}


@dataclass
class Order:
    id: str
    position_id: str
    instrument: Instrument
    segment: Segment
    side: Side
    qty: int
    type: OrderType
    purpose: str                    # PROBE | CONFIRM_ADD | PRESS | HARVEST | EXIT | BROKER_STOP | ...
    created: dt.datetime
    price: float = 0.0
    trigger: float = 0.0
    status: OrderStatus = OrderStatus.NEW
    filled_qty: int = 0
    avg_price: float = 0.0
    updated: dt.datetime | None = None
    algo_id: str = ""
    correlation_id: str = ""
    reject_reason: str = ""
    model_price: float = 0.0        # price the model expected (for slippage measurement)
    charges: float = 0.0
    parent_id: str | None = None    # set on freeze-qty slices
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_exit(self) -> bool:
        return self.purpose in EXIT_PURPOSES

    @property
    def slippage_bps(self) -> float | None:
        if not self.filled_qty or not self.model_price:
            return None
        sgn = 1 if self.side is Side.BUY else -1
        return sgn * (self.avg_price - self.model_price) / self.model_price * 1e4

    def transition(self, new: OrderStatus, now: dt.datetime, note: str = "") -> None:
        if self.status in TERMINAL:
            raise ValueError(f"order {self.id} already terminal ({self.status.value})")
        if new not in ALLOWED.get(self.status, set()):
            raise ValueError(f"illegal transition {self.status.value} -> {new.value}")
        self.history.append({"ts": now.isoformat(), "from": self.status.value, "to": new.value, "note": note})
        self.status = new
        self.updated = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "position_id": self.position_id, "symbol": self.instrument.symbol,
            "instrument": self.instrument.key, "segment": self.segment.value, "side": self.side.value,
            "qty": self.qty, "type": self.type.value, "purpose": self.purpose, "price": round(self.price, 2),
            "trigger": round(self.trigger, 2), "status": self.status.value, "filled_qty": self.filled_qty,
            "avg_price": round(self.avg_price, 2), "created": self.created.isoformat(),
            "updated": self.updated.isoformat() if self.updated else None, "algo_id": self.algo_id,
            "correlation_id": self.correlation_id, "reject_reason": self.reject_reason,
            "slippage_bps": round(self.slippage_bps, 2) if self.slippage_bps is not None else None,
            "charges": round(self.charges, 2), "parent_id": self.parent_id, "history": self.history[-6:],
        }


def build_correlation_id(client_order_id: str, algo_id: str) -> str:
    """Ported from Kubera compliance/algo_id.py: Dhan's correlationId has historically been length-limited."""
    return f"{algo_id}:{client_order_id}"[:25]


def tag_order_payload(payload: dict, client_order_id: str, algo_id: str) -> dict:
    """Algo-ID tagging (A4 SEBI framework). Stamps correlationId (documented) and a best-effort algoId
    key. [VERIFY with Dhan] which field carries the exchange Algo-ID for a self-developed personal algo."""
    tagged = dict(payload)
    tagged["correlationId"] = build_correlation_id(client_order_id, algo_id)
    tagged["algoId"] = algo_id
    return tagged


def slice_for_freeze(qty: int, lot: int, freeze_qty: int | None) -> list[int]:
    """Split an order above the exchange freeze quantity into lot-aligned slices (A11)."""
    if not freeze_qty or qty <= freeze_qty:
        return [qty]
    max_slice = max(lot, (freeze_qty // lot) * lot)
    out = []
    left = qty
    while left > 0:
        s = min(left, max_slice)
        out.append(s)
        left -= s
    return out


class OpsLimiter:
    """Sliding one-second window. Cap is validated < SEBI's threshold at config load."""

    def __init__(self, max_per_second: int) -> None:
        if max_per_second >= ceilings.SEBI_OPS_THRESHOLD:
            raise ValueError("OPS cap must stay below SEBI's threshold")
        self.max = max_per_second
        self.sent: deque[float] = deque()
        self.peak = 0

    def allow(self, t: float) -> bool:
        while self.sent and t - self.sent[0] >= 1.0:
            self.sent.popleft()
        if len(self.sent) < self.max:
            self.sent.append(t)
            self.peak = max(self.peak, len(self.sent))
            return True
        return False


def new_order(position_id: str, inst: Instrument, segment: Segment, side: Side, qty: int, otype: OrderType,
              purpose: str, now: dt.datetime, algo_id: str, price: float = 0.0, trigger: float = 0.0,
              model_price: float = 0.0) -> Order:
    oid = new_id("ord")
    return Order(oid, position_id, inst, segment, side, qty, otype, purpose, now, price=price, trigger=trigger,
                 algo_id=algo_id, correlation_id=build_correlation_id(oid, algo_id), model_price=model_price)
