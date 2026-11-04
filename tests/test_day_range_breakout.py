"""Sthitaprajna 2.0 (D-043): DayRangeBreakout -- a later, second-leg continuation break of the day's
own high/low, only once that level has already carried past the opening range."""
from __future__ import annotations

import datetime as dt

from sthitaprajna.domain import Candle, Direction, Segment
from sthitaprajna.engine.runtime import Engine, _FeatureCacheCtx
from sthitaprajna.strategies.base import Features
from sthitaprajna.strategies.options import DayRangeBreakout
from test_engine_e2e import CAL, DAY, RULES


def _candle(key, t0, i, o, h, l, c):
    return Candle(key, t0 + dt.timedelta(minutes=i), o, h, l, c, 5000)


def _engine(at: dt.time = dt.time(10, 30)) -> Engine:
    from sthitaprajna.config import load_operator
    e = Engine(load_operator(), RULES, CAL, seed=11, start=DAY, warmup_days=2)
    e.run_until(dt.datetime.combine(DAY, at, tzinfo=e.now.tzinfo), step_seconds=30)
    return e


def _ctx_with(e: Engine, feats: Features) -> _FeatureCacheCtx:
    ctx = _FeatureCacheCtx(e)
    ctx._feat_cache[feats.key] = feats
    return ctx


def _features(key: str, e: Engine, *, long: bool | None = True) -> Features:
    """24 filler day bars around px_mid, one bar carrying the day high/low past the opening range
    (+-20 from px_mid), plus a trigger bar. `long=None` builds a flat day with no extreme at all."""
    px_mid = e.market.states[key].price
    atr5 = 40.0
    or_high, or_low = px_mid + 20, px_mid - 20
    t = e.now.replace(second=0, microsecond=0)
    day = [_candle(key, t, i, px_mid, px_mid + 5, px_mid - 5, px_mid) for i in range(24)]
    if long is None:
        price = px_mid
    elif long:
        day[12] = _candle(key, t, 12, px_mid, px_mid + 60, px_mid - 5, px_mid + 55)
        price = px_mid + 65
    else:
        day[12] = _candle(key, t, 12, px_mid, px_mid + 5, px_mid - 60, px_mid - 55)
        price = px_mid - 65
    day.append(_candle(key, t, 24, px_mid, px_mid + 5, px_mid - 5, px_mid))
    c5 = [_candle(key, t, 100, price, price + 1, price - 1, price)]
    return Features(key=key, price=price, vwap=px_mid, atr1=atr5 / 2, atr5=atr5, or_high=or_high, or_low=or_low,
                    ema20=px_mid, ema50=px_mid, adx=28.0, rvol=2.0, c5=c5, day=day,
                    htf_up=bool(long), htf_down=(long is False), bar_id=f"{key}@dayrange")


def test_scan_fires_a_long_candidate_on_a_day_high_break_past_the_opening_range():
    e = _engine()
    e.regime.trend, e.regime.volatility = "TREND_UP", "NORMAL_VOL"
    feats = _features("NSE:NIFTY", e, long=True)
    out = [c for c in DayRangeBreakout().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"]
    assert out, "expected at least one candidate breaking the day high"
    for c in out:
        assert c.direction is Direction.LONG and c.bias is Direction.LONG
        assert c.segment is Segment.INDEX_OPTION and c.defined_risk and not c.sleeve
        assert not c.p_win_calibrated
        assert any(cf.factor == "price_action" and cf.name == "day_level_break" and cf.fired for cf in c.confirmations)


def test_scan_fires_a_short_candidate_on_a_day_low_break_past_the_opening_range():
    e = _engine()
    e.regime.trend, e.regime.volatility = "TREND_DOWN", "NORMAL_VOL"
    feats = _features("NSE:NIFTY", e, long=False)
    out = [c for c in DayRangeBreakout().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"]
    assert out, "expected at least one candidate breaking the day low"
    for c in out:
        assert c.bias is Direction.SHORT


def test_scan_is_empty_when_the_day_extreme_has_not_carried_past_the_opening_range():
    e = _engine()
    e.regime.trend, e.regime.volatility = "TREND_UP", "NORMAL_VOL"
    feats = _features("NSE:NIFTY", e, long=None)
    assert [c for c in DayRangeBreakout().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"] == []


def test_scan_is_empty_in_extreme_volatility():
    e = _engine()
    e.regime.trend, e.regime.volatility = "TREND_UP", "EXTREME_VOL"
    feats = _features("NSE:NIFTY", e, long=True)
    assert [c for c in DayRangeBreakout().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"] == []


def test_scan_is_empty_before_the_opening_range_settles():
    e = _engine(at=dt.time(9, 20))
    e.regime.trend, e.regime.volatility = "TREND_UP", "NORMAL_VOL"
    feats = _features("NSE:NIFTY", e, long=True)
    assert [c for c in DayRangeBreakout().scan(_ctx_with(e, feats)) if c.underlying_key == "NSE:NIFTY"] == []


def test_registered_in_all_strategies():
    from sthitaprajna.strategies.options import all_strategies
    assert any(isinstance(s, DayRangeBreakout) for s in all_strategies())
