"""Load operator settings from a `.env` file into the environment (stdlib only).

* Looked up in the current directory first, then the project root.
* Variables already set in the environment win; empty values are skipped, so a copied `.env.example` changes nothing.
* Only variable NAMES are ever reported. Values (tokens, passwords) are never printed or logged.
"""
from __future__ import annotations

import os
from pathlib import Path


def _candidates() -> list[Path]:
    return [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]


def _parse_line(raw: str) -> tuple[str, str] | None:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    key, sep, value = line.partition("=")
    key, value = key.strip(), value.strip()
    if not sep or not key.isidentifier():
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return key, value


def load_env_file(path: Path | None = None, *, override: bool = False) -> tuple[Path | None, list[str]]:
    """Returns (file used or None, names of the variables it set)."""
    for p in ([path] if path else _candidates()):
        if p is None or not p.is_file():
            continue
        names: list[str] = []
        for raw in p.read_text(encoding="utf-8-sig").splitlines():   # -sig: Windows editors may add a BOM
            kv = _parse_line(raw)
            if kv is None or not kv[1]:
                continue
            if override or kv[0] not in os.environ:
                os.environ[kv[0]] = kv[1]
                names.append(kv[0])
        return p, names
    return None, []
