"""Black-76 on the forward for index options (A6.6), with Greeks and an implied-vol solver.

Pure-python math (no scipy) so it runs anywhere. Theta is per calendar day, vega per 1 vol point.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2 = math.sqrt(2.0)
INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_pdf(x: float) -> float:
    return INV_SQRT_2PI * math.exp(-0.5 * x * x)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float   # per calendar day
    vega: float    # per 1 vol point (0.01)
    vanna: float = 0.0
    charm: float = 0.0


def forward(spot: float, t: float, r: float, q: float = 0.0) -> float:
    return spot * math.exp((r - q) * t)


def price(f: float, k: float, t: float, sigma: float, r: float, is_call: bool) -> float:
    df = math.exp(-r * t)
    if t <= 0 or sigma <= 0:
        intrinsic = max(0.0, f - k) if is_call else max(0.0, k - f)
        return df * intrinsic
    st = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / st
    d2 = d1 - st
    if is_call:
        return df * (f * norm_cdf(d1) - k * norm_cdf(d2))
    return df * (k * norm_cdf(-d2) - f * norm_cdf(-d1))


def greeks(f: float, k: float, t: float, sigma: float, r: float, is_call: bool) -> Greeks:
    p = price(f, k, t, sigma, r, is_call)
    if t <= 0 or sigma <= 0:
        itm = (f > k) if is_call else (f < k)
        return Greeks(p, (1.0 if itm else 0.0) * (1 if is_call else -1), 0.0, 0.0, 0.0)
    df = math.exp(-r * t)
    sqt = math.sqrt(t)
    st = sigma * sqt
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / st
    d2 = d1 - st
    pdf = norm_pdf(d1)
    delta = df * norm_cdf(d1) if is_call else -df * norm_cdf(-d1)
    gamma = df * pdf / (f * st)
    vega = df * f * pdf * sqt / 100.0
    # Theta = -dV/dT = rV - DF * F * pdf(d1) * sigma / (2 sqrt T)  (same form for calls and puts)
    theta = (r * p - df * f * pdf * sigma / (2 * sqt)) / 365.0
    vanna = -df * pdf * d2 / sigma / 100.0
    # charm (delta change per calendar day passing) by finite difference -- display only
    one_day = 1.0 / 365.0
    if t > one_day:
        d1n = (math.log(f / k) + 0.5 * sigma * sigma * (t - one_day)) / (sigma * math.sqrt(t - one_day))
        dfn = math.exp(-r * (t - one_day))
        delta_next = dfn * norm_cdf(d1n) if is_call else -dfn * norm_cdf(-d1n)
        charm = delta_next - delta
    else:
        charm = 0.0
    return Greeks(p, delta, gamma, theta, vega, vanna, charm)


def implied_vol(target: float, f: float, k: float, t: float, r: float, is_call: bool,
                lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-6, max_iter: int = 100) -> float | None:
    """Bisection with a Newton polish. Returns None when the price is outside no-arbitrage bounds."""
    if t <= 0 or target <= 0:
        return None
    df = math.exp(-r * t)
    intrinsic = df * (max(0.0, f - k) if is_call else max(0.0, k - f))
    upper = df * (f if is_call else k)
    if not (intrinsic - 1e-9 <= target <= upper):
        return None
    a, b = lo, hi
    pa = price(f, k, t, a, r, is_call) - target
    for _ in range(max_iter):
        m = 0.5 * (a + b)
        pm = price(f, k, t, m, r, is_call) - target
        if abs(pm) < tol:
            return m
        if (pa < 0) == (pm < 0):
            a, pa = m, pm
        else:
            b = m
    return 0.5 * (a + b)
