"""Index-option hypotheses: directional momentum (A7.3 "Index options, directional") and the
Zero-to-Hero expiry sleeve (A8, D8). Long premium only -- naked selling is disabled (D0)."""
from __future__ import annotations

from sthitaprajna.domain import (
    Candidate, Confirmation, Direction, HalfLife, HypothesisCard, OptionType, Segment, new_id,
)
from sthitaprajna.market import indicators as ind
from sthitaprajna.market import patterns as pat
from sthitaprajna.market.options import ChainRow, atm_strike, iv_rank, iv_rv_spread, pcr, pin_risk, summary
from sthitaprajna.strategies.base import Strategy, StrategyContext, clamp01, compute_features

UNDERLYINGS = (("NIFTY", "NSE:NIFTY"), ("SENSEX", "BSE:SENSEX"), ("BANKNIFTY", "NSE:BANKNIFTY"))
ROLL_DAYS = 3        # stock options: calendar days before expiry when the next month is used (assumption, VERIFY)


def _pick_strike(rows: list[ChainRow], call: bool, target_delta: float, max_spread: float = 0.03) -> ChainRow | None:
    best, best_d = None, 9.0
    for r in rows:
        d = r.ce_delta if call else -r.pe_delta
        bid, ask, ltp = (r.ce_bid, r.ce_ask, r.ce_ltp) if call else (r.pe_bid, r.pe_ask, r.pe_ltp)
        if ltp <= 0.5 or ask <= 0 or (ask - bid) / max(ltp, 1e-9) > max_spread:
            continue
        if abs(d - target_delta) < best_d:
            best, best_d = r, abs(d - target_delta)
    return best


def _pick_contract(ctx: StrategyContext, underlying: str, call: bool, target_delta: float,
                   max_spread: float = 0.03):
    """Pick one tradable contract per direction across the available expiry list.

    General strategies use future-dated index contracts on every trading day, leaving
    same-day gamma bets to ZeroToHero. Stocks retain their physical-settlement buffer.
    Try later expiries when a chain, qualifying strike or executable quote is missing.
    Chain reads are cached/nonblocking in LIVE; a pending chain is retried next scan.
    """
    today = ctx.now.date()
    min_days = 0 if underlying in {name for name, _ in UNDERLYINGS} else ROLL_DAYS
    for expiry in sorted(set(ctx.expiries(underlying))):
        if (expiry - today).days <= min_days:
            continue
        spot, rows = ctx.chain(underlying, expiry)
        row = _pick_strike(rows, call, target_delta, max_spread)
        if row is None:
            continue
        inst = ctx.option_instrument(underlying, expiry, row.strike,
                                     OptionType.CE if call else OptionType.PE)
        q = ctx.quote(inst.key)
        if q is None or q.bid <= 0 or q.ask < q.bid or q.ask <= 0:
            continue
        if (q.ask - q.bid) / max(q.ltp, 1e-9) > max_spread:
            continue
        return expiry, spot, rows, row, inst, q
    return None


def _reversal_confirmation(ctx: StrategyContext, key: str, direction: Direction) -> Confirmation:
    """D-045: extra evidence, never a forced flip -- a candidate whose direction is the opposite side of
    a recent stop-out on the same underlying (REVERSAL_WATCH_MINUTES) gets this confirmation; everything
    else about the candidate must already pass its strategy's own normal gates independently."""
    watch = ctx.reversal_watch(key)
    fired = watch is not None and watch is direction
    detail = (f"opposite-direction stop-out on this underlying within the watch window"
              if fired else "no recent reversal context")
    return Confirmation("reversal_setup", "trend", fired, detail)


def _oi_confirmation(rows: list[ChainRow], spot: float, long: bool) -> Confirmation:
    above = [r for r in rows if r.strike > spot][:3]
    below = [r for r in rows if r.strike < spot][-3:]
    if long:
        ok = sum(r.ce_oi_chg for r in above) < 0 or sum(r.pe_oi_chg for r in below) > 0
        return Confirmation("oi_writers", "oi", ok, "call writers covering above / put writers adding below")
    ok = sum(r.pe_oi_chg for r in below) < 0 or sum(r.ce_oi_chg for r in above) > 0
    return Confirmation("oi_writers", "oi", ok, "put writers covering below / call writers adding above")


def _iv_confirmation(ctx: StrategyContext, underlying: str, rows: list[ChainRow], spot: float) -> Confirmation:
    """D-055: IV rank (D-039), read at entry for the first time -- these strategies all buy premium, and
    paying for already-rich IV right before it mean-reverts erodes P&L even when the direction call is
    right. EOD IV snapshots are index-only and need 5+ sessions (data/live.py's own freshness bar for
    everything else in this file); like `_reversal_confirmation`, no evidence means it does not fire --
    never a vacuous pass for a stock underlying or a fresh deployment with no history yet."""
    hist = ctx.iv_history(underlying)
    if len(hist) < 5:
        return Confirmation("iv_not_rich", "volatility", False, "no IV history yet")
    k = atm_strike(rows, spot)
    atm = next((r for r in rows if r.strike == k), None)
    if atm is None:
        return Confirmation("iv_not_rich", "volatility", False, "no ATM row")
    rank = iv_rank((atm.ce_iv + atm.pe_iv) / 2, hist)
    ok = rank is not None and rank <= 0.70
    return Confirmation("iv_not_rich", "volatility", ok, f"IV rank {rank:.0%}" if rank is not None else "IV rank unavailable")


def _iv_rv_confirmation(ctx: StrategyContext, key: str, rows: list[ChainRow], spot: float) -> Confirmation:
    """D-057: implied-vs-realized volatility spread (D-039's iv_rv_spread), read at entry for the first
    time. A different question from `_iv_confirmation`'s IV rank: not "is IV rich against its OWN recent
    history" but "is IV rich against what the underlying has ACTUALLY been doing" -- a large positive
    spread means paying a premium priced for movement that hasn't shown up in the realized tape yet. 0.08
    (8 vol points) is a starting threshold, not a calibrated one, same honesty as `_iv_confirmation`'s 0.70
    rank cutoff -- Phase 5 calibration replaces both once there's real evidence. No evidence (fewer than 6
    daily closes, e.g. a fresh deployment) means it does not fire, same convention as every other
    D-055/D-056 confirmation here -- never a vacuous pass."""
    closes = ind.daily_closes(ctx.candles(key), 20)
    rv = ind.realized_vol(closes)
    if rv is None:
        return Confirmation("iv_not_overpriced_vs_realized", "volatility", False, "not enough daily closes yet")
    k = atm_strike(rows, spot)
    atm = next((r for r in rows if r.strike == k), None)
    if atm is None:
        return Confirmation("iv_not_overpriced_vs_realized", "volatility", False, "no ATM row")
    spread = iv_rv_spread((atm.ce_iv + atm.pe_iv) / 2, rv)
    ok = spread is not None and spread <= 0.08
    detail = f"IV-RV spread {spread:+.1%}" if spread is not None else "unavailable"
    return Confirmation("iv_not_overpriced_vs_realized", "volatility", ok, detail)


