"""Unvalidated cash-intraday hypotheses. Entries require fresh quotes and completed bars."""
from __future__ import annotations

import datetime as dt

from sthitaprajna.domain import (
    Candidate,
    Confirmation,
    Direction,
    HalfLife,
    HypothesisCard,
    Kind,
    Segment,
    new_id,
)
from sthitaprajna.strategies.base import Strategy, clamp01, compute_features


class _ClosedBars:
    def __init__(self, ctx):
        self.ctx = ctx
        self._feat_cache = {}

    def __getattr__(self, key):
        return getattr(self.ctx, key)

    def candles(self, key):
        return [c for c in self.ctx.candles(key) if c.start + dt.timedelta(minutes=1) <= self.ctx.now]


class CashIntraday(Strategy):
    version = "0.1.0"

    def __init__(self, setup: str):
        self.setup = setup
        self.name = "cash_" + setup
        self.card = HypothesisCard(
            strategy=self.name, version=self.version,
            why="Opening-range breaks or VWAP pullback recoveries may continue with aligned trend and volume.",
            counterparty="Liquidity-taking intraday participants and stopped-out opposing positions.",
            not_arbitraged_because="False breaks, spread and costs can erase the hypothesized edge.",
            works_when="Liquid cash stocks with aligned trend, elevated volume and an executable spread.",
            fails_when="Whipsaw, sudden news, gaps, low volume and persistent liquidity changes.",
            decay="Revalidate with out-of-sample cash-equity data and actual trading costs.",
            holding_period="Intraday only, exits by configured 15:10 IST cutoff.",
            data_needed="NIFTY 500 membership, stock quotes, completed minute bars and daily volume baselines.",
            kill_criteria="Review after negative net expectancy or materially worse execution than the model.",
        )

    def scan(self, ctx):
        if not ctx.cal.is_open(ctx.now) or not 20 <= ctx.cal.minutes_into_session(ctx.now) <= 300:
            return []  # 09:35–14:15 IST; manage existing positions beyond this window
        if ctx.regime.volatility == "EXTREME_VOL":
            return []
        closed = _ClosedBars(ctx)
        out = []
        for key in ctx.stock_keys():
            inst, q = ctx.instrument(key), ctx.quote(key)
            if inst.kind is not Kind.EQUITY or q is None or q.bid <= 0 or q.ask < q.bid or q.spread_bps > 25:
                continue
            if not 0 <= (ctx.now - q.exchange_ts).total_seconds() <= 15:
                continue
            f = compute_features(closed, key)
            if f is None or len(f.day) < 2 or f.atr5 <= 0 or f.rvol < 1.2 or f.adx < 18:
                continue
            last, prev = f.day[-1], f.day[-2]
            if last.start.date() != ctx.now.date() or (ctx.now - last.start).total_seconds() > 120:
                continue
            for direction in (Direction.LONG, Direction.SHORT):
                sign = direction.sign
                aligned = (f.ema20 > f.ema50 and f.htf_up) if sign > 0 else (f.ema20 < f.ema50 and f.htf_down)
                if not aligned or sign * (f.price - f.vwap) <= 0:
                    continue
                if self.setup == "opening_range":
                    level = f.or_high if sign > 0 else f.or_low
                    fired = level is not None and sign * (prev.close - level) <= 0 < sign * (last.close - level)
                else:
                    touched = prev.low <= f.vwap + .15 * f.atr5 if sign > 0 else prev.high >= f.vwap - .15 * f.atr5
                    recovered = last.close > prev.high if sign > 0 else last.close < prev.low
                    fired = touched and recovered and abs(f.price - f.vwap) <= f.atr5
                if not fired:
                    continue
                entry = q.ask if sign > 0 else q.bid
                if abs(entry - last.close) > .5 * f.atr5:
                    continue  # do not chase a signal after the market has moved
                extreme = min(c.low for c in f.day[-3:]) if sign > 0 else max(c.high for c in f.day[-3:])
                risk = max(sign * (entry - extreme) + inst.tick_size, .75 * f.atr5, 4 * inst.tick_size)
                if risk > 2.5 * f.atr5 or risk > .02 * entry:
                    continue
                confirmations = [Confirmation("relative_volume", "volume", True, f"RVOL {f.rvol:.2f}"),
                                 Confirmation("aligned_trend", "trend", True, "EMA and higher timeframe aligned"),
                                 Confirmation("vwap_side", "flow", True, "Price on directional side of VWAP")]
                quality = {"regime": .75, "trend": clamp01(f.adx / 40), "volume": clamp01(f.rvol / 2),
                           "volatility": .7, "breadth": .5, "participant": .5, "liquidity": .85,
                           "execution": .8, "event": .5}
                out.append(Candidate(
                    id=new_id("sig"), ts=ctx.now, strategy=self.name, strategy_version=self.version,
                    instrument=inst, underlying_key=key, direction=direction, segment=Segment.EQ_INTRADAY,
                    entry=entry, stop=entry-sign*risk, target_r=2, p_win=.5, p_win_calibrated=False,
                    half_life=HalfLife.MINUTES, trigger_fired=True, confirmations=confirmations,
                    quality_features=quality, regime_fit=True, defined_risk=False,
                    setup_id=f"{self.name}:{key}:{direction.value}:{ctx.now.date()}", signal_bar=f.bar_id,
                    explanation=f"{self.setup}: completed-bar trigger, trend, VWAP and relative volume aligned",
                    model_confidence=.65, regime_confidence=.75, liquidity_score=.85, atr=f.atr1, beta=inst.beta,
                ))
        return out


def cash_strategies():
    return [CashIntraday("opening_range"), CashIntraday("vwap_pullback")]
