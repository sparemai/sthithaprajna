import datetime as dt
import math

import pytest

from sthitaprajna.config import ConfigError, load_holidays, load_operator, load_rules, operator_from_dict
from sthitaprajna.domain import IST, Exchange, Segment, Side
from sthitaprajna.market import black76
from sthitaprajna.market.calendar import MarketCalendar
from sthitaprajna.market.charges import ChargesModel


@pytest.fixture(scope="module")
def cal():
    return MarketCalendar.from_rules(load_rules(), load_holidays())


def test_every_rule_has_source_and_verified_on_key():
    rules = load_rules()
    for r in rules.rules.values():
        assert r.source_url, r.name
        assert r.status in {"VERIFIED", "VERIFIED_SECONDARY", "VERIFY", "UNKNOWN"}
        if r.status in {"VERIFIED", "VERIFIED_SECONDARY"}:
            assert r.verified_on is not None, r.name


def test_rules_freshness_flags_stale(cal):
    rules = load_rules()
    fresh_today = rules.open_items(dt.date(2026, 9, 27))
    later = rules.open_items(dt.date(2026, 12, 1))  # > 30 days after verification
    assert "nifty_weekly_expiry_weekday" not in fresh_today
    assert "nifty_weekly_expiry_weekday" in later
    assert "freeze_qty_sensex" in fresh_today  # UNKNOWN is always an open item


def test_nifty_tuesday_sensex_thursday_and_holiday_shift(cal):
    n = cal.weekly_expiries("NIFTY", dt.date(2026, 9, 27), 4)
    assert all(d.weekday() == 1 for d in n[:3])
    # 20 Oct 2026 is an NSE holiday in the seed list -> expiry moves to Mon 19 Oct
    assert dt.date(2026, 10, 19) in n
    s = cal.weekly_expiries("SENSEX", dt.date(2026, 9, 27), 3)
    assert all(d.weekday() == 3 for d in s)


def test_trading_minutes(cal):
    now = dt.datetime(2026, 9, 29, 9, 15, tzinfo=IST)
    assert cal.trading_minutes_to(now, dt.date(2026, 9, 29)) == pytest.approx(375)
    after = dt.datetime(2026, 9, 29, 15, 31, tzinfo=IST)
    assert cal.trading_minutes_to(after, dt.date(2026, 9, 29)) == 0


def test_option_charges_by_hand():
    cm = ChargesModel(load_rules())
    # SELL 1 lot Nifty (65) at premium 120 -> turnover 7,800
    c = cm.compute(Segment.INDEX_OPTION, Side.SELL, 120.0, 65)
    assert c.brokerage == 20.0
    assert c.stt == pytest.approx(7800 * 0.0015)
    assert c.exchange == pytest.approx(7800 * 0.0003503)
    assert c.stamp == 0.0
    assert c.gst == pytest.approx((20 + 7800 * 0.0003503 + 7800 * 0.000001) * 0.18)
    b = cm.compute(Segment.INDEX_OPTION, Side.BUY, 120.0, 65)
    assert b.stt == 0 and b.stamp == pytest.approx(7800 * 0.00003)


def test_bse_index_option_uses_bse_rate():
    cm = ChargesModel(load_rules())
    c = cm.compute(Segment.INDEX_OPTION, Side.BUY, 200.0, 20, Exchange.BSE)
    assert c.exchange == pytest.approx(4000 * 0.000325)


def test_intraday_brokerage_is_lower_of_flat_or_pct():
    cm = ChargesModel(load_rules())
    small = cm.compute(Segment.EQ_INTRADAY, Side.BUY, 100.0, 10)  # turnover 1000 -> 0.30
    assert small.brokerage == pytest.approx(0.30)
    big = cm.compute(Segment.EQ_INTRADAY, Side.BUY, 1000.0, 100)  # 1,00,000 -> Rs 20 cap
    assert big.brokerage == 20.0


def test_delivery_sell_has_dp_and_stt_both_sides():
    cm = ChargesModel(load_rules())
    s = cm.compute(Segment.EQ_DELIVERY, Side.SELL, 500.0, 10)
    b = cm.compute(Segment.EQ_DELIVERY, Side.BUY, 500.0, 10)
    assert s.dp > 0 and b.dp == 0
    assert s.stt == pytest.approx(5.0) and b.stt == pytest.approx(5.0)


def test_black76_put_call_parity_and_greeks():
    f, k, t, sig, r = 25000.0, 25100.0, 5 / 252, 0.14, 0.065
    c = black76.price(f, k, t, sig, r, True)
    p = black76.price(f, k, t, sig, r, False)
    assert c - p == pytest.approx(math.exp(-r * t) * (f - k), abs=1e-6)
    g = black76.greeks(f, k, t, sig, r, True)
    assert 0 < g.delta < 1 and g.gamma > 0 and g.vega > 0 and g.theta < 0
    iv = black76.implied_vol(c, f, k, t, r, True)
    assert iv == pytest.approx(sig, abs=1e-4)


def test_black76_iv_rejects_arbitrage_prices():
    assert black76.implied_vol(1e9, 100, 100, 0.1, 0.05, True) is None


def test_operator_config_loads_and_is_strict():
    cfg = load_operator()
    assert cfg.starting_capital == 100000
    assert cfg.tier_for(150000).name == "T1" and cfg.tier_for(250000).name == "T2"


def test_operator_config_cannot_loosen_ceilings():
    import copy

    import yaml
    from sthitaprajna.config import config_dir
    raw = yaml.safe_load((config_dir() / "operator.yaml").read_text())
    bad = copy.deepcopy(raw)
    bad["gears"]["ATTACK"]["open_risk"] = 0.15   # 2.0's open-risk ceiling is 10% (D-036)
    with pytest.raises(ConfigError):
        operator_from_dict(bad)
    bad = copy.deepcopy(raw)
    bad["capital"]["ruin_floor_abs"] = 50000
    with pytest.raises(ConfigError):
        operator_from_dict(bad)
    bad = copy.deepcopy(raw)
    bad["execution"]["max_orders_per_second"] = 10
    with pytest.raises(ConfigError):
        operator_from_dict(bad)
    bad = copy.deepcopy(raw)
    bad["limits"]["naked_option_selling"] = True
    with pytest.raises(ConfigError):
        operator_from_dict(bad)
