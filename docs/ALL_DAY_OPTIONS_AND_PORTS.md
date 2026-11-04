# All-day option setups and configurable ports

The five general strategies (index momentum, stock momentum, index range reversion,
day-range breakout and scalp momentum) evaluate their setups on every trading day,
within their existing session windows. A valid setup is still required: this change
does not force a trade or establish profitability.

## Contract selection

- General index strategies select the earliest usable **future-dated** expiry.
  They do not buy same-day-expiring options. This removes the old expiry-afternoon
  veto: a normal setup can use the next expiry instead.
- Stock strategies keep their existing buffer of more than three calendar days
  before expiry because of physical settlement.
- Expiries supplied by the data provider are sorted and deduplicated. If an expiry
  has no chain, no strike passing the strategy's spread filter, or no usable quote,
  selection tries the next available expiry. There is one selection per direction,
  not a separate trade for every expiry. Live pending chains are retried on later scans.
- Zero-to-Hero remains the separate same-day-expiry strategy. Its gamma/expiry
  regime model does not justify applying its probabilities on ordinary days.
- Existing signal conditions, trading windows, sizing, stops, EV checks and risk
  settings still apply. This is availability/selection logic, not a new calibrated edge.

## Run the packaged terminal on any available port

From the project root, install Python 3.11+ dependencies:

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
sthitaprajna serve --mode sim --port 8790 --db var/sim-8790.sqlite
```

The Python server serves both the API and the included compiled UI on that port.
No frontend server is needed for the packaged terminal. In another terminal, a
second independent simulation can use:

```sh
sthitaprajna serve --mode sim --port 8791 --db var/sim-8791.sqlite
```

Use a different port **and database file** per instance. Operator configuration
remains shared when instances use the same checkout; use separate project copies
if you need independent configuration. These examples start SIM only, not live orders.

## Frontend development with a custom backend port

Create `web/.env.local` (or set the same environment variables in your shell):

```dotenv
STHITAPRAJNA_WEB_PORT=5180
STHITAPRAJNA_BACKEND_PORT=8790
```

Then:

```sh
cd web
npm ci
npm run dev
```

This starts Vite on 5180 and proxies both `/api` HTTP and `/ws` WebSocket traffic
to the backend on 8790. Start the backend separately using `--port 8790`; the Vite
setting does not change the Python server's port. Defaults remain 5173 and 8788.
Restart Vite after editing its environment. Ports must be distinct integers from
1 to 65535. Vite fails if its port is occupied instead of silently changing it.
For another frontend/backend pair, use 5181/8791 in a separate project copy or shell.

## Validation

```sh
python -m pytest
cd web
npm run build
npm test
```

Regression coverage includes all five general strategies on Monday through Friday,
including expiry afternoons; sorting, expired-contract exclusion and fallback for
missing chains, wide spreads and absent quotes; and HTTP/WebSocket port configuration.

Verified in this update: full Python run 300 passed / 2 skipped; dedicated new
regressions 37 passed (including one late-day test added after full-suite collection);
frontend 14 passed and build passed; two custom-port SIM instances plus the HTTP
and WebSocket development proxy passed. This verifies behavior, not trading returns.
