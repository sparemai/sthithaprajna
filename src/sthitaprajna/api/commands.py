"""Operator command handler shared by Discord (D10) and the terminal's command bar.

Rules (D6, D10): commands are accepted only from the operator; they are rate-limited; and NO command can
increase risk or raise the gear -- the set below can only view, protect, reduce, lock, or halt.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

HELP = """Commands (none can increase risk or raise the gear):
/status  /gear  /positions  /pnl  /queue  /help
/target day|week|month <value>[%]   e.g. /target month 8%  or  /target day 1500
/lock [day|week|month]              stop new entries for that horizon
/close <position-id> | /close all   close now (instant, no approval)
/reduce <position-id>               close 50%
/trail <position-id>                trail the stop tighter
/approve <approval-id>  /deny <approval-id>
/kill                               flatten everything and halt"""

READ_ONLY = {"/status", "/gear", "/positions", "/pnl", "/queue", "/help"}
RISK_REDUCING = {"/target", "/lock", "/close", "/reduce", "/trail", "/approve", "/deny", "/kill"}
ALL_COMMANDS = READ_ONLY | RISK_REDUCING


@dataclass
class CommandGate:
    operator_id: str | None = None
    max_per_window: int = 10
    window_s: float = 10.0
    _sent: deque = field(default_factory=deque)

    def allow(self, user_id: str | None) -> tuple[bool, str]:
        if self.operator_id and str(user_id) != str(self.operator_id):
            return False, "commands are accepted only from the operator"
        now = time.monotonic()
        while self._sent and now - self._sent[0] > self.window_s:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_window:
            return False, "rate limited"
        self._sent.append(now)
        return True, ""


def _money(x: float | None) -> str:
    if x is None:
        return "-"
    s = f"{abs(x):,.0f}"
    return ("-" if x < 0 else "") + "Rs " + s


def handle(engine, text: str, user_id: str | None = None, gate: CommandGate | None = None) -> dict[str, Any]:
    from sthitaprajna.engine.snapshot import snapshot
    text = (text or "").strip()
    if gate is not None:
        ok, why = gate.allow(user_id)
        if not ok:
            return {"ok": False, "reply": why}
    parts = text.split()
    if not parts:
        return {"ok": False, "reply": HELP}
    cmd, args = parts[0].lower(), parts[1:]
    if cmd not in ALL_COMMANDS:
        return {"ok": False, "reply": f"unknown command {cmd}\n{HELP}"}
    s = snapshot(engine, include_watch=False)
    if cmd == "/help":
        return {"ok": True, "reply": HELP}
    if cmd == "/status" and s["risk_mode"] == "unbounded":
        a, c, r = s["account"], s["capital"], s["risk"]
        return {"ok": True, "reply": (
            f"[{s['label']}] {s['clock']['now'][:16]} | {'LIVE on Dhan' if s['broker']['live'] else s['mode']} | "
            f"Equity {_money(a['mtm'])} | Day {_money(a['day_pnl'])} | In trades {_money(c['deployed'])} | "
            f"Free {_money(c['free'])} | Positions {r['positions']} | Trades today {r['trades_today']}"
            + (" | KILL SWITCH ON" if r["kill_switch"] else ""))}
    if cmd == "/gear" and s["risk_mode"] == "unbounded":
        return {"ok": True, "reply": "Unbounded mode (D-031): no gears. Each trade gets "
                f"{s['capital']['per_trade_fraction']:.0%} of equity with its own stop-loss and target."}
    if cmd == "/status":
        a, g, r = s["account"], s["gear"], s["risk"]
        return {"ok": True, "reply": (
            f"[{s['label']}] {s['clock']['now'][:16]} | Gear {g['effective']} (R {g['effective_r']:.2%}) | "
            f"Equity {_money(a['mtm'])} | Day {_money(a['day_pnl'])} | Open risk {_money(r['open_risk'])}/"
            f"{_money(r['open_risk_limit'])} | Positions {r['positions']}/{r['max_positions']} | "
            f"Process {s['process']['score']:.0%}" + (" | KILL SWITCH ON" if r["kill_switch"] else ""))}
    if cmd == "/gear":
        g = s["gear"]
        nxt = g["next_downshift_in"]
        return {"ok": True, "reply": f"Gear {g['gear']} (effective {g['effective']}), drawdown {g['drawdown']:.2%}, "
                f"next downshift in {nxt:.2%}" if nxt is not None else "HALTED -- operator review required"}
    if cmd == "/positions":
        if not s["positions"]:
            return {"ok": True, "reply": "No open positions. Cash is a position."}
        lines = [f"{p['id']} {p['symbol']} {p['stage'] if s['risk_mode'] != 'unbounded' else p['direction']} qty {p['qty']} "
                 f"@ {p['avg_entry']} stop {p['stop']}" + (f" target {p['target']}" if p.get("target") else "")
                 + f" P&L {_money(p['net_pnl'])} ({p['r_multiple']:+.2f}R)" for p in s["positions"]]
        return {"ok": True, "reply": "\n".join(lines)}
    if cmd == "/pnl":
        a = s["account"]
        return {"ok": True, "reply": f"[{s['label']}] Day {_money(a['day_pnl'])} | Week {_money(a['week_pnl'])} | "
                f"Month {_money(a['month_pnl'])} | Total {a['total_return_pct']:+.2f}%"}
    if cmd == "/queue":
        rows = [r for r in s["funnel"]["records"] if r["status"] in ("READY", "ARMED", "PENDING_APPROVAL")][:8]
        if not rows:
            return {"ok": True, "reply": "Queue empty: nothing qualifies right now."}
        return {"ok": True, "reply": "\n".join(f"{r['symbol']} {r['strategy']} {r['grade'] or '-'} {r['stage']} "
                                               f"EV {((r['ev'] or {}).get('net_r') or 0):+.2f}R -- {r['reason']}" for r in rows)}
    if cmd == "/target":
        if len(args) < 2 or args[0] not in ("day", "week", "month"):
            return {"ok": False, "reply": "usage: /target day|week|month <value>[%]"}
        m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(%?)", args[1])
        if not m:
            return {"ok": False, "reply": "value must be a number, optionally with %"}
        res = engine.set_target(args[0], float(m.group(1)), "PCT" if m.group(2) else "RUPEES")
        f = res["feasibility"]
        return {"ok": True, "reply": f"Target set ({args[0]}): {f['label']}. Targets never change risk (D5).", "data": f}
    if cmd == "/lock":
        period = args[0] if args else "day"
        if period not in ("day", "week", "month"):
            return {"ok": False, "reply": "usage: /lock [day|week|month]"}
        engine.lock(period)
        return {"ok": True, "reply": f"Locked: no new entries this {period}."}
    if cmd in ("/close", "/reduce", "/trail"):
        if not args:
            return {"ok": False, "reply": f"usage: {cmd} <position-id>" + (" | all" if cmd == "/close" else "")}
        if cmd == "/close" and args[0] == "all":
            r = engine.close_all()
            return {"ok": True, "reply": f"Closing all ({r['closed']})."}
        if cmd == "/trail":
            r = engine.trail_tighter(args[0])
        else:
            r = engine.close(args[0], 1.0 if cmd == "/close" else 0.5)
        return {"ok": r.get("ok", False), "reply": "done" if r.get("ok") else r.get("error", "failed")}
    if cmd in ("/approve", "/deny"):
        if not args:
            return {"ok": False, "reply": f"usage: {cmd} <approval-id>"}
        r = engine.approve(args[0], cmd == "/approve")
        return {"ok": r.get("ok", False), "reply": r.get("result") or r.get("error", "")}
    if cmd == "/kill":
        engine.kill()
        return {"ok": True, "reply": "KILL: flattening everything and halting new entries."}
    return {"ok": False, "reply": HELP}
