"""Candlestick and chart-pattern primitives (A6.1: order blocks, failed breakouts -- popular, unvalidated).

Bar-close only, same discipline as indicators.py: callers pass completed candles, never a forming bar.
Every pattern here is a widely-used heuristic, not a proven edge -- they exist to feed the Confirmation
lists strategies already build (A9.2: independent confirmations), never to trade alone.
"""
from __future__ import annotations

from collections.abc import Sequence

from sthitaprajna.domain import Candle
from sthitaprajna.market import indicators as ind


def _body(c: Candle) -> float:
    return abs(c.close - c.open)


def _range(c: Candle) -> float:
    return max(c.high - c.low, 1e-9)


def _upper_wick(c: Candle) -> float:
    return c.high - max(c.open, c.close)


def _lower_wick(c: Candle) -> float:
    return min(c.open, c.close) - c.low


def bullish_engulfing(prev: Candle, cur: Candle) -> bool:
    """cur's real body fully engulfs prev's, reversing a down bar into a strong up bar."""
    return (prev.close < prev.open and cur.close > cur.open
           and cur.open <= prev.close and cur.close >= prev.open and _body(cur) > _body(prev))


def bearish_engulfing(prev: Candle, cur: Candle) -> bool:
    return (prev.close > prev.open and cur.close < cur.open
           and cur.open >= prev.close and cur.close <= prev.open and _body(cur) > _body(prev))


def hammer(c: Candle, wick_mult: float = 2.0) -> bool:
    """Small body near the top of the range, a long lower wick: rejection of lower prices."""
    b = _body(c)
    return _lower_wick(c) >= wick_mult * max(b, 1e-9) and _upper_wick(c) <= b


def shooting_star(c: Candle, wick_mult: float = 2.0) -> bool:
    """Small body near the bottom of the range, a long upper wick: rejection of higher prices."""
    b = _body(c)
    return _upper_wick(c) >= wick_mult * max(b, 1e-9) and _lower_wick(c) <= b


def doji(c: Candle, body_frac: float = 0.1) -> bool:
    """Body is a small fraction of the bar's range: indecision, often a precursor to reversal."""
    return _body(c) <= body_frac * _range(c)


def bullish_reversal_candle(prev: Candle, cur: Candle) -> bool:
    """Any one of the standard bullish reversal signals on the current bar (A6.1)."""
    return hammer(cur) or bullish_engulfing(prev, cur) or (doji(cur) and cur.close > cur.open)


def bearish_reversal_candle(prev: Candle, cur: Candle) -> bool:
    return shooting_star(cur) or bearish_engulfing(prev, cur) or (doji(cur) and cur.close < cur.open)


def range_compression(c: Sequence[Candle], lookback: int = 20, recent: int = 5, factor: float = 0.7) -> bool:
    """True range compressing: the last `recent` bars' average true range is well below the
    `lookback` bars' average -- a classic pre-breakout squeeze (A6.1, popular but unvalidated)."""
    if len(c) < lookback + 1:
        return False
    tr = ind.true_range(c[-(lookback + 1):])
    base, tight = tr[:-recent].mean(), tr[-recent:].mean()
    return base > 0 and tight <= factor * base


def breakout_pattern(c: Sequence[Candle], lookback: int = 20, recent: int = 5, factor: float = 0.7) -> bool:
    """Range compression immediately followed by the current bar expanding beyond it: the
    squeeze-then-thrust shape a breakout strategy wants to see before trusting a break."""
    if len(c) < lookback + 2:
        return False
    if not range_compression(c[:-1], lookback, recent, factor):
        return False
    tr = ind.true_range(c[-(lookback + 1):])
    return bool(tr[-1] > tr[:-1].mean())