def _pcr_confirmation(rows: list[ChainRow], long: bool) -> Confirmation:
    """D-056: put-call ratio by OI, read at entry for the first time. A fresh long into an already very
    low PCR (<0.7: call-heavy, a crowded-bullish extreme) or a fresh short into an already very high PCR
    (>1.3: put-heavy, a crowded-bearish extreme) is entering alongside the exact positioning that tends to
    unwind first in a reversal -- the standard contrarian reading of a PCR extreme. 0.7-1.3 is treated as
    neutral either way, same as a balanced market."""
    p = pcr(rows)["oi"]
    ok = p >= 0.7 if long else p <= 1.3
    return Confirmation("pcr_not_extreme", "oi", ok, f"PCR(OI) {p:.2f}")


def _pin_risk_confirmation(rows: list[ChainRow], spot: float, dte_days: float) -> Confirmation:
    """D-055: expiry pin-risk proxy (D-039/A6.6), read at entry for the first time. Spot pinned near the
    chain's highest-combined-OI strike close to expiry tends to grind into the pin rather than complete a
    fresh directional move -- caution, not a hard block (the proxy can't see dealer/writer hedging flows
    directly). Inactive (and so confirms cleanly) on every day that isn't close to expiry."""
    pr = pin_risk(rows, spot, dte_days)
    detail = f"spot within {pr['distance_pct']:.2%} of the {pr['strike']:g} pin" if pr["active"] else "no active pin"
    return Confirmation("no_pin_risk", "pin_risk", not pr["active"], detail)


class IndexOptionMomentum(Strategy):
    name = "index_option_momentum"
    version = "0.3.0"
    card = HypothesisCard(
        strategy=name, version=version,
        why="On trend days, index breaks of the opening range with option writers covering at the nearest OI wall tend to extend; long ATM/slightly ITM premium captures it with defined risk.",
        counterparty="Option writers forced to cover as the wall breaks; late premium sellers.",
        not_arbitraged_because="Needs OI + breadth confirmation and tight execution; theta makes most breakout buys lose.",
        works_when="TREND_UP/TREND_DOWN index regimes, VIX stable or rising, breadth aligned.",
        fails_when="Range/pinning days, IV crush, late-session theta.",
        decay="Must be re-validated on the Sep-2025+ expiry structure (Nifty Tue, Sensex Thu).",
        holding_period="15-120 minutes", data_needed="Index 1-min bars, option chain OI/IV (real-time)",
        kill_criteria="Rolling 60-trade expectancy <= 0 after costs; slippage > 2x model.",
    )

    def scan(self, ctx: StrategyContext) -> list[Candidate]:
        mins = ctx.cal.minutes_into_session(ctx.now)
        if not (25 <= mins <= 255) or ctx.regime.volatility == "EXTREME_VOL":
            return []
        out: list[Candidate] = []
        b = ctx.breadth()
        for und, key in UNDERLYINGS:
            f = compute_features(ctx, key)
            if f is None or f.or_high is None or f.or_low is None or f.atr5 <= 0:
                continue
            last5 = f.c5[-1]
            for direction in (Direction.LONG, Direction.SHORT):
                long = direction is Direction.LONG
                level = f.or_high if long else f.or_low
                near = abs(f.price - level) <= 0.4 * f.atr5 or (f.price > level if long else f.price < level)
                if not near or abs(f.price - level) > 1.2 * f.atr5:
                    continue
                trigger = (last5.close > level) if long else (last5.close < level)
                contract = _pick_contract(ctx, und, long, 0.55)
                if contract is None:
                    continue
                expiry, spot, rows, row, inst, q = contract
                delta = abs(row.ce_delta if long else row.pe_delta)
                theta = abs(row.ce_theta if long else row.pe_theta)
                und_stop = max(abs(f.price - (f.or_high + f.or_low) / 2), 0.8 * f.atr5)
                opt_stop_dist = delta * und_stop + theta / 6.0          # ~1 hour of decay allowance
                opt_stop_dist = min(max(opt_stop_dist, 0.08 * q.ask), 0.40 * q.ask)
                entry = q.ask
                stop = round(max(0.05, entry - opt_stop_dist), 2)
                vix_ok = ctx.regime.vix_change > -0.05
                dte_days = ctx.cal.trading_minutes_to(ctx.now, expiry) / 375.0
                confs = [
                    _oi_confirmation(rows, spot, long),
                    Confirmation("breadth_aligned", "breadth", (b["ad_ratio"] > 1.15) if long else (b["ad_ratio"] < 0.87),
                                 f"A/D {b['advancers']}/{b['decliners']}"),
                    Confirmation("htf_structure", "trend", f.htf_up if long else f.htf_down, "15m EMA20 vs EMA50"),
                    Confirmation("vol_supportive", "volatility", vix_ok, f"VIX change {ctx.regime.vix_change:+.1%}"),
                    Confirmation("vwap_acceptance", "flow", f.above_vwap if long else not f.above_vwap, "index vs VWAP"),
                    Confirmation("breakout_pattern", "price_action", pat.breakout_pattern(f.c5),
                                 "range compression then expansion into the break"),
                    _reversal_confirmation(ctx, key, direction),
                    _iv_confirmation(ctx, und, rows, spot),
                    _iv_rv_confirmation(ctx, key, rows, spot),
                    _pin_risk_confirmation(rows, spot, dte_days),
                    _pcr_confirmation(rows, long),
                ]
                n = len({c.factor for c in confs if c.fired})
                s = summary(rows, spot)
                trend_fit = ctx.regime.trend == ("TREND_UP" if long else "TREND_DOWN")
                out.append(Candidate(
                    id=new_id("sig"), ts=ctx.now, strategy=self.name, strategy_version=self.version, instrument=inst,
                    underlying_key=key, direction=Direction.LONG, segment=Segment.INDEX_OPTION, entry=round(entry, 2),
                    stop=stop, target_r=2.0, p_win=min(0.40 + 0.015 * max(0, n - 2), 0.45), p_win_calibrated=False,
                    half_life=HalfLife.MINUTES, trigger_fired=trigger, confirmations=confs,
                    quality_features={
                        "regime": 0.85 if trend_fit else 0.45, "trend": clamp01(f.adx / 40), "volume": 0.5,
                        "volatility": 0.75 if vix_ok else 0.35, "breadth": 1.0 if confs[1].fired else 0.3,
                        "participant": 0.8 if confs[0].fired else 0.35,
                        "liquidity": clamp01(1 - q.spread_bps / 300), "execution": clamp01(1 - q.spread_bps / 400),
                        "event": 1.0,
                    },
                    regime_fit=True, defined_risk=True, bias=direction,
                    setup_id=f"{self.name}:{und}:{ctx.now:%Y%m%d}:{direction.value}", signal_bar=f.bar_id,
                    explanation=(f"{und} {'breaks above' if long else 'breaks below'} OR {level:.0f}; buy {inst.symbol} "
                                 f"(delta {delta:.2f}); PCR {s['pcr']['oi']:.2f}, straddle ±{s['straddle']['pct']:.2%}"),
                    model_confidence=0.5, regime_confidence=0.75 if trend_fit else 0.5, liquidity_score=0.7,
                    atr=opt_stop_dist / 1.2, beta=1.0,
                ))
        return out


