# Sthitaprajna Intraday 500

Separate cash-equity version on branch `intraday-500`. The options version remains on `main`.
Use **`sthitaprajna-intraday`**, not the original `sthitaprajna serve` command.

## Install in PowerShell

Run this from your existing options application's folder. Keep that application in its own window.
The installer creates a separate `Sthitaprajna-Intraday500` folder under your Windows user folder,
copies `.env` locally if found, installs Python dependencies, and starts PAPER on port **8792**.
It never prints or uploads your credentials. Python 3.11+ is required; Node and Git are not required.

```powershell
$setup = Join-Path $env:TEMP "install-intraday500.ps1"
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/sparemai/sthithaprajna/intraday-500/scripts/install-intraday.ps1" -OutFile $setup
powershell -NoProfile -ExecutionPolicy Bypass -File $setup
```

Open **http://127.0.0.1:8792** once startup completes. Ctrl+C stops it.
For a direct ZIP download: https://github.com/sparemai/sthithaprajna/archive/refs/heads/intraday-500.zip

## Start again / use another port

```powershell
Set-Location "$env:USERPROFILE\Sthitaprajna-Intraday500"
.\.venv\Scripts\sthitaprajna-intraday.exe --mode paper --port 8792
```

Changing `--port 8793` automatically selects a separate database and cache for that port.
Defaults: `var/intraday500-paper-8792.sqlite` and `var/intraday500-8792-cache`.
Overrides: `--db <file>` and `--cache-dir <directory>`; do not share these between running instances.
Multiple instances using the same Dhan account share the provider's rate limits; separate ports do not
increase those limits. If you get rate-limit responses, run fewer instances.

`.env` in this new folder holds `DHAN_CLIENT_ID` and `DHAN_DATA_TOKEN`. Your original options folder is
not edited. To edit this copy: `notepad .env`. Restart after saving. A paid/active Dhan data entitlement
may be required for the data APIs. This release accepts only `--mode paper` or `--mode sim`.

## Universe and data

The app downloads the official NIFTY 500 constituent CSV from
https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv and validates schema, unique symbols,
EQ series, ISIN shape and a constituent count between 490 and 510 (corporate actions can temporarily
change the number of securities). Startup prints the actual count. Membership is fixed for that run;
restart each trading day to refresh it. It does not rank the entire exchange by market capitalisation.

A validated list is cached and refreshed daily. If downloading fails, a cache at most seven days old
may be used with a warning. If no usable cache exists, download the official CSV and run:

```powershell
Invoke-WebRequest -UseBasicParsing -Uri "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv" -OutFile .\nifty500.csv
.\.venv\Scripts\sthitaprajna-intraday.exe --mode paper --universe-csv .\nifty500.csv
```

A supplied CSV's membership date is the operator's responsibility. If NSE blocks downloads in
PowerShell, use its website in a browser to save the same CSV. No invented stock list is used as fallback.
Every member must resolve to an EQ-series security in Dhan's instrument master; startup lists missing
symbols and stops if it cannot resolve them. To refresh that mapping, stop the app and delete only
`var/intraday500-8792-cache/dhan-universe.json` and any `dhan-master-*.csv` in that same cache folder.

Quotes rotate over all constituents in batches. The existing scanner ranks movement, relative volume,
breakouts, VWAP, momentum and order-book imbalance, and filters liquidity, spreads, circuits and reported
surveillance restrictions. Up to 20 qualifying names are promoted for full minute-history evaluation.
Other constituents remain in the scanner. Promotion does not guarantee a trade. First-run daily
baselines for 500 stocks load in the background and may take several minutes. Promoted stocks cannot
signal until sufficient history and fresh quotes are available.

## Trading behaviour

- Cash shares, long or short, lot size one. No options, expiry gate, option-chain requests or Zero-to-Hero.
- Runs on normal exchange trading days. No new signals on weekends, configured holidays or closed sessions.
- Opening-range breakout: completed minute close crosses the first 15-minute range, with aligned EMA/
  higher-timeframe trend, VWAP, ADX and relative volume.
- VWAP pullback: a recent bar tests VWAP, then a completed bar recovers in the aligned trend direction,
  with volume confirmation. Both strategies reject stale quotes, stale bars and excessive entry movement.
- New signals: **09:35–14:15 IST**. Stops use recent structure and ATR; initial target is **2R**.
  The existing cost, capital and execution checks still decide whether a signal becomes a position.
- Positions use the existing stop, target and trailing management. Intraday flattening is requested at
  **15:10 IST** by default (`system_intraday_exit_time` in `config/market_rules.yaml`). Keep the app running;
  PAPER fills and exits require usable quotes. Stopping the process does not execute an exit, and open
  PAPER positions are not restored on restart.

This branch preserves the repository's existing `config/operator.yaml`: **unbounded** mode, **40% capital per new
trade**, and **1x cash leverage** by default. Unbounded mode skips the bounded profile's loss limits,
risk ceilings, staging and approvals. It still uses stops, targets and available-capital checks.
These settings are visible in the terminal and editable in the new folder independently of the options app.

PAPER means **live market data, simulated fills, no orders sent to Dhan**. The strategies are unvalidated
hypotheses; profitability has not been established. SIM invents prices for the selected constituent
symbols and is for software testing, not historical performance evidence. Historical backtests and
real-order execution are not included in this initial cash version. Options research screens are hidden.

## Development checks

```bash
python -m pip install -e '.[dev]'
pytest tests/test_intraday500.py
cd web
npm ci
npm test
npm run build
```

Tests use a fake Dhan API and synthetic constituent fixtures: no real account or exchange orders.
The release environment could not access the NSE host, so the live NSE download and your Dhan account
must be verified at startup on your machine. No claim of live market validation is made.
