"""Phase-2 exit criteria for the Aggression Layer (property + unit tests).

* nothing exceeds 3.0% per trade or 6.0% open risk, or trades past the 25% halt
* no code path raises risk after a loss
* gears never step up because of wins or targets
* no stage adds to a losing position; whole-position worst case stays within plan
"""
import datetime as dt

import pytest
from hypothesis import given, settings, strategies as st

from sthitaprajna.aggression import edge_brake
from sthitaprajna.aggression.gears import GearController
from sthitaprajna.aggression.sizer import planned_risk_fraction, size_position
from sthitaprajna.aggression.staging import harvest_qty, plan_confirm_add, plan_press, reversal_lock, trailing_stop
from sthitaprajna.aggression.tiers import TierState
from sthitaprajna.config import load_operator
from sthitaprajna.domain import (
    IST, Candle, Direction, Exchange, Gear, Grade, Instrument, Kind, Leg, PositionStage, Segment,
)
from sthitaprajna.market.options import ChainRow
from sthitaprajna.portfolio.position import Position
from sthitaprajna.risk import ceilings
from sthitaprajna.risk.targets import TargetBook

CFG = load_operator()
T0 = dt.datetime(2026, 9, 29, 10, 0, tzinfo=IST)
GEARS = list(Gear)
GRADES = [Grade.A_PLUS, Grade.A, Grade.B, Grade.C]


# ----------------------------------------------------------------------------- gears
def test_downshift_is_immediate_and_follows_ladder():
    g = GearController(CFG, ref_peak=100_000)
    assert g.on_equity(89_000, T0) is None and g.gear is Gear.ATTACK   # 11% < 12% -> stays ATTACK
    assert g.on_equity(88_000, T0).to is Gear.PRESS
    assert g.on_equity(81_900, T0).to is Gear.GUARD
    assert g.on_equity(76_900, T0).to is Gear.PRESERVE
    assert g.on_equity(72_000, T0).to is Gear.HALTED
    assert g.halted_review_pending


@given(st.lists(st.floats(min_value=50_000, max_value=200_000), min_size=1, max_size=200))
@settings(max_examples=300, deadline=None)
def test_on_equity_never_raises_the_gear(path):
    g = GearController(CFG, ref_peak=100_000)
    prev = g.gear.rank
    for eq in path:
        g.on_equity(eq, T0)
        assert g.gear.rank >= prev, "on_equity (wins, hot streaks) must never raise the gear"
        prev = g.gear.rank


def test_upshift_needs_recovery_and_clean_days_one_step_at_a_time():
    g = GearController(CFG, ref_peak=100_000)
    g.on_equity(81_000, T0)                                # 19% -> GUARD
    assert g.gear is Gear.GUARD
    for _ in range(4):
        assert g.on_day_close(99_000, False, T0) is None   # recovered, but <5 clean days
    ev = g.on_day_close(99_000, False, T0)
    assert ev is not None and ev.to is Gear.PRESS          # exactly one gear up
    for _ in range(4):
        g.on_day_close(99_000, False, T0)
    g.on_day_close(99_000, True, T0)                       # breach resets the clean-day count
    assert g.gear is Gear.PRESS
    for _ in range(5):
        g.on_day_close(99_000, False, T0)
    assert g.gear is Gear.ATTACK


def test_no_upshift_at_the_close_of_a_losing_day():
    g = GearController(CFG, ref_peak=100_000)
    g.on_equity(88_000, T0)
    for _ in range(4):
        g.on_day_close(95_000, False, T0, day_pnl=100)
    assert g.on_day_close(95_000, False, T0, day_pnl=-500) is None   # 5th clean day, but a losing one
    assert g.gear is Gear.PRESS
    assert g.on_day_close(95_500, False, T0, day_pnl=500).to is Gear.ATTACK


def test_no_upshift_without_recovery():
    g = GearController(CFG, ref_peak=100_000)
    g.on_equity(88_000, T0)
    for _ in range(20):
        g.on_day_close(88_000, False, T0)
    assert g.gear is Gear.PRESS


def test_halt_requires_review_and_restarts_in_guard():
    g = GearController(CFG, ref_peak=100_000)
    g.on_equity(71_000, T0)                                # 29% -> HALTED
    for _ in range(10):
        g.on_day_close(100_000, False, T0)
    assert g.gear is Gear.HALTED
    with pytest.raises(PermissionError):
        g.resume_after_halt(71_000, T0, reviewed=False)
    g.resume_after_halt(71_000, T0, reviewed=True)
    assert g.gear is Gear.GUARD and g.ref_peak == 71_000


