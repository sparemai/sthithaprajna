"""Sthitaprajna 2.0 (D-039): the hardened Zero-to-Hero expiry sleeve -- EV-based strike selection across
delta bands, the D8 sleeve budget, and the cumulative-loss kill switch with its operator-review resume."""
from __future__ import annotations

import datetime as dt

import pytest

from sthitaprajna.aggression.sizer import SizingDecision
from sthitaprajna.config import load_operator
from sthitaprajna.domain import (
    IST, Candidate, Direction, Exchange, Gear, Grade, HalfLife, Instrument, Kind, Mode, Segment, new_id,
)
from sthitaprajna.engine.runtime import Engine, _FeatureCacheCtx
from sthitaprajna.market.options import ChainRow
from sthitaprajna.risk.guardian import EntryProposal, RiskContext, RiskGuardian
from sthitaprajna.signal.ev import EVResult
from sthitaprajna.strategies.options import Z2H_BANDS, ZeroToHero, _best_z2h_pick, _z2h_band_scan
from test_engine_e2e import CAL, DAY, RULES
from test_stock_options import crafted

CFG = load_operator()
TIER = CFG.tier_for(CFG.starting_capital)


# ----------------------------------------------------------------------------- _best_z2h_pick (pure EV)
class _FakeInst:
    def __init__(self, key: str, lot_size: int = 65):
        self.key, self.lot_size = key, lot_size


class _FakeQuote:
    def __init__(self, ask: float):
        self.ask, self.spread_bps = ask, 50.0


class _FakeCtx:
    """Duck-typed StrategyContext stand-in: tests _best_z2h_pick's EV comparison in isolation."""
    def __init__(self, asks: dict[float, float]):
        self.asks = asks

    def option_instrument(self, und, expiry, strike, ot):
        return _FakeInst(f"{und}:{strike}:{ot.value}")

    def quote(self, key):
        ask = self.asks.get(float(key.split(":")[1]))
        return None if ask is None else _FakeQuote(ask)


def _rows(strike_deltas: dict[float, float]) -> list[ChainRow]:
    return [ChainRow(strike=k, ce_key=f"CE{k}", pe_key=f"PE{k}", ce_ltp=1.0, pe_ltp=1.0, ce_bid=0.9, ce_ask=1.1,
                     pe_bid=0.9, pe_ask=1.1, ce_oi=0.0, pe_oi=0.0, ce_oi_chg=0.0, pe_oi_chg=0.0, ce_vol=0.0,
                     pe_vol=0.0, ce_iv=0.2, pe_iv=0.2, ce_delta=d, pe_delta=-d) for k, d in strike_deltas.items()]


def test_best_pick_computes_ev_consistently_and_picks_one_band():
    rows = _rows({100: 0.26, 200: 0.18, 300: 0.10})       # roughly NEAR / MID / DEEP target deltas
    ctx = _FakeCtx({100: 5.0, 200: 3.0, 300: 1.0})
    pick = _best_z2h_pick(ctx, "NIFTY", DAY, rows, True, base_p=0.26, regime="GAMMA_ACCELERATION", budget=1_000_000)
    assert pick is not None and pick["label"] in ("NEAR", "MID", "DEEP")
    assert pick["ev_r"] == pytest.approx(pick["p_win"] * pick["payoff_r"] - (1 - pick["p_win"]))
    assert pick["p_win"] <= 0.45                          # never lets a made-up prior look "safe"


def test_best_pick_skips_bands_that_are_unaffordable_or_unquoted():
    rows = _rows({100: 0.26, 200: 0.18, 300: 0.10})
    ctx = _FakeCtx({100: 5.0, 300: 1.0})                   # MID (200) has no quote at all
    pick = _best_z2h_pick(ctx, "NIFTY", DAY, rows, True, base_p=0.26, regime="GAMMA_ACCELERATION",
                          budget=65 * 2.0)                 # only DEEP's lot (65 x Rs 1) fits the budget
    assert pick is not None and pick["label"] == "DEEP"


def test_best_pick_returns_none_when_nothing_is_affordable():
    ctx = _FakeCtx({100: 5.0})
    assert _best_z2h_pick(ctx, "NIFTY", DAY, _rows({100: 0.26}), True, base_p=0.26,
                          regime="GAMMA_ACCELERATION", budget=1.0) is None


# -------------------------------------------------------------------- _z2h_band_scan (D-058 diagnostics)
def test_band_scan_reports_no_strike_uniformly_when_the_chain_fails_the_base_filter():
    """Every row too thin (ltp <= 0.5) or too wide a spread fails `_pick_strike` for every band alike --
    `_pick_strike` picks the single globally-nearest-delta row that passes that filter, so this reason is
    an all-or-nothing chain-quality gate, distinct from the per-band quote/affordability gates below."""
    thin_row = _rows({100: 0.25})
    thin_row[0].ce_ltp = thin_row[0].pe_ltp = 0.3   # below the 0.5 floor _pick_strike enforces
    bands = _z2h_band_scan(_FakeCtx({}), "NIFTY", DAY, thin_row, True, budget=1_000_000)
    assert all(not b["ok"] and "no strike" in b["reason"] for b in bands)


