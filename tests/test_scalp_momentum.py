"""Sthitaprajna 2.0 (D-048): ScalpMomentum -- a short 1-3 minute directional burst traded on its own,
anywhere in the session and in any regime except EXTREME_VOL (deliberately including RANGE), with a tight
stop and a tight target. The other strategies all wait for a structural level; this one doesn't."""
from __future__ import annotations

import datetime as dt

import pytest

from sthitaprajna.domain import Candle, Direction, Segment
from sthitaprajna.engine.runtime import Engine, _FeatureCacheCtx
from sthitaprajna.strategies.base import Features
from sthitaprajna.strategies.options import ScalpMomentum
from test_engine_e2e import CAL, DAY, RULES


def _candle(key, t0, i, o, h, l, c):
    return Candle(key, t0 + dt.timedelta(minutes=i), o, h, l, c, 5000)


def _engine(at: dt.time = dt.time(10, 30)) -> Engine:
    from sthitaprajna.config import load_operator
    e = Engine(load_operator(), RULES, CAL, seed=17, start=DAY, warmup_days=2)
    e.run_until(dt.datetime.combine(DAY, at, tzinfo=e.now.tzinfo), step_seconds=30)
    return e


def _ctx_with(e: Engine, feats: Features) -> _FeatureCacheCtx:
    ctx = _FeatureCacheCtx(e)
    ctx._feat_cache[feats.key] = feats
    return ctx


def _scalp_features(key: str, e: Engine, *, long: bool | None = True) -> Features:
    """2 filler bars, then a 4-bar burst: a small baseline body, then 3 consecutive same-direction closes
    with an accelerating final bar. `long=None` keeps every bar flat (no burst at all)."""
    px_mid = e.market.states[key].price
    atr1 = 15.0
    t = e.now.replace(second=0, microsecond=0)
    filler = [_candle(key, t, i, px_mid, px_mid + 1, px_mid - 1, px_mid) for i in range(2)]
    if long is None:
        burst = [_candle(key, t, 2 + i, px_mid, px_mid + 1, px_mid - 1, px_mid) for i in range(4)]
        price = px_mid
    elif long:
        burst = [
            _candle(key, t, 2, px_mid, px_mid + 2, px_mid - 1, px_mid + 1),
            _candle(key, t, 3, px_mid + 1, px_mid + 4, px_mid + 1, px_mid + 3),
            _candle(key, t, 4, px_mid + 3, px_mid + 7, px_mid + 3, px_mid + 6),
            _candle(key, t, 5, px_mid + 6, px_mid + 11, px_mid + 6, px_mid + 10),
        ]
        price = px_mid + 10
    else:
        burst = [
            _candle(key, t, 2, px_mid, px_mid + 1, px_mid - 2, px_mid - 1),
            _candle(key, t, 3, px_mid - 1, px_mid - 1, px_mid - 4, px_mid - 3),
            _candle(key, t, 4, px_mid - 3, px_mid - 3, px_mid - 7, px_mid - 6),
            _candle(key, t, 5, px_mid - 6, px_mid - 6, px_mid - 11, px_mid - 10),
        ]
        price = px_mid - 10
    day = filler + burst
    return Features(key=key, price=price, vwap=px_mid, atr1=atr1, atr5=atr1 * 3, or_high=None, or_low=None,
                    ema20=px_mid, ema50=px_mid, adx=18.0, rvol=1.5, c5=[burst[-1]], day=day,
                    htf_up=bool(long), htf_down=(long is False), bar_id=f"{key}@scalp")


