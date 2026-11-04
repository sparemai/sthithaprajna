"""Command line: serve the terminal, run backtests / Monte Carlo, audit market rules.

    sthitaprajna serve [--mode sim|paper|live] [--speed 30] [--seed 7] [--port 8788] [--host 127.0.0.1] [--discord]
    sthitaprajna dhan-check     # read-only check of Dhan data access for PAPER (prints no secrets)
    sthitaprajna order-check [--place-test-order] [--symbol SBIN]   # Dhan LIVE-order readiness check
    sthitaprajna backtest --days 5 --seed 7 [--compare-staging]
    sthitaprajna mc --reproduce-d12 | --p 0.45 --b 1.8 --policy attack
    sthitaprajna rules          # freshness audit (A4)
    sthitaprajna feasibility    # Phase-0 Rs 1 lakh feasibility table (SIM model)

LIVE trading (real orders on your Dhan account) needs DHAN_CLIENT_ID + DHAN_TRADING_TOKEN in .env AND
a deliberate STHITAPRAJNA_LIVE_TRADING_CONFIRMED=yes -- credentials alone never turn it on. See
docs/LIVE_TRADING.md before your first live session and run `order-check` first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from sthitaprajna.domain import Mode
from sthitaprajna.envfile import load_env_file


LIVE_CONFIRM_VAR = "STHITAPRAJNA_LIVE_TRADING_CONFIRMED"


def _live_gate() -> str | None:
    """Returns an error message if LIVE isn't allowed to start, else None. Two independent conditions,
    both required: real Dhan trading credentials, AND a deliberate opt-in that credentials alone can't
    satisfy -- exactly the STHITAPRAJNA_API_TOKEN pattern this CLI already uses for remote binding."""
    missing = [n for n in ("DHAN_CLIENT_ID", "DHAN_TRADING_TOKEN") if not os.environ.get(n, "").strip()]
    if missing:
        return ("Cannot start LIVE: missing " + ", ".join(missing) + " in .env. Run `sthitaprajna order-check` "
                "once they're set, before your first live session.")
    if os.environ.get(LIVE_CONFIRM_VAR, "").strip().lower() not in ("yes", "true", "1"):
        return (f"LIVE trading needs one more explicit step: set {LIVE_CONFIRM_VAR}=yes in .env.\n"
                "This is deliberate: Dhan credentials in .env alone never turn real orders on. Read "
                "docs/LIVE_TRADING.md and run `sthitaprajna order-check --place-test-order` first.")
    return None


def _serve(a: argparse.Namespace) -> int:
    import uvicorn

    from sthitaprajna.api.server import build_engine, build_live_engine, build_paper_engine, create_app
    from sthitaprajna.engine.store import Store
    try:
        mode = Mode(a.mode.upper()) if a.mode.lower() != "live" else Mode.LIVE_FULL
    except ValueError:
        print(f"unknown mode {a.mode!r}: use sim, paper or live", file=sys.stderr)
        return 2
    if mode in (Mode.LIVE_PILOT, Mode.LIVE_FULL):
        err = _live_gate()
        if err:
            print(err, file=sys.stderr)
            return 2
    if a.host not in ("127.0.0.1", "localhost") and not os.environ.get("STHITAPRAJNA_API_TOKEN"):
        print("Refusing to bind beyond localhost without STHITAPRAJNA_API_TOKEN set.", file=sys.stderr)
        return 2
    db = a.db if a.db is not None else {
        Mode.PAPER: "var/sthitaprajna-paper.sqlite", Mode.LIVE_PILOT: "var/sthitaprajna-live.sqlite",
        Mode.LIVE_FULL: "var/sthitaprajna-live.sqlite",
    }.get(mode, "var/sthitaprajna.sqlite")
    if mode is Mode.PAPER:
        from sthitaprajna.data.adapters.base import AdapterUnavailable
        from sthitaprajna.data.dhan_live import DhanClient, DhanError
        try:
            client = DhanClient(rules=load_rules())
        except (AdapterUnavailable, ValueError) as exc:
            print(f"Cannot start PAPER: {exc}", file=sys.stderr)
            return 2
        print("PAPER mode: live Dhan market data, simulated fills -- no order reaches the exchange")
        try:
            eng = build_paper_engine(store=Store(db) if db else None, client=client)
        except DhanError as exc:
            print(f"Cannot start PAPER: {exc.explain()}\nRun `sthitaprajna dhan-check` for a full report.", file=sys.stderr)
            return 2
        label = "PAPER, live Dhan data, simulated fills"
    elif mode in (Mode.LIVE_PILOT, Mode.LIVE_FULL):
        from sthitaprajna.data.adapters.base import AdapterUnavailable
        from sthitaprajna.data.dhan_live import DhanError
        from sthitaprajna.execution.live import DhanOrderError
        print("LIVE mode: every entry, stop-loss and exit is a REAL order on your Dhan account.\n"
              "Approval is required on every entry and stage-add until you turn it off (pilot_approvals).\n"
              "Stop everything: the KILL button, /kill on Discord, or close positions in the Dhan app.")
        try:
            eng = build_live_engine(store=Store(db) if db else None)
        except (AdapterUnavailable, DhanError, DhanOrderError, ValueError) as exc:
            print(f"Cannot start LIVE: {exc}\nRun `sthitaprajna order-check` for a full readiness report.", file=sys.stderr)
            return 2
        label = "LIVE on Dhan -- REAL ORDERS"
    else:
        eng = build_engine(mode, seed=a.seed, speed=a.speed, store=Store(db) if db else None)
        label = f"mode {mode.value}, SIM data is not evidence"
        if os.environ.get("DHAN_CLIENT_ID") and os.environ.get("DHAN_DATA_TOKEN"):
            print("Dhan credentials found. For live market data (paper trading) run: sthitaprajna serve --mode paper")
    app = create_app(eng, discord=a.discord)
    print(f"Sthitaprajna 2.0 (Options Hero) terminal on http://{a.host}:{a.port}  ({label})")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


def _dhan_check(a: argparse.Namespace) -> int:
    from sthitaprajna.data.dhan_check import run_check
    return run_check()


def _order_check(a: argparse.Namespace) -> int:
    from sthitaprajna.execution.order_check import run_order_check
    if a.place_test_order:
        print(f"About to place ONE real order on your Dhan account (LIMIT BUY 1 {a.symbol.upper()}, ~3% below "
              "market, cancelled right after). Ctrl-C now to abort.")
    return run_order_check(place_test=a.place_test_order, symbol=a.symbol)


def load_rules():
    from sthitaprajna.config import load_rules as _load
    return _load()


def _backtest(a: argparse.Namespace) -> int:
    from sthitaprajna.config import load_holidays, load_operator, load_rules
    from sthitaprajna.market.calendar import MarketCalendar
    from sthitaprajna.research import backtest
    cfg, rules = load_operator(), load_rules()
    cal = MarketCalendar.from_rules(rules, load_holidays())
    if a.compare_staging:
        out = backtest.compare_staging(cfg, rules, cal, days=a.days, seeds=(a.seed,))
    else:
        out = backtest.run(cfg, rules, cal, days=a.days, seed=a.seed)
        out = {k: out[k] for k in ("variant", "seed", "days", "report", "by_strategy", "kpis")}
    print(json.dumps(out, indent=2, default=str))
    return 0


def _mc(a: argparse.Namespace) -> int:
    from sthitaprajna.config import load_operator
    from sthitaprajna.research import montecarlo
    if a.reproduce_d12:
        print(f"{'scenario':10} {'policy':28} {'median':>7} {'5th pct':>8} {'P(halt)':>8} {'P(loss)':>8}")
        for r in montecarlo.reproduce_d12_table():
            print(f"{r['scenario']:10} {r['policy']:28} {r['median']:>6.2f}x {r['p5']:>7.2f}x {r['p_halt']:>7.1f}% "
                  f"{r['p_loss']:>7.1f}%   (EV/trade incl. gaps {r['ev_r']:+.2f}R)")
        return 0
    out = montecarlo.run_custom(load_operator(), p=a.p, b=a.b, policy=a.policy, n=a.n, t=a.t)
    out.pop("bands", None)
    print(json.dumps(out, indent=2))
    return 0


def _rules(_a: argparse.Namespace) -> int:
    from sthitaprajna.config import load_rules
    r = load_rules()
    for row in r.report():
        print(f"{row['freshness']:10} {row['status']:18} {row['name']:38} {str(row['value'])[:28]:28} {row['source_url'][:60]}")
    print(f"\n{len(r.open_items())} open items (UNVERIFIED or STALE) -- listed in the go-live dossier.")
    return 0


def _feasibility(_a: argparse.Namespace) -> int:
    from sthitaprajna.api.server import build_engine
    from sthitaprajna.research.feasibility import feasibility_table
    e = build_engine(warmup_days=1)
    t = feasibility_table(e, e.cfg)
    print(t["label"])
    print("Budgets at ATTACK:", t["budgets"])
    for r in t["rows"]:
        print(f"{r['structure']:40} lot {r['lot']:>3} prem {r['premium']:>9.2f} risk/lot {r['one_lot_risk']:>9.2f} "
              f"A+:{'Y' if r['fits_A+'] else '-'} A:{'Y' if r['fits_A'] else '-'} B:{'Y' if r['fits_B'] else '-'} "
              f"2-lot staging(A):{'Y' if r['two_lot_staging_fits_A'] else '-'}  [{r['stop']}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sthitaprajna", description="Sthitaprajna 2.0 -- pure index & stock options trading terminal")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--mode", default="sim", help="sim | paper | live (live needs DHAN_TRADING_TOKEN and "
                  f"{LIVE_CONFIRM_VAR}=yes -- see docs/LIVE_TRADING.md)")
    s.add_argument("--speed", type=float, default=30.0)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8788)
    s.add_argument("--db", default=None, help="SQLite file (default: var/sthitaprajna.sqlite, or -paper for PAPER)")
    s.add_argument("--discord", action="store_true")
    s.set_defaults(fn=_serve)
    b = sub.add_parser("backtest")
    b.add_argument("--days", type=int, default=5)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--compare-staging", action="store_true")
    b.set_defaults(fn=_backtest)
    m = sub.add_parser("mc")
    m.add_argument("--reproduce-d12", action="store_true")
    m.add_argument("--p", type=float, default=0.45)
    m.add_argument("--b", type=float, default=1.8)
    m.add_argument("--policy", default="attack", choices=["baseline", "attack", "attack_brake"])
    m.add_argument("--n", type=int, default=20000)
    m.add_argument("--t", type=int, default=250)
    m.set_defaults(fn=_mc)
    sub.add_parser("rules").set_defaults(fn=_rules)
    sub.add_parser("feasibility").set_defaults(fn=_feasibility)
    sub.add_parser("dhan-check", help="read-only check of your Dhan data access for PAPER mode").set_defaults(fn=_dhan_check)
    oc = sub.add_parser("order-check", help="Dhan LIVE-order readiness check (read-only unless --place-test-order)")
    oc.add_argument("--place-test-order", action="store_true",
                    help="places ONE real LIMIT BUY 1 share ~3%% below market and cancels it")
    oc.add_argument("--symbol", default="SBIN", help="symbol for the test order (default SBIN)")
    oc.set_defaults(fn=_order_check)
    a = p.parse_args(argv)
    env_path, env_names = load_env_file()
    if env_path and a.cmd in ("serve", "dhan-check", "order-check"):
        print(f"Settings loaded from {env_path}: {', '.join(env_names) or 'nothing new'}")
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