# D8/A8: EV-based strike selection across delta bands, not one fixed target -- "by measured EV across
# delta bands ... not by payoff fantasy". p_win multipliers are starting PRIORS, not evidence: Phase 4/5
# walk-forward must replace them with per-band calibrated p_win before any of this counts as evidence
# (A2, A12). The regime split reflects the regime's own definition (regime.py): GAMMA_ACCELERATION is
# straddle expansion + rising VIX, which should reward convexity (deepest OTM) most; TREND_EXPIRY is a
# steadier directional grind, which should reward the nearer, higher-probability strikes more.
Z2H_BANDS = (
    # (label, target_delta, payoff_r, p_win multiplier by expiry regime)
    ("NEAR", 0.25, 2.2, {"GAMMA_ACCELERATION": 1.15, "BREAKOUT_EXPIRY": 1.20, "TREND_EXPIRY": 1.30}),
    ("MID",  0.18, 4.0, {"GAMMA_ACCELERATION": 1.00, "BREAKOUT_EXPIRY": 1.00, "TREND_EXPIRY": 1.00}),
    ("DEEP", 0.10, 7.5, {"GAMMA_ACCELERATION": 0.85, "BREAKOUT_EXPIRY": 0.70, "TREND_EXPIRY": 0.55}),
)


def _z2h_band_scan(ctx: StrategyContext, und: str, expiry, rows: list[ChainRow], long: bool,
                   budget: float) -> list[dict]:
    """One record per `Z2H_BANDS` entry, for both `_best_z2h_pick`'s EV comparison and the operator-facing
    "why no candidate" diagnostic (D-058) -- the same lookup read for two purposes, so the diagnostic
    always reflects exactly what the picker itself saw rather than a generic guess. `ok=False` records
    carry a `reason` naming exactly which gate failed (no strike near this delta within the spread filter,
    no live quote, or unaffordable -- with the actual strike/ask/cost numbers for the last one)."""
    ot = OptionType.CE if long else OptionType.PE
    out = []
    for label, target_delta, payoff_r, mult in Z2H_BANDS:
        row = _pick_strike(rows, long, target_delta, max_spread=0.25)
        if row is None:
            out.append({"label": label, "ok": False,
                       "reason": "no strike near this delta passes the price/spread filter"})
            continue
        inst = ctx.option_instrument(und, expiry, row.strike, ot)
        q = ctx.quote(inst.key)
        if q is None or q.ask <= 0:
            out.append({"label": label, "ok": False, "reason": f"strike {row.strike:g} has no live quote"})
            continue
        cost = q.ask * inst.lot_size
        if cost > budget:            # D8 affordability: one lot must fit the sleeve's per-expiry budget
            out.append({"label": label, "ok": False, "reason": f"strike {row.strike:g} @ {q.ask:.2f} x "
                       f"lot {inst.lot_size} = Rs {cost:,.0f} > Rs {budget:,.0f} budget"})
            continue
        out.append({"label": label, "ok": True, "row": row, "inst": inst, "q": q, "target_delta": target_delta,
                    "payoff_r": payoff_r, "mult": mult})
    return out


def _best_z2h_pick(ctx: StrategyContext, und: str, expiry, rows: list[ChainRow], long: bool, base_p: float,
                   regime: str, budget: float) -> dict | None:
    """Prices every band's nearest strike and keeps the one with the highest expected value in R (max
    loss = 1R = the premium paid), among strikes that are affordable within the sleeve budget and pass
    the spread filter. Returns None if nothing in any band qualifies."""
    best = None
    for b in _z2h_band_scan(ctx, und, expiry, rows, long, budget):
        if not b["ok"]:
            continue
        p_win = min(0.45, base_p * b["mult"].get(regime, 1.0))
        ev_r = p_win * b["payoff_r"] - (1 - p_win)
        if best is None or ev_r > best["ev_r"]:
            best = {"label": b["label"], "row": b["row"], "inst": b["inst"], "q": b["q"], "p_win": p_win,
                   "ev_r": ev_r, "payoff_r": b["payoff_r"], "delta": b["target_delta"]}
    return best


