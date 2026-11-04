"""Live Dhan order execution (LIVE mode, operator decision D-034): the order API client and the live broker.

CREDENTIAL ISOLATION: this is the ONLY module that reads the write-scoped DHAN_TRADING_TOKEN. Data adapters use
DHAN_DATA_TOKEN and never import this module. (If Dhan gave you one token, put the same value in both.)

`LiveBroker` has the paper broker's interface (A5.4: the mode is a flag, the engine is identical):
* entries: MARKET for stocks; a marketable LIMIT for options (ask + 2%), so a thin book cannot fill far away.
  Unfilled after ENTRY_TIMEOUT_S -> cancelled; a partial fill becomes the position.
* every position gets a broker-side stop at Dhan: STOP_LOSS_MARKET for stocks, STOP_LOSS (limit) for options.
  Trailing moves are sent as modifications.
* exits: the resting stop is cancelled (or reduced) at Dhan FIRST and the exit goes out only after that, so a stop
  and an exit can never both sell the same quantity. If the stop already traded, no exit is sent. Option exits are
  marketable limits that chase (bid - 3% -> 10% -> 25%) until filled.
* fills, rejections and cancellations come from Dhan's order book, polled in a background thread so the engine loop
  never waits on the network; positions and funds are polled for reconciliation and for the capital check.

[VERIFY] Nothing here has run against a real Dhan account yet (`sthitaprajna order-check` prints what Dhan
returns): endpoint paths and field names (orders, order book, trades, positions, fundlimit), the order-status words,
correlationId length and characters, whether your account needs an Algo-ID field or a registered static IP,
whether SL-M is accepted for options, and the order rate limits.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from sthitaprajna.data.adapters.dhan import DHAN_BASE_URL
from sthitaprajna.data.dhan_live import DhanError, MinInterval, _retry_after, checked_url, error_from_response
from sthitaprajna.domain import IST, Exchange, Instrument, Kind, Mode, Quote, Segment, Side
from sthitaprajna.execution.orders import Order, OrderStatus, OrderType
from sthitaprajna.execution.paper import Fill
from sthitaprajna.market.charges import ChargesModel

LIVE_MODES = (Mode.LIVE_PILOT, Mode.LIVE_FULL)
FILLED = "TRADED"
DEAD = ("CANCELLED", "EXPIRED")
ENTRY_TIMEOUT_S = 15.0          # an entry not filled by then is cancelled (the setup has moved on)
CHASE_AFTER_S = 3.0             # option exits: re-price a working exit after this long
CHASE_STEPS = (0.03, 0.10, 0.25)
OPTION_ENTRY_BUFFER = 0.02      # marketable limit: ask + 2% (at least 2 ticks)
OPTION_STOP_LIMIT_GAP = 0.10    # option stop-loss limit: 10% (at least 3 ticks) beyond the trigger
UNSEEN_TIMEOUT_S = 10.0         # an order that never shows up in Dhan's book is looked up, then given up
EXIT_RETRY_S = 3.0              # after Dhan rejects an exit, wait this long before trying again
ACCOUNT_POLL_S = 15.0           # positions + funds

ORDER_FIXES = {
    "AUTH": "Dhan rejected DHAN_CLIENT_ID / DHAN_TRADING_TOKEN (wrong, or the access token has expired). Generate a "
            "new access token in Dhan's web portal, put it in DHAN_TRADING_TOKEN in .env and restart.",
    "NOT_SUBSCRIBED": "Dhan says this account cannot use this API. Check the API settings of the account in Dhan.",
    "ACCOUNT": "Dhan reports an account problem (a segment not activated, trading blocked, or the Dhan kill switch "
               "is on). Check the account in the Dhan app.",
    "ORDER": "Dhan's order system refused the order: the message says why (margin, price band, lot size, static IP "
             "or Algo-ID). Open positions stay protected by their broker-side stops.",
    "RATE": "Dhan's order rate limit was hit; the system backs off and retries.",
    "INPUT": "Dhan rejected the order request as invalid. This is a bug in Sthitaprajna's request: share this log "
             "line (it contains no secrets).",
    "NO_DATA": "Dhan returned nothing for this request.",
    "SERVER": "Dhan's servers returned an error; retrying.",
    "NETWORK": "Cannot reach Dhan (internet, firewall or DNS); retrying.",
    "OTHER": "Unexpected Dhan response: share this log line (it contains no secrets).",
}


def resolve_mode(requested: Mode, broker_live: bool = False) -> tuple[Mode, list[str]]:
    """The mode actually used and what is missing. LIVE runs exactly when a live Dhan broker is attached (it can
    only be built with the order credentials); otherwise it falls back to PAPER. No other gates (D-034)."""
    if requested not in LIVE_MODES or broker_live:
        return requested, []
    missing = [n for n in ("DHAN_CLIENT_ID", "DHAN_TRADING_TOKEN") if not os.environ.get(n, "").strip()]
    missing.append("live Dhan broker (start with: sthitaprajna serve --mode live)")
    return Mode.PAPER, missing


class DhanOrderError(DhanError):
    def __init__(self, kind: str, message: str, *, code: str | None = None, status: int | None = None,
                 ambiguous: bool = False) -> None:
        super().__init__(kind, message, code=code, status=status)
        self.ambiguous = ambiguous      # a write that may or may not have reached Dhan (timed out mid-flight)

    @property
    def fix(self) -> str:
        return ORDER_FIXES.get(self.kind, ORDER_FIXES["OTHER"])


def order_error(r: httpx.Response, secret: str) -> DhanOrderError:
    e = error_from_response(r, secret)
    kind = "ORDER" if e.kind == "INPUT" and e.status == 400 and e.code in (None, "DH-906") else e.kind
    return DhanOrderError(kind, str(e), code=e.code, status=e.status)


def _rows(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict):
        body = body.get("data", [])
    return [r for r in body if isinstance(r, dict)] if isinstance(body, list) else []


def _one(body: Any) -> dict[str, Any] | None:
    if isinstance(body, list):
        return next((r for r in body if isinstance(r, dict)), None)
    if isinstance(body, dict):
        if isinstance(body.get("data"), (dict, list)):
            return _one(body["data"])
        return body or None
    return None


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def funds_available(d: dict[str, Any] | None) -> float | None:
    """Dhan's /fundlimit spells it `availabelBalance` in its documentation; accept both spellings."""
    if not isinstance(d, dict):
        return None
    for k in ("availabelBalance", "availableBalance", "available_balance"):
        if d.get(k) is not None:
            return _num(d[k])
    return None


