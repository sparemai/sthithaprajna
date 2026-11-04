"""NIFTY 500 cash intraday launcher; real market data with simulated execution by default."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sthitaprajna.config import load_holidays, load_operator, load_rules
from sthitaprajna.data.adapters.base import AdapterUnavailable
from sthitaprajna.data.dhan_live import DhanError
from sthitaprajna.data.live import DisabledFeed, LiveFeed
from sthitaprajna.data.sim import SimSpec
from sthitaprajna.domain import Mode
from sthitaprajna.engine.runtime import Engine
from sthitaprajna.envfile import load_env_file
from sthitaprajna.intraday.market import IntradayMarket
from sthitaprajna.intraday.strategies import cash_strategies
from sthitaprajna.intraday.universe import load_members
from sthitaprajna.market.calendar import MarketCalendar


def build_intraday_engine(members, *, mode=Mode.PAPER, store=None, cache_dir=Path("var/intraday500-cache"),
                          client=None, clock=None, start_poller=True, backfill_days=3, progress=print,
                          seed=7, speed=1, start=None, warmup_days=1):
    if mode not in (Mode.SIM, Mode.PAPER):
        raise ValueError("Intraday 500 currently supports SIM and PAPER only.")
    cfg, rules = load_operator(), load_rules()
    cal = MarketCalendar.from_rules(rules, load_holidays())
    common = {"store": store, "strategies": cash_strategies(), "cash_only": True}
    if mode is Mode.SIM:
        # These prices are invented, not market history or a strategy performance backtest.
        specs = [SimSpec(m.symbol, m.name, 100 + i * 3, 1, m.industry or "OTHER", .3)
                 for i, m in enumerate(members)]
        return Engine(cfg, rules, cal, mode=mode, stock_specs=specs, seed=seed, speed=speed,
                      start=start, warmup_days=warmup_days, **common)
    from sthitaprajna.data.dhan_live import DhanClient
    client = client or DhanClient(rules=rules)
    market = IntradayMarket(cal, client, rules, members=members, clock=clock, cache_dir=cache_dir)
    try:
        market.bootstrap(progress, backfill_days=backfill_days, start_poller=start_poller)
        progress(f"  NIFTY 500: rotating quotes over {len(market.screen)} cash stocks; "
                 "minute history loads for promoted candidates.")
        return Engine(cfg, rules, cal, mode=mode, market=market,
                      feeds=(LiveFeed(market), DisabledFeed()), **common)
    except BaseException:
        market.close()
        raise


def main(argv=None):
    p = argparse.ArgumentParser(prog="sthitaprajna-intraday", description="NIFTY 500 cash intraday · SIM / PAPER")
    p.add_argument("--mode", choices=["sim", "paper"], default="paper")
    p.add_argument("--port", type=int, default=8792)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--db", type=Path)
    p.add_argument("--cache-dir", type=Path)
    p.add_argument("--universe-csv", type=Path, help="Official NIFTY 500 constituent CSV; otherwise auto-download")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--speed", type=float, default=1, help="SIM clock speed (PAPER always uses exchange time)")
    a = p.parse_args(argv)
    if not 1 <= a.port <= 65535:
        p.error("--port must be between 1 and 65535")
    env_path, names = load_env_file()
    if env_path:
        print(f"Settings loaded from {env_path}: {', '.join(names) or 'nothing new'}")
    if a.host not in ("127.0.0.1", "localhost") and not os.environ.get("STHITAPRAJNA_API_TOKEN"):
        p.error("Set STHITAPRAJNA_API_TOKEN before binding beyond localhost.")
    cache = a.cache_dir or Path(f"var/intraday500-{a.port}-cache")
    db = a.db or Path(f"var/intraday500-{a.mode}-{a.port}.sqlite")
    store = None
    engine = None
    try:
        members = load_members(cache, a.universe_csv)
        print(f"Intraday 500 · {len(members)} constituent stocks · {a.mode.upper()} · database {db}")
        print("PAPER uses live Dhan data and simulated fills. SIM uses invented prices. No real orders are sent.")
        from sthitaprajna.engine.store import Store
        store = Store(str(db))
        engine = build_intraday_engine(members, mode=Mode(a.mode.upper()), store=store,
                                       cache_dir=cache, seed=a.seed, speed=a.speed)
        print(f"Risk profile: {engine.cfg.risk_mode}; configured cash leverage "
              f"{engine.cfg.unbounded.get('intraday_leverage', 1)}x. See config/operator.yaml.")
        print(f"Open http://{a.host}:{a.port} · entries 09:35–14:15 IST; "
              f"intraday exit {engine.cal.intraday_exit:%H:%M} IST")
        import uvicorn

        from sthitaprajna.api.server import create_app
        uvicorn.run(create_app(engine), host=a.host, port=a.port, log_level="warning")
        return 0
    except (ValueError, OSError, AdapterUnavailable, DhanError) as exc:
        print(f"Cannot start Intraday 500: {exc}", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            close = getattr(engine.market, "close", None)
            if close:
                close()
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