class ZeroToHero(Strategy):
    """Expiry-afternoon convex sleeve. Most tickets expire worthless; EV decides, not payoff fantasy (A8)."""
    name = "zero_to_hero"
    version = "0.1.0"
    card = HypothesisCard(
        strategy=name, version=version,
        why="On expiry afternoons with gamma acceleration or an OI-wall break, far-OTM options can reprice many-fold.",
        counterparty="Expiry-day premium sellers underpricing tail moves.",
        not_arbitraged_because="Rarely works; most sellers win most days. Edge, if any, is regime-conditional.",
        works_when="GAMMA_ACCELERATION / BREAKOUT_EXPIRY regimes.", fails_when="Pinning, volatility crush (most expiries).",
        decay="Unknown -- must be proven on the current expiry structure.", holding_period="Minutes to expiry close",
        data_needed="Expiry-day chain, straddle, VIX (real-time)",
        kill_criteria="Cumulative sleeve loss > 6% of equity or trailing 12-week expectancy < 0 (D8).",
    )

    def scan(self, ctx: StrategyContext) -> list[Candidate]:
        mins = ctx.cal.minutes_into_session(ctx.now)
        if not (225 <= mins <= 330):
            return []
        regime = ctx.regime.expiry
        if regime not in ("GAMMA_ACCELERATION", "BREAKOUT_EXPIRY", "TREND_EXPIRY"):
            return []
        out = []
        for und, key in UNDERLYINGS[:2]:
            exps = ctx.expiries(und)
            if not exps or exps[0] != ctx.now.date():
                # D-058: `ctx.regime.expiry` is computed off whichever index is actually expiring today
                # (engine._update_regime), so the regime check above can pass while THIS underlying in
                # the loop isn't the one expiring -- make that visible rather than a silent no-op.
                nearest = f"nearest expiry {exps[0]:%d %b}" if exps else "no expiries found"
                ctx.log(f"zero_to_hero: {und} not its own expiry today ({regime} is {ctx.now.date():%d %b}'s "
                       f"regime for another index) -- {nearest} -- skipped")
                continue
            f = compute_features(ctx, key)
            if f is None:
                ctx.log(f"zero_to_hero: {und} eligible ({regime}) but not enough bar history yet -- skipped")
                continue
            long = f.price > f.vwap
            spot, rows = ctx.chain(und, exps[0])
            base_p = 0.26 if regime == "GAMMA_ACCELERATION" else 0.20
            budget = float(ctx.sleeve_cfg()["per_expiry"]) * ctx.equity_sod
            pick = _best_z2h_pick(ctx, und, exps[0], rows, long, base_p, regime, budget)
            if pick is None:
                if not rows:
                    reason = "no live chain for today's expiry"
                else:
                    # D-058: per-band, not a generic catch-all -- names exactly which gate blocked each of
                    # NEAR/MID/DEEP (no strike near that delta, no quote, or unaffordable with the actual
                    # strike/ask/cost), so a large-lot underlying's premiums pricing above the sleeve
                    # budget on a strong trend day (the likely case on a big, missed move) shows up as
                    # hard numbers next time, not a guess.
                    bands = _z2h_band_scan(ctx, und, exps[0], rows, long, budget)
                    reason = " | ".join(f"{b['label']}: {b['reason']}" for b in bands)
                ctx.log(f"zero_to_hero: {und} eligible ({regime}, {'long' if long else 'short'} bias) "
                       f"but no candidate -- {reason}")
                continue
            inst, q = pick["inst"], pick["q"]
            confs = [
                Confirmation("expiry_regime", "volatility", True, regime),
                Confirmation("trend", "trend", ctx.regime.trend != "RANGE", ctx.regime.trend),
                Confirmation("vix_rising", "volatility", ctx.regime.vix_change > 0, f"VIX change {ctx.regime.vix_change:+.1%}"),
                _oi_confirmation(rows, spot, long),
            ]
            out.append(Candidate(
                id=new_id("sig"), ts=ctx.now, strategy=self.name, strategy_version=self.version, instrument=inst,
                underlying_key=key, direction=Direction.LONG, segment=Segment.INDEX_OPTION, entry=q.ask,
                stop=0.05, target_r=pick["payoff_r"], p_win=pick["p_win"], p_win_calibrated=False,
                half_life=HalfLife.MINUTES, trigger_fired=True, confirmations=confs,
                quality_features={"regime": 0.7, "volatility": 0.6, "liquidity": clamp01(1 - q.spread_bps / 2000), "event": 1.0},
                defined_risk=True, sleeve=True, bias=Direction.LONG if long else Direction.SHORT,
                setup_id=f"{self.name}:{und}:{ctx.now:%Y%m%d}", signal_bar=f.bar_id,
                explanation=(f"Zero-to-Hero {inst.symbol} at {q.ask:.2f} in {regime}: {pick['label']} band "
                            f"(delta ~{pick['delta']:.2f}) won on EV ({pick['ev_r']:+.2f}R, p_win {pick['p_win']:.0%} "
                            f"vs {pick['payoff_r']:.1f}R payoff -- UNVALIDATED prior)"),
                model_confidence=0.35, regime_confidence=0.5, liquidity_score=0.4,
            ))
        return out


