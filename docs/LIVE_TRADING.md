# Live trading on Dhan: status and how it works

**Status (28 Sep 2026, Sthitaprajna 2.0, D-040).** The live broker (`src/sthitaprajna/execution/live.py`) is built. It
has been tested only against a fake Dhan order API (`tests/fake_dhan.py`, `tests/test_live_orders.py`) and has **never
placed an order on a real account**. `sthitaprajna serve --mode live` now runs -- the CLI's earlier unconditional
refusal is gone -- but it stays gated behind two things you set yourself in `.env`, both required:

1. `DHAN_CLIENT_ID` and `DHAN_TRADING_TOKEN` -- your real Dhan credentials.
2. `STHITAPRAJNA_LIVE_TRADING_CONFIRMED=yes` -- a deliberate second switch. Credentials alone never turn real orders
   on; you flip this only when you mean it, after reading this file and running `order-check`.

The first live run also defaults to **operator approval on every entry and stage-add** (D9's LIVE-PILOT behaviour),
regardless of which mode string you pass; turn it off from the terminal or `/status` once you trust it. Nothing about
this build's order logic itself has been exercised against the real Dhan API yet -- everything below (the broker, the
engine wiring, reconciliation, the UI, `order-check`) is in place and tested, but your first session is still the
first time any of it talks to Dhan for real.

Whatever the mode, the strategies are **unvalidated hypotheses**. SIM and PAPER results are not evidence of an edge. Trading
them at full size with real money can lose money quickly.

## What happens to each order

| Step | Stocks (cash intraday) | Options (long premium only) |
|---|---|---|
| Entry | `MARKET`, product `INTRADAY` | Marketable `LIMIT` at ask + 2% (at least 2 ticks): no market orders in thin option books |
| Unfilled entry | Cancelled after 15 s; a partial fill becomes the position | Same |
| Stop-loss at Dhan | `STOP_LOSS_MARKET`, resting beyond the system stop (max of 4 ticks and 0.15R) | `STOP_LOSS` (limit) with the limit 10% beyond the trigger (at least 3 ticks) [VERIFY: whether Dhan accepts SL-M for options] |
| Trailing / breakeven | The resting stop is modified at Dhan | Same |
| Exit (target, stop, end of day, kill) | Resting stop cancelled **first**, then `MARKET` | Resting stop cancelled first, then a marketable `LIMIT` at bid − 3% that is re-priced to −10% and then −25% if it does not fill |
| Fills | Read from Dhan's order book (polled in a background thread; average price from the order, else the trade book) | Same |

Quantities are sent in units (a multiple of the lot for options). Tick sizes come from Dhan's instrument master. Every
order carries a `correlationId` (letters and digits, ≤ 20 characters). If `DHAN_EXCHANGE_ALGO_ID` is set, it is also sent
as `algoId` [VERIFY: the field name and whether your account needs it].

## Safeguards

- **Nothing sells twice.** Before any exit, the resting stop is cancelled (or shrunk, for a partial exit) at Dhan, and the
  exit goes out only after Dhan confirms. If the cancel shows the stop already traded, no exit is sent and the stop's
  fill is recorded. Test: `test_stop_that_fills_at_dhan_while_the_engine_exits_never_sells_twice`.
- **Positions closed outside Sthitaprajna.** If you close a position in the Dhan app, or Dhan squares it off, two
  consecutive position snapshots confirm it. The trade is then recorded (at Dhan's day average price, marked approximate)
  and Sthitaprajna's own resting stop is cancelled so it can never sell again. Test:
  `test_a_position_closed_in_the_dhan_app_is_reconciled_and_its_stop_cancelled`.
- **Timeouts are not guesses.** An order request that times out is looked up by its correlationId before anything is
  retried. An order Dhan never shows is given up only after a direct lookup finds nothing.
- **Late fills.** An entry that fills after it was cancelled or given up is tracked and closed at once. An entry that fills
  while the kill switch is being thrown is closed at once.
- **Capital.** The capital given to a trade is capped by Dhan's available funds (`/fundlimit`) as well as by the
  configured capital.
- **Rejections.** Dhan's reason is shown in the event log. After a refused exit the system waits 3 s before trying again,
  while the resting stop keeps protecting the position. A burst of rejections pauses new entries (reject storm).
- **No invented fills.** At the close, a position still open in Sthitaprajna is reported, not faked. Dhan's positions
  decide what happened.
- **Unmanaged positions.** At start-up, open positions and working orders that this run did not create are listed and
  left alone. They may be your own trades.
- **Credentials.** Only `execution/live.py` reads `DHAN_TRADING_TOKEN`, and it never prints it. The data code reads
  `DHAN_DATA_TOKEN`. If Dhan gave you a single access token, put the same value in both.
- **Order rate.** At most 4 order writes per second, far below SEBI's 10 orders per second.

## Checking your account before any live run

`sthitaprajna order-check` runs the readiness report. By default it is read-only: it checks credentials, funds, the
order book, the trade book and positions, and prints the response field names marked [VERIFY]. With
`--place-test-order` it places **one real order**: a LIMIT BUY of 1 share priced 3% below the market (inside the
day's band, so it should not fill), and cancels it at once. If that order fills anyway, it sells the share straight
back. Run the read-only check first; only run `--place-test-order` once that passes.

## Not verified yet [VERIFY]

- Endpoint paths and field names: `/orders`, `/orders/{id}`, `/orders/external/{correlationId}`, `/trades`, `/positions`,
  `/fundlimit` (Dhan documents `availabelBalance` with that spelling; both spellings are read).
- The order-status words (`TRANSIT`, `PENDING`, `TRIGGERED`, `PART_TRADED`, `TRADED`, `CANCELLED`, `EXPIRED`, `REJECTED`).
- Whether Dhan accepts SL-M for options, and its rule for how far an SL-limit price may sit from the trigger.
- Static-IP registration and Algo-ID requirements for API orders under SEBI's retail-algo framework.
- Dhan's order rate limits, auto square-off times, and its policy for stock options near expiry (the strategy skips
  contracts within 3 days of expiry).
- Real charges (a Dhan contract note), and fill quality against the 2% / 3% limit cushions.

## What going live involves

1. Put `DHAN_CLIENT_ID` and `DHAN_TRADING_TOKEN` in `.env`.
2. Run `sthitaprajna order-check` (read-only), then `sthitaprajna order-check --place-test-order`.
3. Set `STHITAPRAJNA_LIVE_TRADING_CONFIRMED=yes` in `.env` -- this is the one deliberate step left for you.
4. `sthitaprajna serve --mode live`. Approval is required on every entry and stage-add by default; watch the
   first session live, with the Dhan app open, and use the KILL button (or `/kill` on Discord) if anything looks
   wrong.
