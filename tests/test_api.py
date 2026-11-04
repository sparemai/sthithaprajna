import datetime as dt
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sthitaprajna.api import commands as cmds
from sthitaprajna.api.server import build_engine, create_app
from sthitaprajna.config import load_operator
from sthitaprajna.domain import IST
from sthitaprajna.discord_bot import format_approval, route


@pytest.fixture(scope="module")
def engine():
    e = build_engine(seed=4, warmup_days=2, start=dt.date(2026, 9, 28))
    e.run_until(dt.datetime.combine(dt.date(2026, 9, 28), dt.time(11, 30), tzinfo=IST), step_seconds=30)
    return e


@pytest.fixture(scope="module")
def client(engine):
    app = create_app(engine, run_loop=False)
    with TestClient(app) as c:
        yield c


def test_health_and_state(client):
    h = client.get("/api/health").json()
    assert h["ok"] and h["mode"] == "SIM" and h["label"] == "SIMULATED"
    s = client.get("/api/state").json()
    for k in ("clock", "account", "gear", "risk", "ceilings", "positions", "funnel", "kpis", "targets", "feeds",
              "process", "watch", "events"):
        assert k in s
    assert s["ceilings"]["max_risk_per_trade"] == 0.05 and s["ceilings"]["max_open_risk"] == 0.10
    assert len(s["watch"]) >= 30


def test_market_endpoints(client):
    c = client.get("/api/candles", params={"key": "NSE:NIFTY", "tf": 5, "limit": 100}).json()
    assert 50 <= len(c["candles"]) <= 100 and len(c["vwap"]) == len(c["candles"])
    ch = client.get("/api/chain", params={"underlying": "NIFTY"}).json()
    assert ch["rows"] and ch["summary"]["max_pain"] > 0 and ch["lot"] == 65
    assert client.get("/api/chain", params={"underlying": "FOO"}).status_code == 400
    d = client.get("/api/depth", params={"key": "NSE:RELIANCE"}).json()
    assert len(d["bids"]) == 5 and d["bids"][0]["price"] < d["asks"][0]["price"]
    assert client.get("/api/candles", params={"key": "NSE:NOPE"}).status_code == 404


def test_chart_overlays_endpoint(client, engine):
    """D-062: display-only technical analysis -- reversal patterns, OI-wall support/resistance (only for
    an underlying with options), and day-range Fibonacci retracement."""
    o = client.get("/api/chart_overlays", params={"key": "NSE:NIFTY", "tf": 5, "limit": 100}).json()
    assert o["support_resistance"]["underlying"] == "NIFTY"
    assert {r["strike"] for r in o["support_resistance"]["resistance"]}
    assert o["fibonacci"]["high"] > o["fibonacci"]["low"]
    assert o["fibonacci"]["levels"]["50.0"] == pytest.approx(
        (o["fibonacci"]["high"] + o["fibonacci"]["low"]) / 2, abs=0.01)
    assert all({"t", "labels"} <= set(p) for p in o["patterns"])
    o_stock = client.get("/api/chart_overlays", params={"key": "NSE:RELIANCE", "tf": 5, "limit": 100}).json()
    assert o_stock["support_resistance"]["underlying"] == "RELIANCE"
    assert client.get("/api/chart_overlays", params={"key": "NSE:NOPE"}).status_code == 404


def test_review_calendar_and_trades_endpoints(client):
    """D-054: planning/retrospection endpoints. This fixture's engine has no store, so /api/trades and the
    Zero-to-Hero capture history are empty, but the shapes and the live/current period must still be there."""
    r = client.get("/api/review").json()
    assert set(r["completed"]) == {"week", "month"} and set(r["current"]) == {"day", "week", "month"}
    cal = client.get("/api/calendar").json()
    assert {row["underlying"] for row in cal["expiries"]} == {"NIFTY", "SENSEX", "BANKNIFTY"}
    assert cal["zero_to_hero"]["total_trades"] == 0 and cal["zero_to_hero"]["captures_by_expiry"] == []
    t = client.get("/api/trades").json()
    assert t == {"total": 0, "trades": []}


