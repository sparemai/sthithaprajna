"""PAPER on live Dhan data, against a fake Dhan API (the real one needs the operator's account).

Covers: instrument resolution from the master, quotes with depth and the /ohlc fallback, error messages that
name the fix without leaking the token, the real session clock (weekend = last close, open/minute/close
events, minute bars from polled quotes), option chains, single-feed arbitration without Upstox, a full paper
trade whose fills come from live quotes, the server and the `dhan-check` report.
"""
import datetime as dt
import json
import time

import httpx
import pytest
from fake_dhan import CLIENT_ID, TOKEN, FakeClock, FakeDhan, ist
from test_engine_e2e import candidate, push

from sthitaprajna import cli
from sthitaprajna.config import load_holidays, load_operator, load_rules
from sthitaprajna.data.dhan_check import run_check
from sthitaprajna.data.dhan_live import (
    DhanClient, DhanError, checked_url, error_from_response, normalise_tick, parse_last_trade_time,
)
from sthitaprajna.data.live import DisabledFeed, LiveFeed, LiveMarket, estimate_beta
from sthitaprajna.domain import IST, Candle, Kind, Mode, OptionType, PositionStage
from sthitaprajna.engine.runtime import Engine
from sthitaprajna.engine.snapshot import snapshot
from sthitaprajna.market.calendar import MarketCalendar

RULES = load_rules()
CFG = load_operator()
CAL = MarketCalendar.from_rules(RULES, load_holidays())
SUNDAY = ist(2026, 9, 27, 11, 0)
MONDAY = dt.date(2026, 9, 28)


def client_for(fake: FakeDhan, **kw) -> DhanClient:
    return DhanClient(CLIENT_ID, TOKEN, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None,
                      rules=RULES, **kw)


def live(tmp_path, start: dt.datetime, backfill_days: int = 2, **fake_kw):
    clock = FakeClock(start)
    fake = FakeDhan(clock, **fake_kw)
    m = LiveMarket(CAL, client_for(fake), RULES, clock=clock, cache_dir=tmp_path, mono=clock.mono)
    m.bootstrap(progress=lambda s: None, backfill_days=backfill_days, start_poller=False)
    return m, fake, clock


def engine_on(m: LiveMarket, pilot: bool = False) -> Engine:
    e = Engine(CFG, RULES, CAL, mode=Mode.PAPER, market=m, feeds=(LiveFeed(m), DisabledFeed()), wall_clock=m.mono)
    e.pilot_approvals = pilot
    return e


def cycle(m: LiveMarket, e: Engine | None, clock: FakeClock, seconds: float = 1.0) -> list[str]:
    clock.advance(seconds)
    m.run_once()
    if e is None:
        return m.step()
    e.tick(0.25)
    return []


# ---------------------------------------------------------------- helpers and errors
def test_small_helpers():
    assert normalise_tick("5.0000") == 0.05 and normalise_tick(0.05) == 0.05 and normalise_tick("1.0") == 0.01
    assert normalise_tick("junk") == 0.05
    t = parse_last_trade_time("26/09/2026 15:29:59")
    assert t == dt.datetime(2026, 9, 26, 15, 29, 59, tzinfo=IST)
    assert parse_last_trade_time("01/01/1980 00:00:00") is None and parse_last_trade_time(None) is None
    assert checked_url("https://api.dhan.co/v2/") == "https://api.dhan.co/v2"
    assert checked_url("http://127.0.0.1:9000/v2").startswith("http://127.0.0.1")
    with pytest.raises(ValueError):
        checked_url("http://evil.example/v2")


