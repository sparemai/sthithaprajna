"""Validated NIFTY 500 membership from NSE Indices; never infer it from Dhan's whole master."""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
MAX_CACHE_DAYS = 7


@dataclass(frozen=True)
class Member:
    symbol: str
    name: str
    industry: str
    isin: str


def parse_members(text: str) -> list[Member]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("Expected the official NIFTY 500 constituent CSV (Symbol, Series, ISIN Code, etc.).")
    members = []
    seen = set()
    for row in reader:
        symbol = (row.get("Symbol") or "").strip().upper()
        isin = (row.get("ISIN Code") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9&_.-]+", symbol) or symbol in seen:
            raise ValueError(f"Invalid or duplicate constituent symbol: {symbol!r}")
        if (row.get("Series") or "").strip() != "EQ" or not re.fullmatch(r"IN[A-Z0-9]{10}", isin):
            raise ValueError(f"Invalid equity series/ISIN for {symbol}")
        seen.add(symbol)
        members.append(Member(symbol, row["Company Name"].strip(), row["Industry"].strip(), isin))
    # Corporate actions can temporarily change the number of constituent securities.
    if not 490 <= len(members) <= 510:
        raise ValueError(f"Expected approximately 500 constituents; received {len(members)}. Refusing this list.")
    return members


def load_members(cache_dir: Path, csv_path: Path | None = None, *, progress=print,
                 today: dt.date | None = None, fetch=None) -> list[Member]:
    today = today or dt.date.today()
    if csv_path is not None:
        members = parse_members(csv_path.read_text(encoding="utf-8-sig"))
        progress(f"NIFTY 500: {len(members)} constituents from supplied CSV {csv_path}; verify its date.")
        return members
    cache = cache_dir / "nifty500.csv"
    stamp = cache_dir / "nifty500.date"
    cached = None
    age = None
    try:
        cached = parse_members(cache.read_text(encoding="utf-8-sig"))
        age = (today - dt.date.fromisoformat(stamp.read_text().strip())).days
    except (OSError, ValueError):
        pass
    if cached and age == 0:
        progress(f"NIFTY 500: {len(cached)} constituents, cache downloaded {today}.")
        return cached

    def download():
        response = httpx.get(URL, timeout=30, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
        response.raise_for_status()
        return response.text

    try:
        text = (fetch or download)()
        members = parse_members(text)
    except (httpx.HTTPError, ValueError) as exc:
        if cached and age is not None and 0 <= age <= MAX_CACHE_DAYS:
            progress(f"WARNING: constituent refresh failed ({type(exc).__name__}); using {age}-day-old "
                     f"NIFTY 500 list with {len(cached)} members.")
            return cached
        raise ValueError("Cannot load a current NIFTY 500 list. Download " + URL +
                         " and start with --universe-csv <path-to-csv>. Cached lists older than 7 days "
                         "are not used automatically.") from exc
    cache_dir.mkdir(parents=True, exist_ok=True)
    temp = cache.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(cache)
    stamp.write_text(today.isoformat(), encoding="ascii")
    progress(f"NIFTY 500: downloaded {len(members)} constituents from NSE Indices on {today}.")
    return members
