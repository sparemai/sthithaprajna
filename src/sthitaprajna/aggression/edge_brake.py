"""Edge Brake (D12.4): live evidence caps the gear. It only caps; it never raises R.

From trade `start_after_trades` (50): gear R <= max(floor_r, kelly_fraction x Kelly) over the last
`window` (150) trades, Kelly = p - (1-p)/b, p = win rate, b = avg win R / avg loss R.
From trade `expectancy_review_after` (100): point-estimate expectancy <= 0 -> cap at GUARD + review.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class EdgeBrakeResult:
    n: int
    active: bool
    ceiling_r: float | None
    win_rate: float | None
    payoff: float | None
    kelly: float | None
    expectancy_r: float | None
    cap_at_guard: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def kelly_fraction(p: float, b: float) -> float:
    if b <= 0:
        return -1.0
    return p - (1 - p) / b


def evaluate(r_outcomes: Sequence[float], cfg_edge: dict[str, float]) -> EdgeBrakeResult:
    n = len(r_outcomes)
    start = int(cfg_edge["start_after_trades"])
    window = int(cfg_edge["window"])
    floor_r = float(cfg_edge["floor_r"])
    frac = float(cfg_edge["kelly_fraction"])
    review_after = int(cfg_edge["expectancy_review_after"])
    if n < start:
        return EdgeBrakeResult(n, False, None, None, None, None, None, False,
                               f"inactive until {start} trades ({n} so far)")
    w = list(r_outcomes[-window:])
    wins = [x for x in w if x > 0]
    losses = [-x for x in w if x < 0]
    p = len(wins) / len(w)
    expectancy = sum(w) / len(w)
    if not losses:
        payoff, k, ceiling = None, None, None
        note = "no losses in window: no Kelly cap"
    else:
        avg_win = sum(wins) / len(wins) if wins else 0.0
        payoff = avg_win / (sum(losses) / len(losses))
        k = kelly_fraction(p, payoff)
        ceiling = max(floor_r, frac * k)
        note = f"quarter-Kelly ceiling {ceiling:.2%}"
    cap = n >= review_after and expectancy <= 0
    if cap:
        note += "; expectancy <= 0 -> capped at GUARD, edge review opened"
    return EdgeBrakeResult(n, True, ceiling, p, payoff, k, expectancy, cap, note)
