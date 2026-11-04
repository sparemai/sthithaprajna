"""Opportunity Scanner over the whole stock universe: scoring, tradability, the live universe (every NSE stock
from the master, rotated through the quote poll), baselines, promotion into the strategy universe, and the API."""
import datetime as dt
import json

import httpx
import pytest
from fake_dhan import CLIENT_ID, TOKEN, FakeClock, FakeDhan, ist
from test_unbounded import unbounded_cfg

from sthitaprajna.config import load_holidays, load_rules
from sthitaprajna.data.dhan_live import DhanClient
from sthitaprajna.data.live import DisabledFeed, LiveFeed, LiveMarket
from sthitaprajna.domain import IST, Candle, Mode
from sthitaprajna.engine.runtime import Engine
from sthitaprajna.intelligence.scanner import (
    ScreenInput, baseline_from_daily, expected_volume_fraction, scan, score,
)
from sthitaprajna.market.calendar import MarketCalendar

RULES = load_rules()
CAL = MarketCalendar.from_rules(RULES, load_holidays())


def boom(**over) -> ScreenInput:
    base = dict(key="NSE:BOOM", symbol="BOOM", ltp=520.0, prev_close=500.0, open=510.0, high=521.0, low=508.0,
                volume=3_000_000, vwap=514.0, w52_high=518.0, w52_low=300.0, upper_circuit=600.0, lower_circuit=400.0,
                buy_qty=90_000, sell_qty=30_000, bid=519.9, ask=520.1, mom15=0.012, avg_vol_20=1_500_000,
                prev_high=512.0, prev_low=495.0, atr_pct=0.02, fno=True, lot=500)
    base.update(over)
    return ScreenInput(**base)


# ---------------------------------------------------------------- scoring
def test_a_booming_stock_scores_high_with_reasons():
    r = score(boom(), minutes_into=120)
    assert r.direction == "UP" and r.score > 0.8 and r.tradable
    for flag in ("GAP UP 2.0%", "NEW 52W HIGH", "ABOVE PREV HIGH", "ABOVE VWAP", "BUYERS 3.0x"):
        assert flag in r.flags
    assert r.rvol is not None and r.rvol > 3 and r.flags[0].startswith("RVOL")


def test_quiet_and_breaking_down_stocks():
    quiet = score(boom(ltp=500.5, open=500.0, high=502.0, low=499.0, vwap=500.4, volume=400_000, buy_qty=10_000,
                       sell_qty=10_000, mom15=0.0, w52_high=700.0), minutes_into=120)
    assert quiet.score < 0.25
    down = score(boom(ltp=478.0, open=490.0, high=491.0, low=477.5, vwap=485.0, buy_qty=20_000, sell_qty=80_000,
                      mom15=-0.015, w52_high=700.0, w52_low=478.5), minutes_into=120)
    assert down.direction == "DOWN" and down.score > 0.7
    assert "BELOW PREV LOW" in down.flags and "NEW 52W LOW" in down.flags and "SELLERS 4.0x" in down.flags


@pytest.mark.parametrize("over,why", [
    ({"series": "BE"}, "BE series"),
    ({"surveillance": "ASM Stage 1"}, "surveillance"),
    ({"ltp": 15.0, "prev_close": 14.0, "open": 14.5, "high": 15.1, "low": 14.4, "vwap": 14.8, "w52_high": 15.0,
      "upper_circuit": 20.0, "lower_circuit": 10.0, "prev_high": 14.5, "bid": 14.99, "ask": 15.01}, "below Rs 20"),
    ({"volume": 20_000}, "illiquid"),
    ({"bid": 518.0, "ask": 522.0}, "spread"),
    ({"upper_circuit": 525.0}, "upper circuit"),
])
def test_untradable_stocks_are_shown_but_flagged(over, why):
    r = score(boom(**over), minutes_into=120)
    assert not r.tradable and why in r.why_not


