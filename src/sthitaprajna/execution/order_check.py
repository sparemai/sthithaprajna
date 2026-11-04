"""`sthitaprajna order-check`: is this Dhan account ready for LIVE orders? Read-only unless asked otherwise.

Prints what works, what does not and how to fix it, plus the [VERIFY] response fields, so the operator's first
run confirms the order-book, positions and funds shapes the live broker reads. Never prints the token or the
client id.

With `--place-test-order` it proves the whole order path with ONE real order: a LIMIT BUY of 1 share priced 3%
below the market (inside the day's price band, so it should not fill), cancelled right away. If it fills anyway
(a 3% drop within a second), the share is sold back at once and the report says so.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from sthitaprajna.data.adapters.base import AdapterUnavailable
from sthitaprajna.data.dhan_live import DhanClient, DhanError, load_universe
from sthitaprajna.data.sim import INDICES, STOCKS
from sthitaprajna.domain import IST
from sthitaprajna.execution.live import (
    DEAD, FILLED, DhanOrderAPI, DhanOrderError, funds_available, reason_of, round_tick, status_of,
)

WORKING = ("PENDING", "TRANSIT", "PART_TRADED", "TRIGGERED")


def run_order_check(out: Callable[[str], None] = print, *, api: DhanOrderAPI | None = None,
                    data_client: DhanClient | None = None, place_test: bool = False, symbol: str = "SBIN",
                    rules: Any = None, cal: Any = None, cache_dir: Path = Path("var/cache"),
                    today: dt.date | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    from sthitaprajna.config import load_holidays, load_operator, load_rules
    from sthitaprajna.market.calendar import MarketCalendar
    rules = rules or load_rules()
    cal = cal or MarketCalendar.from_rules(rules, load_holidays())
    today = today or dt.datetime.now(IST).date()
    what = ("places ONE test order (LIMIT BUY 1 share, 3% below the market) and cancels it" if place_test
            else "read-only: nothing is ordered")
    out(f"Dhan order check -- {what}; secrets are never printed")
    ok = True

    def step(name: str, detail: str, good: bool = True) -> None:
        out(f"  {'OK  ' if good else 'FAIL'}  {name:<16} {detail}")

    if api is None:
        try:
            api = DhanOrderAPI()
        except PermissionError as exc:
            step("credentials", str(exc), False)
            out("Result: NOT READY")
            return 1
    have = [n for n in ("DHAN_CLIENT_ID", "DHAN_TRADING_TOKEN") if os.environ.get(n, "").strip()]
    step("credentials", f"{', '.join(have) or 'injected'} present (values hidden)")

    try:
        funds = api.funds()
    except DhanOrderError as exc:
        step("funds", exc.explain(), False)
        out("Result: NOT READY")
        return 1
    avail = funds_available(funds)
    capital = float(load_operator().starting_capital)
    if avail is None:
        step("funds", "Dhan answered but without an available-balance field", False)
        ok = False
    else:
        note = "" if avail >= capital else f" -- below the configured Rs {capital:,.0f}: trades are sized from Rs {avail:,.0f}"
        step("funds", f"available Rs {avail:,.2f}{note}")
    out(f"        [VERIFY] /fundlimit fields: {', '.join(sorted(funds)) or '(none)'}")

    for name, fetch in (("order book", api.orders), ("trade book", api.trades), ("positions", api.positions)):
        try:
            rows = fetch()
        except DhanOrderError as exc:
            step(name, exc.explain(), False)
            ok = False
            continue
        if name == "order book":
            working = sum(1 for r in rows if status_of(r) in WORKING)
            detail = f"{len(rows)} orders today, {working} working"
        elif name == "positions":
            open_rows = [r for r in rows if float(r.get("netQty") or 0) != 0]
            detail = f"{len(open_rows)} open"
        else:
            detail = f"{len(rows)} trades today"
        step(name, detail)
        if rows:
            out(f"        [VERIFY] {name} fields: {', '.join(sorted(rows[0]))}")
    out("  INFO  Algo-ID          " + ("DHAN_EXCHANGE_ALGO_ID is set: sent with every order" if api.algo_id else
        "not set: orders carry a correlationId only. VERIFY with Dhan whether your account needs an Algo-ID or a "
        "registered static IP for API orders (SEBI retail-algo rules)"))

    if place_test and ok:
        ok = _test_order(out, step, api, data_client, symbol, rules, cache_dir, today, sleep)
    if not ok:
        out("Result: NOT READY")
        return 1
    out("Result: READY for LIVE orders" if place_test else
        "Result: READ-ONLY CHECKS PASSED -- `sthitaprajna order-check --place-test-order` proves order placement")
    return 0


def _test_order(out: Callable[[str], None], step: Callable[..., None], api: DhanOrderAPI,
                data_client: DhanClient | None, symbol: str, rules: Any, cache_dir: Path, today: dt.date,
                sleep: Callable[[float], None]) -> bool:
    sym = symbol.strip().upper()
    try:
        data_client = data_client or DhanClient(rules=rules)
        u = load_universe(data_client, INDICES + STOCKS, {"NIFTY": rules.get("lot_size_nifty", 65),
                                                          "BANKNIFTY": rules.get("lot_size_banknifty", 30),
                                                          "SENSEX": rules.get("lot_size_sensex", 20)},
                          cache_dir, today, progress=lambda m: out("        " + m.strip()))
        eq = u.equities.get(sym)
        if eq is None:
            step("test order", f"{sym} is not in Dhan's instrument master", False)
            return False
        sid, tick = str(eq["sid"]), float(eq.get("tick") or 0.05)
        rec = data_client.quotes({"NSE_EQ": [int(sid)]}).get(("NSE_EQ", sid))
    except (AdapterUnavailable, DhanError) as exc:
        step("test order", f"needs Dhan data for a price: {exc.explain() if isinstance(exc, DhanError) else exc}", False)
        return False
    if rec is None or rec.ltp <= 0:
        step("test order", f"no live price for {sym}", False)
        return False
    price = round_tick(max(rec.ltp * 0.97, rec.lower_circuit + tick if rec.lower_circuit > 0 else 0.0), tick, down=True)
    payload = {"dhanClientId": api.client_id, "correlationId": f"spcheck{uuid.uuid4().hex[:8]}",
               "transactionType": "BUY", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY", "orderType": "LIMIT",
               "validity": "DAY", "securityId": sid, "quantity": 1, "disclosedQuantity": 0, "price": price,
               "triggerPrice": 0.0, "afterMarketOrder": False}
    if api.algo_id:
        payload["algoId"] = api.algo_id
    try:
        resp = api.place(payload)
    except DhanOrderError as exc:
        step("place order", exc.explain(), False)
        return False
    oid = str(resp.get("orderId") or "")
    if not oid:
        step("place order", f"Dhan answered without an order id: {sorted(resp)}", False)
        return False
    if status_of(resp) == "REJECTED":
        step("place order", f"rejected: {reason_of(_safe_order(api, oid)) or 'no reason given'}", False)
        return False
    step("place order", f"LIMIT BUY 1 {sym} @ {price:.2f} (market {rec.ltp:.2f}) accepted, status {status_of(resp)}")
    sleep(1.0)
    row = _safe_order(api, oid)
    if row:
        out(f"        [VERIFY] order fields: {', '.join(sorted(row))}")
    if status_of(row) == FILLED:
        return _sell_back(out, step, api, payload)
    try:
        api.cancel(oid)
    except DhanOrderError as exc:
        step("cancel order", f"{exc.explain()} -- CANCEL ORDER {oid} IN THE DHAN APP NOW", False)
        return False
    sleep(0.5)
    after = _safe_order(api, oid)
    st = status_of(after)
    if st == FILLED:
        return _sell_back(out, step, api, payload)
    step("cancel order", f"status {st or 'unknown'}", st in DEAD)
    return st in DEAD


def _safe_order(api: DhanOrderAPI, oid: str) -> dict[str, Any] | None:
    try:
        return api.order(oid)
    except DhanOrderError:
        return None


def _sell_back(out: Callable[[str], None], step: Callable[..., None], api: DhanOrderAPI, buy: dict[str, Any]) -> bool:
    step("test order", "the 1-share test order FILLED (the price fell 3% within a second): selling it back now", False)
    sell = {**buy, "transactionType": "SELL", "orderType": "MARKET", "price": 0.0,
            "correlationId": f"spcheck{uuid.uuid4().hex[:8]}"}
    try:
        api.place(sell)
    except DhanOrderError as exc:
        step("sell back", f"{exc.explain()} -- SELL THE 1 SHARE IN THE DHAN APP NOW", False)
        return False
    step("sell back", "market SELL 1 sent; the order path works (check the Dhan app)")
    return True