def test_state_check_can_only_lower():
    g = GearController(CFG, ref_peak=100_000)
    g.apply_state_check(True, True, True)
    assert g.effective_gear() is Gear.ATTACK
    g.apply_state_check(True, False, True)
    assert g.effective_gear() is Gear.PRESS
    g.apply_state_check(True, True, True)          # a later "all yes" cannot undo today's drop
    assert g.effective_gear() is Gear.PRESS
    g.on_day_close(100_000, False, T0)
    assert g.effective_gear() is Gear.ATTACK


def test_targets_have_zero_effect_on_gear_and_size():
    g = GearController(CFG, ref_peak=100_000)
    tier = CFG.tier_for(100_000)
    before = planned_risk_fraction(CFG, tier, g.effective_gear(), Grade.A, 0.0, None)
    book = TargetBook()
    book.set_target("month", 50, "PCT", T0)     # absurd target
    book.update({"month": -5000}, {"month": 100_000})  # far behind target
    after = planned_risk_fraction(CFG, tier, g.effective_gear(), Grade.A, 0.0, None)
    assert before == after and g.gear is Gear.ATTACK
    assert book.b_grade_allowed and not book.entry_blocks()


def test_ratchet_mode_after_target_hit():
    book = TargetBook()
    book.set_target("day", 1500, "RUPEES", T0)
    book.update({"day": 1600}, {"day": 100_000})
    assert not book.b_grade_allowed and book.trail_required
    book.update({"day": 3000}, {"day": 100_000})
    assert book.ratchet["day"].floor == 1500
    book.update({"day": 1500}, {"day": 100_000})
    assert book.entry_blocks()


# ----------------------------------------------------------------------------- edge brake
def _outcomes(p, b, n):
    wins = int(round(p * n))
    return [b] * wins + [-1.0] * (n - wins)


def test_edge_brake_matches_spec_examples():
    r = edge_brake.evaluate(_outcomes(0.45, 1.8, 100), CFG.edge_brake)
    assert r.kelly == pytest.approx(0.45 - 0.55 / 1.8, abs=1e-9)
    assert r.ceiling_r == pytest.approx(0.25 * r.kelly)            # 3.6% > 2.5% -> no bite
    r2 = edge_brake.evaluate(_outcomes(0.40, 1.6, 100), CFG.edge_brake)
    assert r2.ceiling_r == pytest.approx(0.010)                    # quarter-Kelly 0.6% -> floor 1.0%
    assert not edge_brake.evaluate(_outcomes(0.45, 1.8, 49), CFG.edge_brake).active
    bad = edge_brake.evaluate(_outcomes(0.30, 1.5, 120), CFG.edge_brake)
    assert bad.cap_at_guard


# ----------------------------------------------------------------------------- sizing ceilings
@given(st.sampled_from(GEARS), st.sampled_from(GRADES), st.floats(0, 0.3),
       st.floats(50_000, 5_000_000), st.one_of(st.none(), st.floats(0.0, 0.2)))
@settings(max_examples=500, deadline=None)
def test_planned_risk_never_exceeds_ceiling(gear, grade, dd, equity, edge):
    tier = CFG.tier_for(equity)
    f = planned_risk_fraction(CFG, tier, gear, grade, dd, edge)
    assert 0.0 <= f <= ceilings.MAX_RISK_PER_TRADE
    if gear is Gear.HALTED or grade is Grade.C:
        assert f == 0.0
    if edge is not None and gear is not Gear.HALTED:
        assert f <= max(edge, 0) * CFG.grades[Grade.A_PLUS].multiplier + 1e-12


def test_a_plus_multiplier_only_in_attack_below_5pct():
    t = CFG.tier_for(100_000)
    assert planned_risk_fraction(CFG, t, Gear.ATTACK, Grade.A_PLUS, 0.02, None) == pytest.approx(0.050)
    assert planned_risk_fraction(CFG, t, Gear.ATTACK, Grade.A_PLUS, 0.06, None) == pytest.approx(0.0417)
    assert planned_risk_fraction(CFG, t, Gear.PRESS, Grade.A_PLUS, 0.12, None) == pytest.approx(0.0333)
    assert planned_risk_fraction(CFG, t, Gear.GUARD, Grade.B, 0.16, None) == 0.0


