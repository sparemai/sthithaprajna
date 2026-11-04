"""Feature primitives (A6.3). Bar-close only: callers pass *completed* candles, never a forming bar."""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import numpy as np

from sthitaprajna.domain import IST, Candle


def closes(c: Sequence[Candle]) -> np.ndarray:
    return np.fromiter((x.close for x in c), float, len(c))


def ema(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) == 0:
        return x
    a = 2.0 / (n + 1)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) < n:
        return np.full_like(x, np.nan, dtype=float)
    c = np.cumsum(np.insert(x.astype(float), 0, 0.0))
    out = np.full(len(x), np.nan)
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def true_range(c: Sequence[Candle]) -> np.ndarray:
    tr = np.empty(len(c))
    for i, b in enumerate(c):
        if i == 0:
            tr[i] = b.high - b.low
        else:
            pc = c[i - 1].close
            tr[i] = max(b.high - b.low, abs(b.high - pc), abs(b.low - pc))
    return tr


def atr(c: Sequence[Candle], n: int = 14) -> float:
    if len(c) < 2:
        return float(c[-1].high - c[-1].low) if c else 0.0
    tr = true_range(c)
    # Wilder smoothing
    a = tr[:n].mean() if len(tr) >= n else tr.mean()
    for v in tr[n:]:
        a = (a * (n - 1) + v) / n
    return float(a)


def rsi(x: np.ndarray, n: int = 14) -> float:
    if len(x) <= n:
        return 50.0
    d = np.diff(x)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au, ad = up[:n].mean(), dn[:n].mean()
    for u, v in zip(up[n:], dn[n:]):
        au = (au * (n - 1) + u) / n
        ad = (ad * (n - 1) + v) / n
    if ad == 0:
        return 100.0
    return float(100 - 100 / (1 + au / ad))


def adx(c: Sequence[Candle], n: int = 14) -> float:
    if len(c) < n + 2:
        return 0.0
    pdm, ndm = [], []
    for i in range(1, len(c)):
        upm = c[i].high - c[i - 1].high
        dnm = c[i - 1].low - c[i].low
        pdm.append(upm if upm > dnm and upm > 0 else 0.0)
        ndm.append(dnm if dnm > upm and dnm > 0 else 0.0)
    tr = true_range(c)[1:]
    pdm_a, ndm_a = np.array(pdm), np.array(ndm)
    atr_s = ema(tr, n)
    pdi = 100 * ema(pdm_a, n) / np.maximum(atr_s, 1e-12)
    ndi = 100 * ema(ndm_a, n) / np.maximum(atr_s, 1e-12)
    dx = 100 * np.abs(pdi - ndi) / np.maximum(pdi + ndi, 1e-12)
    return float(ema(dx, n)[-1])


IST_OFFSET_S = 19800           # IST is a fixed +05:30 offset (no DST) -> integer day/time arithmetic
SESSION_OPEN_S = 9 * 3600 + 15 * 60


def _epoch(b: Candle) -> int:
    return b.t


def _day_key(t: int) -> int:
    return (t + IST_OFFSET_S) // 86400


def _tod(t: int) -> int:
    return (t + IST_OFFSET_S) % 86400


def session_vwap(c: Sequence[Candle]) -> float:
    """VWAP of the current session (candles of the last date in `c`)."""
    if not c:
        return 0.0
    day = _day_key(_epoch(c[-1]))
    pv = v = 0.0
    for b in reversed(c):
        if _day_key(_epoch(b)) != day:
            break
        tp = (b.high + b.low + b.close) / 3
        pv += tp * b.volume
        v += b.volume
    return pv / v if v > 0 else c[-1].close


def session_candles(c: Sequence[Candle], day: dt.date | None = None) -> list[Candle]:
    if not c:
        return []
    if day is None:
        target = _day_key(_epoch(c[-1]))
        out = []
        for b in reversed(c):
            if _day_key(_epoch(b)) != target:
                break
            out.append(b)
        out.reverse()
        return out
    return [b for b in c if b.start.astimezone(IST).date() == day]


