"""Candlestick/chart-pattern primitives (D-041): bar-close, unvalidated-but-standard heuristics that
feed strategies' existing Confirmation lists."""
from __future__ import annotations

import datetime as dt

from sthitaprajna.domain import IST, Candle
from sthitaprajna.market import patterns as pat

T0 = dt.datetime(2026, 9, 29, 10, 0, tzinfo=IST)


def _c(o, h, l, c, i=0):
    return Candle("TEST", T0 + dt.timedelta(minutes=i), o, h, l, c, 1000)


def test_bullish_engulfing():
    prev = _c(100, 101, 97, 98)             # down bar
    cur = _c(97, 103, 96, 102)              # engulfs it, closes strong up
    assert pat.bullish_engulfing(prev, cur)
    assert not pat.bearish_engulfing(prev, cur)


def test_bearish_engulfing():
    prev = _c(98, 102, 97, 101)             # up bar
    cur = _c(102, 103, 96, 97)              # engulfs it, closes strong down
    assert pat.bearish_engulfing(prev, cur)
    assert not pat.bullish_engulfing(prev, cur)


def test_hammer_and_shooting_star():
    ham = _c(100, 100.3, 95, 100.2)         # small body near top, long lower wick, tiny upper wick
    assert pat.hammer(ham) and not pat.shooting_star(ham)
    star = _c(100, 105, 99.7, 99.8)         # small body near bottom, long upper wick, tiny lower wick
    assert pat.shooting_star(star) and not pat.hammer(star)


def test_doji_needs_a_small_body():
    d = _c(100, 102, 98, 100.1)
    assert pat.doji(d)
    trend_bar = _c(100, 106, 99, 105)
    assert not pat.doji(trend_bar)


def test_bullish_reversal_candle_accepts_any_signal():
    prev = _c(100, 101, 97, 98)
    ham = _c(96, 96.5, 91, 96.2)
    assert pat.bullish_reversal_candle(prev, ham)
    plain = _c(96, 97, 95, 96.3)
    assert not pat.bullish_reversal_candle(prev, plain)


def test_range_compression_and_breakout_pattern():
    wide = [_c(100 + i * 0.1, 103 + i * 0.1, 97 + i * 0.1, 100 + i * 0.1, i) for i in range(16)]
    tight = [_c(100 + i * 0.02, 100.3 + i * 0.02, 99.7 + i * 0.02, 100 + i * 0.02, 16 + i) for i in range(5)]
    squeeze = wide + tight
    assert pat.range_compression(squeeze, lookback=20, recent=5)
    thrust = _c(100.1, 106, 100, 105.5, 20)   # big expansion bar right after the squeeze
    assert pat.breakout_pattern(squeeze + [thrust], lookback=20, recent=5)
    assert not pat.breakout_pattern(squeeze + [_c(100.1, 100.4, 99.8, 100.2, 20)], lookback=20, recent=5)


def test_needs_enough_bars():
    short = [_c(100, 101, 99, 100, i) for i in range(5)]
    assert pat.range_compression(short) is False
    assert pat.breakout_pattern(short) is False
