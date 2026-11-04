"""Sthitaprajna 2.0 (D-041): IndexRangeReversion -- the RANGE-regime strategy the other three (which all
need a trend or an expiry event) deliberately leave empty."""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from sthitaprajna.domain import Direction, Segment
from sthitaprajna.engine.runtime import Engine, _FeatureCacheCtx
from sthitaprajna.strategies.base import Features
from sthitaprajna.strategies.options import IndexRangeReversion
from test_engine_e2e import CAL, DAY, RULES


def _range_features(key: str, e: Engine, *, low_side: bool = True) -> Features:
    """22 5-min bars: 10 filler bars (outside the 12-bar range window the strategy looks at), then a
    bar that touches the far extreme (fixing the window's width), 9 more mid-range filler bars, and a
    rejection candle on the final bar at the extreme under test."""
    px_mid = e.market.states[key].price
    atr5 = 40.0
    hi, lo = px_mid + 40, px_mid - 40           # 80-wide range == 2.0x atr5, passes the 1.2x floor
    t = e.now.replace(second=0, microsecond=0)
    far = hi if low_side else lo                # the window's OTHER extreme, touched early so width holds
    bars = [_candle(key, t, i, px_mid, px_mid + 1, px_mid - 1, px_mid) for i in range(11)]
    bars.append(_candle(key, t, 11, px_mid, max(px_mid, far), min(px_mid, far), px_mid))
    bars += [_candle(key, t, 12 + i, px_mid, px_mid + 1, px_mid - 1, px_mid) for i in range(9)]
    if low_side:
        # hammer at the low: small body near the top, long lower wick down to the range low
        bars.append(_candle(key, t, 22, lo + 15, lo + 16, lo, lo + 13))
        price = lo + 13
    else:
        # shooting star at the high: small body near the bottom, long upper wick up to the range high
        bars.append(_candle(key, t, 22, hi - 10, hi, hi - 14, hi - 13))
        price = hi - 13
    return Features(key=key, price=price, vwap=px_mid, atr1=atr5 / 2, atr5=atr5, or_high=None, or_low=None,
                    ema20=px_mid, ema50=px_mid, adx=12.0, rvol=1.0, c5=bars, day=bars,
                    htf_up=False, htf_down=False, bar_id=f"{key}@range")


def _candle(key, t0, i, o, h, l, c):
    from sthitaprajna.domain import Candle
    return Candle(key, t0 + dt.timedelta(minutes=5 * i), o, h, l, c, 5000)


def _z2h_engine() -> Engine:
    from sthitaprajna.config import load_operator
    e = Engine(load_operator(), RULES, CAL, seed=5, start=DAY, warmup_days=2)
    e.run_until(dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=e.now.tzinfo), step_seconds=30)
    return e


def _ctx_with(e: Engine, feats: Features) -> _FeatureCacheCtx:
    ctx = _FeatureCacheCtx(e)
    ctx._feat_cache[feats.key] = feats
    return ctx


def test_scan_is_empty_when_trend_strength_is_high():
    """D-060: the top-level gate reads `trend_strength` (the same ADX `trend_regime()` classifies on)
    directly now, not the binary RANGE label -- a genuinely strong trend (ADX >= 25) still blocks it."""
    e = _z2h_engine()
    e.regime.trend, e.regime.trend_strength, e.regime.volatility = "TREND_UP", 30.0, "LOW_VOL"
    feats = _range_features("NSE:NIFTY", e, low_side=True)
    assert IndexRangeReversion().scan(_ctx_with(e, feats)) == []


def test_scan_admits_a_mildly_trending_session_now():
    """D-060: widened from RANGE-only to trend_strength < 25 -- a TREND_UP label with ADX just above
    trend_regime()'s own 22 cutoff (still "mildly trending", not a real trend) is now admitted."""
    e = _z2h_engine()
    e.regime.trend, e.regime.trend_strength, e.regime.volatility = "TREND_UP", 23.0, "LOW_VOL"
    feats = _range_features("NSE:NIFTY", e, low_side=True)
    assert IndexRangeReversion().scan(_ctx_with(e, feats)), "expected a candidate in a mild, ADX<25 trend"