def test_volume_profile_and_baselines():
    assert expected_volume_fraction(0) == 0 and expected_volume_fraction(375) == pytest.approx(1.0)
    assert expected_volume_fraction(30) > 30 / 375 * 1.3          # the open is heavy
    assert expected_volume_fraction(200) < expected_volume_fraction(250)
    d0 = dt.datetime(2026, 9, 1, tzinfo=IST)
    bars = [Candle("X", d0 + dt.timedelta(days=i), 100, 102 + i % 3, 98, 100 + i % 2, 1000 + 10 * i) for i in range(25)]
    b = baseline_from_daily(bars)
    assert b["prev_high"] == bars[-1].high and b["avg_vol_20"] == pytest.approx(sum(x.volume for x in bars[-20:]) / 20)
    assert 0.03 < b["atr_pct"] < 0.07
    ranked = scan([boom(), boom(key="NSE:Q", symbol="Q", ltp=500.2, mom15=0.0), boom(key="NSE:T", symbol="T", series="BE")], 120)
    assert [r.symbol for r in ranked][:2] == ["BOOM", "Q"] and ranked[-1].symbol == "T"   # untradable sinks


# ---------------------------------------------------------------- the live universe
def client_for(fake):
    return DhanClient(CLIENT_ID, TOKEN, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None, rules=RULES)


def live(tmp_path, start, extra=60, backfill_days=1):
    clock = FakeClock(start)
    fake = FakeDhan(clock, extra_stocks=extra)
    m = LiveMarket(CAL, client_for(fake), RULES, clock=clock, cache_dir=tmp_path, mono=clock.mono)
    m.bootstrap(progress=lambda s: None, backfill_days=backfill_days, start_poller=False)
    return m, fake, clock


def test_every_master_stock_is_screened_with_fno_and_surveillance(tmp_path):
    m, fake, clock = live(tmp_path, ist(2026, 9, 28, 10, 0))
    assert len(m.screen) == 35 + 60                      # 35 resolvable core stocks + the synthetic universe
    assert m.screen["NSE:SYN000"].fno_lot == 500 and m.screen["NSE:SYN001"].fno_lot == 0
    assert m.screen["NSE:SYN004"].series == "BE" and m.screen["NSE:SYN016"].surveillance
    for _ in range(4):                                   # rotating chunks cover everyone
        clock.advance(2)
        m.run_once()
        m.step()
    fresh = [sc for sc in m.screen.values() if sc.ltp > 0]
    assert len(fresh) == len(m.screen)
    sc = m.screen["NSE:SYN010"]
    assert sc.w52_high > sc.ltp > 0 and sc.upper_circuit > 0 and sc.buy_qty > 0
    assert "NSE:SYN010" not in m.states                  # scanner-only: no bars, no arbitration


def test_baselines_load_in_background_are_cached_and_roll_forward(tmp_path):
    m, fake, clock = live(tmp_path, ist(2026, 9, 28, 10, 0), extra=12)
    assert m._baseline_queue
    for _ in range(40):
        clock.advance(2)
        m.run_once()
        m.step()
        if not m._baseline_queue:
            break
    eq_keys = [k for k, sc in m.screen.items() if sc.series == "EQ"]
    assert set(eq_keys) <= set(m.baselines)
    b = m.baselines["NSE:SYN000"]
    assert b["as_of"] == "2026-09-25" and b["avg_vol_20"] == 100_000 and b["prev_close"] == pytest.approx(100.0)
    saved = json.loads((tmp_path / "baselines.json").read_text())
    assert saved["items"]["NSE:SYN000"]["as_of"] == "2026-09-25"
    m2, fake2, _ = live(tmp_path, ist(2026, 9, 28, 10, 5), extra=12)
    assert not m2._baseline_queue                         # reused from the cache, nothing refetched
    assert not [p for p, _ in fake2.requests if p.endswith("/charts/historical")]
    fake.set_price("SYN000", 104.0, volume_add=250_000)
    clock.advance(2)
    m.run_once()
    m.step()
    clock.t = ist(2026, 9, 28, 15, 30, 5)
    assert "close" in m.step()
    rolled = m.baselines["NSE:SYN000"]
    assert rolled["as_of"] == "2026-09-28" and rolled["prev_close"] == pytest.approx(104.0)


