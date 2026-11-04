# Sthitaprajna Intraday 500

Separate **NIFTY 500 cash-equity intraday** version. Long and short opening-range breakout and
VWAP pullback strategies run on normal trading days, without an expiry requirement.

**[Installation, PowerShell commands and behaviour → INTRADAY500.md](INTRADAY500.md)**

**[Download ZIP](https://github.com/sparemai/sthithaprajna/archive/refs/heads/intraday-500.zip)**

- Start with `sthitaprajna-intraday --mode paper --port 8792` after installing with `pip install -e .`.
- PAPER uses live Dhan data and simulated fills. This cash launcher supports SIM and PAPER only.
- Official NIFTY 500 membership is downloaded and validated; quotes cover all resolved members.
  Up to 20 qualifying stocks receive detailed strategy evaluation at a time.
- Entries 09:35–14:15 IST; intraday exits requested at the configured 15:10 IST cutoff.
- Separate folder, database and cache from the [options app on main](https://github.com/sparemai/sthithaprajna/tree/main).
- The existing configuration is preserved: unbounded risk mode, 40% capital per new trade, 1x cash leverage.
- Strategies are unvalidated hypotheses. No profitability or historical backtest result is claimed.

The repository retains the shared options engine for compatibility. The original `sthitaprajna serve`
command still launches that engine. **Use the new `sthitaprajna-intraday` command for this version.**

See [INTRADAY500.md](INTRADAY500.md) for CSV download recovery, independent ports, credentials,
limitations and tests. Prebuilt web assets are included; Windows users do not need Node or Git.