def test_one_lot_too_big_is_skipped_never_forced():
    d = size_position(cfg=CFG, equity=100_000, planned_fraction=0.025, grade=Grade.A, segment=Segment.INDEX_OPTION,
                      entry=150, stop=100, lot_size=65, per_unit_cost=1.0)
    assert not d.ok and "skipped" in d.skip_reason        # 51 x 65 = 3,315 > 2,500
    d2 = size_position(cfg=CFG, equity=100_000, planned_fraction=0.025, grade=Grade.A, segment=Segment.INDEX_OPTION,
                       entry=120, stop=100, lot_size=65, per_unit_cost=0.5)
    assert d2.ok and d2.full_qty == 65 and d2.staging_mode == "single"   # 20.5 x 65 = 1,332.5 -> 1 lot


def test_b_grade_is_probe_only_and_cash_is_leverage_capped():
    d = size_position(cfg=CFG, equity=100_000, planned_fraction=0.0125, grade=Grade.B, segment=Segment.EQ_INTRADAY,
                      entry=1000, stop=990, lot_size=1, per_unit_cost=0.2)
    assert d.staging_mode == "probe_only" and d.full_qty == d.probe_qty
    big = size_position(cfg=CFG, equity=100_000, planned_fraction=0.025, grade=Grade.A, segment=Segment.EQ_INTRADAY,
                        entry=1000, stop=999.5, lot_size=1, per_unit_cost=0.01)
    assert big.full_qty * 1000 <= 100_000                 # single notional <= 1x equity


@given(st.lists(st.lists(st.floats(-2.5, 3.0), min_size=1, max_size=6), min_size=1, max_size=40),
       st.sampled_from([Grade.A_PLUS, Grade.A, Grade.B]))
@settings(max_examples=300, deadline=None)
def test_no_code_path_raises_risk_after_a_loss(days, grade):
    """Intraday: a losing trade never raises the next trade's planned risk (sizing basis is fixed at the
    previous close and the gear can only move down). Across days: a net-losing day never raises the
    next day's planned risk. (A net-winning day may compound upward -- that is D1, not revenge.)"""
    equity_sod = equity = 100_000.0
    g = GearController(CFG, ref_peak=equity)
    tiers = TierState(CFG)

    def planned():
        return planned_risk_fraction(CFG, tiers.effective(), g.effective_gear(), grade,
                                     g.drawdown(equity), None) * equity_sod

    for trades in days:
        day_start_planned = planned()
        day_start_equity = equity
        prev = day_start_planned
        for r in trades:
            equity += r * max(prev, 1.0)
            g.on_equity(equity, T0)
            now = planned()
            if r < 0:
                assert now <= prev + 1e-9, f"intraday risk rose after a loss: {prev} -> {now}"
            prev = now
        breach = equity - day_start_equity <= -0.05 * equity_sod
        g.on_day_close(equity, breach, T0, day_pnl=equity - day_start_equity)
        tiers.on_day_close(equity, equity - day_start_equity, breach)
        equity_sod = equity
        if equity < day_start_equity:
            assert planned() <= day_start_planned + 1e-9, "risk rose after a losing day"
        if g.gear is Gear.HALTED:
            assert planned() == 0.0
            break


# ----------------------------------------------------------------------------- staging
def _pos(direction=Direction.LONG, qty=130, entry=100.0, stop=90.0, full=260, lot=65, planned=2600.0,
         mode="full", stage=PositionStage.PROBE):
    inst = Instrument("NFO:TEST", "TEST", "t", Kind.OPTION, Exchange.NSE, lot_size=lot, tick_size=0.05)
    p = Position("pos-1", "c", "s", inst, "NSE:NIFTY", direction, Segment.INDEX_OPTION, Grade.A, Gear.ATTACK, "SIM",
                 planned_risk=planned, unit_risk=abs(entry - stop), initial_stop=stop, stop=stop,
                 broker_stop=stop - 0.2, staging_mode=mode, full_qty_target=full,
                 confirm_trigger=entry + direction.sign * 5, confirm_deadline=T0 + dt.timedelta(minutes=20),
                 opened_at=T0, legs=[Leg(qty, entry, T0, PositionStage.PROBE)], stage=stage)
    p.mark(entry)
    return p


@given(st.sampled_from([Direction.LONG, Direction.SHORT]), st.floats(50, 150), st.booleans())
@settings(max_examples=400, deadline=None)
def test_confirm_add_never_adds_to_a_loser_and_stays_in_plan(direction, price, fired):
    entry = 100.0
    stop = 90.0 if direction is Direction.LONG else 110.0
    p = _pos(direction, entry=entry, stop=stop)
    plan = plan_confirm_add(p, price, fired, T0, CFG.staging)
    if plan.ok:
        assert p.unrealized(price) > 0
        assert plan.worst_case_after <= p.planned_risk + 1e-6
        assert direction.sign * (plan.new_stop - p.stop) >= 0      # stop only toward profit
        assert direction.sign * (price - plan.new_stop) > 0        # stop still on the loss side
    if p.unrealized(price) <= 0:
        assert not plan.ok