def test_regime_history_endpoint(client, engine):
    """D-058: the regime timeline behind the web UI's regime strip -- at least the first point recorded
    at startup, each with the three fields the strip renders, and `since` trims it."""
    rh = client.get("/api/regime_history").json()
    assert rh["points"] and all({"ts", "trend", "volatility", "expiry"} <= set(p) for p in rh["points"])
    cutoff = rh["points"][-1]["ts"]
    trimmed = client.get("/api/regime_history", params={"since": cutoff}).json()
    assert all(p["ts"] >= cutoff for p in trimmed["points"])
    assert len(trimmed["points"]) <= len(rh["points"])


def test_rules_config_feasibility(client):
    r = client.get("/api/rules").json()
    assert r["rules"] and "freeze_qty_sensex" in r["open_items"]
    assert "freeze_qty_nifty" not in r["open_items"]  # D-050: moved to VERIFIED_SECONDARY
    cfg = client.get("/api/config").json()
    assert len(cfg["strategies"]) == 6 and all(s["card"]["validation_status"] == "UNVALIDATED" for s in cfg["strategies"])
    f = client.get("/api/feasibility").json()
    assert f["budgets"]["A"] == pytest.approx(4170) and f["budgets"]["A+"] == pytest.approx(5000)
    assert f["label"].startswith("SIM MODEL")


def test_operator_config_update_validates_and_persists(client, tmp_path, monkeypatch):
    """D-044: a within-ceiling change is written to disk; a ceiling-breaching change is rejected and
    never written. Redirects the write to a throwaway copy of operator.yaml so this never touches the
    real config file or the already-running engine's config."""
    tmp_cfg = tmp_path / "config"
    tmp_cfg.mkdir()
    shutil.copy(Path("config/operator.yaml"), tmp_cfg / "operator.yaml")
    monkeypatch.setenv("STHITAPRAJNA_CONFIG_DIR", str(tmp_cfg))
    load_operator.cache_clear()
    try:
        cfg = client.get("/api/config").json()
        assert "staging" in cfg["editable_sections"] and cfg["ceilings"]["max_open_risk"] == 0.10

        ok = client.post("/api/operator/config", json={"staging": {"breakeven_at_r": 0.6}}).json()
        assert ok["ok"] and ok["restart_required"] and ok["changed"] == ["staging"]
        written = yaml.safe_load((tmp_cfg / "operator.yaml").read_text())
        assert written["staging"]["breakeven_at_r"] == 0.6

        bad = client.post("/api/operator/config", json={"gears": {"ATTACK": {"open_risk": 0.5}}}).json()
        assert not bad["ok"] and "ceiling" in bad["error"]
        still = yaml.safe_load((tmp_cfg / "operator.yaml").read_text())
        assert still["gears"]["ATTACK"]["open_risk"] != 0.5   # rejected change was never written

        ignored = client.post("/api/operator/config", json={"capital": {"starting": 999}}).json()
        assert ignored["ok"] and ignored["changed"] == []     # capital isn't in OPERATOR_EDITABLE_SECTIONS
    finally:
        load_operator.cache_clear()


def test_research_endpoints(client):
    mc = client.post("/api/research/mc", json={"p": 0.45, "b": 1.8, "policy": "attack", "n": 2000, "t": 50}).json()
    assert mc["label"].startswith("SIMULATION") and len(mc["bands"]["p50"]) == 50
    assert client.post("/api/research/mc", json={"p": 2}).status_code == 400
    t = client.post("/api/research/target", json={"period": "month", "value": 8}).json()
    assert t["label"].startswith("PROBABILITY UNKNOWN")


def test_actions_cannot_open_positions(client):
    for bad in ("buy", "sell", "order", "open", "add", "widen", "gear_up", "set_config"):
        assert client.post(f"/api/actions/{bad}", json={}).status_code == 404


