"""Position model used by the Position Stage Manager (AGG-2.2) and the Risk Guardian.

R conventions (docs/assumptions.md):
* unit_risk        = |probe entry - initial stop| in price terms (the trade's 1R per unit).
* "+xR in favour"  = favourable price move from the average entry of x * unit_risk.
* open profit in R = unrealised Rs / planned-risk Rs (used for the press trigger and 2R lock).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sthitaprajna.domain import Direction, Gear, Grade, Instrument, Leg, PositionStage, Segment


@dataclass
class ClosedLeg:
    qty: int
    price: float
    ts: dt.datetime
    reason: str
    pnl: float


@dataclass
class Position:
    id: str
    candidate_id: str
    strategy: str
    instrument: Instrument
    underlying_key: str
    direction: Direction
    segment: Segment
    grade: Grade
    gear_at_entry: Gear
    mode: str
    planned_risk: float
    unit_risk: float
    initial_stop: float
    stop: float
    broker_stop: float
    staging_mode: str               # full | probe_only | single
    full_qty_target: int
    confirm_trigger: float
    confirm_deadline: dt.datetime
    opened_at: dt.datetime
    legs: list[Leg] = field(default_factory=list)
    closed_legs: list[ClosedLeg] = field(default_factory=list)
    stage: PositionStage = PositionStage.PROBE
    presses: int = 0
    harvested: bool = False
    charges: float = 0.0
    broker_stop_id: str = ""
    closed_at: dt.datetime | None = None
    exit_reason: str = ""
    last_price: float = 0.0
    mfe_price: float = 0.0          # best favourable price seen
    mae_price: float = 0.0          # worst adverse price seen
    setup_id: str = ""
    signal_bar: str = ""
    is_reentry: bool = False
    sleeve: bool = False
    defined_risk: bool = False
    trail_atr: float = 0.0          # ATR snapshot for trailing (price terms)
    stage_history: list[dict[str, Any]] = field(default_factory=list)
    stop_history: list[dict[str, Any]] = field(default_factory=list)   # D-058: {ts, stop, reason} on every
                                                                       # stop tightening -- kept separate
                                                                       # from stage_history, whose entries
                                                                       # are all {"to": <PositionStage>}
                                                                       # transitions read that way elsewhere
                                                                       # (e.g. _close_position's "stages")
    manual_exit: dict[str, Any] | None = None   # counterfactual tracking (D6)
    trigger_ts: dt.datetime | None = None
    beta: float = 1.0
    bias: Direction | None = None
    target: float = 0.0             # unbounded mode: full exit at this price (0 = none)

    @property
    def exposure(self) -> Direction:
        return self.bias or self.direction

    # ------------------------------------------------------------------ quantities
    @property
    def opened_qty(self) -> int:
        return sum(leg.qty for leg in self.legs)

    @property
    def closed_qty(self) -> int:
        return sum(c.qty for c in self.closed_legs)

    @property
    def qty(self) -> int:
        return self.opened_qty - self.closed_qty

    @property
    def is_open(self) -> bool:
        return self.qty > 0 and self.closed_at is None

    @property
    def avg_entry(self) -> float:
        q = self.opened_qty
        return sum(leg.qty * leg.price for leg in self.legs) / q if q else 0.0

    @property
    def sign(self) -> int:
        return self.direction.sign

    # ------------------------------------------------------------------ P&L and risk
    def unrealized(self, price: float | None = None) -> float:
        p = self.last_price if price is None else price
        return self.sign * (p - self.avg_entry) * self.qty

    @property
    def realized(self) -> float:
        return sum(c.pnl for c in self.closed_legs)

    def net_pnl(self, price: float | None = None) -> float:
        return self.realized + self.unrealized(price) - self.charges

    def worst_case_at_stop(self, stop: float | None = None, qty: int | None = None,
                           avg: float | None = None) -> float:
        """Loss in Rs if the stop fills exactly (positive number = loss). Negative = locked profit."""
        s = self.stop if stop is None else stop
        q = self.qty if qty is None else qty
        a = self.avg_entry if avg is None else avg
        return self.sign * (a - s) * q

    @property
    def open_risk(self) -> float:
        """Contribution to portfolio open risk: never negative (locked profit frees budget)."""
        return max(0.0, self.worst_case_at_stop())

    def move_r(self, price: float | None = None) -> float:
        """Favourable move from the average entry, in units of unit_risk."""
        p = self.last_price if price is None else price
        if self.unit_risk <= 0:
            return 0.0
        return self.sign * (p - self.avg_entry) / self.unit_risk

    def open_profit_r(self, price: float | None = None) -> float:
        if self.planned_risk <= 0:
            return 0.0
        return self.unrealized(price) / self.planned_risk

    def r_multiple(self, price: float | None = None) -> float:
        """Net result so far in R of planned risk (for journal/KPIs)."""
        return self.net_pnl(price) / self.planned_risk if self.planned_risk > 0 else 0.0

    def notional(self, price: float | None = None) -> float:
        p = self.last_price if price is None else price
        return abs(p * self.qty)

    def mark(self, price: float) -> None:
        self.last_price = price
        if not self.mfe_price:
            self.mfe_price = self.mae_price = price
        if self.sign * (price - self.mfe_price) > 0:
            self.mfe_price = price
        if self.sign * (price - self.mae_price) < 0:
            self.mae_price = price

    def mfe_r(self) -> float:
        return self.move_r(self.mfe_price) if self.mfe_price else 0.0

    def mae_r(self) -> float:
        return self.move_r(self.mae_price) if self.mae_price else 0.0

    def stop_is_better(self, new_stop: float) -> bool:
        """Stops only move toward profit (A10.12, code-enforced). Compared at the same 2-decimal (paisa)
        precision `p.stop` is always stored at (D-058): a candidate rounds to the same displayed stop as
        the current one is not an improvement, it's rounding noise. Comparing the raw unrounded candidate
        here let reversal_lock()/trailing_stop() re-fire the identical stop value on every tick for as
        long as the underlying evidence (candle pattern, OI shift) stayed in place, producing a storm of
        near-duplicate "stop tightened" log lines and broker-stop syncs with no actual change in the stop."""
        return self.sign * (round(new_stop, 2) - self.stop) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "candidate_id": self.candidate_id, "strategy": self.strategy,
            "instrument": self.instrument.to_dict(), "symbol": self.instrument.symbol,
            "underlying": self.underlying_key, "direction": self.direction.value,
            "exposure": self.exposure.value, "segment": self.segment.value,
            "grade": self.grade.value, "gear_at_entry": self.gear_at_entry.value, "mode": self.mode,
            "planned_risk": round(self.planned_risk, 2), "unit_risk": round(self.unit_risk, 4),
            "initial_stop": round(self.initial_stop, 2), "stop": round(self.stop, 2),
            "broker_stop": round(self.broker_stop, 2), "broker_stop_id": self.broker_stop_id,
            "staging_mode": self.staging_mode, "full_qty_target": self.full_qty_target,
            "stage": self.stage.value, "presses": self.presses, "harvested": self.harvested,
            "qty": self.qty, "opened_qty": self.opened_qty, "avg_entry": round(self.avg_entry, 2),
            "last_price": round(self.last_price, 2), "unrealized": round(self.unrealized(), 2),
            "realized": round(self.realized, 2), "charges": round(self.charges, 2),
            "net_pnl": round(self.net_pnl(), 2), "r_multiple": round(self.r_multiple(), 3),
            "move_r": round(self.move_r(), 3), "open_risk": round(self.open_risk, 2),
            "mfe_r": round(self.mfe_r(), 3), "mae_r": round(self.mae_r(), 3),
            "opened_at": self.opened_at.isoformat(), "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "exit_reason": self.exit_reason, "is_reentry": self.is_reentry, "sleeve": self.sleeve,
            "confirm_trigger": round(self.confirm_trigger, 2),
            "confirm_deadline": self.confirm_deadline.isoformat(),
            "legs": [{"qty": leg.qty, "price": round(leg.price, 2), "ts": leg.ts.isoformat(), "stage": leg.stage.value}
                     for leg in self.legs],
            "closed_legs": [{"qty": c.qty, "price": round(c.price, 2), "ts": c.ts.isoformat(), "reason": c.reason,
                             "pnl": round(c.pnl, 2)} for c in self.closed_legs],
            "stage_history": self.stage_history, "stop_history": self.stop_history, "manual_exit": self.manual_exit,
            "target": round(self.target, 2) if self.target else None,
        }