def test_scan_fires_a_long_candidate_on_an_upward_burst():
    e = _engine()
    e.regime.trend, e.regime.volatility = "RANGE", "NORMAL_VOL"     # deliberately works in RANGE
    feats = _scalp_features("NSE:NIFTY", e, long=True)
    out = [c for c in ScalpMomentum().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"]
    assert out, "expected at least one candidate on the upward burst"
    for c in out:
        assert c.direction is Direction.LONG and c.bias is Direction.LONG
        assert c.segment is Segment.INDEX_OPTION and c.defined_risk and not c.sleeve
        assert c.target_r == 1.0 and not c.p_win_calibrated
        assert any(cf.factor == "price_action" and cf.name == "micro_burst" and cf.fired for cf in c.confirmations)


def test_scan_fires_a_short_candidate_on_a_downward_burst():
    e = _engine()
    e.regime.trend, e.regime.volatility = "TREND_DOWN", "LOW_VOL"
    feats = _scalp_features("NSE:NIFTY", e, long=False)
    out = [c for c in ScalpMomentum().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"]
    assert out, "expected at least one candidate on the downward burst"
    for c in out:
        assert c.bias is Direction.SHORT


def test_scan_is_empty_with_no_burst():
    e = _engine()
    e.regime.trend, e.regime.volatility = "RANGE", "NORMAL_VOL"
    feats = _scalp_features("NSE:NIFTY", e, long=None)
    assert [c for c in ScalpMomentum().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"] == []


def test_scan_is_empty_in_extreme_volatility():
    e = _engine()
    e.regime.trend, e.regime.volatility = "RANGE", "EXTREME_VOL"
    feats = _scalp_features("NSE:NIFTY", e, long=True)
    assert [c for c in ScalpMomentum().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"] == []


def test_scan_is_empty_very_late_in_the_session():
    e = _engine(at=dt.time(15, 10))
    e.regime.trend, e.regime.volatility = "RANGE", "NORMAL_VOL"
    feats = _scalp_features("NSE:NIFTY", e, long=True)
    assert [c for c in ScalpMomentum().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"] == []


def test_registered_in_all_strategies():
    from sthitaprajna.strategies.options import all_strategies
    assert any(isinstance(s, ScalpMomentum) for s in all_strategies())


def test_p_win_formula_can_now_clear_the_ev_margin_of_safety_gate():
    """D-060: the old 0.46-0.52 cap sat BELOW the ~52.6% breakeven win rate a 1:1 target_r implies under
    the standard loss model, so margin_ok was mathematically unreachable at ANY confirmation count --
    confirmed directly against the live EV engine, not assumed. The new 0.46-0.60 range must actually
    clear it at a realistic (not just the theoretical maximum) confirmation count, or the fix didn't fix
    anything the operator could feel."""
    from sthitaprajna.signal.ev import evaluate

    # the old ceiling: provably never passes, at its own best case
    old = evaluate(0.52, 1.0, cost_rupees=0.0, risk_rupees=100.0, min_net_r=0.0, min_gross_to_cost=1.0,
                   p_calibrated=False, gap_prob=0.04)
    assert not old.margin_ok

    # a realistic well-confirmed burst (3 real confirmations beyond the 2 that are always true): p_win 0.55
    realistic = min(0.46 + 0.03 * max(0, 5 - 2), 0.60)
    assert realistic == pytest.approx(0.55)
    e_realistic = evaluate(realistic, 1.0, cost_rupees=0.0, risk_rupees=100.0, min_net_r=0.0,
                           min_gross_to_cost=1.0, p_calibrated=False, gap_prob=0.04)
    assert e_realistic.margin_ok, "a realistically-confirmed burst must clear the gate, not just the maximum"

    # the new ceiling (all 8 unique confirmation factors firing) clears it with real headroom
    ceiling = min(0.46 + 0.03 * max(0, 8 - 2), 0.60)
    assert ceiling == pytest.approx(0.60)
    e_ceiling = evaluate(ceiling, 1.0, cost_rupees=0.0, risk_rupees=100.0, min_net_r=0.0,
                         min_gross_to_cost=1.0, p_calibrated=False, gap_prob=0.04)
    assert e_ceiling.margin_ok and e_ceiling.net_r > e_realistic.net_r
