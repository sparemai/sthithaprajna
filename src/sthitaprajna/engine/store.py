"""SQLite persistence: decisions (A18), closed positions, events, equity, journal.

Any trading day must be replayable from stored data and logs (A5.1). SQLite keeps setup at zero on a
Windows PC or a Mumbai VM; the JSON columns keep the schema stable while objects evolve.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events    (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, ts TEXT, action TEXT, symbol TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS positions (id TEXT PRIMARY KEY, closed_at TEXT, symbol TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS equity    (ts TEXT PRIMARY KEY, equity REAL, drawdown REAL, gear TEXT);
CREATE TABLE IF NOT EXISTS journal   (date TEXT PRIMARY KEY, body TEXT);
CREATE TABLE IF NOT EXISTS iv_history (date TEXT, underlying TEXT, atm_iv REAL, PRIMARY KEY(date, underlying));
CREATE TABLE IF NOT EXISTS period_review (period_type TEXT, period_key TEXT, closed_at TEXT, body TEXT,
                                          PRIMARY KEY(period_type, period_key));
CREATE INDEX IF NOT EXISTS idx_dec_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_evt_ts ON events(ts);
"""


class Store:
    def __init__(self, path: str | Path = "var/sthitaprajna.sqlite") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _exec(self, sql: str, args: tuple) -> None:
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    def event(self, e: dict[str, Any]) -> None:
        self._exec("INSERT INTO events(ts, kind, body) VALUES (?,?,?)", (e["ts"], e["kind"], json.dumps(e)))

    def decision(self, d: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?)",
                   (d["decision_id"], d["timestamp"], d["action"], d.get("symbol", ""), json.dumps(d, default=str)))

    def position(self, p: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
                   (p["id"], p.get("closed_at") or "", p.get("symbol", ""), json.dumps(p, default=str)))

    def equity(self, ts: dt.datetime, equity: float, dd: float, gear: str) -> None:
        self._exec("INSERT OR REPLACE INTO equity VALUES (?,?,?,?)", (ts.isoformat(), equity, dd, gear))

    def journal(self, j: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO journal VALUES (?,?)", (j["date"], json.dumps(j, default=str)))

    def iv_snapshot(self, date: dt.date, underlying: str, atm_iv: float) -> None:
        """One EOD ATM-IV point per underlying (A6.5 IV rank/percentile history, D-039)."""
        self._exec("INSERT OR REPLACE INTO iv_history VALUES (?,?,?)", (date.isoformat(), underlying, atm_iv))

    def iv_history_for(self, underlying: str, limit: int = 252) -> list[float]:
        rows = self.query("SELECT atm_iv FROM iv_history WHERE underlying=? ORDER BY date DESC LIMIT ?",
                          (underlying, limit))
        return [r[0] for r in reversed(rows)]

    def query(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return list(self._db.execute(sql, args))

    def recent_decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        return [json.loads(r[0]) for r in self.query("SELECT body FROM decisions ORDER BY ts DESC LIMIT ?", (limit,))]

    def period_review(self, r: dict[str, Any]) -> None:
        """D-054: plan-vs-actual at the close of a week or month (never day -- that's /api/journal
        already), so the operator can review every period's target against what actually happened,
        not just the live/current one."""
        self._exec("INSERT OR REPLACE INTO period_review VALUES (?,?,?,?)",
                   (r["period_type"], r["period_key"], r["closed_at"], json.dumps(r, default=str)))

    def period_reviews(self, period_type: str, limit: int = 104) -> list[dict[str, Any]]:
        """Most recent first; 104 weeks (2 years) / 104 months default cap -- plenty for a 4-month review."""
        rows = self.query("SELECT body FROM period_review WHERE period_type=? ORDER BY closed_at DESC LIMIT ?",
                          (period_type, limit))
        return [json.loads(r[0]) for r in rows]

    def closed_trade_history(self) -> list[dict[str, Any]]:
        """D-047: every closed position ever recorded, oldest first -- used to restore realized equity
        and the all-time peak across a process restart (PAPER/SIM equity previously reset to
        capital.starting on every restart; see Ledger.restore_from_history)."""
        rows = self.query("SELECT body FROM positions WHERE closed_at != '' ORDER BY closed_at ASC")
        return [json.loads(r[0]) for r in rows]

    def close(self) -> None:
        self._db.close()
