"""Cash profile isolation, official-list validation, and deterministic trading decisions."""
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fake_dhan import CLIENT_ID, TOKEN, FakeClock, FakeDhan, ist
from fastapi.testclient import TestClient

from sthitaprajna.api.server import create_app
from sthitaprajna.config import load_holidays, load_rules
from sthitaprajna.data.dhan_live import DhanClient
from sthitaprajna.domain import Candle, Direction, Exchange, Instrument, Kind, Mode, Quote, Segment, Source
from sthitaprajna.engine.runtime import _FeatureCacheCtx
from sthitaprajna.engine.snapshot import snapshot
from sthitaprajna.intraday.cli import build_intraday_engine, main
from sthitaprajna.intraday.strategies import CashIntraday, _ClosedBars
from sthitaprajna.intraday.universe import load_members, parse_members
from sthitaprajna.market.calendar import MarketCalendar
from sthitaprajna.strategies.base import Features

RULES = load_rules()
CAL = MarketCalendar.from_rules(RULES, load_holidays())


def csv_text(n=500):
    return "Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(
        f"Synthetic {i},Industrials,SYN{i:03d},EQ,INE{i:09d}\n" for i in range(n))


def members():
    return parse_members(csv_text())


def test_constituents_reject_wrong_index_html_duplicates_and_non_cash():
    assert len(members()) == 500
    for bad in (csv_text(50), "<html>Access denied</html>", csv_text().replace("SYN001", "SYN000"),
                csv_text().replace(",EQ,", ",BE,", 1), csv_text().replace("INE000000001", "bad")):
        with pytest.raises(ValueError):
            parse_members(bad)


def test_daily_refresh_cache_fallback_expiry_and_explicit_csv(tmp_path):
    day = dt.date(2026, 11, 4)
    notes = []
    assert len(load_members(tmp_path, today=day, fetch=csv_text, progress=notes.append)) == 500

    def failed():
        raise httpx.ConnectError("offline")

    assert len(load_members(tmp_path, today=day, fetch=failed, progress=notes.append)) == 500
    assert len(load_members(tmp_path, today=day+dt.timedelta(days=7), fetch=failed, progress=notes.append)) == 500
    assert any("7-day-old" in n for n in notes)
    with pytest.raises(ValueError, match="older than 7 days"):
        load_members(tmp_path, today=day+dt.timedelta(days=8), fetch=failed)
    custom = tmp_path / "official.csv"
    custom.write_text(csv_text(), encoding="utf-8")
    assert len(load_members(tmp_path, custom, fetch=failed)) == 500


def context(sign=1, day=4):
    now = ist(2026, 11, day, 10, 0)
    key = "NSE:SYN000"
    def px(x):
        return 100 + sign*(x-100)
    def bar(minute, o, h, low, c):
        return Candle(key, now.replace(minute=minute, hour=9), px(o), max(px(h), px(low)),
                      min(px(h), px(low)), px(c), 100000)
    bars = [bar(57, 101, 101.2, 100.8, 101), bar(58, 101, 101.2, 100.6, 100.9),
            bar(59, 101, 102.2, 100.8, 102)]
    price = px(102)
    q = Quote(key, price, price-.05, price+.05, 1e7, 0, now, now, Source.DHAN)
    inst = Instrument(key, "SYN000", "Test", Kind.EQUITY, Exchange.NSE, sector="Industrials")
    f = Features(key, price, px(101.4), .7, 2, 101 if sign > 0 else 101.5,
                 98.5 if sign > 0 else 99, px(101), px(100.5), 25, 2, bars, bars,
                 sign > 0, sign < 0, key+"@0959")
    ctx = SimpleNamespace(now=now, cal=CAL, regime=SimpleNamespace(volatility="NORMAL_VOL"),
                          stock_keys=lambda: [key], instrument=lambda k: inst, quote=lambda k: q,
                          candles=lambda k: bars)
    return ctx, f, q