def _resp(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://api.dhan.co/v2/marketfeed/quote"))


def test_errors_name_the_fix_and_never_leak_the_token():
    e = error_from_response(_resp(401, {"errorCode": "DH-901", "errorMessage": f"bad token {TOKEN}"}), TOKEN)
    assert e.kind == "AUTH" and e.fatal and "access token" in e.fix and TOKEN not in str(e) + e.explain()
    assert error_from_response(_resp(400, {"errorCode": "DH-902", "errorMessage": "x"})).kind == "NOT_SUBSCRIBED"
    assert error_from_response(_resp(429, {})).kind == "RATE"
    assert error_from_response(_resp(502, {})).kind == "SERVER"
    assert error_from_response(_resp(403, {})).kind == "AUTH"

    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    c = DhanClient(CLIENT_ID, TOKEN, transport=httpx.MockTransport(boom), sleep=lambda s: None)
    with pytest.raises(DhanError) as ei:
        c.quotes({"IDX_I": [13]})
    assert ei.value.kind == "NETWORK" and TOKEN not in str(ei.value)


def test_rate_limit_is_retried_with_backoff_then_explained():
    t = [100.0]
    slept: list[float] = []

    def sleep(sec: float) -> None:
        slept.append(round(sec, 2))
        t[0] += sec

    fake = FakeDhan(FakeClock(SUNDAY))
    c = DhanClient(CLIENT_ID, TOKEN, transport=httpx.MockTransport(fake.handler), sleep=sleep, clock=lambda: t[0])
    fake.rate_limit["/marketfeed/quote"] = 2
    assert c.quotes({"IDX_I": [13]})[("IDX_I", "13")].ltp == pytest.approx(25_000)
    assert slept[:2] == [2.75, 4.25]                        # spacing 1.25 s after each response + 1.5 s, 3.0 s backoff
    fake.rate_limit["/marketfeed/quote"] = 5
    with pytest.raises(DhanError) as ei:
        c.quotes({"IDX_I": [13]})
    assert ei.value.kind == "RATE" and "Another program" in ei.value.fix


def test_spacing_is_measured_from_the_response():
    from sthitaprajna.data.dhan_live import MinInterval
    t = [0.0]
    slept: list[float] = []

    def sleep(sec: float) -> None:
        slept.append(sec)
        t[0] += sec

    lim = MinInterval(1.25, clock=lambda: t[0], sleep=sleep)
    lim.wait()                                              # first call: no wait
    t[0] += 0.4                                             # the request (and a TLS handshake) takes 0.4 s
    lim.done()
    lim.wait()
    assert slept == [pytest.approx(1.25)]                   # 1.25 s after the response, not after the send
    lim.done(extra=1.5)
    assert lim.remaining() == pytest.approx(2.75)


# ---------------------------------------------------------------- bootstrap on a Sunday (today's situation)
def test_sunday_shows_the_last_close_and_never_trades(tmp_path):
    m, fake, clock = live(tmp_path, SUNDAY)
    assert m.session == "PRE_OPEN" and m.day == MONDAY
    nifty = m.states["NSE:NIFTY"]
    assert nifty.security_id == "13" and m.states["BSE:SENSEX"].segment == "IDX_I"
    assert m.states["NSE:INDIAVIX"].security_id == "21"                 # found via the "INDIA VIX" alias
    assert m.states["NSE:RELIANCE"].security_id != "999001"             # the EQ row wins over a BE decoy
    assert "NSE:ETERNAL" not in m.states and any("ETERNAL" in p for p in m.universe.problems)
    assert m.lots["NIFTY"] == 65 and m.instruments["NSE:RELIANCE"].tick_size == 0.05
    assert nifty.price == pytest.approx(25_000) and nifty.prev_close > 0
    days = {c.start.date() for c in nifty.candles}
    assert days == {dt.date(2026, 9, 24), dt.date(2026, 9, 25)} and len(nifty.candles) == 750
    assert nifty.candles[0].start.time() == dt.time(9, 15) and nifty.candles[-1].start.time() == dt.time(15, 29)

    e = engine_on(m)
    for _ in range(20):
        cycle(m, e, clock, 30)
    s = snapshot(e)
    assert s["mode"] == "PAPER" and s["label"] == "PAPER" and s["market"]["source"] == "DHAN"
    assert s["market"]["session"] == "PRE_OPEN" and s["market"]["next_open"].startswith("2026-09-28T09:15")
    assert s["feeds"]["situation"] == "SECONDARY_DOWN" and s["feeds"]["data_confidence"] == 0.8
    assert not any(w["stale"] for w in s["watch"])                      # a closed market is not "stale"
    assert not e.positions and not e.orders and e.ledger.equity == CFG.starting_capital
    assert any("Upstox not configured" in ev["msg"] for ev in e.events)

    m2 = LiveMarket(CAL, client_for(fake), RULES, clock=clock, cache_dir=tmp_path, mono=clock.mono)
    m2.bootstrap(progress=lambda s: None, backfill_days=0, start_poller=False)
    assert fake.master_downloads == 1                                   # ids reused from the 7-day cache


# ---------------------------------------------------------------- the real session clock
def test_open_minute_bars_and_close_follow_the_exchange_clock(tmp_path):
    m, fake, clock = live(tmp_path, ist(2026, 9, 28, 9, 14, 50), backfill_days=1)
    assert m.session == "PRE_OPEN"
    events = cycle(m, None, clock, 10.5)                    # 09:15:00.5
    assert "open" in events and m.session == "OPEN"
    fake.set_price("RELIANCE", 1421.0, volume_add=5000)
    cycle(m, None, clock, 10)                               # 09:15:10
    fake.set_price("RELIANCE", 1419.5, volume_add=3000)
    cycle(m, None, clock, 30)                               # 09:15:40
    fake.set_price("RELIANCE", 1420.2, volume_add=1000)
    events = cycle(m, None, clock, 22)                      # 09:16:02 -> the 09:15 bar is complete
    assert "minute" in events
    bar = m.candles("NSE:RELIANCE")[-1]
    assert bar.start == ist(2026, 9, 28, 9, 15)
    assert (bar.open, bar.high, bar.low, bar.close) == (1420.0, 1421.0, 1419.5, 1419.5)
    assert bar.volume == 8000
    forming = m.candles("NSE:RELIANCE", include_forming=True)[-1]
    assert forming.start == ist(2026, 9, 28, 9, 16) and forming.close == 1420.2
    clock.t = ist(2026, 9, 28, 15, 30, 1)
    assert "close" in cycle(m, None, clock, 0) and m.session == "CLOSED"
    clock.t = ist(2026, 9, 29, 0, 1)
    cycle(m, None, clock, 0)
    assert m.session == "PRE_OPEN" and m.day == dt.date(2026, 9, 29)


def test_index_segment_rejected_by_quote_falls_back_to_ohlc(tmp_path):
    m, fake, _ = live(tmp_path, SUNDAY, backfill_days=0, quote_rejects_idx=True)
    assert m.client.segments_without_quote == {"IDX_I"}
    assert m.states["NSE:NIFTY"].price > 0 and m.states["NSE:RELIANCE"].quote.ask > 0
    assert any("/marketfeed/ohlc" in msg for _, msg in m.drain_notes())


def test_beta_is_estimated_from_real_bars():
    t0 = ist(2026, 9, 25, 9, 15)
    idx, stk = [], []
    pi = ps = 100.0
    for i in range(375):
        r = 0.001 * ((i * 7919) % 13 - 6) / 6
        pi *= 1 + r
        ps *= 1 + 1.5 * r
        t = t0 + dt.timedelta(minutes=i)
        idx.append(Candle("I", t, pi, pi, pi, pi, 0))
        stk.append(Candle("S", t, ps, ps, ps, ps, 0))
    assert estimate_beta(stk, idx) == pytest.approx(1.5, abs=0.05)
    assert estimate_beta(stk[:50], idx[:50]) is None


# ---------------------------------------------------------------- options
@pytest.mark.parametrize("pe_positive", [False, True])
def test_option_chain_contract_ids_and_quotes(tmp_path, pe_positive):
    m, fake, clock = live(tmp_path, ist(2026, 9, 28, 10, 0), backfill_days=0, pe_delta_positive=pe_positive)
    spot, rows = m.chain("NIFTY")
    assert rows == []                                       # never blocks the engine: fetched by the poller
    m.run_once()
    spot, rows = m.chain("NIFTY")
    assert len(rows) == 31 and spot == pytest.approx(25_000)
    assert all(r.ce_delta >= 0 and r.pe_delta <= 0 for r in rows)
    atm = min(rows, key=lambda r: abs(r.strike - spot))
    exp = m.expiries("NIFTY", 1)[0]
    inst = m.option_instrument("NIFTY", exp, atm.strike, OptionType.CE)
    assert inst.dhan_security_id and inst.lot_size == 65 and inst.kind is Kind.OPTION
    q = m.quote(inst.key)                                   # first answer comes from the chain
    assert q is not None and q.ask > 0
    clock.advance(2)
    m.run_once()                                            # now polled directly (with depth)
    m.step()
    assert inst.key in m._opt_quotes and m.depth(inst.key)["asks"]
    polled = [b for p, b in fake.requests if p.endswith("/marketfeed/quote") and "NSE_FNO" in b]
    assert polled and int(inst.dhan_security_id) in polled[-1]["NSE_FNO"]


# ---------------------------------------------------------------- failures while running
def test_expired_token_mid_session_is_explained_and_blocks_new_entries(tmp_path):
    m, fake, clock = live(tmp_path, ist(2026, 9, 28, 10, 0), backfill_days=0)
    e = engine_on(m)
    for _ in range(3):
        cycle(m, e, clock, 1)
    assert e.arbiter.situation == "SECONDARY_DOWN" and e.arbiter.entries_allowed()
    fake.error = {"status": 401, "code": "DH-901", "message": "Client ID or user generated access token is invalid or expired."}
    for _ in range(8):
        cycle(m, e, clock, 1)
    msgs = [ev["msg"] for ev in e.events]
    assert any("access token" in x for x in msgs) and not any(TOKEN in x for x in msgs)
    assert e.arbiter.situation == "BOTH_DOWN" and not e.arbiter.entries_allowed()
    assert any(h.startswith("feeds") for h in e._risk_ctx().operational_halts)
    assert m.health["error_kind"] == "AUTH"
    fake.error = None
    for _ in range(3):
        cycle(m, e, clock, 1)
    assert e.arbiter.situation == "SECONDARY_DOWN"          # recovers by itself once the token works


# ---------------------------------------------------------------- a full paper trade on live quotes
def test_paper_trade_fills_on_live_bid_ask_and_broker_stop(tmp_path):
    m, fake, clock = live(tmp_path, ist(2026, 9, 28, 10, 0), backfill_days=1)
    e = engine_on(m, pilot=True)
    e.strategies = []
    for _ in range(3):
        cycle(m, e, clock, 1)
    key = "NSE:RELIANCE"
    push(e, candidate(e, key, unit=10.0))
    pend = [a for a in e.approvals.values() if a.status == "PENDING"]
    assert len(pend) == 1 and pend[0].kind == "ENTRY"
    assert e.approve(pend[0].id, True)["ok"]
    for _ in range(3):
        cycle(m, e, clock, 1)
    assert len(e.positions) == 1
    p = next(iter(e.positions.values()))
    ask = m.states[key].quote.ask
    assert p.avg_entry == pytest.approx(ask) and p.stage is PositionStage.PROBE
    assert p.broker_stop_id in e.broker.resting
    fake.set_price("RELIANCE", p.broker_stop - 2.0)         # a real gap through the broker-side stop
    for _ in range(3):
        cycle(m, e, clock, 1)
    assert not e.positions
    closed = e.ledger.closed_trades[-1]
    assert closed["net"] < 0 and e.closed[-1].closed_legs[-1].price == pytest.approx(m.states[key].quote.bid)
    assert snapshot(e)["label"] == "PAPER"


# ---------------------------------------------------------------- poller thread, server, CLI, dhan-check
def test_background_poller_runs_and_stops(tmp_path):
    clock = FakeClock(ist(2026, 9, 28, 10, 0))
    fake = FakeDhan(clock)
    m = LiveMarket(CAL, client_for(fake), RULES, clock=clock, cache_dir=tmp_path)
    m.bootstrap(progress=lambda s: None, backfill_days=0, start_poller=True)
    time.sleep(0.4)
    m.close()
    assert m.health["polls"] > 2 and not m._thread.is_alive()


def test_closed_market_chain_request_wakes_the_poller(tmp_path):
    clock = FakeClock(SUNDAY)                     # closed: the poller normally sleeps 30 s between rounds
    fake = FakeDhan(clock)
    m = LiveMarket(CAL, client_for(fake), RULES, clock=clock, cache_dir=tmp_path)
    m.bootstrap(progress=lambda s: None, backfill_days=0, start_poller=True)
    try:
        time.sleep(0.2)
        t = time.monotonic()
        spot, rows = m.chain_blocking("NIFTY", timeout=5)
        assert rows and time.monotonic() - t < 3
    finally:
        m.close()


def test_server_serves_paper_state_chain_and_refuses_sim_controls(tmp_path):
    from fastapi.testclient import TestClient

    from sthitaprajna.api.server import build_paper_engine, create_app
    clock = FakeClock(ist(2026, 9, 28, 10, 0))
    fake = FakeDhan(clock)
    eng = build_paper_engine(client=client_for(fake), clock=clock, start_poller=False, backfill_days=1,
                             cache_dir=tmp_path, progress=lambda s: None)
    with TestClient(create_app(eng, run_loop=False)) as c:
        s = c.get("/api/state").json()
        assert s["mode"] == "PAPER" and s["market"]["source"] == "DHAN" and s["market"]["session"] == "OPEN"
        assert c.post("/api/actions/speed", json={"speed": 60}).json()["ok"] is False
        assert c.post("/api/actions/inject", json={"fault": "primary_down"}).json()["ok"] is False
        ch = c.get("/api/chain", params={"underlying": "NIFTY"}).json()
        assert len(ch["rows"]) == 31 and ch["lot"] == 65 and ch["source"] == "DHAN"
        d = c.get("/api/depth", params={"key": "NSE:RELIANCE"}).json()
        assert len(d["bids"]) == 5 and d["asks"][0]["price"] > d["bids"][0]["price"]
        f = c.get("/api/feasibility").json()
        assert f["label"].startswith("LIVE DHAN PREMIUMS") and f["rows"]


def test_cli_refuses_live_and_explains_missing_dhan_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for n in ("DHAN_CLIENT_ID", "DHAN_DATA_TOKEN", "DHAN_TRADING_TOKEN", "STHITAPRAJNA_LIVE_TRADING_CONFIRMED"):
        monkeypatch.delenv(n, raising=False)
    assert cli.main(["serve", "--mode", "live_pilot"]) == 2
    err = capsys.readouterr().err
    assert "DHAN_CLIENT_ID" in err and "DHAN_TRADING_TOKEN" in err
    monkeypatch.setenv("DHAN_CLIENT_ID", "x")
    monkeypatch.setenv("DHAN_TRADING_TOKEN", "y")
    assert cli.main(["serve", "--mode", "live"]) == 2         # credentials alone are not enough (D-040)
    assert "STHITAPRAJNA_LIVE_TRADING_CONFIRMED" in capsys.readouterr().err
    assert cli.main(["serve", "--mode", "paper"]) == 2
    assert "DHAN_DATA_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "var").exists()                   # nothing created before credentials check out


def test_dhan_check_report(tmp_path):
    clock = FakeClock(SUNDAY)
    fake = FakeDhan(clock)
    fake.rate_limit["/marketfeed/quote"] = 1                # the operator's first real run hit exactly this
    lines: list[str] = []
    assert run_check(lines.append, client=client_for(fake), cache_dir=tmp_path, today=SUNDAY.date()) == 0
    text = "\n".join(lines)
    assert "NIFTY=13(master)" in text and "[VERIFY] /marketfeed/quote NSE_EQ fields:" in text
    assert "NSE_EQ depth sides: buy, sell; level fields: orders, price, quantity" in text
    assert "consistent: ohlc.close is the previous close" in text
    assert "Result: READY" in text and TOKEN not in text and CLIENT_ID not in text
    quote_calls = [p for p, _ in fake.requests if p.endswith("/marketfeed/quote")]
    assert len(quote_calls) == 2                            # one 429 answered, then one retry: a single logical call
    fake.error = {"status": 401, "code": "DH-901", "message": "expired"}
    lines.clear()
    assert run_check(lines.append, client=client_for(fake), cache_dir=tmp_path, today=SUNDAY.date()) == 1
    assert "access token" in "\n".join(lines) and "NOT READY" in "\n".join(lines)
    assert json.loads((tmp_path / "dhan-universe.json").read_text())["items"]["NSE:NIFTY"]["security_id"] == "13"


# ----------------------------------------------------------------------------- D-040: LIVE gate, dummy Algo-ID, order-check
def test_cli_order_check_subcommand_is_wired_and_fails_closed_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for n in ("DHAN_CLIENT_ID", "DHAN_TRADING_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    assert cli.main(["order-check"]) == 1
    out = capsys.readouterr().out
    assert "DHAN_CLIENT_ID" in out and "NOT READY" in out


def test_cli_serve_live_starts_once_credentials_and_confirmation_are_both_present(tmp_path, monkeypatch, capsys):
    """The live engine build itself is exercised by test_live_orders.py against a fake Dhan API; this only
    checks the CLI's two-part gate lets a live start attempt through once both conditions are satisfied."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DHAN_CLIENT_ID", "x")
    monkeypatch.setenv("DHAN_TRADING_TOKEN", "y")
    monkeypatch.setenv("STHITAPRAJNA_LIVE_TRADING_CONFIRMED", "yes")
    assert cli.main(["serve", "--mode", "live"]) == 2   # fails past the gate, at the real Dhan connection attempt
    err = capsys.readouterr().err
    assert "needs one more explicit step" not in err and "missing DHAN_CLIENT_ID, DHAN_TRADING_TOKEN" not in err
    assert "Cannot start LIVE" in err                   # a connection failure, not a gate refusal


def test_paper_and_sim_engines_never_tag_orders_with_a_real_algo_id(monkeypatch):
    monkeypatch.setenv("DHAN_EXCHANGE_ALGO_ID", "REAL123")   # even if it's sitting in .env for later
    from sthitaprajna.api.server import build_engine

    e_sim = build_engine(Mode.SIM, warmup_days=1)
    assert e_sim.algo_id == "SIM-ALGO-0000"
    e_paper = Engine(CFG, RULES, CAL, mode=Mode.PAPER, warmup_days=1)  # no live market/broker -> not live_orders
    assert e_paper.mode is Mode.PAPER and e_paper.algo_id == "PAPER-ALGO-0000"
