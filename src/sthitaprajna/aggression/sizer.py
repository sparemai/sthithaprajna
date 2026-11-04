"""Conviction Sizer (AGG-1 component 2; A10.4; D2 lot granularity).

planned risk = gear R x grade multiplier, capped by the 3.0% hard ceiling and the Edge Brake.
Adjustments (volatility, spread, correlation, uncertainty) can only DECREASE size.
Derivatives cannot go below one lot: if one lot exceeds planned risk the trade is SKIPPED, never forced.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from sthitaprajna.config import OperatorConfig, TierSpec
from sthitaprajna.domain import Gear, Grade, Segment
from sthitaprajna.risk import ceilings


def gear_r(cfg: OperatorConfig, tier: TierSpec, gear: Gear, edge_ceiling: float | None) -> float:
    r = cfg.gears[gear].r * cfg.tier_factor(tier)
    if edge_ceiling is not None:
        r = min(r, edge_ceiling)          # the brake only caps
    return max(0.0, r)


def grade_multiplier(cfg: OperatorConfig, gear: Gear, grade: Grade, drawdown: float) -> float:
    if grade is Grade.C or grade not in cfg.gears[gear].grades:
        return 0.0
    if grade is Grade.A_PLUS and (gear is not Gear.ATTACK or drawdown >= cfg.a_plus_max_dd):
        return cfg.grades[Grade.A].multiplier   # A+ multiplier only in ATTACK while dd < 5%
    return cfg.grades[grade].multiplier


def planned_risk_fraction(cfg: OperatorConfig, tier: TierSpec, gear: Gear, grade: Grade, drawdown: float,
                          edge_ceiling: float | None) -> float:
    return ceilings.clamp_risk_fraction(gear_r(cfg, tier, gear, edge_ceiling)
                                        * grade_multiplier(cfg, gear, grade, drawdown))


@dataclass
class SizingDecision:
    planned_risk: float                 # Rs budget for the whole position (after decreases)
    planned_risk_fraction: float
    base_planned_risk: float            # before adjustments
    adjustments: dict[str, float] = field(default_factory=dict)
    lot_size: int = 1
    per_unit_risk: float = 0.0          # stop distance + per-unit costs/slippage
    full_qty: int = 0
    probe_qty: int = 0
    staging_mode: str = "full"          # full | probe_only | single
    risk_at_full: float = 0.0           # Rs lost if stopped at full size
    skip_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.skip_reason is None and self.probe_qty > 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def size_position(*, cfg: OperatorConfig, equity: float, planned_fraction: float, grade: Grade,
                  segment: Segment, entry: float, stop: float, lot_size: int, per_unit_cost: float,
                  adjustments: dict[str, float] | None = None, probe_only: bool = False) -> SizingDecision:
    base = planned_fraction * equity
    adj = {k: min(1.0, max(0.0, float(v))) for k, v in (adjustments or {}).items()}
    factor = math.prod(adj.values()) if adj else 1.0
    pr = base * factor
    d = SizingDecision(pr, pr / equity if equity > 0 else 0.0, base, adj, lot_size)
    unit = abs(entry - stop)
    if unit <= 0 or entry <= 0:
        d.skip_reason = "invalid stop: must be on the loss side of entry"
        return d
    if pr <= 0:
        d.skip_reason = "zero planned risk in this gear/grade"
        return d
    d.per_unit_risk = unit + max(0.0, per_unit_cost)
    per_lot = d.per_unit_risk * lot_size
    lots = int(pr // per_lot)
    if lots < 1:
        d.skip_reason = (f"one lot risks Rs {per_lot:,.0f} > planned Rs {pr:,.0f} -- skipped, never forced")
        return d

    if segment is Segment.EQ_INTRADAY:
        cap_units = int(equity * float(cfg.limits["cash_intraday_single_notional_x"]) // entry)
        lots = min(lots, cap_units // lot_size)
    elif segment is Segment.EQ_DELIVERY:
        cap_units = int(equity * float(cfg.limits["cash_delivery_single"]) // entry)
        lots = min(lots, cap_units // lot_size)
    if lots < 1:
        d.skip_reason = "leverage cap leaves less than one unit"
        return d

    probe_frac = cfg.staging["probe_fraction"]
    staging = cfg.grades[grade].staging if grade in cfg.grades else "probe_only"
    probe_lots = max(1, int(lots * probe_frac))
    if probe_only or staging == "probe_only":
        d.staging_mode = "probe_only"
        lots = probe_lots          # B grade: the probe IS the position
    elif lots == 1:
        d.staging_mode = "single"  # only one lot fits: probe is the full position, P2 skipped
        probe_lots = 1
    d.full_qty = lots * lot_size
    d.probe_qty = probe_lots * lot_size
    d.risk_at_full = d.full_qty * d.per_unit_risk
    return d


def size_by_capital(*, capital: float, equity: float, segment: Segment, entry: float, stop: float, lot_size: int,
                    per_unit_cost: float, leverage: float = 1.0) -> SizingDecision:
    """Unbounded mode (D-031): quantity from the capital given to the trade, entered in full at once.

    Options: whole lots of premium the capital can buy. Cash intraday: capital x leverage of stock.
    The only skip is physical: when one lot (or one share) costs more than the capital available.
    """
    d = SizingDecision(0.0, 0.0, 0.0, {}, lot_size, staging_mode="single")
    unit = abs(entry - stop)
    if unit <= 0 or entry <= 0:
        d.skip_reason = "invalid stop: must be on the loss side of entry"
        return d
    if capital <= 0:
        d.skip_reason = "no free capital (all of it is in open trades)"
        return d
    power = capital * (max(1.0, leverage) if segment is Segment.EQ_INTRADAY else 1.0)
    per_lot = entry * lot_size
    lots = int(power // per_lot)
    if lots < 1:
        d.skip_reason = f"one lot costs Rs {per_lot:,.0f} but Rs {power:,.0f} is available"
        return d
    d.per_unit_risk = unit + max(0.0, per_unit_cost)
    d.full_qty = d.probe_qty = lots * lot_size
    d.risk_at_full = d.full_qty * d.per_unit_risk
    d.planned_risk = d.base_planned_risk = d.risk_at_full           # informational: Rs lost if the stop fills
    d.planned_risk_fraction = d.planned_risk / equity if equity > 0 else 0.0
    return d