class StockOptionMomentum(Strategy):
    """Booming F&O stocks expressed through their options (operator decision D-031: allowed at Tier 1).

    Long premium only (loss capped at the premium paid), intraday, and never a contract within ROLL_DAYS of its
    expiry (stock F&O settles physically). Stop and target come from the stock's breakout structure via delta.
    """
    name = "stock_option_momentum"
    version = "0.2.0"
    card = HypothesisCard(
        strategy=name, version=version,
        why="A stock breaking its opening range on heavy relative volume, with sector and market aligned, often "
            "extends for the rest of the move; a near-ATM option captures it with a defined maximum loss.",
        counterparty="Option writers and range faders caught short gamma as the stock trends.",
        not_arbitraged_because="Needs volume + flow confirmation and tight execution; stock-option spreads are wide "
                               "and theta punishes weak breaks, so most naive buys lose.",
        works_when="Stocks the scanner ranks as booming (RVOL >= 1.5, breakout, VWAP acceptance) on trend days.",
        fails_when="Failed breakouts, IV crush after news, illiquid strikes, late-session theta.",
        decay="Unknown; UNVALIDATED -- to be measured in PAPER before any conclusion.",
        holding_period="15-120 minutes", data_needed="Stock 1-min bars, stock option chain (Dhan), RVOL",
        kill_criteria="Rolling 60-trade expectancy <= 0 after costs, or slippage > 2x model.",
    )

    def scan(self, ctx: StrategyContext) -> list[Candidate]:
        mins = ctx.cal.minutes_into_session(ctx.now)
        if not (25 <= mins <= 255) or ctx.regime.volatility == "EXTREME_VOL":
            return []
        out: list[Candidate] = []
        for und, key in ctx.option_underlyings():
            f = compute_features(ctx, key)
            if f is None or f.or_high is None or f.or_low is None or f.atr5 <= 0 or f.rvol < 1.5:
                continue
            last5 = f.c5[-1]
            for direction in (Direction.LONG, Direction.SHORT):
                long = direction is Direction.LONG
                level = f.or_high if long else f.or_low
                beyond = f.price > level if long else f.price < level
                if not beyond or abs(f.price - level) > 1.5 * f.atr5:
                    continue
                trigger = (last5.close > level) if long else (last5.close < level)
                contract = _pick_contract(ctx, und, long, 0.55, max_spread=0.06)
                if contract is None:
                    continue
                expiry, spot, rows, row, inst, q = contract
                delta = abs(row.ce_delta if long else row.pe_delta)
                theta = abs(row.ce_theta if long else row.pe_theta)
                und_stop = max(abs(f.price - (f.or_high + f.or_low) / 2), 0.8 * f.atr5)
                opt_stop = min(max(delta * und_stop + theta / 6.0, 0.10 * q.ask), 0.40 * q.ask)
                sector = ctx.instrument(key).sector
                sec = ctx.sector_change(sector) if sector not in ("", "OTHER") else 0.0
                b = ctx.breadth()
                dte_days = ctx.cal.trading_minutes_to(ctx.now, expiry) / 375.0
                confs = [
                    Confirmation("relative_volume", "volume", f.rvol >= 1.5, f"RVOL {f.rvol:.2f}"),
                    Confirmation("vwap_acceptance", "flow", f.above_vwap if long else not f.above_vwap, "stock vs VWAP"),
                    Confirmation("htf_structure", "trend", f.htf_up if long else f.htf_down, "15m EMA20 vs EMA50"),
                    Confirmation("sector_aligned", "sector", (sec > 0.001) if long else (sec < -0.001), f"sector {sec:+.2%}"),
                    Confirmation("breadth_aligned", "breadth", (b["ad_ratio"] > 1.15) if long else (b["ad_ratio"] < 0.87),
                                 f"A/D {b['advancers']}/{b['decliners']}"),
                    Confirmation("breakout_pattern", "price_action", pat.breakout_pattern(f.c5),
                                 "range compression then expansion into the break"),
                    _reversal_confirmation(ctx, key, direction),
                    _iv_confirmation(ctx, und, rows, spot),
                    _iv_rv_confirmation(ctx, key, rows, spot),
                    _pin_risk_confirmation(rows, spot, dte_days),
                    _pcr_confirmation(rows, long),
                ]
                n = len({c.factor for c in confs if c.fired})
                out.append(Candidate(
                    id=new_id("sig"), ts=ctx.now, strategy=self.name, strategy_version=self.version, instrument=inst,
                    underlying_key=key, direction=Direction.LONG, segment=Segment.STOCK_OPTION, entry=round(q.ask, 2),
                    stop=round(max(0.05, q.ask - opt_stop), 2), target_r=2.0,
                    p_win=min(0.40 + 0.015 * max(0, n - 2), 0.45), p_win_calibrated=False,
                    half_life=HalfLife.MINUTES, trigger_fired=trigger, confirmations=confs,
                    quality_features={
                        "regime": 0.8 if ctx.regime.trend != "RANGE" else 0.5, "trend": clamp01(f.adx / 40),
                        "volume": clamp01((f.rvol - 0.6) / 1.4), "volatility": 0.7, "breadth": 1.0 if confs[4].fired else 0.3,
                        "participant": clamp01((f.rvol - 0.8) / 1.5), "liquidity": clamp01(1 - q.spread_bps / 400),
                        "execution": clamp01(1 - q.spread_bps / 500), "event": 1.0,
                    },
                    regime_fit=True, defined_risk=True, bias=direction,
                    setup_id=f"{self.name}:{und}:{ctx.now:%Y%m%d}:{direction.value}", signal_bar=f.bar_id,
                    explanation=(f"{und} {'breaks above' if long else 'breaks below'} OR {level:.2f} on RVOL {f.rvol:.1f}; "
                                 f"buy {inst.symbol} (delta {delta:.2f}, lot {inst.lot_size})"),
                    model_confidence=0.5, regime_confidence=0.6, liquidity_score=0.5, atr=opt_stop / 1.2,
                    beta=ctx.instrument(key).beta,
                ))
        return out


