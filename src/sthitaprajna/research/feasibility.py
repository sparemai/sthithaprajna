"""Phase 0 exit criterion: the Rs 1 lakh feasibility table at ATTACK gear.

For each Tier-1 structure: one-lot premium/margin and stop risk in Rs, and whether it fits the planned
risk for grades A+ / A / B (D12.5), including whether two-lot staging fits (AGG-2.2).

Premiums come from the SIM Black-76 chain (VIX-linked) -- a MODEL, not Dhan's margin calculator. The
go-live dossier must replace these with Dhan's real margin API numbers [VERIFY].
"""
from __future__ import annotations

from typing import Any

from sthitaprajna.aggression.sizer import planned_risk_fraction
from sthitaprajna.config import OperatorConfig
from sthitaprajna.domain import Exchange, Gear, Grade, OptionType, Segment, Side
from sthitaprajna.market.charges import ChargesModel


def feasibility_table(e, cfg: OperatorConfig, equity: float | None = None,
                      premium_stop_pcts: tuple[float, ...] = (0.20, 0.30)) -> dict[str, Any]:
    equity = equity or cfg.starting_capital
    tier = cfg.tiers[0]
    charges: ChargesModel = e.charges
    budgets = {g.value: planned_risk_fraction(cfg, tier, Gear.ATTACK, g, 0.0, None) * equity
               for g in (Grade.A_PLUS, Grade.A, Grade.B)}
    rows: list[dict[str, Any]] = []
    for und, exch, delta_targets in (("NIFTY", Exchange.NSE, (0.60, 0.50, 0.35)),
                                     ("SENSEX", Exchange.BSE, (0.60, 0.50, 0.35)),
                                     ("BANKNIFTY", Exchange.NSE, (0.50,))):
        exps = e.market.expiries(und)
        fetch = getattr(e.market, "chain_blocking", None)
        spot, chain = fetch(und, exps[0]) if fetch else e.market.chain(und, exps[0])
        if not chain:
            continue                       # live chain not available yet: omit rather than guess
        lot = e.market.option_instrument(und, exps[0], chain[0].strike, OptionType.CE).lot_size
        for d in delta_targets:
            row = min(chain, key=lambda r: abs(r.ce_delta - d))
            prem = row.ce_ask
            for sp in premium_stop_pcts:
                stop_pts = prem * sp
                rt = charges.round_trip(Segment.INDEX_OPTION, prem, prem - stop_pts, lot, exch)
                one_lot_risk = stop_pts * lot + rt
                rows.append({
                    "structure": f"{und} {'weekly' if und != 'BANKNIFTY' else 'monthly'} CE ~{d:.2f} delta",
                    "expiry": exps[0].isoformat(), "strike": row.strike, "lot": lot, "premium": round(prem, 2),
                    "premium_outlay": round(prem * lot, 2), "stop": f"{sp:.0%} of premium",
                    "one_lot_risk": round(one_lot_risk, 2),
                    **{f"fits_{g}": one_lot_risk <= b for g, b in budgets.items()},
                    "two_lot_staging_fits_A": 2 * one_lot_risk <= budgets["A"],
                    "margin_pct_equity": round(prem * lot / equity, 4),
                })
    # cash equities: sized precisely (lot 1) -- limited by the 1x single-notional cap
    for key in ("NSE:RELIANCE", "NSE:HDFCBANK", "NSE:TATASTEEL"):
        q = e.market.quote(key)
        px = (q.ask if q.ask > 0 else q.ltp) if q else 0.0
        if px <= 0:
            continue
        stop_pts = px * 0.008
        qty_cap = int(equity * float(cfg.limits["cash_intraday_single_notional_x"]) // px)
        per_unit = stop_pts + charges.round_trip(Segment.EQ_INTRADAY, px, px - stop_pts, qty_cap, Exchange.NSE) / max(qty_cap, 1)
        rows.append({
            "structure": f"{key.split(':')[1]} cash intraday (0.8% stop)", "expiry": None, "strike": None, "lot": 1,
            "premium": round(px, 2), "premium_outlay": round(qty_cap * px * 0.2, 2), "stop": "0.8% of price",
            "one_lot_risk": round(per_unit, 2), "max_qty_1x_notional": qty_cap,
            "risk_at_cap": round(per_unit * qty_cap, 2),
            **{f"fits_{g}": per_unit <= b for g, b in budgets.items()},
            "two_lot_staging_fits_A": True, "margin_pct_equity": round(qty_cap * px * 0.2 / equity, 4),
        })
    live = e.market.source.value != "SIM"
    return {"label": ("LIVE DHAN PREMIUMS -- margins still modelled; confirm with Dhan's margin API before go-live [VERIFY]"
                      if live else "SIM MODEL -- replace with Dhan margin API numbers before go-live [VERIFY]"),
            "equity": equity, "gear": "ATTACK", "budgets": {k: round(v, 2) for k, v in budgets.items()},
            "rows": rows, "sell_side_stt_check": charges.compute(Segment.INDEX_OPTION, Side.SELL, 100.0, 65).to_dict()}
