"""Sthitaprajna 2.0 options-chain analytics (D-036/D-039): OI build-up classification, IV rank/percentile,
IV-RV spread, pin risk, and the realized-vol inputs they depend on."""
from __future__ import annotations

import datetime as dt

import pytest

from sthitaprajna.domain import IST, Candle
from sthitaprajna.market import indicators as ind
from sthitaprajna.market.options import ChainRow, iv_percentile, iv_rank, iv_rv_spread, oi_buildup, pin_risk, summary


def _row(strike, ce_oi=1000.0, pe_oi=1000.0, ce_oi_chg=0.0, pe_oi_chg=0.0, ce_iv=0.15, pe_iv=0.15) -> ChainRow:
    return ChainRow(strike=strike, ce_key=f"CE{strike}", pe_key=f"PE{strike}", ce_ltp=10.0, pe_ltp=10.0,
                    ce_bid=9.8, ce_ask=10.2, pe_bid=9.8, pe_ask=10.2, ce_oi=ce_oi, pe_oi=pe_oi,
                    ce_oi_chg=ce_oi_chg, pe_oi_chg=pe_oi_chg, ce_vol=500.0, pe_vol=500.0,
                    ce_iv=ce_iv, pe_iv=pe_iv, ce_delta=0.5, pe_delta=-0.5)


# ----------------------------------------------------------------------------- OI build-up (A6.4)
def test_buildup_price_up_oi_up_is_long_buildup():
    rows = [_row(25000, ce_oi_chg=5000, pe_oi_chg=-2000)]
    b = oi_buildup(rows, underlying_chg_pct=0.004, spot=25000)
    assert b["per_strike"][0]["ce"] == "LONG_BUILDUP"
    assert b["per_strike"][0]["pe"] == "SHORT_COVERING"


def test_buildup_price_down_oi_up_is_short_buildup():
    rows = [_row(25000, ce_oi_chg=3000, pe_oi_chg=6000)]
    b = oi_buildup(rows, underlying_chg_pct=-0.004, spot=25000)
    assert b["per_strike"][0]["ce"] == "SHORT_BUILDUP"
    assert b["per_strike"][0]["pe"] == "SHORT_BUILDUP"


def test_buildup_price_down_oi_down_is_long_unwinding():
    rows = [_row(25000, ce_oi_chg=-1500)]
    b = oi_buildup(rows, underlying_chg_pct=-0.002, spot=25000)
    assert b["per_strike"][0]["ce"] == "LONG_UNWINDING"


def test_buildup_unknown_price_change_is_flat_never_a_guess():
    rows = [_row(25000, ce_oi_chg=5000, pe_oi_chg=-5000)]
    b = oi_buildup(rows, underlying_chg_pct=None, spot=25000)
    assert b["per_strike"][0]["ce"] == "FLAT" and b["per_strike"][0]["pe"] == "FLAT"
    assert b["aggregate"]["ce"] == "FLAT"


def test_buildup_near_strikes_ordered_by_distance_from_spot():
    rows = [_row(k) for k in (24800, 24900, 25000, 25100, 25200, 25300)]
    b = oi_buildup(rows, underlying_chg_pct=0.001, near=3, spot=25000)
    assert [r["strike"] for r in b["per_strike"]] == [25000, 24900, 25100]


# ----------------------------------------------------------------------------- IV rank / percentile (A6.5)
def test_iv_rank_and_percentile_need_five_points():
    assert iv_rank(0.16, [0.14, 0.15, 0.16, 0.17]) is None      # only 4 points
    assert iv_percentile(0.16, [0.14, 0.15, 0.16, 0.17]) is None


def test_iv_rank_at_extremes_and_middle():
    hist = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20]
    assert iv_rank(0.10, hist) == pytest.approx(0.0)
    assert iv_rank(0.20, hist) == pytest.approx(1.0)
    assert iv_rank(0.15, hist) == pytest.approx(0.5, abs=0.01)


def test_iv_percentile_counts_strictly_lower_sessions():
    hist = [0.10, 0.12, 0.14, 0.16, 0.18]
    assert iv_percentile(0.14, hist) == pytest.approx(2 / 5)


def test_iv_rv_spread_none_without_realized_vol():
    assert iv_rv_spread(0.18, None) is None
    assert iv_rv_spread(0.18, 0.12) == pytest.approx(0.06)


# ----------------------------------------------------------------------------- realized vol (A6.7)
def test_daily_closes_excludes_the_forming_session():
    base = dt.datetime(2026, 9, 20, 9, 15, tzinfo=IST)
    candles = []
    for d in range(5):
        day = base + dt.timedelta(days=d)
        candles.append(Candle("NSE:NIFTY", day, 100 + d, 100 + d, 100 + d, 100 + d, 1000))
    closes = ind.daily_closes(candles, lookback_days=20)
    assert closes == [100.0, 101.0, 102.0, 103.0]        # day 4 (today) excluded


def test_realized_vol_needs_six_closes_and_is_annualized_positive():
    assert ind.realized_vol([100, 101, 99]) is None
    walk = [100, 101, 99, 102, 98, 103, 97, 104]
    rv = ind.realized_vol(walk)
    assert rv is not None and rv > 0


# ----------------------------------------------------------------------------- pin risk (A6.6)
def test_pin_risk_inactive_far_from_expiry():
    rows = [_row(k, ce_oi=5000, pe_oi=5000) for k in (24900, 25000, 25100)]
    assert pin_risk(rows, spot=25000, dte_days=2.0)["active"] is False


def test_pin_risk_active_when_spot_pins_the_peak_oi_strike_on_expiry_day():
    rows = [_row(k, ce_oi=1000, pe_oi=1000) for k in (24900, 25100)]
    rows.append(_row(25000, ce_oi=50000, pe_oi=50000))
    r = pin_risk(rows, spot=25001, dte_days=0.1)
    assert r["active"] and r["strike"] == 25000


# ----------------------------------------------------------------------------- summary() wiring
def test_summary_includes_all_new_fields_and_stays_backward_compatible():
    rows = [_row(k, ce_oi=(9000 if k == 25000 else 1000), pe_oi=(9000 if k == 25000 else 1000),
                ce_oi_chg=1000, pe_oi_chg=-500) for k in (24900, 25000, 25100)]
    plain = summary(rows, 25000)                          # old call shape still works
    assert "buildup" in plain and plain["iv_rank"] is None
    rich = summary(rows, 25000, underlying_chg_pct=0.003, iv_history=[0.1, 0.12, 0.14, 0.16, 0.18],
                   realized_vol=0.11, dte_days=0.2)
    assert rich["iv_rank"] is not None and rich["iv_rv_spread"] is not None
    assert rich["pin_risk"]["strike"] == 25000