def round_tick(px: float, tick: float, down: bool) -> float:
    tick = tick if tick and tick > 0 else 0.05
    n = px / tick
    n = math.floor(n + 1e-9) if down else math.ceil(n - 1e-9)
    return round(n * tick, 2)


def exchange_segment(inst: Instrument) -> str:
    fno = inst.kind is Kind.OPTION or inst.key.startswith(("NFO:", "BFO:"))
    bse = inst.exchange is Exchange.BSE or inst.key.startswith(("BSE:", "BFO:"))
    return ("BSE" if bse else "NSE") + ("_FNO" if fno else "_EQ")


def product_type(segment: Segment) -> str:
    return "CNC" if segment is Segment.EQ_DELIVERY else "INTRADAY"


def correlation_id(order_id: str) -> str:
    """Letters and digits only, at most 20 characters [VERIFY Dhan's limit]; unique per order."""
    return ("sp" + re.sub(r"[^0-9A-Za-z]", "", order_id))[:20]


class DhanOrderAPI:
    """Dhan v2 order endpoints (write scope). Reads DHAN_TRADING_TOKEN only; never logs it."""

    def __init__(self, client_id: str | None = None, token: str | None = None, *, base_url: str | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float = 4.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 algo_id: str | None = None) -> None:
        cid = (client_id or os.environ.get("DHAN_CLIENT_ID", "")).strip()
        tok = (token or os.environ.get("DHAN_TRADING_TOKEN", "")).strip()
        if not (cid and tok):
            raise PermissionError("LIVE orders need DHAN_CLIENT_ID and DHAN_TRADING_TOKEN in .env (your Dhan access "
                                  "token; the same value as DHAN_DATA_TOKEN if Dhan gave you a single token)")
        self.client_id, self._secret = cid, tok
        self.base_url = checked_url(base_url or os.environ.get("STHITAPRAJNA_DHAN_BASE_URL") or DHAN_BASE_URL)
        self.algo_id = (algo_id if algo_id is not None else os.environ.get("DHAN_EXCHANGE_ALGO_ID", "")).strip()
        self._kw = dict(base_url=self.base_url, transport=transport, timeout=timeout, sleep=sleep, clock=clock,
                        algo_id=self.algo_id)
        self._http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(timeout, connect=3.0),
                                  transport=transport,
                                  headers={"access-token": tok, "client-id": cid, "Content-Type": "application/json",
                                           "Accept": "application/json"})
        self.write_gap = MinInterval(0.25, clock, sleep)    # <= 4 writes/s: far below SEBI's 10 orders/s
        self.read_gap = MinInterval(0.2, clock, sleep)
        self.calls: Counter[str] = Counter()

    def clone(self) -> DhanOrderAPI:
        """A second connection (the order-book poller thread gets its own)."""
        return DhanOrderAPI(self.client_id, self._secret, **self._kw)

    def close(self) -> None:
        self._http.close()

    def _req(self, method: str, path: str, *, body: dict[str, Any] | None = None, write: bool = False) -> Any:
        gap = self.write_gap if write else self.read_gap
        tries = 1 if write else 2                           # a 429 on a write means it was NOT accepted: one retry
        for attempt in range(tries + 1):
            gap.wait()
            self.calls[f"{method} {path.split('?')[0]}"] += 1
            try:
                r = self._http.request(method, path, json=body)
            except httpx.TimeoutException:
                gap.done()
                raise DhanOrderError("NETWORK", f"Dhan {path}: timed out", ambiguous=write) from None
            except httpx.TransportError as exc:
                gap.done()
                sent = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                raise DhanOrderError("NETWORK", f"Dhan {path}: {type(exc).__name__}", ambiguous=write and sent) from None
            gap.done()
            if r.status_code == 404 and method == "GET":
                return None
            if r.status_code >= 400:
                err = order_error(r, self._secret)
                if err.kind == "RATE" and attempt < tries:
                    gap.done(extra=_retry_after(r) or 1.0 * (attempt + 1))
                    continue
                raise err
            if not r.content:
                return {}
            try:
                out = r.json()
            except ValueError:
                raise DhanOrderError("OTHER", f"Dhan {path}: response is not JSON") from None
            if isinstance(out, dict) and str(out.get("status", "")).lower() == "failure":
                raise order_error(r, self._secret)
            return out
        raise AssertionError("unreachable")

    # writes
    def place(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _one(self._req("POST", "/orders", body=payload, write=True)) or {}

    def modify(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _one(self._req("PUT", f"/orders/{order_id}", body=payload, write=True)) or {}

    def cancel(self, order_id: str) -> dict[str, Any]:
        return _one(self._req("DELETE", f"/orders/{order_id}", write=True)) or {}

    def kill_switch(self, activate: bool = True) -> dict[str, Any]:
        """Dhan's account-level kill switch [VERIFY semantics]. Not called automatically."""
        q = "ACTIVATE" if activate else "DEACTIVATE"
        return _one(self._req("POST", f"/killswitch?killSwitchStatus={q}", write=True)) or {}

    # reads
    def order(self, order_id: str) -> dict[str, Any] | None:
        return _one(self._req("GET", f"/orders/{order_id}"))

    def find_by_correlation(self, cid: str) -> dict[str, Any] | None:
        return _one(self._req("GET", f"/orders/external/{cid}"))

    def orders(self) -> list[dict[str, Any]]:
        return _rows(self._req("GET", "/orders"))

    def trades(self, order_id: str | None = None) -> list[dict[str, Any]]:
        return _rows(self._req("GET", f"/trades/{order_id}" if order_id else "/trades"))

    def positions(self) -> list[dict[str, Any]]:
        return _rows(self._req("GET", "/positions"))

    def funds(self) -> dict[str, Any]:
        out = _one(self._req("GET", "/fundlimit"))
        return out if isinstance(out, dict) else {}


def status_of(row: dict[str, Any] | None) -> str:
    return str((row or {}).get("orderStatus") or "").upper()


def filled_of(row: dict[str, Any] | None) -> int:
    r = row or {}
    return int(_num(r.get("filledQty") or r.get("tradedQuantity") or 0))


def reason_of(row: dict[str, Any] | None) -> str:
    r = row or {}
    return str(r.get("omsErrorDescription") or r.get("omsErrorCode") or "").strip()


@dataclass
class _Track:
    broker_id: str
    sent: float                     # wall time the order went out
    touched: float = 0.0            # wall time of our last change (modify/cancel): older book rows are ignored
    cancel_requested: bool = False
    chase: int = 0
    retry_at: float = 0.0           # next attempt at a cancel that Dhan refused


class LiveBroker:
    """The paper broker's interface over Dhan's real order API (see the module docstring)."""

    live = True

    def __init__(self, api: DhanOrderAPI, charges: ChargesModel, quote: Callable[[str], Quote | None], *,
                 poll_api: DhanOrderAPI | None = None, wall: Callable[[], float] = time.monotonic,
                 start_poller: bool = True) -> None:
        self.api, self.charges, self.quote = api, charges, quote
        self.poll_api = poll_api or api.clone()
        self.wall = wall
        self.pending: dict[str, Order] = {}            # entries and exits working at Dhan
        self.resting: dict[str, Order] = {}            # broker-side stops resting at Dhan
        self.rejects: list[dt.datetime] = []
        self.track: dict[str, _Track] = {}
        self.failed: list[Order] = []                  # rejected / cancelled / unfilled: the engine cleans up
        self.notes: list[str] = []                     # lines for the engine log
        self._ready: list[Fill] = []                   # fills learnt outside process() (a stop that traded first)
        self._exit_block: dict[str, float] = {}
        self._lock = threading.Lock()
        self._book: dict[str, dict[str, Any]] = {}
        self._book_wall = -1e18
        self._positions: list[dict[str, Any]] = []
        self._positions_wall = -1e18
        self.positions_seq = 0
        self.available_funds: float | None = None
        self.health: dict[str, Any] = {"polls": 0, "last_ok": None, "error": None}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        if start_poller:
            self.start()

    # ------------------------------------------------------------------ poller (background thread)
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="dhan-orders", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.api.close()
        self.poll_api.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:     # the poller must never die; the error is shown and retried
                self.health["error"] = exc.explain() if isinstance(exc, DhanError) else repr(exc)
            with self._lock:
                gap = 0.7 if self.pending else 2.0 if self.resting else 5.0
            self._wake.wait(gap)
            self._wake.clear()

    def poll_once(self, account: bool = False) -> None:
        """One refresh: the order book while anything is working, positions and funds every ACCOUNT_POLL_S."""
        t0 = self.wall()
        with self._lock:
            need_book = bool(self.pending or self.resting)
        if need_book:
            rows = self.poll_api.orders()
            with self._lock:
                self._book = {str(r.get("orderId")): r for r in rows if r.get("orderId")}
                self._book_wall = t0          # rows show the state at or after t0
        if account or t0 - self._positions_wall >= ACCOUNT_POLL_S:
            pos, funds = self.poll_api.positions(), self.poll_api.funds()
            with self._lock:
                self._positions, self._positions_wall = pos, t0
                self.positions_seq += 1
                self.available_funds = funds_available(funds)
        self.health.update(polls=self.health["polls"] + 1, error=None,
                           last_ok=dt.datetime.now(IST).isoformat(timespec="seconds"))

    def position_snapshot(self) -> tuple[int, float, dict[tuple[str, str], dict[str, float]]] | None:
        """(sequence, wall time the fetch started, {(segment, security id): net qty and averages}), intraday only."""
        with self._lock:
            if self.positions_seq == 0:
                return None
            out: dict[tuple[str, str], dict[str, float]] = {}
            for r in self._positions:
                if str(r.get("productType") or "INTRADAY").upper() != "INTRADAY":
                    continue
                k = (str(r.get("exchangeSegment") or ""), str(r.get("securityId") or ""))
                out[k] = {"net": _num(r.get("netQty")), "buy_avg": _num(r.get("buyAvg")),
                          "sell_avg": _num(r.get("sellAvg"))}
            return self.positions_seq, self._positions_wall, out

    # ------------------------------------------------------------------ engine-facing interface
    def submit(self, order: Order, now: dt.datetime) -> bool:
        why = self._unplaceable(order)
        if why:
            return self._reject(order, now, why, count=False)
        if order.is_exit and order.type is not OrderType.SL_M:
            ok, why = self._clear_stop_for_exit(order, now)
            if not ok:
                return self._reject(order, now, why, count=False)
        order.correlation_id = correlation_id(order.id)
        payload = self._payload(order)
        try:
            resp = self.api.place(payload)
        except DhanOrderError as exc:
            if not exc.ambiguous:
                if order.is_exit:
                    self._exit_block[order.position_id] = self.wall() + EXIT_RETRY_S
                return self._reject(order, now, str(exc) if exc.kind == "ORDER" else exc.explain(),
                                    count=exc.kind in ("ORDER", "ACCOUNT"))
            resp = {}                      # timed out mid-flight: found later by correlationId (or given up)
            self.notes.append(f"{order.instrument.symbol} {order.purpose}: Dhan did not answer in time; "
                              f"tracking it by correlationId {order.correlation_id}")
        bid = str(resp.get("orderId") or "")
        order.transition(OrderStatus.SUBMITTED, now, f"Dhan order {bid or '?'}")
        self._price_note(order, payload)
        self.track[order.id] = _Track(bid, self.wall())
        if status_of(resp) == "REJECTED":
            row = self._lookup(bid) or resp
            self._drop(order.id)
            if order.is_exit:
                self._exit_block[order.position_id] = self.wall() + EXIT_RETRY_S
            return self._reject(order, now, reason_of(row) or "rejected by Dhan", count=True, transition=True)
        if order.type is OrderType.SL_M:
            order.transition(OrderStatus.OPEN, now, f"resting stop @ {order.trigger:.2f} at Dhan")
            self.resting[order.id] = order
        else:
            self.pending[order.id] = order
        self._wake.set()
        return True

    def modify_stop(self, order_id: str, trigger: float, now: dt.datetime) -> bool:
        o, tr = self.resting.get(order_id), self.track.get(order_id)
        if o is None or tr is None or not tr.broker_id:
            return False
        otype, price, trig = self._type_price(o, trigger=trigger)
        try:
            self.api.modify(tr.broker_id, self._modify_payload(o, tr.broker_id, o.qty, otype, price, trig))
        except DhanOrderError as exc:
            self.notes.append(f"{o.instrument.symbol}: moving the broker-side stop to {trig:.2f} failed ({exc})")
            return False
        o.trigger, o.price = trig, price
        tr.touched = self.wall()
        o.history.append({"ts": now.isoformat(), "from": "OPEN", "to": "OPEN", "note": f"trigger -> {trig:.2f}"})
        return True

    def modify_qty(self, order_id: str, qty: int, now: dt.datetime) -> None:
        o, tr = self.resting.get(order_id), self.track.get(order_id)
        if o is None or tr is None or not tr.broker_id or qty == o.qty or qty <= 0:
            return
        otype, price, trig = self._type_price(o)
        try:
            self.api.modify(tr.broker_id, self._modify_payload(o, tr.broker_id, qty, otype, price, trig))
        except DhanOrderError as exc:
            self.notes.append(f"{o.instrument.symbol}: resizing the broker-side stop to {qty} failed ({exc})")
            return
        o.qty = qty
        tr.touched = self.wall()
        o.history.append({"ts": now.isoformat(), "from": "OPEN", "to": "OPEN", "note": f"qty -> {qty}"})

    def cancel(self, order_id: str, now: dt.datetime) -> bool:
        """Cancel at Dhan. A working entry/exit leaves only when Dhan's book confirms (a fill may still arrive);
        a resting stop is checked for fills right away. False = Dhan did not accept the cancel."""
        o = self.pending.get(order_id) or self.resting.get(order_id)
        tr = self.track.get(order_id)
        if o is None or tr is None:
            return True
        if not tr.broker_id:
            tr.cancel_requested = True       # not seen at Dhan yet: cancelled as soon as it is
            return False
        try:
            self.api.cancel(tr.broker_id)
        except DhanOrderError as exc:
            self.notes.append(f"{o.instrument.symbol} {o.purpose}: cancel failed ({exc})")
            return False
        tr.cancel_requested, tr.touched = True, self.wall()
        if order_id in self.resting:
            self._settle_cancelled_stop(o, now)
        return True

    def process(self, quotes: dict[str, Quote], now: dt.datetime) -> list[Fill]:
        fills, self._ready = self._ready, []
        with self._lock:
            book, book_wall = dict(self._book), self._book_wall
        by_cid = {str(r.get("correlationId")): r for r in book.values() if r.get("correlationId")}
        w = self.wall()
        for oid, o in list(self.pending.items()) + list(self.resting.items()):
            tr = self.track.get(oid)
            if tr is None:
                continue
            row = book.get(tr.broker_id) if tr.broker_id else by_cid.get(o.correlation_id)
            if row is None:
                if book_wall - tr.sent > UNSEEN_TIMEOUT_S:
                    self._resolve_unseen(o, tr, now, fills)
                continue
            if not tr.broker_id:
                tr.broker_id = str(row.get("orderId"))
                if tr.cancel_requested:
                    self.cancel(oid, now)
            if book_wall < tr.touched:
                continue                          # this row predates our last change to the order
            self._apply_row(o, tr, row, now, fills, w)
        return fills

    def reject_count(self, now: dt.datetime, window_s: float = 60.0) -> int:
        self.rejects = [t for t in self.rejects if (now - t).total_seconds() <= window_s]
        return len(self.rejects)

    def exit_wait(self, position_id: str) -> float:
        """Seconds until an exit for this position may be sent again (after Dhan refused one)."""
        return max(0.0, self._exit_block.get(position_id, 0.0) - self.wall())

    def drain_failed(self) -> list[Order]:
        out, self.failed = self.failed, []
        return out

    # ------------------------------------------------------------------ internals
    def _apply_row(self, o: Order, tr: _Track, row: dict[str, Any], now: dt.datetime, fills: list[Fill],
                   w: float) -> None:
        st, filled = status_of(row), filled_of(row)
        stop_leg = o.id in self.resting
        if st == FILLED:
            fills.append(self._fill(o, row, now, "broker-side stop triggered" if stop_leg else ""))
            self._drop(o.id)
        elif st == "REJECTED":
            self._drop(o.id)
            if o.is_exit:
                self._exit_block[o.position_id] = w + EXIT_RETRY_S
            self._fail(o, now, reason_of(row) or "rejected by Dhan", rejected=True)
        elif st in DEAD:
            self._drop(o.id)
            if filled > 0:
                fills.append(self._fill(o, row, now, f"{filled} filled, rest {st.lower()}", qty=filled))
            elif stop_leg and not tr.cancel_requested:
                self.notes.append(f"{o.instrument.symbol}: broker-side stop was {st.lower()} outside Sthitaprajna "
                                  f"(Dhan app?) -- placing it again")
                self._fail(o, now, f"{st.lower()} at Dhan")
            else:
                self._fail(o, now, "not filled in time, cancelled" if tr.cancel_requested else f"{st.lower()} at Dhan")
        elif not stop_leg:                        # still working
            age = w - tr.sent
            if not o.is_exit and not tr.cancel_requested and age > ENTRY_TIMEOUT_S and w >= tr.retry_at:
                self.notes.append(f"{o.instrument.symbol}: entry not filled in {ENTRY_TIMEOUT_S:.0f}s -> cancelling")
                if not self.cancel(o.id, now):
                    tr.retry_at = w + 2.0
            elif o.is_exit and o.instrument.is_option and w - max(tr.sent, tr.touched) > CHASE_AFTER_S \
                    and tr.chase < len(CHASE_STEPS) - 1:
                self._chase(o, tr, row, now)

    def _chase(self, o: Order, tr: _Track, row: dict[str, Any], now: dt.datetime) -> None:
        price = self._marketable(o, tr.chase + 1)
        if price <= 0:
            return
        qty = max(1, o.qty - filled_of(row))
        try:
            self.api.modify(tr.broker_id, self._modify_payload(o, tr.broker_id, qty, "LIMIT", price, 0.0))
        except DhanOrderError as exc:
            self.notes.append(f"{o.instrument.symbol}: re-pricing the exit failed ({exc})")
            tr.touched = self.wall()
            return
        tr.chase += 1
        tr.touched = self.wall()
        o.price = price
        self.notes.append(f"{o.instrument.symbol}: exit not filled -> limit {price:.2f} "
                          f"(bid - {CHASE_STEPS[tr.chase]:.0%})")

    def _resolve_unseen(self, o: Order, tr: _Track, now: dt.datetime, fills: list[Fill]) -> None:
        """An order Dhan's book never showed: look it up directly once; nothing there -> it was not placed."""
        try:
            row = self.api.order(tr.broker_id) if tr.broker_id else self.api.find_by_correlation(o.correlation_id)
        except DhanOrderError as exc:
            self.notes.append(f"{o.instrument.symbol} {o.purpose}: lookup failed ({exc}); still checking")
            tr.sent = self.wall()
            return
        if row:
            tr.broker_id = str(row.get("orderId") or tr.broker_id)
            self._apply_row(o, tr, row, now, fills, self.wall())
            return
        self._drop(o.id)
        self._fail(o, now, "Dhan has no record of this order (not placed)")

    def _clear_stop_for_exit(self, order: Order, now: dt.datetime) -> tuple[bool, str]:
        """Before any exit: remove (or shrink) the resting stop at Dhan so both can never sell the same quantity."""
        wait = self._exit_block.get(order.position_id, 0.0) - self.wall()
        if wait > 0:
            return False, f"retrying in {wait:.0f}s after Dhan refused the last exit"
        stop = next((s for s in self.resting.values() if s.position_id == order.position_id), None)
        if stop is None:
            return True, ""
        tr = self.track.get(stop.id)
        if tr is None or not tr.broker_id:
            return False, "the broker-side stop is still being placed; retrying"
        left = stop.qty - order.qty
        try:
            if left > 0:
                otype, price, trig = self._type_price(stop)
                self.api.modify(tr.broker_id, self._modify_payload(stop, tr.broker_id, left, otype, price, trig))
                stop.qty, tr.touched = left, self.wall()
                return True, ""
            self.api.cancel(tr.broker_id)
        except DhanOrderError as exc:
            row = self._lookup(tr.broker_id)
            st, filled = status_of(row), filled_of(row)
            if st == FILLED or filled > 0:
                self._ready.append(self._fill(stop, row or {}, now, "broker-side stop triggered", qty=filled or None))
                self._drop(stop.id)
                return False, "not sent: the broker-side stop already sold first"
            if st in DEAD or st == "REJECTED":
                self._drop(stop.id)
                stop.transition(OrderStatus.CANCELLED, now, f"{st.lower()} at Dhan")
                return True, ""
            return False, f"could not cancel the broker-side stop first ({exc}); retrying"
        tr.cancel_requested = True
        ok = self._settle_cancelled_stop(stop, now)
        return ok, ("" if ok else "not sent: the broker-side stop already sold part first")

    def _settle_cancelled_stop(self, stop: Order, now: dt.datetime) -> bool:
        """After Dhan accepted a stop's cancel: True if it had no fills. Any fill it got first is reported."""
        tr = self.track.get(stop.id)
        row = self._lookup(tr.broker_id) if tr else None
        filled = filled_of(row)
        self._drop(stop.id)
        if filled > 0:
            self._ready.append(self._fill(stop, row or {}, now, "broker-side stop triggered", qty=filled))
            return False
        stop.transition(OrderStatus.CANCELLED, now, "cancelled at Dhan")
        return True

    def _lookup(self, broker_id: str) -> dict[str, Any] | None:
        if not broker_id:
            return None
        try:
            return self.api.order(broker_id)
        except DhanOrderError:
            with self._lock:
                return self._book.get(broker_id)

    def _unplaceable(self, o: Order) -> str:
        inst = o.instrument
        if not inst.dhan_security_id:
            return f"no Dhan security id for {inst.symbol}"
        if o.qty <= 0:
            return "quantity must be positive"
        if inst.is_option and inst.lot_size > 1 and o.qty % inst.lot_size:
            return f"quantity {o.qty} is not a multiple of the lot {inst.lot_size}"
        if o.type is OrderType.MARKET and inst.is_option and self._marketable(o, 0) <= 0:
            return f"no live quote to price the {inst.symbol} order"
        return ""

    def _type_price(self, o: Order, trigger: float | None = None) -> tuple[str, float, float]:
        inst, sell = o.instrument, o.side is Side.SELL
        tick = inst.tick_size or 0.05
        if o.type is OrderType.SL_M:
            trig = round_tick(o.trigger if trigger is None else trigger, tick, down=sell)
            if inst.is_option:                    # SL-M in options is refused by many brokers [VERIFY for Dhan]
                gap = max(3 * tick, OPTION_STOP_LIMIT_GAP * trig)
                return "STOP_LOSS", max(tick, round_tick(trig - gap if sell else trig + gap, tick, down=sell)), trig
            return "STOP_LOSS_MARKET", 0.0, trig
        if o.type is OrderType.LIMIT:
            return "LIMIT", round_tick(o.price, tick, down=sell), 0.0
        if inst.is_option:                        # no market orders in options: a marketable limit instead
            return "LIMIT", self._marketable(o, 0), 0.0
        return "MARKET", 0.0, 0.0

    def _marketable(self, o: Order, step: int) -> float:
        q = self.quote(o.instrument.key)
        tick = o.instrument.tick_size or 0.05
        ltp = q.ltp if q is not None else 0.0
        if o.side is Side.BUY:
            ref = q.ask if q is not None and q.ask > 0 else ltp
            return round_tick(ref + max(2 * tick, OPTION_ENTRY_BUFFER * ref), tick, down=False) if ref > 0 else 0.0
        ref = q.bid if q is not None and q.bid > 0 else ltp
        if ref <= 0:
            return 0.0
        cushion = CHASE_STEPS[min(step, len(CHASE_STEPS) - 1)]
        return max(tick, round_tick(ref - max(2 * tick, cushion * ref), tick, down=True))

    def _payload(self, o: Order) -> dict[str, Any]:
        otype, price, trig = self._type_price(o)
        if o.type is OrderType.SL_M:
            o.trigger = trig
        o.price = price
        p: dict[str, Any] = {
            "dhanClientId": self.api.client_id, "correlationId": o.correlation_id, "transactionType": o.side.value,
            "exchangeSegment": exchange_segment(o.instrument), "productType": product_type(o.segment),
            "orderType": otype, "validity": "DAY", "securityId": str(o.instrument.dhan_security_id),
            "quantity": int(o.qty), "disclosedQuantity": 0, "price": price, "triggerPrice": trig,
            "afterMarketOrder": False,
        }
        if self.api.algo_id:
            p["algoId"] = self.api.algo_id          # [VERIFY] field name with Dhan for your account
        return p

    def _modify_payload(self, o: Order, broker_id: str, qty: int, otype: str, price: float,
                        trig: float) -> dict[str, Any]:
        return {"dhanClientId": self.api.client_id, "orderId": broker_id, "orderType": otype, "legName": "",
                "quantity": int(qty), "price": price, "disclosedQuantity": 0, "triggerPrice": trig, "validity": "DAY"}

    def _price_note(self, o: Order, payload: dict[str, Any]) -> None:
        if payload["orderType"] != o.type.value and o.type is not OrderType.SL_M:
            o.history.append({"ts": o.created.isoformat(), "from": o.status.value, "to": o.status.value,
                              "note": f"sent as {payload['orderType']} {payload['price']:.2f}"})

    def _fill(self, o: Order, row: dict[str, Any], now: dt.datetime, note: str, qty: int | None = None) -> Fill:
        qty = int(qty or filled_of(row) or o.qty)
        px = _num(row.get("averageTradedPrice")) or self._trade_vwap(str(row.get("orderId") or ""))
        if px <= 0:
            q = self.quote(o.instrument.key)
            px = (q.ltp if q is not None else 0.0) or o.trigger or o.price or o.model_price
            self.notes.append(f"{o.instrument.symbol}: Dhan gave no fill price; using {px:.2f} until reconciled")
        ch = self.charges.compute(o.segment, o.side, px, qty, o.instrument.exchange).total
        o.filled_qty, o.avg_price, o.charges = qty, px, ch
        if o.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            o.transition(OrderStatus.FILLED, now, note or "filled at Dhan")
        return Fill(o, qty, px, now, ch)

    def _trade_vwap(self, broker_id: str) -> float:
        if not broker_id:
            return 0.0
        try:
            trades = self.api.trades(broker_id)
        except DhanOrderError:
            return 0.0
        q = sum(_num(t.get("tradedQuantity")) for t in trades)
        return sum(_num(t.get("tradedQuantity")) * _num(t.get("tradedPrice")) for t in trades) / q if q else 0.0

    def _drop(self, oid: str) -> None:
        self.pending.pop(oid, None)
        self.resting.pop(oid, None)
        self.track.pop(oid, None)

    def _reject(self, o: Order, now: dt.datetime, why: str, *, count: bool, transition: bool = True) -> bool:
        o.reject_reason = why
        if transition and o.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            o.transition(OrderStatus.REJECTED, now, why)
        if count:
            self.rejects.append(now)
        return False

    def _fail(self, o: Order, now: dt.datetime, why: str, rejected: bool = False) -> None:
        o.reject_reason = why
        if o.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            o.transition(OrderStatus.REJECTED if rejected else OrderStatus.CANCELLED, now, why)
        if rejected:
            self.rejects.append(now)
        self.failed.append(o)