def test_band_scan_names_exactly_which_gate_blocked_each_band():
    """D-058: a large-lot underlying's premiums pricing above the sleeve budget on a strong trend day
    should show up as hard per-band numbers (strike, ask, lot cost vs budget), not a single generic
    catch-all -- this is what the operator sees in the "why no candidate" log line."""
    rows = _rows({100: 0.25, 200: 0.18, 300: 0.10})     # exact NEAR/MID/DEEP target deltas -> distinct rows
    ctx = _FakeCtx({100: 20.0, 300: 1.0})                # MID (200) has no quote at all; NEAR is too pricey
    bands = {b["label"]: b for b in _z2h_band_scan(ctx, "NIFTY", DAY, rows, True, budget=500.0)}
    assert set(bands) == {"NEAR", "MID", "DEEP"}
    # _FakeInst's default lot (65): 20.0 x 65 = Rs 1,300, over the Rs 500 budget -- named explicitly
    assert not bands["NEAR"]["ok"] and "1,300" in bands["NEAR"]["reason"] and "500" in bands["NEAR"]["reason"]
    assert not bands["MID"]["ok"] and "no live quote" in bands["MID"]["reason"]
    assert bands["DEEP"]["ok"] and bands["DEEP"]["row"].strike == 300


def test_band_scan_reports_ok_for_an_affordable_band():
    rows = _rows({100: 0.25})
    ctx = _FakeCtx({100: 5.0})
    bands = _z2h_band_scan(ctx, "NIFTY", DAY, rows, True, budget=1_000_000)
    near = next(b for b in bands if b["label"] == "NEAR")
    assert near["ok"] and near["row"].strike == 100 and near["q"].ask == 5.0


def test_regime_conditional_multipliers_favor_deep_otm_most_under_gamma_acceleration():
    """Gamma acceleration (straddle expansion + rising VIX) should reward the deepest band relative to
    the nearest one more than a steadier trend expiry does -- the regime split must not be arbitrary."""
    near_mult, deep_mult = dict(Z2H_BANDS[0][3]), dict(Z2H_BANDS[2][3])
    gamma_ratio = deep_mult["GAMMA_ACCELERATION"] / near_mult["GAMMA_ACCELERATION"]
    trend_ratio = deep_mult["TREND_EXPIRY"] / near_mult["TREND_EXPIRY"]
    assert gamma_ratio > trend_ratio


# ----------------------------------------------------------------------------- ZeroToHero.scan() gating
def _ctx_at(e: Engine, feats, minute: int) -> _FeatureCacheCtx:
    ctx = _FeatureCacheCtx(e)
    ctx.now = dt.datetime.combine(e.now.date(), dt.time(9, 15), tzinfo=e.now.tzinfo) + dt.timedelta(minutes=minute)
    ctx._feat_cache[feats.key] = feats
    ctx.expiries = lambda und: [ctx.now.date()]
    return ctx


def _z2h_engine() -> Engine:
    e = Engine(CFG, RULES, CAL, seed=5, start=DAY, warmup_days=2)
    e.run_until(dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=IST), step_seconds=30)
    return e


def test_scan_is_empty_outside_the_expiry_afternoon_window():
    e = _z2h_engine()
    e.regime.expiry = "GAMMA_ACCELERATION"
    feats = crafted(e, key="NSE:NIFTY")
    ctx = _ctx_at(e, feats, minute=100)                    # late morning, not the expiry afternoon
    assert ZeroToHero().scan(ctx) == []


def test_scan_is_empty_outside_the_allowed_expiry_regimes():
    e = _z2h_engine()
    for bad_regime in ("PINNING", "VOLATILITY_CRUSH", "RANGE_EXPIRY", None):
        e.regime.expiry = bad_regime
        feats = crafted(e, key="NSE:NIFTY")
        ctx = _ctx_at(e, feats, minute=260)
        assert ZeroToHero().scan(ctx) == []


def test_scan_produces_a_sleeve_candidate_with_defined_risk_when_conditions_are_met():
    e = _z2h_engine()
    e.regime.expiry = "GAMMA_ACCELERATION"
    feats = crafted(e, key="NSE:NIFTY")
    ctx = _ctx_at(e, feats, minute=260)
    out = ZeroToHero().scan(ctx)
    for c in out:
        assert c.sleeve and c.defined_risk and c.segment is Segment.INDEX_OPTION
        assert c.direction is Direction.LONG and c.stop == 0.05    # long premium only; max loss = premium
        assert 0.0 < c.p_win <= 0.45 and c.target_r > 0
        assert not c.p_win_calibrated                              # Honesty Contract: an unproven prior