class IndexRangeReversion(Strategy):
    """Fades index range extremes back toward the mean (A7.3 "Mean reversion", D-041). The one gap the
    other two index/stock strategies deliberately leave open: they need TREND regimes and stand down
    in RANGE; this strategy exists only for RANGE, and only in calm volatility.

    D-060, OPERATOR DECISION: loosened on request after a month of PAPER trading produced zero trades --
    this account's sessions were trend-heavy (the other strategies' many winners depended on exactly that),
    leaving few strict-RANGE windows. Three changes, each still grounded in the strategy's own hypothesis
    rather than arbitrary: (1) the top-level gate now reads `trend_strength` (the same ADX value
    `trend_regime()` itself classifies on) directly instead of requiring the binary RANGE label, admitting
    RANGE days plus mildly-trending ones (ADX up to 25, vs RANGE's own <22 cutoff) -- still "low ADX",
    just a wider band on the same signal. (2) volatility now excludes only EXTREME_VOL, matching what the
    card's own fails_when already said (EXTREME_VOL, not HIGH_VOL) -- the code was stricter than the
    documented hypothesis. (3) the per-underlying range-width and near-extreme filters are both widened.
    None of this is a validated edge; it is untested evidence the EV math still has to earn, same as
    every other hypothesis card here."""
    name = "index_range_reversion"
    version = "0.3.0"
    card = HypothesisCard(
        strategy=name, version=version,
        why="In a range-bound, low-ADX session, price that overextends to the session range's edge with a "
            "rejection candle and put/call-writer defence tends to mean-revert toward VWAP before it breaks out.",
        counterparty="Late breakout chasers buying/selling right at the range extreme, stopped out on the revert.",
        not_arbitraged_because="Needs a genuine RANGE regime, a real rejection candle and OI defence together; "
                               "most range 'fades' are just breakouts that haven't happened yet.",
        works_when="RANGE trend regime or a mildly-trending one (ADX < 25), any volatility short of EXTREME_VOL "
                   "(D-060: loosened from RANGE-only, LOW/NORMAL-vol-only after a month with zero trades).",
        fails_when="The range breaks (this is exactly the failure mode a breakout strategy profits from); "
                   "EXTREME_VOL sessions, where 'ranges' are usually just a pause mid-trend; a genuinely "
                   "trending session wrongly let through by the widened ADX < 25 band.",
        decay="Unknown -- must be proven on the current expiry structure, same as the other two. D-060's "
              "loosening is an operator decision to generate evidence, not itself evidence of an edge -- "
              "watch whether the wider admission window just produces more losing fades into real trends.",
        holding_period="15-90 minutes (mean reversion targets are closer than breakout targets)",
        data_needed="Index 1-min bars, option chain OI (real-time)",
        kill_criteria="Rolling 60-trade expectancy <= 0 after costs; slippage > 2x model.",
    )

    def scan(self, ctx: StrategyContext) -> list[Candidate]:
        mins = ctx.cal.minutes_into_session(ctx.now)
        if not (25 <= mins <= 255):
            return []
        if ctx.regime.trend_strength >= 25 or ctx.regime.volatility == "EXTREME_VOL":
            return []
        out: list[Candidate] = []
        b = ctx.breadth()
        for und, key in UNDERLYINGS:
            f = compute_features(ctx, key)
            if f is None or len(f.c5) < 22 or f.atr5 <= 0 or f.adx >= 28:
                continue
            window = f.c5[-12:]                                    # last 60 minutes defines "the range"
            hi, lo = max(x.high for x in window), min(x.low for x in window)
            if hi - lo < 0.8 * f.atr5:                              # too tight to be a tradeable range
                continue
            rsi = ind.rsi(ind.closes(f.c5), 14)
            prev, last = f.c5[-2], f.c5[-1]
            for direction in (Direction.LONG, Direction.SHORT):
                long = direction is Direction.LONG
                extreme = lo if long else hi
                near_extreme = abs(f.price - extreme) <= 0.5 * f.atr5    # D-060: widened from 0.35x
                if not near_extreme:
                    continue
                rejected = pat.bullish_reversal_candle(prev, last) if long else pat.bearish_reversal_candle(prev, last)
                if not rejected:
                    continue
                trigger = (last.close > lo + 0.15 * f.atr5) if long else (last.close < hi - 0.15 * f.atr5)
                contract = _pick_contract(ctx, und, long, 0.50, max_spread=0.04)
                if contract is None:
                    continue
                expiry, spot, rows, row, inst, q = contract
                delta = abs(row.ce_delta if long else row.pe_delta)
                theta = abs(row.ce_theta if long else row.pe_theta)
                mid = (hi + lo) / 2
                und_stop = max(abs(extreme - f.price) + 0.2 * f.atr5, 0.5 * f.atr5)
                opt_stop_dist = delta * und_stop + theta / 6.0
                opt_stop_dist = min(max(opt_stop_dist, 0.08 * q.ask), 0.35 * q.ask)
                entry = q.ask
                stop = round(max(0.05, entry - opt_stop_dist), 2)
                rsi_ok = rsi <= 35 if long else rsi >= 65
                dte_days = ctx.cal.trading_minutes_to(ctx.now, expiry) / 375.0
                confs = [
                    Confirmation("range_regime", "regime", True, f"ADX {f.adx:.0f}, range {hi - lo:.1f} ({(hi - lo) / f.atr5:.1f}x ATR5)"),
                    Confirmation("reversal_candle", "price_action", True,
                                "bullish reversal at range low" if long else "bearish reversal at range high"),
                    Confirmation("rsi_extreme", "momentum", rsi_ok, f"RSI {rsi:.0f}"),
                    _oi_confirmation(rows, spot, long),
                    Confirmation("breadth_aligned", "breadth", (b["ad_ratio"] > 1.05) if long else (b["ad_ratio"] < 0.95),
                                 f"A/D {b['advancers']}/{b['decliners']}"),
                    _iv_confirmation(ctx, und, rows, spot),
                    _iv_rv_confirmation(ctx, key, rows, spot),
                    _pin_risk_confirmation(rows, spot, dte_days),
                    _pcr_confirmation(rows, long),
                ]
                n = len({c.factor for c in confs if c.fired})
                out.append(Candidate(
                    id=new_id("sig"), ts=ctx.now, strategy=self.name, strategy_version=self.version, instrument=inst,
                    underlying_key=key, direction=Direction.LONG, segment=Segment.INDEX_OPTION, entry=round(entry, 2),
                    stop=stop, target_r=1.8, p_win=min(0.42 + 0.015 * max(0, n - 2), 0.48), p_win_calibrated=False,
                    half_life=HalfLife.MINUTES, trigger_fired=trigger, confirmations=confs,
                    quality_features={
                        "regime": 0.75, "trend": clamp01(1 - f.adx / 20), "volume": 0.5,
                        "volatility": 0.7 if ctx.regime.volatility == "LOW_VOL" else 0.55,
                        "breadth": 1.0 if confs[4].fired else 0.3, "participant": 0.7 if confs[3].fired else 0.35,
                        "liquidity": clamp01(1 - q.spread_bps / 300), "execution": clamp01(1 - q.spread_bps / 400),
                        "event": 1.0,
                    },
                    regime_fit=True, defined_risk=True, bias=direction,
                    setup_id=f"{self.name}:{und}:{ctx.now:%Y%m%d}:{direction.value}", signal_bar=f.bar_id,
                    explanation=(f"{und} rejects the {'range low' if long else 'range high'} "
                                 f"{extreme:.0f} (range {lo:.0f}-{hi:.0f}) back toward mid {mid:.0f}; "
                                 f"buy {inst.symbol} (delta {delta:.2f}); RSI {rsi:.0f}"),
                    model_confidence=0.45, regime_confidence=0.65, liquidity_score=0.65,
                    atr=opt_stop_dist / 1.2, beta=1.0,
                ))
        return out


