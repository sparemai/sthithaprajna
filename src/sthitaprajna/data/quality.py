"""Data Quality Guardian (A5.1 #1): gaps, stale ticks, bad prints, clock drift, missing symbols.
Bad data -> affected signals blocked; systemic problems -> operational halt."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sthitaprajna.domain import Quote


@dataclass
class DataQualityGuardian:
    stale_after_s: float = 5.0
    max_jump_index: float = 0.03
    max_jump_stock: float = 0.08
    max_clock_drift_s: float = 2.0
    last: dict[str, Quote] = field(default_factory=dict)
    blocked: dict[str, str] = field(default_factory=dict)
    halts: dict[str, str] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def _event(self, now: dt.datetime, key: str, msg: str) -> None:
        self.events.append({"ts": now.isoformat(), "key": key, "msg": msg})
        self.events = self.events[-100:]

    def check(self, now: dt.datetime, quotes: list[Quote], expected: list[str], is_index: set[str],
              local_clock: dt.datetime | None = None) -> None:
        for q in quotes:
            prev = self.last.get(q.key)
            reason = None
            if q.ltp <= 0 or q.ltp != q.ltp:
                reason = "non-positive/NaN price"
            elif q.bid > 0 and q.ask > 0 and q.bid > q.ask:
                reason = "crossed quote"
            elif prev is not None and prev.ltp > 0 and not q.key.startswith(("NFO:", "BFO:")):
                jump = abs(q.ltp / prev.ltp - 1)
                lim = self.max_jump_index if q.key in is_index else self.max_jump_stock
                if jump > lim and (q.exchange_ts - prev.exchange_ts).total_seconds() < 60:
                    reason = f"bad print? {jump:.1%} jump"
            if reason:
                if self.blocked.get(q.key) != reason:
                    self._event(now, q.key, reason)
                self.blocked[q.key] = reason
            else:
                if q.key in self.blocked and not self.blocked[q.key].startswith("stale"):
                    self.blocked.pop(q.key, None)
                self.last[q.key] = q
        for key in expected:
            q = self.last.get(key)
            if q is None:
                self.blocked[key] = "missing symbol"
            elif (now - q.receive_ts).total_seconds() > self.stale_after_s:
                if not self.blocked.get(key, "").startswith("stale"):
                    self._event(now, key, "stale ticks")
                self.blocked[key] = "stale ticks"
            elif self.blocked.get(key) in ("stale ticks", "missing symbol"):
                self.blocked.pop(key, None)
        if local_clock is not None:
            drift = abs((local_clock - now).total_seconds())
            if drift > self.max_clock_drift_s:
                self.halts["clock_drift"] = f"clock drift {drift:.1f}s"
            else:
                self.halts.pop("clock_drift", None)

    def to_dict(self) -> dict[str, Any]:
        return {"blocked": dict(list(self.blocked.items())[:50]), "halts": dict(self.halts),
                "events": self.events[-20:]}
