"""Staging Engine + Winner Maximiser rules (AGG-2.2, D12.6). Pure functions -- no I/O.

P1 PROBE        50% of planned risk (or 1 lot if that is the smallest fit)
P2 CONFIRM-ADD  probe >= +0.5R in favour AND a follow-through confirmation inside the window.
                Combined worst-case loss <= planned risk (tighten the stop if needed).
P3 PRESS        <= 2 adds, each <= 50% of full size; open profit >= 1R; grade still >= A.
                Until open profit reaches 2R: whole-position worst case <= original planned risk.
                From 2R onward: the stop must lock >= 50% of open profit.
P4 HARVEST      30-50% at +2R; the remainder trails.
Invariant: no stage ever adds to a losing position; stops only move toward profit.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sthitaprajna.domain import Candle, Direction, Grade, PositionStage
from sthitaprajna.market.options import ChainRow
from sthitaprajna.market.patterns import bearish_reversal_candle, bullish_reversal_candle
from sthitaprajna.portfolio.position import Position


@dataclass(frozen=True)
class AddPlan:
    ok: bool
    qty: int = 0
    new_stop: float = 0.0
    new_avg: float = 0.0
    worst_case_after: float = 0.0
    stage: PositionStage | None = None
    rule: str = ""
    reason: str = ""


def _reject(reason: str) -> AddPlan:
    return AddPlan(False, reason=reason)


def min_stop_gap(pos: Position) -> float:
    """A tightened stop must still sit a sensible distance from price on the loss side."""
    return max(2 * pos.instrument.tick_size, 0.25 * pos.unit_risk)


def _round_lots(qty: float, lot: int) -> int:
    return int(qty // lot) * lot


def required_stop_after_add(pos: Position, price: float, add_qty: int, lock_mode: bool,
                            lock_fraction: float) -> tuple[float, float]:
    """Stop that keeps the whole position inside the rule after adding `add_qty` at `price`.
    Returns (stop, new_avg). Only ever tightens relative to the current stop."""
    q, avg, s = pos.qty, pos.avg_entry, pos.sign
    q2 = q + add_qty
    avg2 = (q * avg + add_qty * price) / q2
    if lock_mode:
        open_profit = s * (price - avg) * q  # the new leg has no P&L at the moment it is added
        req = avg2 + s * lock_fraction * open_profit / q2
    else:
        req = avg2 - s * pos.planned_risk / q2
    stop = req if s * (req - pos.stop) > 0 else pos.stop
    return stop, avg2


def _fit_add(pos: Position, price: float, max_add: int, lock_mode: bool, lock_fraction: float,
             stage: PositionStage, rule: str) -> AddPlan:
    lot = pos.instrument.lot_size
    gap = min_stop_gap(pos)
    add = _round_lots(max_add, lot)
    while add >= lot:
        stop, avg2 = required_stop_after_add(pos, price, add, lock_mode, lock_fraction)
        if pos.sign * (price - stop) >= gap:
            wc = pos.worst_case_at_stop(stop=stop, qty=pos.qty + add, avg=avg2)
            return AddPlan(True, add, stop, avg2, wc, stage, rule, "ok")
        add -= lot
    return _reject("no add size keeps the whole position inside the risk rule with a sane stop")


def plan_confirm_add(pos: Position, price: float, confirmation_fired: bool, now: dt.datetime,
                     staging_cfg: dict[str, float]) -> AddPlan:
    if pos.stage is not PositionStage.PROBE or pos.staging_mode != "full":
        return _reject("confirm-add only from a full-staging probe")
    if now > pos.confirm_deadline:
        return _reject("confirm window expired")
    if pos.unrealized(price) <= 0:
        return _reject("never add to a losing position")
    if pos.move_r(price) < staging_cfg["confirm_add_min_r"]:
        return _reject(f"probe at {pos.move_r(price):+.2f}R < +{staging_cfg['confirm_add_min_r']}R")
    if not confirmation_fired:
        return _reject("no follow-through confirmation yet")
    remaining = pos.full_qty_target - pos.qty
    if remaining < pos.instrument.lot_size:
        return _reject("already at full size")
    return _fit_add(pos, price, remaining, lock_mode=False, lock_fraction=0.0,
                    stage=PositionStage.CONFIRM_ADD, rule="worst case <= planned risk")


def plan_press(pos: Position, price: float, grade_now: Grade, trend_ok: bool, gear_allows_pressing: bool,
               staging_cfg: dict[str, float]) -> AddPlan:
    allowed_from = {PositionStage.CONFIRM_ADD, PositionStage.PRESS_1}
    if not (pos.stage in allowed_from or (pos.stage is PositionStage.PROBE and pos.staging_mode == "single")):
        return _reject(f"press not allowed from stage {pos.stage.value}")
    if pos.staging_mode == "probe_only":
        return _reject("probe-only position (grade B) never presses")
    if not gear_allows_pressing:
        return _reject("current gear does not allow pressing")
    if pos.presses >= int(staging_cfg["press_max_adds"]):
        return _reject("maximum press adds reached")
    if pos.unrealized(price) <= 0:
        return _reject("never add to a losing position")
    if grade_now.rank > Grade.A.rank:
        return _reject("trade no longer grades A or better")
    if not trend_ok:
        return _reject("trend strength not confirmed")
    op_r = pos.open_profit_r(price)
    if op_r < staging_cfg["press_min_open_profit_r"]:
        return _reject(f"open profit {op_r:.2f}R < {staging_cfg['press_min_open_profit_r']}R")
    lock_mode = op_r >= staging_cfg["press_lock_from_r"]
    max_add = staging_cfg["press_add_fraction_of_full"] * pos.full_qty_target
    stage = PositionStage.PRESS_1 if pos.presses == 0 else PositionStage.PRESS_2
    rule = "lock >= 50% open profit" if lock_mode else "worst case <= original planned risk"
    return _fit_add(pos, price, max(max_add, pos.instrument.lot_size), lock_mode,
                    staging_cfg["press_lock_fraction"], stage, rule)


def harvest_qty(pos: Position, price: float, staging_cfg: dict[str, float]) -> int:
    if pos.harvested or pos.move_r(price) < staging_cfg["harvest_at_r"]:
        return 0
    lot = pos.instrument.lot_size
    if pos.qty < 2 * lot:
        return 0  # a single lot cannot be partially harvested; it trails instead
    q = _round_lots(staging_cfg["harvest_fraction"] * pos.qty, lot)
    q = max(lot, q)
    return min(q, pos.qty - lot)


def trailing_stop(pos: Position, price: float, staging_cfg: dict[str, float], atr_mult: float = 2.0,
                  is_expiry_day: bool = False) -> float | None:
    """Below +2R (D-042): a breakeven tier at `breakeven_at_r` and a partial-lock tier at `early_lock_at_r`
    protect a position that runs up fast and reverses before the original 2R harvest/lock threshold ever
    fires -- otherwise a single-lot position (which cannot be partially harvested) has no profit protection
    at all until 2R. From +2R: lock >= 50% of open profit; after harvest also trail by ATR.

    `is_expiry_day` (D-045): when the position's own contract expires today, extrinsic value decays on a
    convex curve into the close -- the same open profit is worth locking in sooner, because theta is taking
    a larger bite of it with every passing minute than it would on a normal day. On expiry day the breakeven
    and early-lock tiers fire at `expiry_breakeven_at_r`/`expiry_early_lock_at_r` instead (earlier R
    thresholds; same lock fraction), falling back to the normal tiers if those keys aren't configured.
    Returns a better stop, or None if nothing improves on the current one."""
    s = pos.sign
    best = pos.stop
    op_r = pos.move_r(price)
    if pos.unrealized(price) > 0:
        breakeven_at = staging_cfg.get("expiry_breakeven_at_r" if is_expiry_day else "breakeven_at_r",
                                       staging_cfg.get("breakeven_at_r", 0.0))
        if breakeven_at > 0 and op_r >= breakeven_at and s * (pos.avg_entry - best) > 0:
            best = pos.avg_entry
        early_at = staging_cfg.get("expiry_early_lock_at_r" if is_expiry_day else "early_lock_at_r",
                                   staging_cfg.get("early_lock_at_r", 0.0))
        early_frac = staging_cfg.get("early_lock_fraction", 0.0)
        if early_at > 0 and op_r >= early_at:
            lock = pos.avg_entry + early_frac * (price - pos.avg_entry)
            if s * (lock - best) > 0:
                best = lock
    if op_r >= staging_cfg["press_lock_from_r"] or pos.harvested:
        # halfway between entry and price locks 50% of open profit, for longs and shorts alike
        lock = pos.avg_entry + staging_cfg["press_lock_fraction"] * (price - pos.avg_entry)
        if pos.unrealized(price) > 0 and s * (lock - best) > 0:
            best = lock
    if pos.harvested and pos.trail_atr > 0:
        trail = price - s * atr_mult * pos.trail_atr
        if s * (trail - best) > 0 and s * (price - trail) > 0:
            best = trail
    return best if s * (best - pos.stop) > 0 else None


def reversal_lock(pos: Position, price: float, prev_candle: Candle | None, cur_candle: Candle | None,
                  chain_rows: list[ChainRow] | None, spot: float, staging_cfg: dict[str, float]) -> float | None:
    """D-053: active, leading evidence on top of the passive R-multiple tiers in `trailing_stop()` above.
    A candle pattern reversing against the position's view (A6.1) AND an opposing shift in option-chain OI
    (A6.4) are two independent signal families; requiring BOTH -- same discipline entry Confirmations use,
    never trading on one factor alone -- keeps this from reacting to ordinary noise. Only ever tightens
    the stop (same invariant as everywhere else in staging) and never triggers an exit by itself; a missing
    candle pair or chain (no data yet) means no evidence, never a false trigger. `reversal_exit_enabled`
    lets the operator switch this off if it turns out to hurt more than it helps -- it is new and
    unvalidated, same honesty as every strategy's own hypothesis card.

    `reversal_lock_min_r` (D-058): below this move_r, reversal evidence alone doesn't arm the lock --
    only a comfortably positive move does. A real trade run under the original rule (armed on ANY
    positive unrealized profit) showed it firing minutes after entry on a barely-positive move, which can
    lock in a trivial profit and cap the position's upside before the trade has even confirmed in its
    favour. A small default (0.3R), same honesty as every other threshold here: a starting point, not
    calibrated."""
    if not staging_cfg.get("reversal_exit_enabled", True):
        return None
    if pos.move_r(price) < float(staging_cfg.get("reversal_lock_min_r", 0.3)):
        return None
    long = pos.exposure is Direction.LONG
    if prev_candle is None or cur_candle is None:
        return None
    candle_ok = bearish_reversal_candle(prev_candle, cur_candle) if long else bullish_reversal_candle(prev_candle, cur_candle)
    if not candle_ok:
        return None
    if not chain_rows:
        return None
    above = [r for r in chain_rows if r.strike > spot][:3]
    below = [r for r in chain_rows if r.strike < spot][-3:]
    if long:
        oi_ok = sum(r.ce_oi_chg for r in above) > 0 or sum(r.pe_oi_chg for r in below) < 0
    else:
        oi_ok = sum(r.pe_oi_chg for r in below) > 0 or sum(r.ce_oi_chg for r in above) < 0
    if not oi_ok:
        return None
    frac = float(staging_cfg.get("reversal_lock_fraction", 0.5))
    lock = pos.avg_entry + frac * (price - pos.avg_entry)
    return lock if pos.sign * (lock - pos.stop) > 0 else None


def tighten_manual(pos: Position, price: float) -> float | None:
    """Operator 'Trail tighter' (D6): halve the distance between price and stop, toward profit only."""
    new = pos.stop + (price - pos.stop) * 0.5
    if pos.sign * (price - new) <= min_stop_gap(pos) or not pos.stop_is_better(new):
        return None
    return new
