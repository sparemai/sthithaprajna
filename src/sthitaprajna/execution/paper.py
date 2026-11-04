"""Paper broker: identical interface to the live broker (A5.4 -- mode flag only).

* MARKET buys fill at the ask, sells at the bid, on the NEXT quote after submission (next-tick fills).
* Broker-side protective stops (A5.5 rule 4) rest as SL-M orders slightly beyond the system stop and
  trigger on the traded price; a gap through the trigger fills at the (worse) market -- as in reality.
* Failure injection: order API down -> submissions rejected (exits retry); reject storm -> rejects.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from sthitaprajna.domain import Quote, Side
from sthitaprajna.execution.orders import Order, OrderStatus, OrderType
from sthitaprajna.market.charges import ChargesModel


@dataclass
class Fill:
    order: Order
    qty: int
    price: float
    ts: dt.datetime
    charges: float


@dataclass
class PaperBroker:
    charges: ChargesModel
    api_down: Callable[[], bool] = lambda: False
    reject_storm: Callable[[], bool] = lambda: False
    pending: dict[str, Order] = field(default_factory=dict)
    resting: dict[str, Order] = field(default_factory=dict)      # broker-side SL orders
    rejects: list[dt.datetime] = field(default_factory=list)

    def submit(self, order: Order, now: dt.datetime) -> bool:
        if self.api_down():
            order.transition(OrderStatus.REJECTED, now, "order API unreachable")
            order.reject_reason = "order API unreachable"
            self.rejects.append(now)
            return False
        if self.reject_storm() and not order.is_exit:
            order.transition(OrderStatus.REJECTED, now, "RMS reject (injected)")
            order.reject_reason = "RMS reject (injected)"
            self.rejects.append(now)
            return False
        order.transition(OrderStatus.SUBMITTED, now)
        if order.type is OrderType.SL_M:
            order.transition(OrderStatus.OPEN, now, f"resting stop @ {order.trigger:.2f}")
            self.resting[order.id] = order
        else:
            self.pending[order.id] = order
        return True

    def modify_stop(self, order_id: str, trigger: float, now: dt.datetime) -> bool:
        o = self.resting.get(order_id)
        if o is None or self.api_down():
            return False
        o.trigger = trigger
        o.history.append({"ts": now.isoformat(), "from": "OPEN", "to": "OPEN", "note": f"trigger -> {trigger:.2f}"})
        return True

    def modify_qty(self, order_id: str, qty: int, now: dt.datetime) -> None:
        o = self.resting.get(order_id)
        if o is not None:
            o.qty = qty
            o.history.append({"ts": now.isoformat(), "from": "OPEN", "to": "OPEN", "note": f"qty -> {qty}"})

    def cancel(self, order_id: str, now: dt.datetime) -> None:
        o = self.pending.pop(order_id, None) or self.resting.pop(order_id, None)
        if o is not None and o.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            o.transition(OrderStatus.CANCELLED, now)

    def process(self, quotes: dict[str, Quote], now: dt.datetime) -> list[Fill]:
        fills: list[Fill] = []
        for oid, o in list(self.pending.items()):
            q = quotes.get(o.instrument.key)
            if q is None or q.ltp <= 0 or q.exchange_ts <= o.created and q.receive_ts <= o.created:
                continue   # wait for the next tick after submission
            px = self._market_price(o, q)
            if o.type is OrderType.LIMIT and ((o.side is Side.BUY and px > o.price) or (o.side is Side.SELL and px < o.price)):
                continue
            fills.append(self._fill(o, px, now))
            del self.pending[oid]
        for oid, o in list(self.resting.items()):
            q = quotes.get(o.instrument.key)
            if q is None or q.ltp <= 0:
                continue
            hit = (q.ltp <= o.trigger) if o.side is Side.SELL else (q.ltp >= o.trigger)
            if hit:
                fills.append(self._fill(o, self._market_price(o, q), now, note="broker-side stop triggered"))
                del self.resting[oid]
        return fills

    @staticmethod
    def _market_price(o: Order, q: Quote) -> float:
        if o.side is Side.BUY:
            return q.ask if q.ask > 0 else q.ltp
        return q.bid if q.bid > 0 else q.ltp

    def _fill(self, o: Order, px: float, now: dt.datetime, note: str = "") -> Fill:
        ch = self.charges.compute(o.segment, o.side, px, o.qty, o.instrument.exchange).total
        o.filled_qty = o.qty
        o.avg_price = px
        o.charges = ch
        o.transition(OrderStatus.FILLED, now, note)
        return Fill(o, o.qty, px, now, ch)

    def reject_count(self, now: dt.datetime, window_s: float = 60.0) -> int:
        self.rejects = [t for t in self.rejects if (now - t).total_seconds() <= window_s]
        return len(self.rejects)