@pytest.mark.parametrize("setup", ["opening_range", "vwap_pullback"])
@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("day", [2, 3, 4, 5, 6])
def test_long_short_cash_signals_every_weekday(monkeypatch, setup, sign, day):
    ctx, f, _q = context(sign, day)
    monkeypatch.setattr("sthitaprajna.intraday.strategies.compute_features", lambda c, k: f)
    signals = CashIntraday(setup).scan(ctx)
    assert len(signals) == 1
    c = signals[0]
    assert c.segment is Segment.EQ_INTRADAY and c.instrument.expiry is None
    assert c.direction is (Direction.LONG if sign > 0 else Direction.SHORT)
    assert sign*(c.entry-c.stop) > 0 and c.independent_confirmations() == 3
    assert not c.p_win_calibrated and not c.sleeve


@pytest.mark.parametrize("reason", ["stale_quote", "spread", "no_volume", "stale_bar", "no_trigger", "late", "weekend"])
def test_reject_bad_inputs(monkeypatch, reason):
    ctx, f, q = context()
    if reason == "stale_quote":
        q.exchange_ts -= dt.timedelta(seconds=30)
    elif reason == "spread":
        q.ask += 2
    elif reason == "no_volume":
        f.rvol = .5
    elif reason == "stale_bar":
        f.day[-1].start -= dt.timedelta(minutes=5)
    elif reason == "no_trigger":
        f.or_high = 105
    elif reason == "late":
        ctx.now = ctx.now.replace(hour=14, minute=16)
    else:
        ctx.now = ist(2026, 11, 7, 10)
    monkeypatch.setattr("sthitaprajna.intraday.strategies.compute_features", lambda c, k: f)
    assert not CashIntraday("opening_range").scan(ctx)


def test_forming_bar_excluded():
    ctx, f, _q = context()
    forming = replace(f.day[-1], start=ctx.now, close=1e6)
    ctx.candles = lambda k: f.day+[forming]
    assert _ClosedBars(ctx).candles(f.key) == f.day


def paper(tmp_path, omit=False):
    clock = FakeClock(ist(2026, 11, 4, 10))
    fake = FakeDhan(clock, extra_stocks=500)
    fake.series.clear()  # all 500 synthetic constituents are EQ-series
    if omit:
        fake.series[fake.sid("SYN499")] = "BE"
    client = DhanClient(CLIENT_ID, TOKEN, transport=httpx.MockTransport(fake.handler),
                        sleep=lambda s: None, rules=RULES)
    e = build_intraday_engine(members(), client=client, clock=clock, cache_dir=tmp_path,
                              start_poller=False, backfill_days=1, progress=lambda s: None)
    return e, fake, clock


def test_500_member_paper_scan_excludes_nonmembers_and_all_option_calls(tmp_path):
    e, fake, clock = paper(tmp_path)
    try:
        m = e.market
        assert len(m.screen) == 500 and "NSE:RELIANCE" not in m.screen
        fake.set_price("SYN499", fake.px[fake.sid("SYN499")]*1.02)
        fake.set_price("SYN000", fake.px[fake.sid("SYN000")]*.98)
        for _ in range(4):
            clock.advance(1.3)
            m.poll_once()
            m._apply_inbox()
        quote_requests = [body for path, body in fake.requests if "marketfeed" in path]
        ids = {sid for b in quote_requests for sid in b.get("NSE_EQ", [])}
        assert {int(s.sid) for s in m.screen.values()}.issubset(ids)
        assert len(m.states) == 4  # history is loaded on demand, not 500 full intraday downloads
        assert m.breadth() == {"advancers": 1, "decliners": 1, "ad_ratio": 1}
        assert m.promote("NSE:SYN000")
        assert m.instruments["NSE:SYN000"].sector == "Industrials"
        assert m.instruments["NSE:SYN000"].lot_size == 1
        m.service_expiries()
        e._update_regime()
        assert all("optionchain" not in path for path, _ in fake.requests)
        assert not fake.order_calls and e.mode is Mode.PAPER
        assert len(m.screen_inputs()) == 500
        clock.advance(31)
        assert not m.screen_inputs()
        with TestClient(create_app(e, run_loop=False)) as c:
            assert c.get("/api/chain").status_code == 404
            assert c.get("/api/feasibility").status_code == 404
            assert c.get("/api/calendar").json()["expiries"] == []
            assert c.get("/api/chart_overlays", params={"key": "NSE:NIFTY"}).status_code == 200
            assert c.post("/api/research/backtest", json={}).status_code == 400
            s = c.get("/api/state").json()
            assert s["product"] == "intraday500"
            assert s["market"]["scanner"]["universe"] == 500
            assert all("optionchain" not in path for path, _ in fake.requests)
    finally:
        e.market.close()