# ----------------------------------------------------------------------------- D8 sleeve kill switch (guardian)
# Direct RiskGuardian.check_entry() unit tests: a hand-built happy-path proposal that passes every other
# check, so only the sleeve-specific ones under test can flip the verdict.
_INST = Instrument(key="NFO:NIFTY:TEST:CE", symbol="NIFTY25000CE", name="NIFTY CE", kind=Kind.OPTION,
                  exchange=Exchange.NSE, lot_size=65, tick_size=0.05, sector="", beta=1.0)


def _sleeve_proposal() -> EntryProposal:
    c = Candidate(id=new_id("sig"), ts=dt.datetime(2026, 9, 29, 13, 0, tzinfo=IST), strategy="zero_to_hero",
                 strategy_version="0.1.0", instrument=_INST, underlying_key="NSE:NIFTY", direction=Direction.LONG,
                 segment=Segment.INDEX_OPTION, entry=10.0, stop=0.05, target_r=4.0, p_win=0.26,
                 p_win_calibrated=False, half_life=HalfLife.MINUTES, trigger_fired=True, confirmations=[],
                 quality_features={}, regime_fit=True, defined_risk=True, sleeve=True,
                 setup_id="zero_to_hero:NIFTY:test", signal_bar="b1", explanation="test",
                 model_confidence=0.5, regime_confidence=0.5, liquidity_score=0.5)
    sizing = SizingDecision(planned_risk=1000.0, planned_risk_fraction=0.01, base_planned_risk=1000.0,
                            lot_size=65, full_qty=65, probe_qty=65, staging_mode="probe_only", risk_at_full=1000.0)
    ev = EVResult(p_win=0.26, win_r=4.0, loss_r=1.0, cost_r=0.1, gross_r=0.5, net_r=0.4, margin_ok=True,
                 reason="ok", p_calibrated=False)
    return EntryProposal(candidate=c, grade=Grade.A, sizing=sizing, qty=65, risk_now=650.0,
                         notional_now=650.0, margin_now=650.0, ev=ev)


def _happy_ctx(**overrides) -> RiskContext:
    base = dict(mode=Mode.PAPER, now=dt.datetime(2026, 9, 29, 13, 0, tzinfo=IST), equity_sod=100_000.0,
               equity_mtm=100_000.0, all_time_peak=100_000.0, gear=Gear.ATTACK, drawdown=0.0, tier=TIER)
    base.update(overrides)
    return RiskContext(**base)


def test_happy_path_sleeve_entry_is_approved():
    v = RiskGuardian(CFG).check_entry(_happy_ctx(), _sleeve_proposal())
    assert v.approved, v.reason


def test_sleeve_entry_blocked_once_cumulative_loss_hits_the_kill_threshold():
    kill_at = float(CFG.sleeve["kill_cumulative_loss"]) * 100_000.0
    ctx = _happy_ctx(sleeve_cumulative_pnl=-kill_at)          # exactly at the D8 kill threshold
    v = RiskGuardian(CFG).check_entry(ctx, _sleeve_proposal())
    assert not v.approved and "sleeve_not_killed" in v.reason


def test_sleeve_entry_still_allowed_just_short_of_the_kill_threshold():
    kill_at = float(CFG.sleeve["kill_cumulative_loss"]) * 100_000.0
    ctx = _happy_ctx(sleeve_cumulative_pnl=-(kill_at - 500.0))
    v = RiskGuardian(CFG).check_entry(ctx, _sleeve_proposal())
    assert v.approved, v.reason


def test_sleeve_budget_check_blocks_once_the_per_expiry_budget_is_used_up():
    used = float(CFG.sleeve["per_expiry"]) * 100_000.0
    ctx = _happy_ctx(sleeve_used={"expiry": used})            # already at the per-expiry cap
    v = RiskGuardian(CFG).check_entry(ctx, _sleeve_proposal())
    assert not v.approved and "sleeve_budget_ok" in v.reason


def test_resume_after_review_clears_the_sleeve_kill_and_only_the_sleeve_kill():
    e = Engine(CFG, RULES, CAL, seed=5, start=DAY, warmup_days=2)
    kill_at = float(CFG.sleeve["kill_cumulative_loss"]) * e.ledger.equity_sod
    e.sleeve_cumulative_pnl = -kill_at
    assert e.resume(reviewed=False) == {"ok": False, "error": "operator review required"}
    result = e.resume(reviewed=True)
    assert result["ok"] and "Zero-to-Hero" in "; ".join(result["result"])
    assert e.sleeve_cumulative_pnl == 0.0
    assert e.resume(reviewed=True) == {"ok": False, "error": "nothing to resume"}   # idempotent, not a free reset