class DayRangeBreakout(Strategy):
    """Trades a break of the running SESSION high/low once that level has already carried past the
    opening range (D-043) -- a later, second-leg continuation signal distinct from the opening-range
    breaks IndexOptionMomentum/StockOptionMomentum trade in the first 25 minutes. Covers both index
    underlyings and scanner-promoted F&O stocks. This is the "day high/day low break" idea the operator
    asked about; previous-day-high and 52-week breaks are scanner-screening criteria only (README) and
    are not yet wired into any strategy's own entry trigger."""
    name = "day_range_breakout"
    version = "0.2.0"
    card = HypothesisCard(
        strategy=name, version=version,
        why="A fresh day high (or day low) that has already carried past the opening range, on relative "
            "volume and trend structure aligned, is a second-leg continuation signal distinct from the "
            "first-hour opening-range break -- late momentum that shakes out range/mean-reversion fades.",
        counterparty="Range and mean-reversion traders fading the level, and first-leg profit-takers.",
        not_arbitraged_because="Needs the day high/low to already exceed the opening range plus RVOL and "
                               "trend confirmation together; most bare new-high prints are noise inside "
                               "an existing range and would whipsaw a naive breakout rule.",
        works_when="TREND_UP/TREND_DOWN regimes, after the opening range settles, RVOL elevated.",
        fails_when="Range regimes, exhaustion/blow-off reversals right at the new high/low, late-session theta.",
        decay="Unknown -- UNVALIDATED, to be measured in PAPER same as every other strategy here.",
        holding_period="15-120 minutes", data_needed="1-min bars for the session so far, option chain OI/IV",
        kill_criteria="Rolling 60-trade expectancy <= 0 after costs; slippage > 2x model.",
    )

    def scan(self, ctx: StrategyContext) -> list[Candidate]:
        mins = ctx.cal.minutes_into_session(ctx.now)
        if not (40 <= mins <= 255) or ctx.regime.volatility == "EXTREME_VOL":
            return []
        index_names = {n for n, _ in UNDERLYINGS}
        out: list[Candidate] = []
        b = ctx.breadth()
        for und, key in list(UNDERLYINGS) + list(ctx.option_underlyings()):
            is_index = und in index_names
            f = compute_features(ctx, key)
            if f is None or f.or_high is None or f.or_low is None or f.atr5 <= 0 or len(f.day) < 20:
                continue
            if not is_index and f.rvol < 1.5:
                continue
            prior = f.day[:-1]
            day_hi, day_lo = max(c.high for c in prior), min(c.low for c in prior)
            last5 = f.c5[-1]
            for direction in (Direction.LONG, Direction.SHORT):
                long = direction is Direction.LONG
                level = day_hi if long else day_lo
                # only a level that has already carried past the opening range counts as a second-leg break
                if (day_hi <= f.or_high) if long else (day_lo >= f.or_low):
                    continue
                beyond = f.price > level if long else f.price < level
                if not beyond or abs(f.price - level) > 1.2 * f.atr5:
                    continue
                trigger = (last5.close > level) if long else (last5.close < level)
                contract = _pick_contract(ctx, und, long, 0.55, max_spread=0.05)
                if contract is None:
                    continue
                expiry, spot, rows, row, inst, q = contract
                delta = abs(row.ce_delta if long else row.pe_delta)
                theta = abs(row.ce_theta if long else row.pe_theta)
                und_stop = max(abs(f.price - level), 0.8 * f.atr5)
                opt_stop = min(max(delta * und_stop + theta / 6.0, 0.10 * q.ask), 0.40 * q.ask)
                segment = Segment.INDEX_OPTION if is_index else Segment.STOCK_OPTION
                dte_days = ctx.cal.trading_minutes_to(ctx.now, expiry) / 375.0
                confs = [
                    Confirmation("day_level_break", "price_action", True,
                                 f"{'day high' if long else 'day low'} {level:.2f} already past the opening range"),
                    Confirmation("relative_volume", "volume", f.rvol >= 1.3, f"RVOL {f.rvol:.2f}"),
                    Confirmation("htf_structure", "trend", f.htf_up if long else f.htf_down, "15m EMA20 vs EMA50"),
                    _oi_confirmation(rows, spot, long),
                    Confirmation("breadth_aligned", "breadth", (b["ad_ratio"] > 1.1) if long else (b["ad_ratio"] < 0.9),
                                 f"A/D {b['advancers']}/{b['decliners']}"),
                    Confirmation("breakout_pattern", "price_action", pat.breakout_pattern(f.c5),
                                 "range compression then expansion into the break"),
                    _reversal_confirmation(ctx, key, direction),
                    _iv_confirmation(ctx, und, rows, spot),
                    _iv_rv_confirmation(ctx, key, rows, spot),
                    _pin_risk_confirmation(rows, spot, dte_days),
                    _pcr_confirmation(rows, long),
                ]
                n = len({c.factor for c in confs if c.fired})
                out.append(Candidate(
                    id=new_id("sig"), ts=ctx.now, strategy=self.name, strategy_version=self.version, instrument=inst,
                    underlying_key=key, direction=Direction.LONG, segment=segment, entry=round(q.ask, 2),
                    stop=round(max(0.05, q.ask - opt_stop), 2), target_r=2.2,
                    p_win=min(0.40 + 0.015 * max(0, n - 2), 0.46), p_win_calibrated=False,
                    half_life=HalfLife.MINUTES, trigger_fired=trigger, confirmations=confs,
                    quality_features={
                        "regime": 0.8 if ctx.regime.trend != "RANGE" else 0.4, "trend": clamp01(f.adx / 40),
                        "volume": clamp01((f.rvol - 0.6) / 1.4), "volatility": 0.7,
                        "breadth": 1.0 if confs[4].fired else 0.3, "participant": 1.0 if confs[3].fired else 0.35,
                        "liquidity": clamp01(1 - q.spread_bps / 400), "execution": clamp01(1 - q.spread_bps / 500),
                        "event": 1.0,
                    },
                    regime_fit=True, defined_risk=True, bias=direction,
                    setup_id=f"{self.name}:{und}:{ctx.now:%Y%m%d}:{direction.value}", signal_bar=f.bar_id,
                    explanation=(f"{und} {'breaks the day high' if long else 'breaks the day low'} {level:.2f} "
                                 f"(already past the opening range); buy {inst.symbol} (delta {delta:.2f})"),
                    model_confidence=0.5, regime_confidence=0.6, liquidity_score=0.55, atr=opt_stop / 1.2,
                    beta=ctx.instrument(key).beta if not is_index else 1.0,
                ))
        return out


