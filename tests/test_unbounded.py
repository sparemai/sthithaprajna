"""Unbounded risk mode (operator decision D-031): capital-based sizing, one full entry with stop-loss and
target, no ceilings / gears / loss limits / streak stops / staging / approvals. Only the operator's kill
switch, market hours, funds, a defined stop and valid data can stop an entry.
"""
import copy
import datetime as dt
import os

import pytest
from test_engine_e2e import CAL, DAY, RULES, FakeWall, candidate, push, step

from sthitaprajna.config import load_operator, operator_from_dict
from sthitaprajna.domain import IST, Direction, Gear
from sthitaprajna.engine.runtime import Engine, _Env
from sthitaprajna.engine.snapshot import snapshot


def unbounded_cfg(**over):
    raw = copy.deepcopy(load_operator().raw)
    raw["risk_mode"] = "unbounded"
    raw["unbounded"] = {**(raw.get("unbounded") or {}), **over}
    saved = os.environ.pop("STHITAPRAJNA_RISK_MODE", None)   # the suite pins bounded; this test wants the yaml
    try:
        return operator_from_dict(raw)
    finally:
        if saved is not None:
            os.environ["STHITAPRAJNA_RISK_MODE"] = saved


def ub_engine(**over) -> tuple[Engine, FakeWall]:
    wall = FakeWall()
    e = Engine(unbounded_cfg(**over), RULES, CAL, seed=3, start=DAY, warmup_days=2, wall_clock=wall)
    e.strategies = []
    e.market.freeze = True
    e.run_until(dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=IST), step_seconds=30)
    return e, wall


def enter(e: Engine, key: str = "NSE:RELIANCE", unit: float = 10.0, direction=Direction.LONG, n: int = 0):
    c = candidate(e, key, unit=unit, direction=direction)
    if n:                                           # a fresh signal (new setup id and bar), not a repeat
        c.setup_id, c.signal_bar = f"{c.setup_id}:{n}", f"{key}@{n + 1}"
    push(e, c)
    step(e)
    return [p for p in e.positions.values() if p.instrument.key == key]


def test_suite_default_is_bounded_and_yaml_default_is_unbounded():
    assert load_operator().risk_mode == "bounded"            # pinned by tests/conftest.py
    assert unbounded_cfg().is_unbounded


