"""Sthitaprajna 2.0 (D-055/D-056/D-057): IV rank, expiry pin-risk, put-call ratio and the implied-vs-
realized volatility spread (all D-039's own chain/volatility analytics), read at entry for the first time.
All four only ever add evidence to a candidate a strategy would already have produced -- never a hard
block. IV rank, pin-risk and the IV-RV spread default to "no evidence" (does not fire) rather than a
vacuous pass when the data isn't there yet (fresh deployment, no store, or a stock underlying with no
persisted IV history, or fewer than 6 daily closes); PCR is always computable from the live chain,
so it has no such "no data" state."""
from __future__ import annotations

import datetime as dt

from sthitaprajna.domain import IST, Candle
from sthitaprajna.market.options import ChainRow
from sthitaprajna.strategies.options import (
    _iv_confirmation, _iv_rv_confirmation, _pcr_confirmation, _pin_risk_confirmation,
)

T0 = dt.datetime(2026, 9, 29, 10, 0, tzinfo=IST)


def _row(strike, ce_oi=0.0, pe_oi=0.0, ce_iv=0.20, pe_iv=0.20):
    return ChainRow(strike, "ce", "pe", 1.0, 1.0, 0.9, 1.1, 0.9, 1.1, ce_oi, pe_oi, 0.0, 0.0,
                    100.0, 100.0, ce_iv, pe_iv, 0.5, 0.5)


def _daily_candles(closes: list[float]) -> list[Candle]:
    """One bar per calendar day, oldest first -- enough distinct days for ind.daily_closes/realized_vol."""
    return [Candle("TEST", T0 + dt.timedelta(days=i), c, c, c, c, 1000) for i, c in enumerate(closes)]


class _FakeCtx:
    def __init__(self, history=(), candles=()):
        self._history = history
        self._candles = candles

    def iv_history(self, underlying):
        return self._history

    def candles(self, key):
        return self._candles


ROWS = [_row(k, ce_iv=0.20, pe_iv=0.20) for k in (24600, 24650, 24700, 24750, 24800)]


def test_iv_confirmation_does_not_fire_with_fewer_than_five_sessions_of_history():
    c = _iv_confirmation(_FakeCtx([0.18, 0.22, 0.19]), "NIFTY", ROWS, 24700)
    assert not c.fired and c.factor == "volatility" and c.name == "iv_not_rich"
    assert not _iv_confirmation(_FakeCtx([]), "NIFTY", ROWS, 24700).fired     # a stock underlying: no history ever


def test_iv_confirmation_fires_when_current_iv_is_not_rich():
    # history ranges 0.15-0.30; today's ATM IV (0.20) sits well below the 0.70 rank threshold
    hist = [0.15, 0.18, 0.22, 0.25, 0.30]
    c = _iv_confirmation(_FakeCtx(hist), "NIFTY", ROWS, 24700)
    assert c.fired and "IV rank" in c.detail


def test_iv_confirmation_does_not_fire_when_iv_is_already_rich():
    rich_rows = [_row(k, ce_iv=0.29, pe_iv=0.29) for k in (24600, 24650, 24700, 24750, 24800)]
    hist = [0.15, 0.18, 0.20, 0.22, 0.25]    # today's 0.29 is at/above the top of this window -> rank ~1.0
    c = _iv_confirmation(_FakeCtx(hist), "NIFTY", rich_rows, 24700)
    assert not c.fired


def test_iv_confirmation_handles_no_atm_row_gracefully():
    hist = [0.15, 0.18, 0.20, 0.22, 0.25]
    c = _iv_confirmation(_FakeCtx(hist), "NIFTY", [], 24700)
    assert not c.fired and c.detail == "no ATM row"


def test_pin_risk_confirmation_fires_when_not_close_to_expiry():
    rows = [_row(24700, ce_oi=1_000_000, pe_oi=1_000_000)] + [_row(k) for k in (24600, 24650, 24750, 24800)]
    c = _pin_risk_confirmation(rows, 24702.0, dte_days=2.0)      # far from expiry: pin_risk() is never active
    assert c.fired and c.factor == "pin_risk" and c.name == "no_pin_risk"


