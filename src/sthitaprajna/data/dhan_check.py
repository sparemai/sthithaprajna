"""`sthitaprajna dhan-check`: a read-only readiness report for PAPER mode on live Dhan data.

Prints what works, what does not and how to fix it, including the [VERIFY] facts (quote field names, index
security ids, candle alignment, put-delta sign) so the operator's first run confirms them. Never prints a
token or the client id, and never calls an order endpoint.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, Callable

from sthitaprajna.data.adapters.base import AdapterUnavailable
from sthitaprajna.data.adapters.dhan import parse_dhan_option_chain
from sthitaprajna.data.dhan_live import DhanClient, DhanError, load_universe, records_from_body
from sthitaprajna.data.sim import INDICES, STOCKS
from sthitaprajna.domain import IST

SAMPLE = ("NSE:NIFTY", "NSE:BANKNIFTY", "BSE:SENSEX", "NSE:INDIAVIX", "NSE:RELIANCE", "NSE:HDFCBANK")


def run_check(out: Callable[[str], None] = print, *, client: DhanClient | None = None, rules: Any = None,
              cal: Any = None, cache_dir: Path = Path("var/cache"), today: dt.date | None = None) -> int:
    from sthitaprajna.config import load_holidays, load_rules
    from sthitaprajna.market.calendar import MarketCalendar
    rules = rules or load_rules()
    cal = cal or MarketCalendar.from_rules(rules, load_holidays())
    today = today or dt.datetime.now(IST).date()
    out("Dhan connection check -- read-only: nothing is ordered, and secrets are never printed")
    ok = True

    def step(name: str, detail: str, good: bool = True) -> None:
        out(f"  {'OK  ' if good else 'FAIL'}  {name:<18} {detail}")

    have = [n for n in ("DHAN_CLIENT_ID", "DHAN_DATA_TOKEN") if os.environ.get(n, "").strip()]
    if client is None:
        try:
            client = DhanClient(rules=rules)
        except AdapterUnavailable:
            missing = [n for n in ("DHAN_CLIENT_ID", "DHAN_DATA_TOKEN") if n not in have]
            step("credentials", f"missing {', '.join(missing)} in .env", False)
            if "DHAN_DATA_TOKEN" in missing and os.environ.get("DHAN_TRADING_TOKEN"):
                out("        DHAN_DATA_TOKEN is empty but DHAN_TRADING_TOKEN is set. If Dhan gave you a single access "
                    "token, put it in DHAN_DATA_TOKEN as well (data calls never use the trading variable).")
            out("Result: NOT READY")
            return 1
    step("credentials", f"{', '.join(have) or 'injected'} present (values hidden)")

    try:
        u = load_universe(client, INDICES + STOCKS, {"NIFTY": rules.get("lot_size_nifty", 65),
                                                      "BANKNIFTY": rules.get("lot_size_banknifty", 30),
                                                      "SENSEX": rules.get("lot_size_sensex", 20)},
                          cache_dir, today, progress=lambda m: out("        " + m.strip()))
    except DhanError as exc:
        step("instrument ids", exc.explain(), False)
        out("Result: NOT READY")
        return 1
    idx = ", ".join(f"{k.split(':')[1]}={u.items[k].security_id}({u.items[k].source})"
                    for k in ("NSE:NIFTY", "NSE:BANKNIFTY", "BSE:SENSEX", "NSE:INDIAVIX") if k in u.items)
    step("instrument ids", f"{len(u.items)}/{len(INDICES) + len(STOCKS)} resolved; {idx}; lots {u.lots}")
    for p in u.problems:
        out(f"        note: {p}")

    sample = [u.items[k] for k in SAMPLE if k in u.items]
    segs: dict[str, list[int]] = {}
    for r in sample:
        segs.setdefault(r.segment, []).append(int(r.security_id))
    try:
        raw = client._post("quote", "/marketfeed/quote", segs)      # one request: fields and prices together
    except DhanError as exc:
        step("market quotes", exc.explain(), False)
        out("Result: NOT READY")
        return 1
    data = (raw or {}).get("data") or {}
    for seg, rows in data.items():
        first = next(iter((rows or {}).values()), None)
        if isinstance(first, dict):
            out(f"        [VERIFY] /marketfeed/quote {seg} fields: {', '.join(sorted(first))}")
            dep = first.get("depth")
            if isinstance(dep, dict):
                lvl = next((v[0] for v in dep.values() if isinstance(v, list) and v and isinstance(v[0], dict)), None)
                out(f"        [VERIFY] {seg} depth sides: {', '.join(sorted(dep))}"
                    + (f"; level fields: {', '.join(sorted(lvl))}" if lvl else ""))
    recs = records_from_body(raw, "quote")
    lines = []
    for r in sample:
        q = recs.get((r.segment, str(r.security_id)))
        if q is None:
            lines.append(f"{r.symbol}: no quote")
            continue
        lt = q.last_trade.strftime("%d-%b %H:%M:%S") if q.last_trade else "-"
        lines.append(f"{r.symbol} {q.ltp:,.2f} (prev close {q.prev_close:,.2f}, bid {q.bid:,.2f} / ask {q.ask:,.2f}, "
                     f"last trade {lt})")
    good = any(recs.get((r.segment, str(r.security_id))) for r in sample)
    step("market quotes", lines[0] if lines else "no quotes", good)
    for line in lines[1:]:
        out(f"        {line}")
    nq = (data.get("IDX_I") or {}).get(str(u.items["NSE:NIFTY"].security_id)) if "NSE:NIFTY" in u.items else None
    if isinstance(nq, dict):
        try:
            ltp, chg = float(nq.get("last_price") or 0), float(nq.get("net_change") or 0)
            oc = float((nq.get("ohlc") or {}).get("close") or 0)
            verdict = "consistent: ohlc.close is the previous close" if abs((ltp - chg) - oc) <= 0.05 else \
                "MISMATCH: previous close needs a different source"
            out(f"        [VERIFY] NIFTY ohlc.close {oc:,.2f} vs last_price - net_change {ltp - chg:,.2f} -> {verdict}")
        except (TypeError, ValueError):
            pass
    ok &= good

    nifty = u.items.get("NSE:NIFTY")
    if nifty is not None:
        last = cal.trading_day_on_or_before(today)
        try:
            bars = client.intraday(nifty.key, nifty.security_id, nifty.segment, nifty.instrument,
                                   cal.prev_trading_day(last), today + dt.timedelta(days=1))
            if bars:
                step("1-min history", f"NIFTY {len(bars)} bars, {bars[0].start:%d-%b %H:%M} .. {bars[-1].start:%d-%b %H:%M} IST "
                                      "[VERIFY: sessions should start 09:15]")
            else:
                step("1-min history", "no bars returned", False)
                ok = False
        except DhanError as exc:
            step("1-min history", exc.explain(), False)
            ok = False
        try:
            exps = client.expiries(nifty.security_id, "IDX_I")
            step("option expiries", "NIFTY " + ", ".join(e.isoformat() for e in exps[:3]), bool(exps))
            future = [e for e in exps if e >= today]
            if future:
                body = client.option_chain(nifty.security_id, "IDX_I", future[0])
                spot, rows = parse_dhan_option_chain(body, "NIFTY", future[0])
                if rows:
                    atm = min(rows, key=lambda r: abs(r.strike - spot))
                    step("option chain", f"NIFTY {future[0]}: {len(rows)} strikes, spot {spot:,.2f}, ATM {atm.strike:,.0f} "
                                         f"CE {atm.ce_ltp} (delta {atm.ce_delta:+.2f}) / PE {atm.pe_ltp} (delta {atm.pe_delta:+.2f})")
                else:
                    step("option chain", "empty chain", False)
                    ok = False
        except DhanError as exc:
            step("option data", exc.explain(), False)
            ok = False
    client.close()
    out("Result: READY -- start with `sthitaprajna serve --mode paper`" if ok else
        "Result: PARTLY READY -- quotes work; the failed items above will show as gaps (see the fixes)")
    return 0 if ok else 1