def test_state_check_and_target_do_not_raise_risk(client, engine):
    before = engine.gears.effective_gear().rank
    planned_before = client.get("/api/state").json()["risk"]["planned_by_grade"]
    r = client.post("/api/actions/target", json={"period": "month", "value": 50, "unit": "PCT"}).json()
    assert r["ok"] and r["feasibility"]["risk_effect"].startswith("NONE")
    assert client.get("/api/state").json()["risk"]["planned_by_grade"] == planned_before
    client.post("/api/actions/state_check", json={"slept_well": True, "calm": False, "undistracted": True})
    assert engine.gears.effective_gear().rank >= before


def test_command_bar_and_sim_controls(client):
    r = client.post("/api/actions/command", json={"text": "/status"}).json()
    assert r["ok"] and "Gear" in r["reply"]
    assert not client.post("/api/actions/command", json={"text": "/buy RELIANCE"}).json()["ok"]
    assert client.post("/api/actions/speed", json={"speed": 60}).json()["speed"] == 60
    assert client.post("/api/actions/inject", json={"fault": "secondary_down", "seconds": 5}).json()["ok"]
    assert client.post("/api/actions/inject", json={"fault": "clear"}).json()["ok"]
    assert not client.post("/api/actions/inject", json={"fault": "meteor"}).json()["ok"]


def test_kill_and_resume(client, engine):
    assert client.post("/api/actions/kill").json()["ok"] and engine.kill_switch
    assert not client.post("/api/actions/resume", json={"reviewed": False}).json()["ok"]
    assert client.post("/api/actions/resume", json={"reviewed": True}).json()["ok"] and not engine.kill_switch


def test_websocket_streams_snapshot(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot" and "account" in msg
        ws.send_text("ping")
        assert ws.receive_json()["type"] == "pong"


def test_token_auth(engine, monkeypatch):
    monkeypatch.setenv("STHITAPRAJNA_API_TOKEN", "s3cret")
    app = create_app(engine, run_loop=False)
    with TestClient(app) as c:
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/state", headers={"x-operator-token": "s3cret"}).status_code == 200


def test_commands_operator_gate_and_rate_limit(engine):
    gate = cmds.CommandGate(operator_id="42", max_per_window=3)
    assert not cmds.handle(engine, "/status", user_id="7", gate=gate)["ok"]
    for _ in range(3):
        assert cmds.handle(engine, "/status", user_id="42", gate=gate)["ok"]
    assert cmds.handle(engine, "/status", user_id="42", gate=gate)["reply"] == "rate limited"


def test_no_command_can_raise_risk(engine):
    """Every command in the whitelist leaves gear rank and planned risk no higher than before."""
    from sthitaprajna.engine.snapshot import snapshot
    samples = ["/status", "/gear", "/positions", "/pnl", "/queue", "/help", "/target day 1500", "/target month 12%",
               "/lock week", "/close nope", "/reduce nope", "/trail nope", "/approve x", "/deny x"]
    assert {s.split()[0] for s in samples} | {"/kill"} == cmds.ALL_COMMANDS
    for text in samples:
        g0 = engine.gears.effective_gear().rank
        p0 = snapshot(engine, include_watch=False)["risk"]["planned_by_grade"]
        cmds.handle(engine, text)
        assert engine.gears.effective_gear().rank >= g0, text
        p1 = snapshot(engine, include_watch=False)["risk"]["planned_by_grade"]
        assert all(p1[k] <= p0[k] + 1e-9 for k in p0), text


def test_discord_formatting_and_routing():
    assert route({"kind": "exit"}) == "trades" and route({"kind": "gear"}) == "risk" and route({"kind": "x"}) == "system"
    a = {"id": "apr-1", "kind": "ENTRY", "seconds_left": 42.0, "summary": {
        "strategy": "orb", "symbol": "RELIANCE", "exposure": "LONG", "grade": "A", "gear": "ATTACK", "stage": "PROBE",
        "qty": 30, "full_qty": 60, "entry": 1400, "stop": 1390, "risk_rupees": 300.0, "ev_r": 0.25, "confidence": 0.6,
        "quality": 71.0, "reason": "test", "harvest": "40% at +2R", "p_calibrated": False}}
    txt = format_approval(a)
    assert "UNCALIBRATED" in txt and "timeout = skip" in txt and "token" not in txt.lower()