def test_confirm_add_requires_half_r_and_confirmation():
    p = _pos()
    assert not plan_confirm_add(p, 104.0, True, T0, CFG.staging).ok      # +0.4R
    assert not plan_confirm_add(p, 106.0, False, T0, CFG.staging).ok     # no confirmation
    ok = plan_confirm_add(p, 106.0, True, T0, CFG.staging)
    assert ok.ok and ok.qty == 130
    late = plan_confirm_add(p, 106.0, True, T0 + dt.timedelta(hours=1), CFG.staging)
    assert not late.ok


@given(st.floats(100, 200), st.integers(0, 1))
@settings(max_examples=300, deadline=None)
def test_press_rules(price, presses):
    p = _pos(qty=260, stage=PositionStage.CONFIRM_ADD)
    p.presses = presses
    plan = plan_press(p, price, Grade.A, True, True, CFG.staging)
    op_r = p.open_profit_r(price)
    if plan.ok:
        assert op_r >= 1.0
        if op_r >= 2.0:  # lock >= 50% of open profit
            locked = -plan.worst_case_after
            assert locked >= 0.5 * p.unrealized(price) - 1e-6
        else:
            assert plan.worst_case_after <= p.planned_risk + 1e-6
    else:
        assert op_r < 1.0 or "no add size" in plan.reason
    assert not plan_press(p, price, Grade.B, True, True, CFG.staging).ok
    assert not plan_press(p, price, Grade.A, True, False, CFG.staging).ok


def test_harvest_and_trailing():
    p = _pos(qty=260, stage=PositionStage.CONFIRM_ADD)
    assert harvest_qty(p, 115.0, CFG.staging) == 0          # +1.5R
    q = harvest_qty(p, 121.0, CFG.staging)                   # +2.1R
    assert q == 65 and q < p.qty                              # 40% of 260 = 104 -> 1 lot (65)
    new = trailing_stop(p, 121.0, CFG.staging)
    assert new is not None and new >= 100 + 0.5 * 21 - 1e-9


def test_early_profit_lock_tiers_before_2r():
    """D-042: a fast move that peaks below the 2R harvest/lock threshold and reverses still gets
    some profit protection, instead of riding all the way back to the original stop."""
    p = _pos()                                                    # entry 100, stop 90, unit risk 10
    assert trailing_stop(p, 104.0, CFG.staging) is None            # +0.4R: below the breakeven tier
    new = trailing_stop(p, 105.0, CFG.staging)                     # +0.5R: breakeven tier fires
    assert new == pytest.approx(100.0)
    p.stop = new
    new2 = trailing_stop(p, 110.0, CFG.staging)                    # +1.0R: early-lock tier fires (30% of 10)
    assert new2 == pytest.approx(103.0) and new2 > new
    p.stop = new2
    assert trailing_stop(p, 106.0, CFG.staging) is None            # a pullback never loosens an already-locked stop


def test_early_lock_tiers_never_move_against_an_open_loss():
    p = _pos()
    assert trailing_stop(p, 95.0, CFG.staging) is None              # still underwater: no breakeven, no lock


def test_expiry_day_decay_tiers_fire_earlier_than_normal_tiers():
    """D-045: the same open profit (+0.35R) does nothing on a normal day (breakeven_at_r=0.5) but
    already moves the stop to breakeven on the contract's own expiry day (expiry_breakeven_at_r=0.3)."""
    p = _pos()                                                       # entry 100, stop 90, unit risk 10
    assert trailing_stop(p, 103.5, CFG.staging) is None               # +0.35R, normal day: below 0.5R breakeven
    new = trailing_stop(p, 103.5, CFG.staging, is_expiry_day=True)    # +0.35R, expiry day: above 0.3R breakeven
    assert new == pytest.approx(100.0)
    p.stop = new
    assert trailing_stop(p, 106.0, CFG.staging) is None                              # +0.6R, normal day: below 1.0R
    new2 = trailing_stop(p, 106.0, CFG.staging, is_expiry_day=True)                  # +0.6R, expiry day: at 0.6R early lock
    assert new2 == pytest.approx(101.8) and new2 > new                              # 30% of 6 = 1.8


# ----------------------------------------------------------------------------- D-053 reversal evidence
def _candle(o, h, l, c, i=0):
    return Candle("TEST", T0 + dt.timedelta(minutes=i), o, h, l, c, 1000)