def test_full_entry_with_stop_loss_and_target_without_approval():
    e, _ = ub_engine()
    assert not e.pilot_approvals
    (p,) = enter(e)
    ask = e.market.quote("NSE:RELIANCE").ask
    assert p.qty == int(40_000 // ask)                      # 40% of Rs 1,00,000, fully funded, in ONE order (D-051)
    assert p.target == pytest.approx(p.avg_entry + 2.0 * p.unit_risk, abs=0.01)
    assert p.broker_stop_id in e.broker.resting and not e.approvals
    assert any(ev["kind"] == "entry" and "target" in ev["msg"] for ev in e.events)
    e.market.set_price("NSE:RELIANCE", p.target + 0.5)
    step(e, 30, 2)
    assert not e.positions and e.ledger.closed_trades[-1]["reason"] == "TARGET"
    assert e.ledger.closed_trades[-1]["net"] > 0
    assert e.ledger.closed_trades[-1]["stages"] == ["PROBE", "EXIT"]   # one entry, one exit: no adds, no harvest


def test_profit_lock_tiers_apply_in_unbounded_mode():
    """D-052: before this, an unbounded position had zero profit protection below the single
    trail_to_breakeven_at_r (1.0 by default) -- it could run to +0.9R and reverse all the way back to the
    original stop with nothing locked in. The same breakeven/early-lock tiers bounded mode has (D-042)
    now also tighten an unbounded position's stop, without adding any probe/confirm-add/press/harvest
    machinery (sizing stays a single full entry, exactly as D-031 intends)."""
    e, _ = ub_engine()
    (p,) = enter(e, unit=10.0)
    original_stop = p.stop
    planned_risk = p.planned_risk
    assert p.move_r(p.avg_entry) == 0.0
    # +0.6R: above staging's breakeven_at_r (0.5) but below trail_to_breakeven_at_r (1.0) -- the old code
    # left the stop untouched here.
    e.market.set_price("NSE:RELIANCE", p.avg_entry + 0.6 * p.unit_risk)
    step(e, 30, 2)
    assert p.stop > original_stop                     # tightened even though +1R was never reached
    assert p.stop >= p.avg_entry - 1e-6                # at least at breakeven
    assert not e.positions or p.id in e.positions      # still open: the tier only moves the stop, no exit
    # it reverses back through breakeven -- nowhere near the original stop, which is still 1.0R away
    e.market.set_price("NSE:RELIANCE", p.avg_entry - 0.05 * p.unit_risk)
    step(e, 30, 2)
    assert not e.positions and e.ledger.closed_trades[-1]["reason"] in ("STOP", "BROKER_STOP")
    # far better than the ~-1R (minus costs) loss the pre-D-052 code would have taken all the way down
    # to the original, untightened stop
    assert e.ledger.closed_trades[-1]["net"] > -0.25 * planned_risk


def test_profit_lock_does_not_refire_on_an_unchanged_price():
    """D-058: a real trade's log showed the SAME "profit-lock stop tightened" line firing seven times in
    14 seconds, several at the identical stop value and timestamp. Root cause: `stop_is_better()` compared
    an unrounded candidate against the already-2-decimal-rounded `p.stop`, so a candidate that rounded to
    the SAME displayed stop still looked like an improvement every tick the underlying evidence held.
    Fixed by rounding the candidate before the comparison; this locks in that fix by re-running `step()`
    several times at a frozen price and asserting `p.stop_history` (D-058's structured record, separate
    from `stage_history`) gains at most one entry, not one per tick."""
    e, _ = ub_engine()
    (p,) = enter(e, unit=10.0)
    e.market.set_price("NSE:RELIANCE", p.avg_entry + 0.6 * p.unit_risk)   # arms the breakeven/early-lock tier
    step(e, 30, 2)
    assert len(p.stop_history) == 1 and p.stop_history[0]["stop"] == pytest.approx(p.stop)
    stop_after_first = p.stop
    for _ in range(5):                       # same price, several more ticks: must NOT refire
        step(e, 5, 1)
    assert p.stop == stop_after_first
    assert len(p.stop_history) == 1


def test_breakeven_trail_then_tighter_profit_lock_in_unbounded_mode():
    """D-052: at +1.2R, unbounded mode now also gets the early-lock tier (not just plain breakeven) --
    it locks `early_lock_fraction` of open profit, exactly as bounded mode does."""
    e, _ = ub_engine()
    (p,) = enter(e)
    entry = p.avg_entry
    planned_risk = p.planned_risk
    price = entry + 1.2 * p.unit_risk
    e.market.set_price("NSE:RELIANCE", price)
    step(e)
    expected = entry + e.cfg.staging["early_lock_fraction"] * (price - entry)
    assert p.stop == pytest.approx(round(expected, 2), abs=0.01)
    assert p.stop > round(entry, 2)                  # better than plain breakeven (D-052)
    e.market.set_price("NSE:RELIANCE", entry - 0.3)
    step(e, 30, 2)
    assert not e.positions and e.ledger.closed_trades[-1]["reason"] in ("STOP", "BROKER_STOP")
    # far better than the ~-1R (minus costs) loss a plain, untightened stop would have taken
    assert e.ledger.closed_trades[-1]["net"] > -0.25 * planned_risk


def test_no_ceiling_blocks_trading_after_big_losses():
    e, _ = ub_engine(capital_per_trade=1.0)
    for i in range(6):                                      # bounded: 3 losses -> cooldown, 4 -> done, -5% -> done
        (p,) = enter(e, unit=60.0, n=i + 1)
        e.market.set_price("NSE:RELIANCE", p.stop - 1.0)
        step(e, 30, 2)
        assert not e.positions
        e.market.set_price("NSE:RELIANCE", 1420.0)
        step(e)
    day_loss = e.ledger.equity - e.ledger.day_start
    assert day_loss < -0.05 * 100_000                       # far past the bounded daily loss limit
    assert e.ledger.losing_streak >= 6
    assert len(enter(e, unit=60.0, n=99)) == 1              # ...and it still takes the next trade
    msgs = " ".join(ev["msg"] for ev in e.events)
    assert "HALTED" not in msgs and "cooldown" not in msgs and "daily loss limit" not in msgs
    assert e.gears.effective_gear() is Gear.ATTACK and not snapshot(e)["risk"]["halts"]


def test_correlation_cap_blocks_a_second_bet_on_the_same_underlying_direction_same_cycle():
    """D-046: unbounded mode has no correlation-bucket check (that's bounded-only), so without this cap,
    two strategies agreeing on the same underlying+direction in the same cycle would each independently
    commit a full capital_per_trade slice to what is really one correlated bet."""
    e, _ = ub_engine(capital_per_trade=0.3)
    c1 = candidate(e, "NSE:RELIANCE", unit=10.0, direction=Direction.LONG)
    c2 = candidate(e, "NSE:RELIANCE", unit=10.0, direction=Direction.LONG)
    c2.strategy, c2.setup_id = "scripted_2", "scripted_2:NSE:RELIANCE:LONG"   # a distinct strategy's own setup_id
    e.funnel.ingest([c1, c2], e.now)
    ready = e.funnel.evaluate(_Env(e, e._risk_ctx()))
    e._deploy(ready)
    step(e)
    positions = [p for p in e.positions.values() if p.instrument.key == "NSE:RELIANCE"]
    assert len(positions) == 1
    rejected = [r for r in e.funnel.records.values() if r.status == "REJECTED" and "correlation cap" in r.reason]
    assert len(rejected) == 1


def test_correlation_cap_does_not_block_different_underlyings_or_opposite_directions():
    e, _ = ub_engine(capital_per_trade=0.2)
    c1 = candidate(e, "NSE:RELIANCE", unit=10.0, direction=Direction.LONG)
    c2 = candidate(e, "NSE:ICICIBANK", unit=10.0, direction=Direction.LONG)
    c3 = candidate(e, "NSE:RELIANCE", unit=10.0, direction=Direction.SHORT)
    c3.setup_id, c3.signal_bar = c3.setup_id + ":short", "NSE:RELIANCE@short"
    e.funnel.ingest([c1, c2, c3], e.now)
    ready = e.funnel.evaluate(_Env(e, e._risk_ctx()))
    e._deploy(ready)
    rejected = [r for r in e.funnel.records.values() if "correlation cap" in r.reason]
    assert not rejected


def test_free_capital_is_shared_between_entries():
    e, _ = ub_engine(capital_per_trade=0.6)
    (a,) = enter(e, "NSE:RELIANCE", unit=10.0)
    used_a = a.qty * a.avg_entry
    assert used_a <= 60_000 and used_a > 55_000
    (b,) = enter(e, "NSE:ICICIBANK", unit=10.0)
    assert b.qty * b.avg_entry <= 100_000 - used_a + 1      # got only what was still free
    assert e.free_capital() < 3_000
    push(e, candidate(e, "NSE:INFY", unit=10.0))
    step(e)
    assert not [p for p in e.positions.values() if p.instrument.key == "NSE:INFY"]
    assert any("capital_fits" in r.reason for r in e.funnel.records.values() if "INFY" in r.candidate.instrument.key)


def test_only_mandatory_checks_can_block():
    e, _ = ub_engine()
    e.kill_switch = True
    push(e, candidate(e))
    step(e)
    assert not e.positions
    rec = next(iter(e.funnel.records.values()))
    assert rec.status == "REJECTED" and "kill_switch_off" in rec.reason
    names = [c["name"] for c in rec.verdict["checks"]]
    assert "per_trade_ceiling" not in names and "open_risk_ok" not in names and "daily_loss_ok" not in names


def test_operator_can_still_switch_approvals_on():
    e, wall = ub_engine()
    assert e.set_pilot_approvals(True)["ok"]
    push(e, candidate(e))
    pend = [a for a in e.approvals.values() if a.status == "PENDING"]
    assert len(pend) == 1
    assert e.approve(pend[0].id, True)["ok"]
    step(e)
    assert len(e.positions) == 1


def test_snapshot_reports_risk_mode_and_capital():
    e, _ = ub_engine()
    enter(e)
    s = snapshot(e)
    assert s["risk_mode"] == "unbounded"
    cap = s["capital"]
    assert cap["per_trade_fraction"] == 0.40 and cap["deployed"] > 0
    assert cap["free"] == pytest.approx(cap["equity"] - cap["deployed"], abs=1)
