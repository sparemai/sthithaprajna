"""Hard ceilings (D12.2) -- enforced in code, changeable by no layer at runtime.

These are module-level constants on purpose: no config file, API call, Discord command, learning loop
or Aggression Layer component can change them while the process runs. Changing one is a code change
(operator decision, gate (e)) that shows up in review and in the git history.

operator.yaml is validated against these at load time, so Part D can only ever be *stricter*.
"""
from __future__ import annotations

from typing import Final

MAX_RISK_PER_TRADE: Final[float] = 0.050     # planned risk <= 5.0% of equity per trade (2.0, D-036: aggressive)
MAX_OPEN_RISK: Final[float] = 0.100          # total open risk <= 10.0% of equity (2.0, D-036)
HALT_DRAWDOWN: Final[float] = 0.28           # no new trades at >= 28% drawdown from peak (2.0, D-036; kept
                                              # below RUIN_FLOOR_PCT_MIN's 30%-drawdown-equivalent on purpose,
                                              # so the gear ladder halts before the ruin floor ever has to bind)
RUIN_FLOOR_ABS_MIN: Final[float] = 70_000.0  # ruin floor never below max(Rs 70,000, 70% of peak)
RUIN_FLOOR_PCT_MIN: Final[float] = 0.70
SEBI_OPS_THRESHOLD: Final[int] = 10          # system cap must stay strictly below this


def clamp_risk_fraction(fraction: float) -> float:
    """The last line of defence for per-trade risk: never above the ceiling, never negative."""
    if fraction != fraction:  # NaN guard -> refuse to size
        return 0.0
    return max(0.0, min(fraction, MAX_RISK_PER_TRADE))


def clamp_open_risk_limit(fraction: float) -> float:
    if fraction != fraction:
        return 0.0
    return max(0.0, min(fraction, MAX_OPEN_RISK))


def ruin_floor(all_time_peak: float, abs_floor: float = RUIN_FLOOR_ABS_MIN,
               pct_floor: float = RUIN_FLOOR_PCT_MIN) -> float:
    """max(Rs 70,000, 70% of all-time peak). Config may raise either term, never lower it."""
    return max(max(abs_floor, RUIN_FLOOR_ABS_MIN), max(pct_floor, RUIN_FLOOR_PCT_MIN) * all_time_peak)
