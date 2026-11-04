"""Session, holiday and expiry calendar (A4). Weekdays come from market_rules, never hardcoded.

In production the expiry list is loaded from the Dhan instrument master every morning; the computed
calendar here is the fallback and the simulator's source of truth.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sthitaprajna.domain import IST

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4}
SESSION_MINUTES = 375  # 09:15 -> 15:30
TRADING_DAYS_PER_YEAR = 252


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


@dataclass(frozen=True)
class MarketCalendar:
    holidays: frozenset[dt.date]
    session_open: dt.time = dt.time(9, 15)
    session_close: dt.time = dt.time(15, 30)
    intraday_exit: dt.time = dt.time(15, 10)
    nifty_weekday: str = "TUE"
    sensex_weekday: str = "THU"

    @classmethod
    def from_rules(cls, rules, holidays: frozenset[dt.date]) -> "MarketCalendar":
        return cls(
            holidays=holidays,
            session_open=_hm(rules.get("session_open", "09:15")),
            session_close=_hm(rules.get("session_close", "15:30")),
            intraday_exit=_hm(rules.get("system_intraday_exit_time", "15:10")),
            nifty_weekday=rules.get("nifty_weekly_expiry_weekday", "TUE"),
            sensex_weekday=rules.get("sensex_weekly_expiry_weekday", "THU"),
        )

    # -------------------------------------------------------------------------- trading days
    def is_trading_day(self, d: dt.date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def next_trading_day(self, d: dt.date) -> dt.date:
        d = d + dt.timedelta(days=1)
        while not self.is_trading_day(d):
            d += dt.timedelta(days=1)
        return d

    def prev_trading_day(self, d: dt.date) -> dt.date:
        d = d - dt.timedelta(days=1)
        while not self.is_trading_day(d):
            d -= dt.timedelta(days=1)
        return d

    def trading_day_on_or_before(self, d: dt.date) -> dt.date:
        while not self.is_trading_day(d):
            d -= dt.timedelta(days=1)
        return d

    def open_dt(self, d: dt.date) -> dt.datetime:
        return dt.datetime.combine(d, self.session_open, tzinfo=IST)

    def close_dt(self, d: dt.date) -> dt.datetime:
        return dt.datetime.combine(d, self.session_close, tzinfo=IST)

    def is_open(self, ts: dt.datetime) -> bool:
        t = ts.astimezone(IST)
        return self.is_trading_day(t.date()) and self.session_open <= t.time() < self.session_close

    def minutes_into_session(self, ts: dt.datetime) -> float:
        t = ts.astimezone(IST)
        return (t - self.open_dt(t.date())).total_seconds() / 60.0

    # -------------------------------------------------------------------------- expiries
    def weekday_for(self, underlying: str) -> str | None:
        u = underlying.upper()
        if u in ("NIFTY", "NIFTY50"):
            return self.nifty_weekday
        if u == "SENSEX":
            return self.sensex_weekday
        return None

    def weekly_expiries(self, underlying: str, start: dt.date, count: int = 4) -> list[dt.date]:
        wd = self.weekday_for(underlying)
        if wd is None:
            return self.monthly_expiries(underlying, start, count)
        target = WEEKDAYS[wd]
        out: list[dt.date] = []
        d = start
        # nominal expiry weekday; holiday -> previous trading day (A4)
        while len(out) < count:
            delta = (target - d.weekday()) % 7
            nominal = d + dt.timedelta(days=delta)
            actual = self.trading_day_on_or_before(nominal)
            if actual >= start and actual not in out:
                out.append(actual)
            d = nominal + dt.timedelta(days=1)
        return out

    def monthly_expiries(self, underlying: str, start: dt.date, count: int = 3) -> list[dt.date]:
        """Last <weekday> of each month (Bank Nifty: last Tuesday -- a VERIFY item)."""
        wd = WEEKDAYS["THU" if underlying.upper() in ("SENSEX", "BANKEX") else "TUE"]
        out: list[dt.date] = []
        y, m = start.year, start.month
        while len(out) < count:
            nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
            last = nxt - dt.timedelta(days=1)
            while last.weekday() != wd:
                last -= dt.timedelta(days=1)
            actual = self.trading_day_on_or_before(last)
            if actual >= start:
                out.append(actual)
            y, m = nxt.year, nxt.month
        return out

    def is_expiry_day(self, underlying: str, d: dt.date) -> bool:
        exps = self.weekly_expiries(underlying, d, 1)
        return bool(exps) and exps[0] == d

    def trading_minutes_to(self, now: dt.datetime, expiry: dt.date) -> float:
        """Time to expiry in trading minutes (A6.6: time-to-expiry in trading minutes)."""
        now = now.astimezone(IST)
        exp_close = self.close_dt(expiry)
        if now >= exp_close:
            return 0.0
        total = 0.0
        d = now.date()
        while d <= expiry:
            if self.is_trading_day(d):
                o, c = self.open_dt(d), self.close_dt(d)
                start = max(o, now) if d == now.date() else o
                if c > start:
                    total += (c - start).total_seconds() / 60.0
            d += dt.timedelta(days=1)
        return total

    def year_fraction_to(self, now: dt.datetime, expiry: dt.date) -> float:
        return self.trading_minutes_to(now, expiry) / (SESSION_MINUTES * TRADING_DAYS_PER_YEAR)
