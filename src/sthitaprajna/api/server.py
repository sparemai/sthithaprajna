"""Sthitaprajna terminal server: REST + WebSocket over the running engine, and the web UI.

* Binds to 127.0.0.1 by default. For a remote VM set STHITAPRAJNA_API_TOKEN: every /api and /ws call
  must then carry it (header `x-operator-token` or `?token=`). Never expose the port without it.
* Operator actions mirror D6/D10: view, approve/deny, close, reduce, trail tighter, lock, target,
  state check, kill, resume-after-review. There is deliberately NO endpoint that opens or enlarges
  a position, widens a stop, or raises the gear.
* Part D's gears/grades/staging/limits/sleeve/edge_brake/targets sections ARE editable, from
  /api/operator/config (D-044) -- but every write is re-validated with the same validate_operator()
  check the YAML file itself goes through, so it can never loosen a hard ceiling (risk/ceilings.py);
  a change takes effect on the next restart, never mid-session. capital, execution (SEBI order-rate
  cap), risk_mode and the hard ceilings themselves stay file/code-only, never reachable from here.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import yaml

from sthitaprajna import __version__
from sthitaprajna.api import commands as cmds
from sthitaprajna.config import ConfigError, config_dir, load_holidays, load_operator, load_rules, operator_from_dict
from sthitaprajna.domain import Mode, OptionType
from sthitaprajna.engine.runtime import Engine
from sthitaprajna.engine.snapshot import expiry_calendar, period_review, snapshot, watchlist, zero_to_hero_report
from sthitaprajna.market import indicators as ind
from sthitaprajna.market.calendar import MarketCalendar
from sthitaprajna.market import patterns as pat
from sthitaprajna.market.options import oi_walls, summary as chain_summary
from sthitaprajna.research import backtest, montecarlo
from sthitaprajna.research.feasibility import feasibility_table
from sthitaprajna.research.target_plan import feasibility
from sthitaprajna.risk import ceilings
from sthitaprajna.strategies.options import UNDERLYINGS as INDEX_UNDERLYING_KEYS

STATIC = Path(__file__).parent / "static"

# D-044: the only Part D sections the terminal may retune (everything else -- capital, execution's SEBI
# order-rate cap, risk_mode, tiers, data, discord, approvals, overnight -- is file/CLI-only).
OPERATOR_EDITABLE_SECTIONS = ("gears", "grades", "staging", "limits", "sleeve", "edge_brake", "targets")


def _merge_operator_raw(raw: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Deep-merges only OPERATOR_EDITABLE_SECTIONS from `patch` into a copy of the current raw operator.yaml
    dict; a patched dict-of-dict (gears, grades) merges one level deep so a partial patch (e.g. just
    {"gears": {"ATTACK": {"r": 0.03}}}) doesn't drop every other gear's settings."""
    import copy
    merged = copy.deepcopy(raw)
    for section, values in patch.items():
        if section not in OPERATOR_EDITABLE_SECTIONS or not isinstance(values, dict):
            continue
        cur = merged.setdefault(section, {})
        for k, v in values.items():
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                cur[k].update(v)
            else:
                cur[k] = v
    return merged
TICK_S = 0.25
PUSH_S = 0.5


class Hub:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.clients: set[WebSocket] = set()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.d12_cache: list[dict[str, Any]] | None = None
        self.gate = cmds.CommandGate(max_per_window=30)
        self.last_snapshot: dict[str, Any] | None = None


def build_engine(mode: Mode = Mode.SIM, seed: int = 7, speed: float = 30.0, start: dt.date | None = None,
                 warmup_days: int = 5, store=None) -> Engine:
    cfg, rules = load_operator(), load_rules()
    cal = MarketCalendar.from_rules(rules, load_holidays())
    return Engine(cfg, rules, cal, mode=mode, seed=seed, speed=speed, start=start, warmup_days=warmup_days,
                  store=store)