def opening_range(c: Sequence[Candle], minutes: int = 15) -> tuple[float, float] | None:
    s = session_candles(c)
    if not s:
        return None
    t0 = _epoch(s[0])
    rng = [b for b in s if _epoch(b) - t0 < minutes * 60]
    if len(s) <= len(rng):  # range not yet complete
        return None
    return max(b.high for b in rng), min(b.low for b in rng)


def relative_volume(c: Sequence[Candle], lookback_days: int = 5) -> float:
    """Today's cumulative volume vs. the same time-of-day on prior sessions (A6.2)."""
    if not c:
        return 1.0
    last = _epoch(c[-1])
    tod, today = _tod(last), _day_key(last)
    by_day: dict[int, float] = {}
    for b in c[-375 * (lookback_days + 1):]:
        t = _epoch(b)
        if _tod(t) <= tod:
            k = _day_key(t)
            by_day[k] = by_day.get(k, 0.0) + b.volume
    cur = by_day.pop(today, 0.0)
    prior = sorted(by_day.items())[-lookback_days:]
    if not prior:
        return 1.0
    base = float(np.mean([v for _, v in prior]))
    return cur / base if base > 0 else 1.0


def resample(c: Sequence[Candle], minutes: int) -> list[Candle]:
    """Aggregate 1-minute candles into N-minute candles aligned to the session open (09:15)."""
    out: list[Candle] = []
    cur: Candle | None = None
    cur_start = -1
    step = minutes * 60
    for b in c:
        t = _epoch(b)
        bs = t - ((_tod(t) - SESSION_OPEN_S) % step)
        if bs != cur_start:
            if cur is not None:
                out.append(cur)
            cur_start = bs
            cur = Candle(b.key, dt.datetime.fromtimestamp(bs, tz=IST), b.open, b.high, b.low, b.close, b.volume, b.oi, bs)
        else:
            if b.high > cur.high:
                cur.high = b.high
            if b.low < cur.low:
                cur.low = b.low
            cur.close = b.close
            cur.volume += b.volume
            cur.oi = b.oi
    if cur is not None:
        out.append(cur)
    return out


def supertrend(c: Sequence[Candle], n: int = 10, mult: float = 3.0) -> int:
    """+1 uptrend / -1 downtrend on the last bar."""
    if len(c) < n + 1:
        return 0
    tr = true_range(c)
    atr_s = ema(tr, n)
    trend = 1
    fub = flb = 0.0
    for i, b in enumerate(c):
        mid = (b.high + b.low) / 2
        ub, lb = mid + mult * atr_s[i], mid - mult * atr_s[i]
        if i == 0:
            fub, flb = ub, lb
            continue
        fub = ub if (ub < fub or c[i - 1].close > fub) else fub
        flb = lb if (lb > flb or c[i - 1].close < flb) else flb
        if trend == 1 and b.close < flb:
            trend = -1
        elif trend == -1 and b.close > fub:
            trend = 1
    return trend


def donchian(c: Sequence[Candle], n: int = 20) -> tuple[float, float]:
    w = c[-n:]
    return max(b.high for b in w), min(b.low for b in w)


def daily_closes(c: Sequence[Candle], lookback_days: int = 20) -> list[float]:
    """Last completed-bar close of each of the trailing `lookback_days` sessions (A6.7 realized vol input).
    Today's still-forming session is excluded -- only full days measure realized volatility fairly."""
    if not c:
        return []
    last_day = _day_key(_epoch(c[-1]))
    by_day: dict[int, float] = {}
    for b in c:
        d = _day_key(_epoch(b))
        if d == last_day:
            continue
        by_day[d] = b.close
    return [v for _, v in sorted(by_day.items())[-lookback_days:]]


def realized_vol(closes: Sequence[float], annualize_days: int = 252) -> float | None:
    """Annualized close-to-close realized volatility (A6.7), as a fraction (0.18 = 18%) to compare
    directly against ATM IV. None below 6 closes -- fewer than that is noise, not a volatility estimate."""
    if len(closes) < 6:
        return None
    x = np.asarray(closes, dtype=float)
    if np.any(x <= 0):
        return None
    r = np.diff(np.log(x))
    if len(r) == 0:
        return None
    return float(np.std(r, ddof=1) * np.sqrt(annualize_days))