def test_booming_stock_joins_the_strategy_universe_and_leaves_when_cold(tmp_path):
    m, fake, clock = live(tmp_path, ist(2026, 9, 28, 11, 0), extra=12)
    e = Engine(unbounded_cfg(), RULES, CAL, mode=Mode.PAPER, market=m, feeds=(LiveFeed(m), DisabledFeed()),
               wall_clock=clock.mono)
    e.strategies = []                                     # scanner mechanics only
    for _ in range(30):                                   # baselines and a full sweep
        clock.advance(2)
        m.run_once()
        e.tick(0.25)
    s = fake.sid("SYN003")
    fake.set_price("SYN003", 126.0, volume_add=1_300_000)  # +4%: new 52w high, heavy volume, buyers in control
    fake.w52[s] = (122.0, 90.0)
    fake.book[s] = (90_000.0, 20_000.0)
    fake.vwap[s] = 123.0
    for _ in range(4):
        clock.advance(2)
        m.run_once()
        e.tick(0.25)
    clock.advance(16)
    e.tick(0.25)
    top = e.scanner_rows[0]
    assert top.symbol == "SYN003" and top.direction == "UP" and top.tradable
    assert "NSE:SYN003" in m.promoted and "NSE:SYN003" in m.stock_keys
    assert any("SYN003 joins the strategy universe" in ev["msg"] for ev in e.events)
    assert "NSE:SYN004" not in m.promoted                 # BE series is never promoted
    clock.advance(2)
    m.run_once()                                          # its 1-minute history arrives in the background
    e.tick(0.25)
    assert len(m.candles("NSE:SYN003")) > 100
    fake.set_price("SYN003", 121.0)                       # the move fades back to yesterday's close
    fake.book[s] = (10_000.0, 10_000.0)
    fake.vwap[s] = 121.0
    fake.w52[s] = (160.0, 90.0)
    for _ in range(40):
        clock.advance(30)
        m.run_once()
        e.tick(0.25)
    assert "NSE:SYN003" not in m.promoted and any("cooled off" in ev["msg"] for ev in e.events)


def test_scanner_and_search_api(tmp_path):
    from fastapi.testclient import TestClient

    from sthitaprajna.api.server import create_app
    m, fake, clock = live(tmp_path, ist(2026, 9, 28, 10, 0), extra=12)
    e = Engine(unbounded_cfg(), RULES, CAL, mode=Mode.PAPER, market=m, feeds=(LiveFeed(m), DisabledFeed()),
               wall_clock=clock.mono)
    for _ in range(6):
        clock.advance(2)
        m.run_once()
        e.tick(0.25)
    with TestClient(create_app(e, run_loop=False)) as c:
        s = c.get("/api/state").json()
        assert s["scanner"]["summary"]["universe"] == len(m.screen) and s["scanner"]["top"]
        assert s["risk_mode"] == "unbounded"
        allr = c.get("/api/scanner", params={"limit": 500}).json()
        assert allr["total"] == len(e.scanner_rows)
        fno = c.get("/api/scanner", params={"segment": "fno"}).json()["rows"]
        assert fno and all(r["fno"] for r in fno)
        tradable = c.get("/api/scanner", params={"tradable": "yes"}).json()["rows"]
        assert all(r["tradable"] for r in tradable)
        hits = c.get("/api/search", params={"q": "SYN01"}).json()
        assert [h["symbol"] for h in hits] == ["SYN010", "SYN011"]
        bars = c.get("/api/candles", params={"key": "NSE:SYN010"}).json()   # scanner-only stock: fetched history
        assert len(bars["candles"]) > 10
