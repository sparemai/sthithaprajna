# PAPER on live Dhan data

`sthitaprajna serve` alone runs **SIM**, a synthetic market on your laptop. **PAPER** runs the same engine on
**real Dhan market data**, with **simulated fills** at the live bid/ask. No order ever reaches the exchange.
Every number is labelled **PAPER**.

| | SIM (default) | PAPER (`--mode paper`) |
|---|---|---|
| Prices, history, option chains | Synthetic, generated locally | Live from Dhan (read-only) |
| Clock | Accelerated (30×), ignores weekends | Real IST clock and exchange calendar |
| Fills | Simulated | Simulated at the live bid/ask, next tick |
| Needs | Nothing | `DHAN_CLIENT_ID` and `DHAN_DATA_TOKEN` in `.env`, plus Dhan Data API access |
| Weekend / holiday | Keeps trading synthetic days | Shows the last close, no trading, "next session" banner |

## Start it (Git Bash)

```bash
git pull origin claude/eloquent-bell-cjnp9h
pip install -e ".[dev,discord]"
sthitaprajna dhan-check              # read-only report: what works, what does not, how to fix it
sthitaprajna serve --mode paper      # add --discord for the Discord channel
```

The first start downloads Dhan's public instrument master to find security ids (cached for 7 days in
`var/cache/`). It then backfills 5 sessions of 1-minute history, about 15 s for 39 instruments. The console
shows each step.

## What happens during the day

* **Before 09:15 / weekends / holidays:** the terminal shows the last traded prices and a *Market closed, next
  session ...* banner. The engine idles: no scans, no entries, and the feeds are not arbitrated.
* **09:15 to 15:30:** quotes with 5-level depth are polled about once a second, within Dhan's limits (see
  `config/market_rules.yaml`, `dhan_*`, all [VERIFY]). Minute bars are built from those quotes, and strategies
  see completed bars only. Option chains are fetched on demand (at most one request per 3 s), and any contract
  the engine trades is added to the quote poll.
* **Paper fills:** market orders fill at the live ask (buy) or bid (sell) on the next quote. Broker-side stops
  trigger on the live price, and a gap fills at the worse price.
* **15:30:** intraday positions are closed and the journal entry is written (label PAPER).

## Only Dhan configured (no Upstox)

That is fine. The A5.5 arbitration row *"Dhan healthy, Upstox down"* applies: **new entries are allowed and data
confidence is capped at 0.8**, which lowers conviction scores. The System page shows Upstox as *not configured*.
If Dhan's data stops (for example an expired token) there is no second feed. New entries pause at once. After
5 minutes without data the engine sends flatten orders (A5.5 "both down"). In paper they fill at the first live
price once data returns, because a paper fill needs a real price.

## When something is wrong

The console, the terminal's event log and a red banner name the problem and the fix:

| Message | Fix |
|---|---|
| `Dhan data credentials missing` | Put `DHAN_CLIENT_ID` and `DHAN_DATA_TOKEN` in `.env` (in the folder you start from) |
| `... (AUTH) ... access token` | Token wrong or expired: generate a new access token in Dhan's web portal, update `.env`, restart |
| `... (NOT_SUBSCRIBED) ...` | Your Dhan account has no Data API access; enable it in Dhan |
| `/marketfeed/quote rejected segment IDX_I; using /marketfeed/ohlc` | Harmless: index quotes come without depth |
| `ETERNAL: not in Dhan's instrument master ... dropped` | Symbol renamed or delisted; the rest of the universe runs |

`sthitaprajna dhan-check` prints the same diagnosis without starting the engine. Its output contains no
secrets, so it is safe to share.

## Honest status

* **Verified against the real Dhan API.** Kubera (21 Sep 2026) confirmed the instrument-master columns, NIFTY
  index id 13 with segment `IDX_I` for option chains, integer ids in `/marketfeed` bodies, the
  `/charts/intraday` request and response, and the option-chain response shape. The operator's first
  `dhan-check` (27 Sep 2026) confirmed more:
  * credentials, and the master download (197,057 rows, all 40 instruments resolved);
  * index ids NIFTY 13, BANKNIFTY 25, SENSEX 51 and INDIA VIX 21, and lots 65 / 30 / 20, all from the master;
  * `IDX_I` accepted inside `/marketfeed/quote`;
  * the quote response's field names: `last_price`, `ohlc`, `depth`, `last_trade_time`, `net_change`,
    `volume`, `oi` and others.
* **Found by that run:** a 1.05 s send-to-send gap between quote calls drew HTTP 429. Spacing is now measured
  from the previous response (1.25 s), and rate-limit answers are retried with backoff.
* **Not yet verified** (the next `dhan-check` prints each): the depth level fields, whether `ohlc.close` is the
  previous close, candle alignment, put-delta sign, the DH-9xx error table, and the master's tick-size unit.
* **Not built yet:** carrying paper equity and open positions across restarts. Each start begins at ₹1,00,000.
  The history stays in `var/sthitaprajna-paper.sqlite`. Also not built: Upstox as a second live feed, Dhan's
  WebSocket feed (REST polling is used), and automatic token renewal.
* The strategies are still **unvalidated hypotheses**. Paper results are evidence about execution and process,
  not about edge, until Phase 4 validation passes.
