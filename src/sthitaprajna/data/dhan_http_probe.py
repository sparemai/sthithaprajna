"""Read-only comparison of Dhan quote request formats; never calls order APIs."""
from __future__ import annotations

import html
import json
import os
import re
import time

import httpx

from sthitaprajna.envfile import load_env_file

URL = "https://api.dhan.co/v2/marketfeed/quote"


def redact(text: str, client_id: str, token: str) -> str:
    text = html.unescape(text)
    for secret in sorted((x for x in (client_id, token) if x), key=len, reverse=True):
        text = text.replace(secret, "[hidden]")
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[hidden JWT]", text)
    text = re.sub(r"<[^>]*>", " ", text)
    return " ".join(text.split())[:350]


def report(label: str, status: int, headers, body: bytes, client_id: str, token: str) -> None:
    print(f"{label}: HTTP {status}; bytes={len(body)}")
    print("  content-type:", redact(headers.get("content-type", "missing"), client_id, token))
    print("  server:", redact(headers.get("server", "missing"), client_id, token))
    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except ValueError:
        print("  response:", redact(text, client_id, token) or "[empty body]")
        return
    if 200 <= status < 300 and isinstance(data, dict) and str(data.get("status", "")).lower() not in ("failure", "failed", "error"):
        rows = data.get("data")
        got_quote = isinstance(rows, dict) and isinstance(rows.get("NSE_EQ"), dict) and "2885" in rows["NSE_EQ"]
        print("  JSON response; requested quote returned:", got_quote)
    else:
        print("  response:", redact(text, client_id, token))


def probe_httpx(client: httpx.Client, client_id: str, token: str) -> None:
    variants = (
        ("A: current JSON format", {"json": {"NSE_EQ": [2885]}}),
        ("B: spaced JSON", {"content": json.dumps({"NSE_EQ": [2885]})}),
        ("C: official SDK payload", {"content": json.dumps({"NSE_EQ": [2885], "dhanClientId": client_id})}),
    )
    for label, kwargs in variants:
        try:
            response = client.post(URL, **kwargs)
            report(label, response.status_code, response.headers, response.content, client_id, token)
            if response.status_code == 429:
                print("Rate limit reached; stopping comparisons.")
                return
        except (httpx.HTTPError, ValueError) as exc:
            print(label + ":", type(exc).__name__, "(exception details omitted)")
        time.sleep(1.5)


def main() -> int:
    load_env_file()
    client_id = os.environ.get("DHAN_CLIENT_ID", "").strip()
    token = os.environ.get("DHAN_DATA_TOKEN", "").strip()
    if not client_id or not token:
        print("Missing DHAN_CLIENT_ID or DHAN_DATA_TOKEN in .env")
        return 1
    print("Read-only Dhan HTTP diagnostic; tokens/client ID are hidden.")
    print("Token character count:", len(token), "; contains whitespace:", any(c.isspace() for c in token))
    print("Client ID is numeric:", client_id.isdigit())
    print("Configured API URL matches official:", os.environ.get("STHITAPRAJNA_DHAN_BASE_URL", "https://api.dhan.co/v2").rstrip("/") == "https://api.dhan.co/v2")
    print("Proxy variables present:", ", ".join(k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy") if os.environ.get(k)) or "none")
    headers = {"access-token": token, "client-id": client_id, "Content-Type": "application/json", "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=20, follow_redirects=False) as client:
        probe_httpx(client, client_id, token)
    print("Diagnostic complete. No application settings or credentials were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