class ScalpMomentum(Strategy):
    """Small, quick moves (D-048) -- the other strategies all wait for a structural level (opening range,
    day high/low, a range extreme) to break or hold; this one trades a short 1-3 minute directional burst
    on its own, anywhere in the session, in any regime except EXTREME_VOL (deliberately including RANGE,
    where the other momentum/breakout strategies stand down). Tight stop, tight target, higher-delta near-
    ATM strike so the option actually moves on a small underlying move -- a deep-OTM strike (like the other
    strategies sometimes use) barely responds to a 0.3-ATR wiggle, which defeats the point here."""
    name = "scalp_momentum"
    version = "0.4.0"   # D-060: raised the p_win ceiling -- the strategy could mathematically never clear
                        # the EV margin-of-safety gate at its old 0.52 cap (see card below)
    card = HypothesisCard(
        strategy=name, version=version,
        why="A short, genuine 1-3 minute directional burst (not just noise) near VWAP, on modest relative "
            "volume, tends to continue for a few more minutes even when the larger-timeframe trend is flat "
            "or ranging -- small, frequent moves captured with a tight stop and a tight target, rather than "
            "waiting for one big breakout.",
        counterparty="Scalpers and market-makers on the other side of the micro-move, and traders who faded "
                     "the burst expecting an immediate reversion.",
        not_arbitraged_because="Needs a genuine burst (three consecutive same-direction 1-minute closes with "
                               "an accelerating bar) plus VWAP proximity (to keep the stop tight) together; "
                               "most apparent 1-minute 'bursts' are noise that reverses within a bar or two.",
        works_when="Any regime except EXTREME_VOL, including RANGE -- deliberately the strategy that keeps "
                   "trading small moves when the other four are waiting for a bigger setup.",
        fails_when="Choppy noise that looks like a burst but isn't, EXTREME_VOL whipsaws, or a wide option "
                   "spread that eats a target this small. D-051 loosened the rvol/acceleration/VWAP-proximity "
                   "thresholds to fire more often, which raises exactly this false-positive risk -- the strike "
                   "spread filter (cost protection) was deliberately left untouched.",
        decay="Unknown -- UNVALIDATED, to be measured in PAPER same as every other strategy here. D-060, "
              "OPERATOR DECISION not a calibration: the 0.46-0.52 p_win range this strategy shipped with (its "
              "own 1:1 target_r) sits BELOW the ~52.6% breakeven win rate the EV engine's standard loss model "
              "implies -- margin_ok was mathematically unreachable at ANY confirmation count, so this strategy "
              "produced zero trades in a month regardless of real signal quality (verified against the live "
              "ev.evaluate() thresholds, not assumed). Raised to 0.46-0.60 on the operator's explicit "
              "instruction, with NO new evidence that well-confirmed bursts actually win more than 52% of the "
              "time -- watch this strategy's own rolling expectancy closer than the others once it has traded "
              "enough to have one; it is the single most likely candidate for the kill criteria below to fire "
              "for real.",
        holding_period="2-15 minutes", data_needed="1-minute bars (already collected), option chain OI/IV",
        kill_criteria="Rolling 60-trade expectancy <= 0 after costs; slippage > 2x model.",
    )

    def scan(self, ctx: StrategyContext) -> list[Candidate]:
        mins = ctx.cal.minutes_into_session(ctx.now)
        if not (25 <= mins <= 350) or ctx.regime.volatility == "EXTREME_VOL":
            return []
        index_names = {n for n, _ in UNDERLYINGS}
        out: list[Candidate] = []
        b = ctx.breadth()
        for und, key in list(UNDERLYINGS) + list(ctx.option_underlyings()):
            is_index = und in index_names
            f = compute_features(ctx, key)
            if f is None or f.atr1 <= 0 or len(f.day) < 6:
                continue
            if not is_index and f.rvol < 1.0:   # D-051: loosened from 1.1 so more genuine bursts qualify
                continue
            bars = f.day[-4:]              # last 3 closes form the burst; the bar before that sets the baseline
            for direction in (Direction.LONG, Direction.SHORT):
                long = direction is Direction.LONG
                closes_rising = bars[1].close < bars[2].close < bars[3].close
                closes_falling = bars[1].close > bars[2].close > bars[3].close
                burst = closes_rising if long else closes_falling
                if not burst:
                    continue
                last_body = abs(bars[3].close - bars[3].open)
                baseline_body = max(abs(bars[0].close - bars[0].open), 0.01)
                if last_body < 1.1 * baseline_body:            # genuine acceleration, not just drift (D-051: was 1.2)
                    continue
                if abs(f.price - f.vwap) > 1.3 * f.atr1:        # already overextended: risk is no longer tight (D-051: was 1.0)
                    continue
                contract = _pick_contract(ctx, und, long, 0.60, max_spread=0.05 if is_index else 0.06)
                if contract is None:
                    continue
                expiry, spot, rows, row, inst, q = contract
                delta = abs(row.ce_delta if long else row.pe_delta)
                theta = abs(row.ce_theta if long else row.pe_theta)
                und_stop = max(0.6 * f.atr1, 0.1)
                opt_stop_dist = delta * und_stop + theta / 10.0   # a short hold needs less decay allowance
                opt_stop_dist = min(max(opt_stop_dist, 0.06 * q.ask), 0.30 * q.ask)
                entry = q.ask
                stop = round(max(0.05, entry - opt_stop_dist), 2)
                segment = Segment.INDEX_OPTION if is_index else Segment.STOCK_OPTION
                dte_days = ctx.cal.trading_minutes_to(ctx.now, expiry) / 375.0
                confs = [
                    Confirmation("micro_burst", "price_action", True,
                                 f"3 consecutive 1-min closes {'up' if long else 'down'}, accelerating"),
                    Confirmation("vwap_proximity", "flow", True, f"within 1.0x ATR1 of VWAP ({f.vwap:.2f})"),
                    Confirmation("relative_volume", "volume", f.rvol >= 1.1, f"RVOL {f.rvol:.2f}"),
                    _oi_confirmation(rows, spot, long),
                    Confirmation("breadth_aligned", "breadth", (b["ad_ratio"] > 1.05) if long else (b["ad_ratio"] < 0.95),
                                 f"A/D {b['advancers']}/{b['decliners']}"),
                    _reversal_confirmation(ctx, key, direction),
                    _iv_confirmation(ctx, und, rows, spot),
                    _iv_rv_confirmation(ctx, key, rows, spot),
                    _pin_risk_confirmation(rows, spot, dte_days),
                    _pcr_confirmation(rows, long),
                ]
                n = len({c.factor for c in confs if c.fired})
                out.append(Candidate(
                    id=new_id("sig"), ts=ctx.now, strategy=self.name, strategy_version=self.version, instrument=inst,
                    underlying_key=key, direction=Direction.LONG, segment=segment, entry=round(entry, 2),
                    stop=stop, target_r=1.0, p_win=min(0.46 + 0.03 * max(0, n - 2), 0.60), p_win_calibrated=False,
                    half_life=HalfLife.MINUTES, trigger_fired=True, confirmations=confs,
                    quality_features={
                        "regime": 0.6, "trend": clamp01(f.adx / 40), "volume": clamp01((f.rvol - 0.6) / 1.0),
                        "volatility": 0.6, "breadth": 1.0 if confs[4].fired else 0.3,
                        "participant": 1.0 if confs[3].fired else 0.35,
                        "liquidity": clamp01(1 - q.spread_bps / 350), "execution": clamp01(1 - q.spread_bps / 450),
                        "event": 1.0,
                    },
                    regime_fit=True, defined_risk=True, bias=direction,
                    setup_id=f"{self.name}:{und}:{ctx.now:%Y%m%d%H%M}:{direction.value}", signal_bar=f.bar_id,
                    explanation=(f"{und} {'burst up' if long else 'burst down'} near VWAP {f.vwap:.2f}; "
                                 f"buy {inst.symbol} (delta {delta:.2f}) for a small quick move"),
                    model_confidence=0.4, regime_confidence=0.5, liquidity_score=0.5, atr=opt_stop_dist / 1.2,
                    beta=ctx.instrument(key).beta if not is_index else 1.0,
                ))
        return out


def all_strategies() -> list[Strategy]:
    """Sthitaprajna 2.0 is options-only: index-option momentum, the Zero-to-Hero expiry sleeve, stock-option
    momentum on scanner-promoted F&O names, index range reversion (D-041) for the RANGE regime the other
    strategies deliberately leave alone, the day-high/day-low continuation break (D-043), and a scalp
    strategy (D-048) for small, quick moves anywhere in the session including RANGE. No cash-equity
    strategy runs in this build."""
    return [IndexOptionMomentum(), ZeroToHero(), StockOptionMomentum(), IndexRangeReversion(), DayRangeBreakout(),
            ScalpMomentum()]