def _row(strike, ce_oi_chg=0.0, pe_oi_chg=0.0):
    return ChainRow(strike, "ce", "pe", 1.0, 1.0, 0.9, 1.1, 0.9, 1.1, 1000.0, 1000.0, ce_oi_chg, pe_oi_chg,
                    100.0, 100.0, 0.2, 0.2, 0.5, 0.5)


# a bearish reversal: prev is a strong up bar, cur engulfs it closing down (bearish_engulfing)
BEARISH_PREV, BEARISH_CUR = _candle(98, 102, 97, 101), _candle(102, 103, 96, 97, 1)
# a plain, non-reversal up bar: nothing for bearish_reversal_candle to fire on
FLAT_PREV, FLAT_CUR = _candle(100, 101, 99, 100.5), _candle(100.5, 102, 100, 101.5, 1)
# call writers adding above spot + put writers unchanged below: bearish OI evidence against a LONG
BEARISH_OI_ROWS = [_row(95), _row(105, ce_oi_chg=500)]
NEUTRAL_OI_ROWS = [_row(95), _row(105)]


def test_reversal_lock_needs_both_candle_and_oi_evidence_together():
    p = _pos()                                                        # LONG, entry 100, stop 90
    price = 104.0                                                     # +0.4R: in profit
    assert reversal_lock(p, price, BEARISH_PREV, BEARISH_CUR, NEUTRAL_OI_ROWS, 100.0, CFG.staging) is None
    assert reversal_lock(p, price, FLAT_PREV, FLAT_CUR, BEARISH_OI_ROWS, 100.0, CFG.staging) is None
    new = reversal_lock(p, price, BEARISH_PREV, BEARISH_CUR, BEARISH_OI_ROWS, 100.0, CFG.staging)
    assert new == pytest.approx(100.0 + 0.5 * 4.0)                     # reversal_lock_fraction (0.5) of the +4 profit


def test_reversal_lock_never_fires_without_open_profit():
    p = _pos()
    assert reversal_lock(p, 95.0, BEARISH_PREV, BEARISH_CUR, BEARISH_OI_ROWS, 100.0, CFG.staging) is None


def test_reversal_lock_can_be_disabled():
    cfg = {**CFG.staging, "reversal_exit_enabled": False}
    p = _pos()
    assert reversal_lock(p, 104.0, BEARISH_PREV, BEARISH_CUR, BEARISH_OI_ROWS, 100.0, cfg) is None


def test_reversal_lock_never_loosens_an_already_better_stop():
    p = _pos()
    p.stop = 103.0                                                    # already tighter than the 102.0 lock below
    assert reversal_lock(p, 104.0, BEARISH_PREV, BEARISH_CUR, BEARISH_OI_ROWS, 100.0, CFG.staging) is None


def test_reversal_lock_handles_missing_candles_or_chain_gracefully():
    p = _pos()
    assert reversal_lock(p, 104.0, None, None, BEARISH_OI_ROWS, 100.0, CFG.staging) is None
    assert reversal_lock(p, 104.0, BEARISH_PREV, BEARISH_CUR, [], 100.0, CFG.staging) is None


def test_short_trailing_locks_profit_correctly():
    p = _pos(direction=Direction.SHORT, entry=100.0, stop=110.0)
    new = trailing_stop(p, 78.0, CFG.staging)                # +2.2R for a short
    assert new is not None and new == pytest.approx(89.0)    # halfway: locks 50% of 22 points
    assert new < p.stop


# ----------------------------------------------------------------------------- tiers (D3)
def test_tier_promotion_is_never_automatic():
    t = TierState(CFG)
    for _ in range(30):
        t.on_day_close(250_000, 100, False)
    assert t.tier.name == "T1"                                   # equity alone never promotes
    assert not t.promote(True, 50, 0.2)["ok"]                    # < 100 trades
    assert not t.promote(False, 150, 0.2)["ok"]                  # needs the operator
    assert t.promote(True, 150, 0.2)["ok"] and t.tier.name == "T2"


def test_tier_demotion_on_a_losing_day_never_raises_r():
    t = TierState(CFG, index=1)                                  # T2: ATTACK R 3.5%
    ev = t.on_day_close(170_000, -5000, False)                   # < 90% of 2L floor
    assert ev["to"] == "T1" and t.effective().attack_r == pytest.approx(0.035)
    t.on_day_close(171_000, 1000, False)                         # non-losing close releases the hold
    assert t.effective().attack_r == pytest.approx(0.0417)