def test_scan_is_empty_only_in_extreme_volatility_now():
    """D-060: loosened from (LOW_VOL, NORMAL_VOL)-only to "anything but EXTREME_VOL", matching the card's
    own fails_when (which only ever named EXTREME_VOL, not HIGH_VOL)."""
    e = _z2h_engine()
    e.regime.trend, e.regime.trend_strength, e.regime.volatility = "RANGE", 10.0, "EXTREME_VOL"
    feats = _range_features("NSE:NIFTY", e, low_side=True)
    assert IndexRangeReversion().scan(_ctx_with(e, feats)) == []


def test_scan_now_trades_in_high_volatility():
    e = _z2h_engine()
    e.regime.trend, e.regime.trend_strength, e.regime.volatility = "RANGE", 10.0, "HIGH_VOL"
    feats = _range_features("NSE:NIFTY", e, low_side=True)
    assert IndexRangeReversion().scan(_ctx_with(e, feats)), "expected a candidate in HIGH_VOL (D-060)"


def test_scan_is_empty_when_adx_is_not_low():
    """D-060: the per-underlying ADX ceiling widened from 20 to 28 -- 25 now passes, 30 still doesn't."""
    e = _z2h_engine()
    e.regime.trend, e.regime.trend_strength, e.regime.volatility = "RANGE", 10.0, "LOW_VOL"
    feats = dataclasses.replace(_range_features("NSE:NIFTY", e, low_side=True), adx=30.0)
    assert IndexRangeReversion().scan(_ctx_with(e, feats)) == []


def test_scan_fades_the_range_low_with_a_bullish_candidate():
    e = _z2h_engine()
    e.regime.trend, e.regime.trend_strength, e.regime.volatility = "RANGE", 10.0, "LOW_VOL"
    feats = _range_features("NSE:NIFTY", e, low_side=True)
    out = IndexRangeReversion().scan(_ctx_with(e, feats))
    assert out, "expected at least one candidate fading the range low"
    for c in out:
        assert c.direction is Direction.LONG and c.bias is Direction.LONG
        assert c.segment is Segment.INDEX_OPTION and c.defined_risk and not c.sleeve
        assert c.target_r == pytest.approx(1.8)
        assert not c.p_win_calibrated
        assert any(cf.factor == "price_action" and cf.fired for cf in c.confirmations)


def test_scan_fades_the_range_high_with_a_bearish_candidate():
    e = _z2h_engine()
    e.regime.trend, e.regime.trend_strength, e.regime.volatility = "RANGE", 10.0, "NORMAL_VOL"
    feats = _range_features("NSE:NIFTY", e, low_side=False)
    out = IndexRangeReversion().scan(_ctx_with(e, feats))
    assert out, "expected at least one candidate fading the range high"
    for c in out:
        assert c.bias is Direction.SHORT
        assert any(cf.factor == "price_action" and cf.fired for cf in c.confirmations)


def test_scan_skips_a_range_too_tight_to_trade():
    e = _z2h_engine()
    e.regime.trend, e.regime.trend_strength, e.regime.volatility = "RANGE", 10.0, "LOW_VOL"
    key = "NSE:NIFTY"
    px_mid = e.market.states[key].price
    atr5 = 40.0
    t = e.now.replace(second=0, microsecond=0)
    # every bar stays within +-5 of px_mid: window width (10) is well under the D-060 0.8x atr5 (32) floor
    bars = [_candle(key, t, i, px_mid, px_mid + 5, px_mid - 5, px_mid) for i in range(22)]
    feats = Features(key=key, price=px_mid, vwap=px_mid, atr1=atr5 / 2, atr5=atr5, or_high=None, or_low=None,
                     ema20=px_mid, ema50=px_mid, adx=12.0, rvol=1.0, c5=bars, day=bars,
                     htf_up=False, htf_down=False, bar_id=f"{key}@tight")
    assert IndexRangeReversion().scan(_ctx_with(e, feats)) == []


def test_registered_in_all_strategies():
    from sthitaprajna.strategies.options import all_strategies
    assert any(isinstance(s, IndexRangeReversion) for s in all_strategies())