def test_pin_risk_confirmation_does_not_fire_when_pinned_on_expiry_day():
    rows = [_row(24700, ce_oi=2_000_000, pe_oi=2_000_000)] + [_row(k) for k in (24600, 24650, 24750, 24800)]
    c = _pin_risk_confirmation(rows, 24701.0, dte_days=0.1)      # on expiry day, spot right on the OI peak
    assert not c.fired and "pin" in c.detail


def test_pin_risk_confirmation_fires_on_expiry_day_when_spot_is_far_from_the_peak():
    rows = [_row(24700, ce_oi=2_000_000, pe_oi=2_000_000)] + [_row(k) for k in (24600, 24650, 24750, 24800)]
    c = _pin_risk_confirmation(rows, 25200.0, dte_days=0.1)      # expiry day, but spot is well away from the pin
    assert c.fired


def test_pcr_confirmation_fires_for_a_long_when_pcr_is_not_crowded_bullish():
    rows = [_row(24700, ce_oi=1_000_000, pe_oi=1_000_000)]   # PCR(OI) = 1.0: balanced
    c = _pcr_confirmation(rows, long=True)
    assert c.fired and c.factor == "oi" and c.name == "pcr_not_extreme"


def test_pcr_confirmation_does_not_fire_for_a_long_when_pcr_is_crowded_bullish():
    rows = [_row(24700, ce_oi=1_000_000, pe_oi=500_000)]   # PCR(OI) = 0.5: call-heavy, crowded bullish
    c = _pcr_confirmation(rows, long=True)
    assert not c.fired


def test_pcr_confirmation_does_not_fire_for_a_short_when_pcr_is_crowded_bearish():
    rows = [_row(24700, ce_oi=500_000, pe_oi=1_000_000)]   # PCR(OI) = 2.0: put-heavy, crowded bearish
    c = _pcr_confirmation(rows, long=False)
    assert not c.fired


def test_pcr_confirmation_fires_for_a_short_when_pcr_is_not_crowded_bearish():
    rows = [_row(24700, ce_oi=1_000_000, pe_oi=1_000_000)]
    c = _pcr_confirmation(rows, long=False)
    assert c.fired


ROWS_LOW_IV = [_row(k, ce_iv=0.12, pe_iv=0.12) for k in (24600, 24650, 24700, 24750, 24800)]
ROWS_HIGH_IV = [_row(k, ce_iv=0.40, pe_iv=0.40) for k in (24600, 24650, 24700, 24750, 24800)]
# small, steady daily moves -> low realized volatility
QUIET_CLOSES = _daily_candles([100, 100.4, 99.9, 100.3, 99.9, 100.4, 100.0, 100.3])
# much larger daily swings -> higher realized volatility
WILD_CLOSES = _daily_candles([100, 104, 97, 103, 96, 105, 95, 104])


def test_iv_rv_confirmation_does_not_fire_with_fewer_than_six_daily_closes():
    ctx = _FakeCtx(candles=_daily_candles([100, 101, 99, 100.5]))   # only 3 prior-day closes
    c = _iv_rv_confirmation(ctx, "NSE:NIFTY", ROWS_LOW_IV, 24700)
    assert not c.fired and c.factor == "volatility" and c.name == "iv_not_overpriced_vs_realized"


def test_iv_rv_confirmation_does_not_fire_when_iv_is_rich_against_realized_moves():
    ctx = _FakeCtx(candles=QUIET_CLOSES)
    c = _iv_rv_confirmation(ctx, "NSE:NIFTY", ROWS_HIGH_IV, 24700)
    assert not c.fired and "spread" in c.detail


def test_iv_rv_confirmation_fires_when_iv_is_not_rich_against_realized_moves():
    ctx = _FakeCtx(candles=WILD_CLOSES)
    c = _iv_rv_confirmation(ctx, "NSE:NIFTY", ROWS_LOW_IV, 24700)
    assert c.fired


def test_iv_rv_confirmation_handles_no_atm_row_gracefully():
    ctx = _FakeCtx(candles=WILD_CLOSES)
    c = _iv_rv_confirmation(ctx, "NSE:NIFTY", [], 24700)
    assert not c.fired and c.detail == "no ATM row"