def build_paper_engine(store=None, progress=print, *, client=None, clock=None, start_poller: bool = True,
                       backfill_days: int = 5, cache_dir: Path = Path("var/cache")) -> Engine:
    """PAPER on live Dhan data (data/live.py): real quotes and history, simulated fills, Upstox optional."""
    from sthitaprajna.data.dhan_live import DhanClient
    from sthitaprajna.data.live import DisabledFeed, LiveFeed, LiveMarket
    cfg, rules = load_operator(), load_rules()
    cal = MarketCalendar.from_rules(rules, load_holidays())
    client = client or DhanClient(rules=rules)
    market = LiveMarket(cal, client, rules, clock=clock, cache_dir=cache_dir)
    try:
        market.bootstrap(progress, backfill_days=backfill_days, start_poller=start_poller)
    except BaseException:
        market.close()
        raise
    return Engine(cfg, rules, cal, mode=Mode.PAPER, store=store, market=market,
                  feeds=(LiveFeed(market), DisabledFeed()))


def build_live_engine(store=None, progress=print, *, client=None, order_api=None, clock=None,
                      start_poller: bool = True, backfill_days: int = 5, cache_dir: Path = Path("var/cache"),
                      wall=time.monotonic) -> Engine:
    """LIVE on Dhan (operator decision D-034): live Dhan data and REAL orders through the live broker."""
    from sthitaprajna.data.dhan_live import DhanClient
    from sthitaprajna.data.live import DisabledFeed, LiveFeed, LiveMarket
    from sthitaprajna.execution.live import DhanOrderAPI, LiveBroker, status_of
    from sthitaprajna.market.charges import ChargesModel
    cfg, rules = load_operator(), load_rules()
    cal = MarketCalendar.from_rules(rules, load_holidays())
    client = client or DhanClient(rules=rules)
    order_api = order_api or DhanOrderAPI()
    market = LiveMarket(cal, client, rules, clock=clock, cache_dir=cache_dir, mono=wall)
    broker = None
    try:
        broker = LiveBroker(order_api, ChargesModel(rules), market.quote, wall=wall, start_poller=False)
        broker.poll_once(account=True)              # the order token works, funds known, before anything else
        market.bootstrap(progress, backfill_days=backfill_days, start_poller=start_poller)
        foreign_orders = [r for r in broker.poll_api.orders()
                          if status_of(r) in ("PENDING", "TRANSIT", "PART_TRADED", "TRIGGERED")]
    except BaseException:
        market.close()
        if broker is not None:
            broker.close()
        raise
    e = Engine(cfg, rules, cal, mode=Mode.LIVE_FULL, store=store, market=market,
               feeds=(LiveFeed(market), DisabledFeed()), broker=broker, wall_clock=wall)
    snap = broker.position_snapshot()
    held = [k for k, v in (snap[2] if snap else {}).items() if v["net"]]
    if broker.available_funds is not None:
        e._log("system", f"Dhan funds available: Rs {broker.available_funds:,.0f} (capital used per trade is capped "
                         "by this and by the configured capital)")
    if held or foreign_orders:
        e._log("risk", f"Dhan shows {len(held)} open intraday position(s) and {len(foreign_orders)} working order(s) "
                       "that this run did not create (an earlier run or your own trades): they are NOT managed. "
                       "Close or cancel them in the Dhan app if they were Sthitaprajna's.")
    if start_poller:
        broker.start()
    return e


