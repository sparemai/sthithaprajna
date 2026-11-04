"""Expected Value Engine (A9.6) and the D4 margin of safety.

EXPECTANCY = P_WIN x EXPECTED_WIN - P_LOSS x EXPECTED_LOSS - EXPECTED_COSTS   (all in R)

EXPECTED_LOSS includes a gap allowance: a fraction of losses gap through the stop (D12.1 uses 4% of
losses at -2.5R). The margin of safety requires net EV >= 0.10R AND gross edge >= 2x expected costs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class EVResult:
    p_win: float
    win_r: float
    loss_r: float
    cost_r: float
    gross_r: float
    net_r: float
    margin_ok: bool
    reason: str
    p_calibrated: bool

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def expected_loss_r(gap_prob: float = 0.04, gap_loss_r: float = 2.5, slippage_r: float = 0.05) -> float:
    """Average loss in R when a trade loses: most at -1R (+slippage), a few gap through."""
    return (1 - gap_prob) * (1.0 + slippage_r) + gap_prob * gap_loss_r


def evaluate(p_win: float, win_r: float, cost_rupees: float, risk_rupees: float, *, min_net_r: float,
             min_gross_to_cost: float, p_calibrated: bool, gap_prob: float = 0.04,
             gap_loss_r: float = 2.5, slippage_r: float = 0.05) -> EVResult:
    p = min(max(p_win, 0.0), 1.0)
    loss_r = expected_loss_r(gap_prob, gap_loss_r, slippage_r)
    cost_r = cost_rupees / risk_rupees if risk_rupees > 0 else float("inf")
    gross = p * win_r - (1 - p) * loss_r
    net = gross - cost_r
    reasons = []
    if net < min_net_r:
        reasons.append(f"net EV {net:+.2f}R < {min_net_r:.2f}R")
    if cost_r > 0 and gross < min_gross_to_cost * cost_r:
        reasons.append(f"gross edge {gross:.2f}R < {min_gross_to_cost:.0f}x costs ({cost_r:.2f}R)")
    ok = not reasons
    return EVResult(p, win_r, loss_r, cost_r, gross, net, ok, "; ".join(reasons) or "passes margin of safety",
                    p_calibrated)