def test_unresolved_constituent_fails_instead_of_silently_trading_partial_universe(tmp_path):
    with pytest.raises(ValueError, match="SYN499"):
        paper(tmp_path, omit=True)


def test_sim_500_members_and_no_expiry_dependency(monkeypatch):
    e = build_intraday_engine(members(), mode=Mode.SIM, warmup_days=1, start=dt.date(2026, 11, 3))
    assert len(e.market.stock_keys) == 500
    assert all(s.name.startswith("cash_") for s in e.strategies)
    monkeypatch.setattr(e.market, "chain", lambda *a: pytest.fail("cash engine requested an option chain"))
    e._update_regime()
    e._snapshot_iv_history(e.now.date())
    e.tick(1)
    assert len(e.market.candles("NSE:SYN499")) >= 200
    assert snapshot(e)["product"] == "intraday500"
    e.scanner_rows = [SimpleNamespace(key="NSE:SYN000", tradable=True, score=.8),
                      SimpleNamespace(key="NSE:SYN001", tradable=False, score=.9)]
    assert _FeatureCacheCtx(e).stock_keys() == ["NSE:SYN000"]


def test_live_mode_rejected():
    with pytest.raises(SystemExit):
        main(["--mode", "live"])
    with pytest.raises(ValueError, match="SIM and PAPER"):
        build_intraday_engine([], mode=Mode.LIVE_FULL)


def test_cli_ports_have_independent_databases_and_caches(tmp_path, monkeypatch):
    from sthitaprajna.intraday import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_env_file", lambda: (None, []))
    seen_cache, seen_db = [], []
    def load(cache, csv):
        seen_cache.append(str(cache))
        return members()
    def build(m, **kw):
        seen_db.append(kw["store"].path)
        return SimpleNamespace(cfg=SimpleNamespace(risk_mode="unbounded", unbounded={}),
                               cal=CAL, market=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "load_members", load)
    monkeypatch.setattr(cli, "build_intraday_engine", build)
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: None)
    assert main([]) == 0
    assert main(["--port", "8793"]) == 0
    assert seen_cache == ["var/intraday500-8792-cache", "var/intraday500-8793-cache"]
    assert seen_db == ["var/intraday500-paper-8792.sqlite", "var/intraday500-paper-8793.sqlite"]


@pytest.mark.parametrize("sign", [1, -1])
def test_cash_strategy_through_funnel_fill_stop_and_intraday_exit(monkeypatch, sign):
    ctx, f, _ = context(sign)
    e = build_intraday_engine(members()[:1], mode=Mode.SIM, warmup_days=0, start=ctx.now.date())
    e.market.now = ctx.now
    e.market.freeze = True
    e.market.set_price(f.key, f.price)
    e.strategies = []
    e.tick(.25)
    monkeypatch.setattr(e.market, "chain", lambda *a: pytest.fail("cash trade requested option data"))
    monkeypatch.setattr("sthitaprajna.intraday.strategies.compute_features", lambda c, k: f)
    e.scanner_rows = [SimpleNamespace(key=f.key, tradable=True, score=.8)]
    signals = CashIntraday("opening_range").scan(_FeatureCacheCtx(e))
    assert len(signals) == 1
    from sthitaprajna.engine.runtime import _Env
    e.funnel.ingest(signals, e.now)
    ready = e.funnel.evaluate(_Env(e, e._risk_ctx()))
    e._deploy(ready)
    for _ in range(3):
        e.tick(.25)
    assert len(e.positions) == 1, [(r.status, r.reason) for r in e.funnel.records.values()]
    position = next(iter(e.positions.values()))
    assert position.segment is Segment.EQ_INTRADAY and position.broker_stop_id
    assert position.sign == sign
    e.market.now = ctx.now.replace(hour=15, minute=10)
    for _ in range(3):
        e.tick(.25)
    assert not e.positions
    assert e.closed[-1].exit_reason == "EOD"