def create_app(engine: Engine | None = None, *, run_loop: bool = True, discord: bool = False) -> FastAPI:
    token = os.environ.get("STHITAPRAJNA_API_TOKEN", "").strip()
    hub_holder: dict[str, Hub] = {}

    async def loop(hub: Hub) -> None:
        last_push = 0.0
        last = time.monotonic()
        while True:
            now = time.monotonic()
            dt_real = min(now - last, 1.0)
            last = now
            try:
                hub.engine.tick(dt_real)
            except Exception as exc:  # the loop must never die silently
                hub.engine._log("error", f"engine tick failed: {exc!r}")
            if now - last_push >= PUSH_S and hub.clients:
                last_push = now
                snap = snapshot(hub.engine)
                hub.last_snapshot = snap
                dead = []
                for ws in list(hub.clients):
                    try:
                        await ws.send_json(snap)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    hub.clients.discard(ws)
            await asyncio.sleep(TICK_S)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        eng = engine or build_engine()
        hub = Hub(eng)
        hub_holder["hub"] = hub
        task = asyncio.create_task(loop(hub)) if run_loop else None
        bot = None
        if discord:
            from sthitaprajna.discord_bot import run_bot
            bot = run_bot(eng)
        try:
            yield
        finally:
            if task:
                task.cancel()
            if bot:
                await bot.stop()
            close = getattr(eng.market, "close", None)     # live data: stop the Dhan poller
            if close:
                await run_in_threadpool(close)
            stop_broker = getattr(eng.broker, "close", None)   # LIVE: stop the order-book poller
            if stop_broker:
                await run_in_threadpool(stop_broker)

    app = FastAPI(title="Sthitaprajna", version=__version__, lifespan=lifespan)

    def hub() -> Hub:
        return hub_holder["hub"]

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if token and request.url.path.startswith("/api"):
            supplied = request.headers.get("x-operator-token") or request.query_params.get("token")
            if supplied != token:
                return JSONResponse({"error": "operator token required"}, status_code=401)
        return await call_next(request)

    # ------------------------------------------------------------------ read
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        e = hub().engine
        return {"ok": True, "version": __version__, "mode": e.mode.value, "label": e.perf_label.value,
                "now": e.now.isoformat(), "auth": bool(token)}

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return snapshot(hub().engine)

    @app.get("/api/watchlist")
    def get_watch() -> list[dict[str, Any]]:
        return watchlist(hub().engine)

    @app.get("/api/scanner")
    def scanner_rows(segment: str = "all", direction: str = "all", tradable: str = "all", q: str = "",
                     limit: int = Query(200, ge=1, le=3000)) -> dict[str, Any]:
        """The whole ranked universe (the snapshot carries only the top 30)."""
        e = hub().engine
        rows = e.scanner_rows
        if segment == "fno":
            rows = [r for r in rows if r.fno]
        elif segment == "cash":
            rows = [r for r in rows if not r.fno]
        if direction in ("up", "down"):
            rows = [r for r in rows if r.direction == direction.upper()]
        if tradable == "yes":
            rows = [r for r in rows if r.tradable]
        if q:
            qq = q.strip().upper()
            rows = [r for r in rows if r.symbol.startswith(qq) or qq in r.name.upper()]
        summary = snapshot(e, include_watch=False)["scanner"]["summary"]
        return {"summary": summary, "total": len(rows), "rows": [r.to_dict() for r in rows[:limit]],
                "label": e.perf_label.value}

    @app.get("/api/search")
    def search(q: str = Query("", max_length=40)) -> list[dict[str, Any]]:
        return hub().engine.market.search(q)

    @app.get("/api/candles")
    def candles(key: str, tf: int = Query(1, ge=1, le=60), limit: int = Query(400, ge=10, le=3000)) -> dict[str, Any]:
        from sthitaprajna.market import indicators as ind
        e = hub().engine
        in_universe = key in e.market.states or key in getattr(e.market, "screen", {})
        if not in_universe:
            raise HTTPException(404, f"unknown instrument {key}")
        c1 = e.market.view_candles(key) if key not in e.market.states else e.market.candles(key, include_forming=True)
        c = ind.resample(c1[-(limit * tf + tf):], tf) if tf > 1 else c1[-limit:]
        vwap = []
        pv = v = 0.0
        day = None
        for b in c:
            d = (b.t + 19800) // 86400
            if d != day:
                day, pv, v = d, 0.0, 0.0
            pv += (b.high + b.low + b.close) / 3 * b.volume
            v += b.volume
            vwap.append({"t": b.t, "v": round(pv / v, 2) if v else b.close})
        return {"key": key, "tf": tf, "candles": [b.to_dict() for b in c[-limit:]], "vwap": vwap[-limit:],
                "note": "last bar may be forming (display only; strategies see completed bars)"}

    @app.get("/api/chart_overlays")
    def chart_overlays(key: str, tf: int = Query(5, ge=1, le=60),
                       limit: int = Query(320, ge=10, le=1000)) -> dict[str, Any]:
        """D-062: chart-level technical analysis, display-only -- never read by any strategy's entry/EV
        logic, same as the Chain page's PCR/max-pain/OI-wall figures before D-055 made some of them
        Confirmations. Three pieces, each reusing an existing, already-grounded computation rather than
        inventing a new one: (1) candlestick reversal patterns (`market/patterns.py`, already used as
        strategy Confirmations -- here just drawn where they fire); (2) support/resistance from option-
        chain OI walls (`market/options.py:oi_walls`, already shown on the Chain page) for whichever
        underlying this instrument actually is, index or F&O stock -- omitted, not guessed, for anything
        else (a plain stock with no options, or an option contract's own premium chart); (3) Fibonacci
        retracement levels anchored on the session's OWN day high/low (the same reference
        `DayRangeBreakout` already trades off), not a subjectively eyeballed swing."""
        e = hub().engine
        in_universe = key in e.market.states or key in getattr(e.market, "screen", {})
        if not in_universe:
            raise HTTPException(404, f"unknown instrument {key}")
        c1 = e.market.view_candles(key) if key not in e.market.states else e.market.candles(key, include_forming=True)
        c = ind.resample(c1[-(limit * tf + tf):], tf) if tf > 1 else c1[-limit:]
        c = c[-limit:]

        patterns = []
        for i in range(1, len(c)):
            prev, cur = c[i - 1], c[i]
            fired = {
                "bullish_engulfing": pat.bullish_engulfing(prev, cur), "bearish_engulfing": pat.bearish_engulfing(prev, cur),
                "hammer": pat.hammer(cur), "shooting_star": pat.shooting_star(cur), "doji": pat.doji(cur),
            }
            labels = [k for k, v in fired.items() if v]
            if labels:
                bullish = any(lbl in ("bullish_engulfing", "hammer") for lbl in labels) or (fired["doji"] and cur.close > cur.open)
                patterns.append({"t": cur.t, "labels": labels, "bullish": bullish})
        if len(c) >= 22 and pat.breakout_pattern(c):
            patterns.append({"t": c[-1].t, "labels": ["range_compression_breakout"], "bullish": c[-1].close > c[-1].open})
        patterns.sort(key=lambda p: p["t"])
        # these single/two-bar shapes (doji, hammer...) are loosely defined and fire often -- over a
        # multi-day window that is a wall of markers, not something to look at. Cap to the most recent
        # ones; detection itself still ran over the full fetched series, so the prev-bar context behind
        # each one is correct, only the DISPLAY list is trimmed.
        patterns = patterns[-60:]

        sr = None
        chain_syms = {k: sym for sym, k in list(INDEX_UNDERLYING_KEYS) + list(e.market.option_underlyings())}
        sym = chain_syms.get(key)
        if sym:
            exps = e.market.expiries(sym, 1)
            if exps:
                spot, rows = e.market.chain(sym, exps[0])
                if rows:
                    w = oi_walls(rows, spot, top=3)
                    sr = {"underlying": sym, "expiry": exps[0].isoformat(), "spot": round(spot, 2), **w}

        fib = None
        today = ind.session_candles(c1)
        if today:
            hi, lo = max(x.high for x in today), min(x.low for x in today)
            if hi > lo:
                span = hi - lo
                fib = {"high": round(hi, 2), "low": round(lo, 2),
                      "levels": {pct: round(hi - pct / 100 * span, 2)
                                for pct in (0.0, 23.6, 38.2, 50.0, 61.8, 78.6, 100.0)}}

        return {"key": key, "tf": tf, "patterns": patterns, "support_resistance": sr, "fibonacci": fib,
                "note": "display-only technical analysis -- never read by any strategy's entry or EV logic"}

    @app.get("/api/chain")
    def chain(underlying: str = "NIFTY", expiry: str | None = None) -> dict[str, Any]:
        e = hub().engine
        u = underlying.upper()
        if u not in ("NIFTY", "SENSEX", "BANKNIFTY"):
            raise HTTPException(400, "underlying must be NIFTY, SENSEX or BANKNIFTY")
        exps = e.market.expiries(u, 4)
        exp = dt.date.fromisoformat(expiry) if expiry else exps[0]
        fetch = getattr(e.market, "chain_blocking", None)    # live: wait for the first fetch (API thread only)
        spot, rows = fetch(u, exp) if fetch else e.market.chain(u, exp)
        chg_pct = rv = None
        ukey = dict(INDEX_UNDERLYING_KEYS).get(u)
        if ukey:
            c1 = e.market.candles(ukey)
            today = ind.session_candles(c1)
            if today and today[0].open:
                chg_pct = (today[-1].close - today[0].open) / today[0].open
            rv = ind.realized_vol(ind.daily_closes(c1, 20))
        iv_hist = e.store.iv_history_for(u, 252) if e.store else []
        dte_days = round(e.cal.trading_minutes_to(e.now, exp) / 375.0, 3)
        sm = chain_summary(rows, spot, underlying_chg_pct=chg_pct, iv_history=iv_hist, realized_vol=rv,
                           dte_days=dte_days)
        lot = e.market.option_instrument(u, exp, rows[0].strike, OptionType.CE).lot_size if rows else \
            int(e.rules.get(f"lot_size_{u.lower()}", 0) or 0)
        return {"underlying": u, "expiry": exp.isoformat(), "expiries": [x.isoformat() for x in exps],
                "spot": round(spot, 2), "summary": sm, "rows": [r.to_dict() for r in rows],
                "dte_minutes": round(e.cal.trading_minutes_to(e.now, exp), 1),
                "lot": lot, "label": e.perf_label.value, "source": e.market.source.value,
                "note": None if rows else "option chain loading from Dhan -- retry in a few seconds"}

    @app.get("/api/depth")
    def depth(key: str) -> dict[str, Any]:
        return hub().engine.market.depth(key)

    @app.get("/api/equity")
    def equity() -> dict[str, Any]:
        e = hub().engine
        return {"label": e.perf_label.value, "curve": e.ledger.equity_curve[-6000:], "daily": e.ledger.daily}

    @app.get("/api/decisions")
    def decisions(limit: int = Query(200, ge=1, le=600), action: str | None = None) -> list[dict[str, Any]]:
        ds = list(hub().engine.decisions)[::-1]
        if action:
            ds = [d for d in ds if d["action"] == action.upper()]
        return ds[:limit]

    @app.get("/api/journal")
    def journal() -> dict[str, Any]:
        e = hub().engine
        return {"days": e.journal[::-1], "manual_exits": e.manual_exits, "calibration": e.calibration.to_dict()}

    @app.get("/api/regime_history")
    def regime_history(since: str | None = None) -> dict[str, Any]:
        """D-058: the session's (trend, volatility, expiry) timeline, so the web UI can show a regime
        strip alongside the index chart -- "why no trade was taken" is often simply "the regime wasn't
        one any strategy trades." `since` (ISO datetime) trims to one day's worth for a long-running SIM."""
        points = hub().engine.regime_history
        if since:
            points = [p for p in points if p["ts"] >= since]
        return {"points": points}

    @app.get("/api/review")
    def review() -> dict[str, Any]:
        """D-054: daily/weekly/monthly plan vs actual. Daily is /api/journal's own `days`; this adds the
        completed week/month history (plan-vs-actual, persisted at each period's close) plus the live
        current period for all three, so a session can tell at a glance whether it's on plan."""
        return period_review(hub().engine)

    @app.get("/api/calendar")
    def calendar(count: int = Query(4, ge=1, le=12)) -> dict[str, Any]:
        """D-054: upcoming expiries per underlying, plus the Zero-to-Hero sleeve's capture history and
        budget -- the planning surface for "expiries, zero to hero captures"."""
        e = hub().engine
        return {"expiries": expiry_calendar(e, count), "zero_to_hero": zero_to_hero_report(e)}

    @app.get("/api/trades")
    def trades(limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        """D-054: the full closed-trade history from the store (not the 6000-point equity-curve cap, not
        the journal's daily rollups) -- "keeping all the data for the entire 4 months ... review every
        trade." Newest first."""
        e = hub().engine
        all_trades = e.store.closed_trade_history()[::-1] if e.store else []
        return {"total": len(all_trades), "trades": all_trades[offset:offset + limit]}

    @app.get("/api/rules")
    def rules() -> dict[str, Any]:
        e = hub().engine
        today = e.now.date()
        return {"version": e.rules.version, "stale_after_days": e.rules.stale_after_days,
                "rules": e.rules.report(today), "open_items": e.rules.open_items(today)}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        e = hub().engine
        return {"part_d": e.cfg.raw, "strategies": [s.info() for s in e.strategies],
                "editable_sections": list(OPERATOR_EDITABLE_SECTIONS),
                "ceilings": {"max_risk_per_trade": ceilings.MAX_RISK_PER_TRADE, "max_open_risk": ceilings.MAX_OPEN_RISK,
                             "halt_drawdown": ceilings.HALT_DRAWDOWN, "ruin_floor_abs_min": ceilings.RUIN_FLOOR_ABS_MIN,
                             "ruin_floor_pct_min": ceilings.RUIN_FLOOR_PCT_MIN, "sebi_ops_threshold": ceilings.SEBI_OPS_THRESHOLD},
                "note": ("Part D's gears/grades/staging/limits/sleeve/edge_brake/targets sections are editable "
                         "from /api/operator/config (D-044), validated against the ceilings above before anything "
                         "is written -- Part D can only ever be stricter than them, never looser. Everything else "
                         "here (capital, execution, risk_mode, tiers, data, discord, approvals) is file/CLI-only.")}

    @app.post("/api/operator/config")
    def update_operator_config(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """D-044: retune Part D (gears/grades/staging/limits/sleeve/edge_brake/targets) from the terminal.
        Every change is validated with the same validate_operator() check config/operator.yaml itself goes
        through before anything is written -- nothing here can loosen a hard ceiling (risk/ceilings.py);
        that stays a code change, gate (e). Saved changes take effect on the next restart: guardian, funnel,
        gears, sizer and staging each capture Part D once at startup, so hot-swapping mid-session would leave
        them reading different numbers from each other, which is worse than requiring a restart.

        KNOWN LIMITATION: this writes operator.yaml with yaml.safe_dump, which does not round-trip the
        file's hand-written comments (every number in it currently carries an explanation and a decision
        reference). The first save from this endpoint strips them. Fixing that needs a comment-preserving
        writer (e.g. ruamel.yaml) -- not done here; flagged rather than silently accepted."""
        e = hub().engine
        merged = _merge_operator_raw(e.cfg.raw, body)
        try:
            operator_from_dict(merged)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        path = config_dir() / "operator.yaml"
        path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
        load_operator.cache_clear()
        changed = sorted(set(body) & set(OPERATOR_EDITABLE_SECTIONS))
        e._log("config", f"Part D updated from the terminal: {', '.join(changed)} (restart to apply)")
        return {"ok": True, "restart_required": True, "changed": changed,
                "note": "Saved to config/operator.yaml and validated against the hard ceilings. Restart the "
                        "server for the running engine to pick this up. Note: this rewrites the whole file "
                        "and drops its hand-written comments -- the numbers are correct, the documentation "
                        "explaining them is not preserved yet."}

    @app.get("/api/feasibility")
    def feas() -> dict[str, Any]:
        e = hub().engine
        return feasibility_table(e, e.cfg)

    # ------------------------------------------------------------------ research
    @app.get("/api/research/d12")
    async def d12() -> dict[str, Any]:
        h = hub()
        if h.d12_cache is None:
            h.d12_cache = await run_in_threadpool(montecarlo.reproduce_d12_table)
        return {"label": "SIMULATION (reproduces MASTER_PROMPT D12.1 exactly)", "rows": h.d12_cache}

    @app.post("/api/research/mc")
    async def mc(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        e = hub().engine
        p = float(body.get("p", 0.45))
        b = float(body.get("b", 1.8))
        if not (0.05 <= p <= 0.95 and 0.2 <= b <= 10):
            raise HTTPException(400, "p must be in [0.05, 0.95] and b in [0.2, 10]")
        policy = body.get("policy", "attack")
        if policy not in ("baseline", "attack", "attack_brake"):
            raise HTTPException(400, "policy must be baseline | attack | attack_brake")
        return await run_in_threadpool(montecarlo.run_custom, e.cfg, p=p, b=b, policy=policy,
                                       n=int(body.get("n", 20000)), t=int(body.get("t", 250)),
                                       gap_p=float(body.get("gap_p", 0.04)), gap_loss=float(body.get("gap_loss", 2.5)),
                                       seed=int(body.get("seed", 7)))

    @app.post("/api/research/target")
    def target_preview(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Feasibility preview without setting a target (A16)."""
        e = hub().engine
        period = body.get("period", "month")
        value = float(body.get("value", 8))
        unit = str(body.get("unit", "PCT")).upper()
        base = {"day": e.ledger.day_start, "week": e.ledger.week_start, "month": e.ledger.month_start}[period]
        pct = value / 100 if unit == "PCT" else value / base
        days = max(1, len(e.ledger.daily))
        tpd = max(0.5, len(e.ledger.r_outcomes) / days) if e.ledger.r_outcomes else 2.0
        return feasibility(e.cfg, target_pct=pct, period=period, r_outcomes=e.ledger.r_outcomes,
                           label=e.perf_label.value, equity=base, trades_per_day=tpd)

    @app.post("/api/research/backtest")
    def start_backtest(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        h = hub()
        days = int(min(max(int(body.get("days", 5)), 1), 30))
        seed = int(body.get("seed", 7))
        compare = bool(body.get("compare_staging", False))
        job = str(uuid.uuid4())[:8]
        h.jobs[job] = {"id": job, "status": "running", "started": time.time(), "days": days, "seed": seed}
        e = h.engine

        def work() -> None:
            try:
                if compare:
                    res = backtest.compare_staging(e.cfg, e.rules, e.cal, days=days, seeds=(seed,))
                else:
                    res = backtest.run(e.cfg, e.rules, e.cal, days=days, seed=seed)
                h.jobs[job].update(status="done", result=res)
            except Exception as exc:  # pragma: no cover
                h.jobs[job].update(status="error", error=repr(exc))
        threading.Thread(target=work, daemon=True).start()
        return {"job": job}

    @app.get("/api/research/jobs/{job}")
    def job(job: str) -> dict[str, Any]:
        j = hub().jobs.get(job)
        if not j:
            raise HTTPException(404, "no such job")
        return j

    # ------------------------------------------------------------------ operator actions (D6 / D9 / D10)
    @app.post("/api/actions/{action}")
    def act(action: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        e = hub().engine
        try:
            if action == "approve":
                return e.approve(str(body["id"]), bool(body.get("approved", True)))
            if action == "close":
                return e.close(str(body["position_id"]), float(body.get("fraction", 1.0)))
            if action == "trail":
                return e.trail_tighter(str(body["position_id"]))
            if action == "close_all":
                return e.close_all()
            if action == "lock":
                return e.lock(str(body.get("period", "day")))
            if action == "target":
                return e.set_target(str(body["period"]), float(body["value"]), str(body.get("unit", "PCT")))
            if action == "kill":
                return e.kill()
            if action == "resume":
                return e.resume(bool(body.get("reviewed", False)))
            if action == "state_check":
                return e.state_check_answer(bool(body.get("slept_well")), bool(body.get("calm")),
                                            bool(body.get("undistracted")))
            if action == "pilot_approvals":
                return e.set_pilot_approvals(bool(body.get("on", True)))
            if action == "speed":
                return e.set_speed(float(body.get("speed", 30)))
            if action == "pause":
                if e.mode is not Mode.SIM:
                    return {"ok": False, "error": "pause exists only in SIM"}
                e.paused = bool(body.get("paused", not e.paused))
                return {"ok": True, "paused": e.paused}
            if action == "inject":
                return e.inject(str(body.get("fault", "")), float(body.get("seconds", 60)))
            if action == "command":
                return cmds.handle(e, str(body.get("text", "")), gate=hub().gate)
        except KeyError as exc:
            raise HTTPException(400, f"missing field {exc}") from exc
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        raise HTTPException(404, f"unknown action {action}")

    # ------------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if token and websocket.query_params.get("token") != token:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        h = hub()
        h.clients.add(websocket)
        try:
            await websocket.send_json(snapshot(h.engine))
            while True:
                msg = await websocket.receive_text()
                if msg == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            h.clients.discard(websocket)

    # ------------------------------------------------------------------ UI
    if (STATIC / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = STATIC / path
            if path and f.is_file():
                return FileResponse(f)
            return FileResponse(STATIC / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def no_ui():
            return HTMLResponse("<h1>Sthitaprajna</h1><p>The web UI is not built. Run <code>cd web && npm ci && "
                                "npm run build</code>. The API is live at <a href='/docs'>/docs</a>.</p>")
    return app
