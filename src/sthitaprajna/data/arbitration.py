"""Feed Arbitration (A5.5 rule 3) -- never average feeds.

| Situation                         | DATA_CONFIDENCE          | New entries           | Open positions               |
| Both healthy and agree            | High                     | Allowed               | Normal                       |
| Both healthy, disagree (> max(2 ticks, 5 bps), > 3 s) | Low for that instrument | Blocked on it | Managed on primary; alert |
| Dhan healthy, Upstox down         | Capped at 0.8            | Allowed               | Normal; alert                |
| Dhan down, Upstox healthy         | Low                      | Blocked               | Monitored via Upstox; exits via Dhan |
| Both down                         | None                     | Blocked               | Broker-side stops; flatten after 5 min |
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sthitaprajna.domain import Quote


@dataclass
class FeedState:
    name: str
    last_receive: dt.datetime | None = None
    quotes: dict[str, Quote] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    updates: int = 0
    disconnects: int = 0
    was_healthy: bool = True

    def healthy(self, now: dt.datetime, stale_s: float) -> bool:
        return self.last_receive is not None and (now - self.last_receive).total_seconds() <= stale_s

    def ingest(self, quotes: list[Quote]) -> None:
        for q in quotes:
            self.quotes[q.key] = q
            lat = (q.receive_ts - q.exchange_ts).total_seconds() * 1000
            self.latencies_ms.append(lat)
        if len(self.latencies_ms) > 2000:
            self.latencies_ms = self.latencies_ms[-2000:]
        if quotes:
            self.last_receive = max(q.receive_ts for q in quotes)
            self.updates += len(quotes)

    def stats(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms[-500:])
        p = (lambda f: lat[min(len(lat) - 1, int(f * len(lat)))] if lat else None)  # noqa: E731
        return {"name": self.name, "updates": self.updates, "disconnects": self.disconnects,
                "latency_p50_ms": p(0.5), "latency_p95_ms": p(0.95),
                "last_receive": self.last_receive.isoformat() if self.last_receive else None}


class FeedArbiter:
    def __init__(self, data_cfg: dict[str, Any], primary: str = "DHAN", secondary: str = "UPSTOX") -> None:
        self.cfg = data_cfg
        self.primary = FeedState(primary)
        self.secondary = FeedState(secondary)
        self._disagree_since: dict[str, dt.datetime] = {}
        self.disagreeing: dict[str, float] = {}
        self.situation = "BOTH_AGREE"
        self.both_down_since: dt.datetime | None = None
        self.alerts: list[dict[str, Any]] = []

    def _alert(self, now: dt.datetime, msg: str) -> None:
        self.alerts.append({"ts": now.isoformat(), "msg": msg})
        self.alerts = self.alerts[-50:]

    def update(self, now: dt.datetime, primary: list[Quote], secondary: list[Quote], tick: float = 0.05) -> None:
        self.primary.ingest(primary)
        self.secondary.ingest(secondary)
        stale = float(self.cfg["stale_after_seconds"])
        ph, sh = self.primary.healthy(now, stale), self.secondary.healthy(now, stale)
        for fs, h in ((self.primary, ph), (self.secondary, sh)):
            if fs.was_healthy and not h:
                fs.disconnects += 1
                self._alert(now, f"{fs.name} feed stale/down")
            elif not fs.was_healthy and h:
                self._alert(now, f"{fs.name} feed recovered")
            fs.was_healthy = h
        if ph and sh:
            self._check_disagreement(now, tick)
            self.situation = "DISAGREE" if self.disagreeing else "BOTH_AGREE"
            self.both_down_since = None
        elif ph:
            self.situation = "SECONDARY_DOWN"
            self.disagreeing.clear()
            self.both_down_since = None
        elif sh:
            self.situation = "PRIMARY_DOWN"
            self.disagreeing.clear()
            self.both_down_since = None
        else:
            self.situation = "BOTH_DOWN"
            self.both_down_since = self.both_down_since or now

    def _check_disagreement(self, now: dt.datetime, tick: float) -> None:
        bps = float(self.cfg["disagree_bps"])
        ticks = float(self.cfg["disagree_ticks"])
        persist = float(self.cfg["disagree_persist_seconds"])
        for key, pq in self.primary.quotes.items():
            sq = self.secondary.quotes.get(key)
            if sq is None or pq.ltp <= 0:
                continue
            thr = max(ticks * tick, pq.ltp * bps / 1e4)
            diff = abs(pq.ltp - sq.ltp)
            if diff > thr:
                since = self._disagree_since.setdefault(key, now)
                if (now - since).total_seconds() > persist and key not in self.disagreeing:
                    self.disagreeing[key] = diff / pq.ltp * 1e4
                    self._alert(now, f"feeds disagree on {key}: {self.disagreeing[key]:.1f} bps")
                elif key in self.disagreeing:
                    self.disagreeing[key] = diff / pq.ltp * 1e4
            else:
                self._disagree_since.pop(key, None)
                self.disagreeing.pop(key, None)

    # ------------------------------------------------------------------ queries
    def data_confidence(self, key: str | None = None) -> float:
        s = self.situation
        if s == "BOTH_DOWN":
            return 0.0
        if s == "PRIMARY_DOWN":
            return 0.3
        if s == "SECONDARY_DOWN":
            return float(self.cfg["secondary_down_confidence_cap"])
        if key is not None and key in self.disagreeing:
            return 0.3
        return 1.0

    def best_quote(self, key: str) -> Quote | None:
        """Primary when healthy; otherwise the secondary is used for MONITORING only."""
        if self.situation in ("BOTH_AGREE", "DISAGREE", "SECONDARY_DOWN"):
            return self.primary.quotes.get(key) or self.secondary.quotes.get(key)
        if self.situation == "PRIMARY_DOWN":
            return self.secondary.quotes.get(key)
        return self.primary.quotes.get(key) or self.secondary.quotes.get(key)

    def entries_allowed(self) -> bool:
        return self.situation in ("BOTH_AGREE", "DISAGREE", "SECONDARY_DOWN")

    def should_flatten(self, now: dt.datetime) -> bool:
        lim = float(self.cfg["both_down_flatten_after_seconds"])
        return self.both_down_since is not None and (now - self.both_down_since).total_seconds() >= lim

    def feed_agreement(self) -> str:
        return {"BOTH_AGREE": "AGREE", "DISAGREE": "DISAGREE", "SECONDARY_DOWN": "SECONDARY_DOWN",
                "PRIMARY_DOWN": "PRIMARY_DOWN", "BOTH_DOWN": "BOTH_DOWN"}[self.situation]

    def to_dict(self) -> dict[str, Any]:
        return {"situation": self.situation, "primary": self.primary.stats(), "secondary": self.secondary.stats(),
                "disagreeing": {k: round(v, 2) for k, v in self.disagreeing.items()},
                "data_confidence": self.data_confidence(), "entries_allowed": self.entries_allowed(),
                "alerts": self.alerts[-15:]}
