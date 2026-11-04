# Progress

_Last updated: 2026-10-22. Read this with `CLAUDE.md` at the start of every session. Type
"continue" to resume from "Next"._

**2026-10-22 (D-050):** operator shared a real paper-trading screenshot — equity Rs 1,00,000 → Rs 1,90,485 over
~2 weeks (9 visible trades, 44% win rate) — plus a Dhan `DH-907` log line, the `sthitaprajna rules` audit, and
the "Errors: 20" dashboard stat, and asked to fix what's fixable and "go more aggressive." Diagnosed both error
signals (benign scanner baseline-fetch gap; a cumulative, log-throttled error counter, not 20 distinct
failures) and resolved two of three UNKNOWN freeze-quantity rules with sourced data; found but deliberately did
not fix the freeze-qty slicing gap above. Did not change any risk parameter in response to "more aggressive" —
n=9 trades is far below the system's own `edge_brake.start_after_trades: 50`, and raising risk specifically
after a hot streak is exactly what the Aggression Layer's drawdown-only gear logic (not performance) is designed
to resist. Operator was told this directly; no config change made without their explicit follow-up.

**D-051 (same day, follow-up):** operator came back wanting bigger `capital_per_trade` AND more concurrent
positions AND more small-move capture, believing "more capital = more profit." Pointed out the arithmetic: no
leverage on long option buying means bigger-per-trade and more-concurrent directly trade off against each
other on the same capital pool, and bigger size scales losses exactly as much as it scales wins. Asked the
operator to pick a point on that tradeoff via two direct multiple-choice questions rather than guessing.
Operator picked capital_per_trade 0.40 (~2 concurrent positions, up from 0.25/~4) and chose to loosen
ScalpMomentum's trigger thresholds (rvol 1.1->1.0, acceleration 1.2->1.1, VWAP-extension 1.0x->1.3x ATR1) so
it fires more often. Left the strike-spread and cost-to-edge filters untouched on purpose — loosening those
would undercut the cost-math warning given alongside this change. 5 tests updated for the new 40% figure;
full suite green after.

**2026-10-25 (D-052):** operator pasted a real 22 Oct trade log — 3 positions ran up to a combined ~Rs 30,000
unrealized profit, then the market reversed and all three closed at their stop (-0.95R to -1.11R each) — and
asked what happened and how to capture that profit instead. Root cause: `unbounded` mode's exit logic
(`_manage_positions`) only ever checked the target and a single `trail_to_breakeven_at_r` (1.0) threshold; it
never called the `trailing_stop()` function that gives bounded mode its graduated breakeven/early-lock/press-
lock tiers (D-042/D-045). Since D-049 switched the account to `unbounded`, every one of those earlier fixes
had been silently inactive for real paper trades — a position below +1R had zero profit protection, exactly
reproducing what the operator saw. Fixed by calling `trailing_stop()` from the unbounded branch too, reusing
the same `cfg.staging` tiers without adding any probe/add/press/harvest machinery. Two tests added/updated;
full suite green. Separately flagged, not built without a go-ahead: using the already-built chain OI/IV and
candle-pattern detectors (currently entry-only) to tighten a stop *while a position is open*, ahead of a pure
R-multiple trigger — a bigger, net-new feature than this fix.

**Same day, 25 Oct 2026:** operator asked me to turn Rs 1,00,000 into Rs 50,00,000 (50x) in 4 months (26 Oct
2026 – 26 Feb 2027) by building an autonomous system of my own design (proposed repo: `mission_possible`) that
makes its own trading decisions toward that fixed deadline target. See the reply in this session's transcript
for the full reasoning: declined to frame this as a commitment or promise, laid out the actual compounding
math (50x in 4 months needs ~+166% every single month with zero down months, or compounding that fast on a
trade-by-trade basis — not something achievable by a risk-managed strategy; pursuing it as a fixed goal would
require position sizing that makes total capital loss the most likely outcome, not a dependable path to Rs
50L). Offered constructive alternatives instead (better retrospection/planning tooling, a realistic stretch
target with honest odds) rather than either building an unbounded autonomous real-money system or pretending
confidence that isn't warranted.

**Same day, follow-up (D-053/D-054):** operator took the two constructive alternatives from above ("go for
it"). **D-053** built the reversal-detection exit feature flagged as not-yet-built in D-052: `aggression/
staging.py:reversal_lock()` tightens a position's stop immediately when a candle pattern reversing against
its view AND an opposing option-chain OI shift both agree (not either alone), instead of waiting for the next
passive R-multiple tier; wired into `_manage_positions` for both bounded and unbounded positions, toggleable
via new `staging.reversal_exit_enabled`/`reversal_lock_fraction` config keys, 6 new tests. **D-054** built the
planning/retrospection infrastructure: a new `period_review` store table + `Engine._close_period_review()`
hooked into the existing week/month-boundary detection in `_start_of_day()` (plan vs actual, persisted, not
just live); `expiry_calendar()` and `zero_to_hero_report()` (every sleeve capture ever closed, grouped by
expiry, from the store's full history); three new endpoints `/api/review`, `/api/calendar`, `/api/trades`. The
store already kept every event/decision/position/equity point indefinitely, so "keep all the data" needed no
new work, just a way to read it back. 7 new tests (`test_period_review.py` + 1 in `test_api.py`). No new web UI
page yet — backend/API only. Full suite: 212 passing.

**2026-10-26 (D-055):** operator asked for the two remaining follow-ups: a web UI page for D-054, and wiring
D-039's IV rank/percentile/pin-risk analytics into strategy entries (computed since the chain-analytics work,
never read by any strategy's `scan()` until now). Built `web/src/views/Plan.tsx` (current/completed period
review, expiry calendar, Zero-to-Hero captures, full trade history), routed as `plan`; verified with
`npm run build`, `npm test`, both Playwright e2e suites against a running SIM server (27 checks, zero console
errors), and a screenshot. Wired `market/options.py:iv_rank`/`pin_risk` into new `_iv_confirmation()`/
`_pin_risk_confirmation()` Confirmations, appended to all five directional strategies (not `ZeroToHero`,
whose own EV-by-delta-band strike selection specifically wants IV *expansion* — a generic "avoid rich IV"
confirmation would work against it, not for it). Both follow `_reversal_confirmation`'s existing convention:
no evidence (no IV history yet, a stock underlying, far from expiry) means the confirmation does not fire,
never a vacuous pass. New `StrategyContext.iv_history()`. 7 new tests. Full suite: 226 passing.

**Same day, follow-up (D-056):** operator pasted their real closed-trade history and asked why only
`index_option_momentum`/`day_range_breakout` ever appear and why `zero_to_hero` has never fired. Traced
D-046's unbounded correlation cap on a hunch that it picks whichever strategy scans first — wrong, verified:
`PickupFunnel.evaluate()` already returns candidates sorted by `utility` (EV × grade × confidence ×
liquidity) descending, so the best-ranked candidate wins, not an arbitrary first one; reported as a dead end,
not shipped as a guessed fix. Real reasons: `index_range_reversion` needs `RANGE` regime, mutually exclusive
with the trending regime most of this history's winners depended on; `zero_to_hero` only fires 13:00-14:45
IST (not the operator's believed 13:30-15:20) on a NIFTY-Tue/SENSEX-Thu expiry *and* only in 3 of 6 possible
expiry-regime classifications — which one actually occurred on real expiry afternoons was never logged, so
zero trades there was indistinguishable from a bug. Fixed: `_update_regime()` now logs every `regime.expiry`
transition. Also found PCR was computed and displayed but never read by any strategy, same gap as IV
rank/pin-risk before D-055 — added `_pcr_confirmation()` to the same five strategies, flagging to the
operator (not silently correcting) that their own pasted PCR-interpretation table reads internally
inconsistently, and implementing the standard contrarian reading instead. Declined the much larger ask in
the same message — a ~25-topic TA/quant curriculum (Elliott Wave, harmonic patterns, Kalman filters, ML,
HFT, etc.) and "become the world's smartest trader" — with reasoning given directly: most of it lacks
validated edge, and piling on unvalidated indicators is the overfitting risk the operator's own curriculum
names as its own topic. 6 new tests. Full suite: 232 passing.

**Same day, follow-up (D-057):** operator asked to pick the strongest items from the 25-topic curriculum,
build those, skip the rest. Built implied-vs-realized volatility spread (`iv_rv_spread`, already computed
for the Chain view, never read by a strategy) as `_iv_rv_confirmation()` on the same five strategies —
genuinely different from D-055's IV rank (own-history richness vs. richness-against-actual-recent-moves).
Investigated volatility skew (`risk_reversal_25d`, also already computed, also unread) and *deliberately
did not* wire it in: unlike IV rank/PCR, there's no single confidently-grounded directional reading of a
skew extreme without real historical calibration — guessing one would be fabricating confidence, not
adding it. Ranked and declined the rest with reasons (infrastructure/data this system doesn't have, directly
conflicts with A5.3's no-LLM-in-the-live-order-path rule, too subjective to encode without curve-fitting, or
already the system's standing discipline rather than a new feature). Flagged good next candidates without
building them this round: MACD/RSI/OBV divergence (would extend D-053's exit logic), Volume Profile,
Ichimoku, deeper multi-timeframe context, Fibonacci zones. Declined the "world's smartest trader" framing
directly — the mechanism that makes this work good is verify-before-build, not a persona. On the
paper-trading-only reframe of the 50L/Feb-2027 goal: paper trading on live Dhan quotes behaves size-for-size
like real trading, so "hitting" the number through oversized positions would only prove oversized positions
produce oversized numbers, not real edge, and wouldn't transfer to real capital — declined to tune toward
it; committed to continuing real, tested improvements and letting the Plan page show the honest trajectory.
4 new tests. Full suite: 236 passing.

**2026-10-27 (D-058):** operator pasted a real expiry-day log -- 19 `expiry regime ->` transitions, and a
NIFTY 22750 CE trade with 7 "profit-lock stop tightened (reversal evidence)" lines firing in 14 seconds,
several at the identical stop value and timestamp -- and asked for the regime messages explained, the exit
checked for correctness, and a visual way to see why trades (including Zero-to-Hero) did or didn't fire.
Root cause of the duplicate tightenings: `Position.stop_is_better()` compared an unrounded candidate stop
against the already-2-decimal-rounded `p.stop`, so a candidate that rounded to the exact same displayed
value still looked like an improvement every tick the underlying evidence held -- fixed by rounding before
the comparison; a new test freezes price for several ticks and asserts exactly one tightening, not one per
tick. Also found and fixed two adjacent issues the same trade exposed: `reversal_lock()` armed on any
positive profit at all (added a 0.3R minimum, flagged as a starting point); `_sync_broker_stop()` rounded
the broker-side trigger to 2 decimals instead of the instrument's real tick size (switched to the existing
`round_tick()` helper, same direction `_type_price()` already uses). Added `ZeroToHero.scan()` diagnostic
logging so an eligible-regime scan that still finds nothing says exactly why, extending D-056's blind-spot
fix. Built the visualization asked for: every stop-tightening now records a structured point in a new
`Position.stop_history` field (kept separate from `stage_history`, whose entries are all stage transitions
read that way elsewhere -- an early version reused `stage_history` directly and broke that contract, caught
by the full suite before anything shipped). `stop_history` is already persisted, so it flows through
`/api/trades` for free; a new
`regime_history` timeline behind `/api/regime_history`; `CandleChart` gained a stop-over-time step line and
trade entry/exit markers; a new `RegimeStrip` component shows the session's regime as a colored timeline.
Wired into `Terminal.tsx`. Verified with the full backend suite, `npm run build`, `npm test`, both
Playwright e2e suites (zero console errors), and screenshots of both the default index view and a real open
position. 2 new backend tests.

**2026-10-29 (D-059):** operator used the new D-058 regime strip itself to flag a real miss -- a ~2000-point
SENSEX trend-expiry rally the Zero-to-Hero sleeve never traded -- and pasted the scan logs, asking to
validate the exact reason. Confirmed the Rs 3,156 sleeve budget matched 1.5% of equity exactly (working as
configured), but the D-058 diagnostic's generic catch-all couldn't say which of the three delta bands
failed or why, so the miss couldn't actually be validated against real numbers. Extracted `_z2h_band_scan()`
(same pricing/affordability logic `_best_z2h_pick` already had, now shared) so each band's exact failure
reason -- no liquid strike, no quote, or unaffordable with the real strike/ask/cost numbers -- shows up in
the log. Declined to raise the sleeve budget over one hindsight-profitable miss, citing the operator's own
Queue page principle (controls aren't weakened because a missed trade later worked, A13). 2 new tests.
Full suite still green.

**Same day, follow-up (D-060):** operator noticed `IndexRangeReversion` and `ScalpMomentum` had never closed
a trade in a month and asked to validate why, then explicitly chose to loosen both. `IndexRangeReversion`
was healthy but regime-starved (needs RANGE + calm vol, a mostly-trending month gave it little to do).
`ScalpMomentum` was a real bug: its own p_win ceiling (0.52) sat below the ~52.6% breakeven its 1:1
target_r needs under the EV engine's loss model -- verified directly against `ev.evaluate()`, gross edge
was negative at every p_win it could produce, so Grade C ("no trade", a hard unconditional gate) was
guaranteed regardless of real signal quality. Given the honest tradeoff (raise target_r vs raise the
confidence ceiling, neither backed by calibration data), the operator chose the ceiling: p_win formula
raised to 0.46-0.60 (v0.3.0), verified a realistic well-confirmed case now actually clears the gate, not
just a number that looks better on paper. Flagged in the card as an operator decision, not new evidence --
watch this strategy's own expectancy closely. `IndexRangeReversion` loosened on request (v0.2.0): the
top-level gate now reads the underlying ADX (`trend_strength < 25`) instead of the binary RANGE label,
volatility now excludes only EXTREME_VOL (matching what the card's fails_when already said), and the
per-underlying ADX/range-width/near-extreme filters were each widened. Neither change is validated edge;
both strategies' kill criteria are what will actually tell. 5 new tests.

**Same day, follow-up (D-061):** operator asked, as a general risk-appetite instruction (not off one
trade's hindsight -- that ask was declined in D-059), to raise the Zero-to-Hero sleeve budget. Doubled
across the board (`per_expiry` 1.5%->3%, `per_week` 3%->6%, `per_month` 7%->14%, same ratios preserved);
`kill_cumulative_loss` left at 10% unchanged as the sleeve's own hard stop. No config validation bound
existed on these keys, confirmed by reading `config.validate_operator()`. Tests read the config
dynamically, so none needed changes; full suite re-verified.

**Same day, follow-up (D-062):** operator asked for chart-level technical analysis -- resistance, support,
reversal patterns, Fibonacci -- on index and F&O-stock charts. Built it scoped as display-only, reusing
existing grounded computations rather than inventing new ones: candlestick patterns (`market/patterns.py`,
already used as strategy Confirmations) now drawn where they fire; support/resistance from each
underlying's own OI walls (already on the Chain page), only where options actually exist; Fibonacci
retracement anchored on the session's own day high/low (the same reference `DayRangeBreakout` already
trades off), the one new computation, objective because the anchor is objective. None of it feeds any
strategy's entry or EV logic. New `/api/chart_overlays` endpoint; `CandleChart` gained resistance/support/
fib price lines and pattern markers (reusing D-058's existing mechanisms); wired into `Terminal.tsx` behind
an "Analysis" toggle. Verified with the full suite, build/test, both e2e suites, and screenshots on both an
index and a stock. 1 new test.

**This is Sthitaprajna 2.0**, a fork of the v1 progress recorded below, narrowed to pure index + stock options
(D-036) with a retuned aggressive-but-bounded risk config (D-037), deepened chain analytics and a hardened
Zero-to-Hero sleeve (D-039), and a reachable (gated) LIVE mode (D-040). Full detail: `docs/decisions.md` D-036
onward. Everything from "Status by phase" down to "Findings the operator should read" is v1's own history, kept
for context; the "2.0 fork" sections below it are current.

**Risk mode: BOUNDED (2.0 default, D-037), aggressive-tuned.** 5.0% planned risk per trade, 10.0% open-risk ceiling,
gears ATTACK→PRESS→GUARD→PRESERVE→HALTED, a Zero-to-Hero sleeve with its own budget and cumulative-loss kill
switch. v1's `risk_mode: unbounded` (D-031: no ceilings, 25% of equity per trade) is still one YAML line away if
you want it back — see D-037 for the reasoning either way.

## Status by phase (B0) -- v1 history, mostly unchanged by the fork except where noted

## Status by phase (B0)

| Phase | Status | Evidence | Blocked on |
|---|---|---|---|
| **0: Audit, rules, data foundation** | **Mostly done** | `market_rules.yaml` (48 rules: 22 verified with source and date, the rest flagged VERIFY/UNKNOWN), loaders, calendar, ported Dhan/Upstox adapters, feasibility table (`sthitaprajna feasibility`, live premiums in PAPER), `sthitaprajna dhan-check` | Operator's first `dhan-check` output (data inventory, [VERIFY] facts); 5-session feed benchmark; Upstox credentials |
| **1: Backtest engine** | **Partial** | Same-engine backtester (staged entries, adds, partials, trailing, gap-through fills, full charges); no-look-ahead bar handling tested | Historical Dhan/Upstox data for "replay a known day"; a real Dhan contract note to match charges |
| **2: Walking skeleton + aggression plumbing** | **Done on SIM** | Full A5.2 loop; probe → confirm-add → harvest → exit with approvals and reconciliation (`test_full_staged_trade...`); property tests for every exit criterion; kill switch end to end | Discord bot needs a token (gate a); the code and tests exist |
| **3: Second feed + intelligence** | **Partial** | Feed arbitration (all 5 A5.5 rows tested), failure injection (primary / both / order API / gap), data-quality guardian, regime/vol/expiry/change-point engines, chain analytics | Real Upstox adapter live test; latency benchmark; breadth / large-participant / episode-memory engines are thin or missing |
| **4: Alpha Factory v1** | **Not started (by design)** | Three hypothesis cards (2.0 is options-only, D-036), all UNVALIDATED, staged-vs-single comparison tooling. Zero-to-Hero's strike selection now compares EV across three delta bands (D-039) but the p_win priors behind that comparison are still hand-set, not calibrated | Needs real history: walk-forward, red team, multiple-testing corrections |
| **5: Signal combination + Aggression Layer** | **Machinery done, calibration pending** | EV engine, weakest-link confidence, grading, calibration tracker, full Aggression Layer, Target-to-Plan, D12.1 re-run tooling | Out-of-sample data to calibrate grades and TQS weights |
| 6: Paper (6 weeks) | **Can start (plumbing)** | `serve --mode paper`: live Dhan quotes/history/chains, simulated fills at live bid/ask, real clock, single-feed without Upstox; scanner over every NSE stock; stock options | Persist equity and open positions across restarts (O4); first real-API session (O13); Phase 4 survivors for evidence of edge |
| 7–8: Live | **Broker built and reachable, never run for real (D-040)** | `execution/live.py` live Dhan broker (stop-first exits, reconciliation, late-fill handling), engine wiring, `build_live_engine`, `order-check` now a real CLI command; tested against a fake order API (`docs/LIVE_TRADING.md`). `serve --mode live` now runs when gated conditions are met (real credentials + `STHITAPRAJNA_LIVE_TRADING_CONFIRMED=yes`), instead of being hardcoded off | **Operator**: put real credentials in `.env`, run `order-check` (read-only, then `--place-test-order`), set the confirm flag, then a watched first session (O15) |

## Done this session
- Architecture decided with the operator (D-001..003); fresh `sthitaprajna` package; Kubera broker knowledge ported.
- Core: gears, edge brake, tiers, sizer, staging, Risk Guardian, targets/ratchet, funnel, decision objects.
- SIM market with dual feeds and failure injection; paper broker as exchange emulation; SQLite store.
- Research: Monte Carlo reproducing D12.1 exactly; Target-to-Plan; metrics; backtest; feasibility.
- API + WebSocket, operator command whitelist, Discord bot module, CLI.
- Web terminal (Tesla-light default, dark, mobile) with 10 views; palette validated for colour-blind separation.
- Tests: 110 pytest (property + e2e + Discord/.env + live Dhan data against a fake API), 5 UI unit tests, 20 + 3 Playwright E2E checks.
- Part C red team run against the build: 10 findings fixed (incl. 6 High), 12 open and tracked in `docs/red_team.md`.
- **PAPER on live Dhan data** (D-025..D-029): `data/dhan_live.py` (client, master, rate limits, errors that name the fix),
  `data/live.py` (LiveMarket on the real clock, background poller, minute bars from quotes, 5-session backfill, option
  chains on demand, betas from data), engine accepts a live market (Upstox optional, A5.5 single-feed), `serve --mode paper`,
  `dhan-check`, PAPER banners and live-data panel in the terminal. Tested against a fake Dhan API (`tests/fake_dhan.py`) in
  pytest and end to end in the browser (closed Sunday and an open session). Guide: `docs/PAPER_LIVE_DATA.md`.
- Discord hardening (F11, D-023/D-024): `.env` loaded automatically; bot forwarding by event sequence with batching;
  heartbeat tied to the gateway connection; start failures explained with their fix. Operator guide: `docs/DISCORD_SETUP.md`.
  Tested against a stand-in client and the real discord.py classes; **not yet tested against Discord itself** (needs the operator's token).

## Done on 2026-09-28 (operator: "screen every stock", "take any stock with options", "no restrictions or stages ... take the orders")
- **Unbounded risk mode** (D-031): capital sizing, one full entry with stop-loss and target, breakeven at +1R, only the
  mandatory checks can stop an entry.
- **Scanner over every NSE stock** (D-032): the whole master is rotated through the quote poll; booming score with reasons;
  tradability filters; up to 20 promoted into the strategy universe; baselines cached and rolled at the close.
- **Stock options** (D-033): `StockOptionMomentum` on promoted F&O stocks; Dhan stock chains (UnderlyingSeg NSE_EQ), master
  lots, the next month within 3 days of expiry, fractional strikes.
- **Live Dhan broker** (D-034): real orders behind the engine's broker interface, tested against a fake order API. The CLI
  still refuses `--mode live`: the permission check in this session declined that one change, and go-live is the
  operator's gate. Found and fixed on the way: TARGET exits were not classed as exits (F14), which would have bypassed
  stop-first in live.
- **Terminal for unbounded mode** (D-035): capital gauge, booming list, stop-loss → target bars, Scanner page, whole-market
  search, Booming watchlist tab, LIVE banner, unbounded Monk page; `/status` and `/gear` answer honestly.
- Tests: 153 pytest (incl. 13 scanner, 10 stock-option, 12 live-order), 5 UI unit tests, browser e2e 24/24 (unbounded),
  23/23 (bounded), approvals 3/3.

## Findings the operator should read
0. **Unbounded + live is the highest-risk configuration this system has.** At 25% of equity per trade with no loss limits,
   and strategies with no evidence of edge (O1, O2), losses compound quickly. Recommended: PAPER on live data first, then
   live with a smaller `capital_per_trade` (operator.yaml).
1. **In-year drawdowns are deep even with a real edge.** Under the D12.1 assumptions, ATTACK sees a ≥ 20% drawdown at
   some point in about 75% of years (median worst drawdown about 22%) [SIMULATION]. GUARD and PRESERVE will be normal parts
   of an aggressive year.
2. **₹1 lakh granularity.** Most index-option setups fit one lot for A/A+ only, B-grade almost never fits, and two-lot staging
   rarely fits (SIM model). Staging will mostly run through cash equities.
3. **Two spec tensions resolved conservatively** (please confirm): no upshift at the close of a losing day (D-004), and no
   R% rise from a losing-day tier demotion (D-017).
4. **Budget 2026 STT** (options 0.15%, futures 0.05%) is higher than the rates in Kubera's code. It is in the cost model.

## Unverified / open items
- 21 market rules UNVERIFIED (stamp duty, GST, session times, Bank Nifty weekday, MIS square-off, IPFT…).
  `freeze_qty_nifty`/`freeze_qty_banknifty` resolved to VERIFIED_SECONDARY in D-050 (NSE circular NSE/FAOP/76693);
  `freeze_qty_sensex` stays UNKNOWN (sources disagree, no dated BSE circular found — check BSE's own contract
  master file directly).
- **Known gap for LIVE, not yet fixed (D-050):** `execution/orders.py:slice_for_freeze()` computes the right
  split for an order above the freeze quantity, but `engine/runtime.py:_flush_orders()` only logs that split —
  it still submits the original order at full quantity through `broker.submit()`. Harmless in PAPER/SIM
  (`PaperBroker.submit()` never checks qty against freeze_qty), but Dhan/NSE would reject an oversized single
  LIVE order rather than silently accept it. Fixing it for real means teaching `pending_entries`/`pending_adds`/
  `exit_orders` (today keyed one order id per logical intent) to track multiple child order ids per intent — a
  real feature, not a one-line patch. Needs operator go-ahead before being built.
- Dhan: index security id for the option-chain API, Algo-ID payload field, stop order types for options, kill-switch endpoint, static-IP flow.
- Dhan orders (live broker): endpoint paths and fields, order-status words, SL-M in options, correlationId limits, rate
  limits, auto square-off times, stock-option expiry policy (`docs/LIVE_TRADING.md`).
- Upstox: data-only static-IP requirement, websocket connection limits, whether its data may be used for decisions executed at Dhan.

## Done in the 2.0 fork (2026-09-28)
- **D-036, pure options scope**: deleted `strategies/intraday.py` (the three v1 cash-equity strategies); `all_strategies()`
  now returns only `IndexOptionMomentum`, `ZeroToHero`, `StockOptionMomentum`, all three at every tier and in both
  risk modes.
- **D-037, aggressive-but-bounded risk config**: `risk_mode: bounded` restored as the default (not v1's `unbounded`),
  hard ceilings raised (5.0%/10.0%/28%, `risk/ceilings.py`), `config/operator.yaml` retuned to match preserving v1's
  internal ratios, Zero-to-Hero sleeve budget roughly doubled. Fixed a real bug found on the way: the sleeve's
  affordability check hardcoded the old 0.75% budget instead of reading config.
- **D-039, deepened chain analytics + hardened Zero-to-Hero**: OI build-up classification, IV rank/percentile
  (persisted EOD history), IV-RV spread, expiry pin-risk proxy, all wired into `/api/chain` and the Chain view.
  Zero-to-Hero strike selection now compares EV across three delta bands instead of one fixed strike. Implemented
  the D8 cumulative-loss kill switch, which previously existed only as an unread config key.
- **D-040, LIVE reachable**: `serve --mode live` works when gated (real credentials + an explicit confirm flag);
  defaults to approval-required on every entry the first time; `order-check` wired as a real CLI command; PAPER/SIM
  always tag orders with a dummy Algo-ID regardless of what's in `.env`.
- Rebrand + port 8787→8788 (D-038). Test suite: 153 → 183 (30 new tests for the above).
- **No code path in this fork has ever executed against a real Dhan account.** LIVE stays gated behind explicit
  operator action; PAPER on live Dhan data is the safe way to exercise everything else against real market data.

## Next (in order)
0. Operator: decide whether the D-037 aggressive risk numbers and the D-040 LIVE gate design match your intent
   before enabling anything live — both are logged with full reasoning in `docs/decisions.md` and flagged for review.
1. Operator: put Dhan **data** credentials in `.env`, run `sthitaprajna dhan-check`, then one PAPER session in market
   hours; fix whatever [VERIFY] item it contradicts. Persist paper equity and open positions across restarts (O4) so
   a 6-week paper clock can start, and add Upstox as the second live feed when its token is available.
2. Pull 12+ months of history (incl. expired options) → Phase 1 known-day replay + contract-note charge match.
3. Phase 4: walk-forward + red team for the three options strategies (including Zero-to-Hero's delta-band priors);
   kill what fails; calibrate grades (Phase 5).
4. Discord bot live test with the operator's token (`docs/DISCORD_SETUP.md`), then Phase 6 paper for 6 weeks.
5. Only once 1-4 show real evidence of edge: operator puts Dhan **trading** credentials in `.env`, runs
   `order-check`, sets `STHITAPRAJNA_LIVE_TRADING_CONFIRMED=yes`, and watches the first live session personally.


## 2026-11-04 — all-day contracts and custom network ports

User-requested update: the five general option strategies now use a shared future-expiry
contract selector. Index strategies avoid 0DTE and try later available expiries when a
chain, spread-qualified strike or valid quote is unavailable. The old expiry-afternoon
strategy veto is removed; normal session/signal/EV/risk rules still apply. Stock options
retain the >3-day physical-settlement buffer. Zero-to-Hero remains expiry-specific.
Candidate strategy versions were incremented to distinguish the changed selection.

Vite accepts STHITAPRAJNA_WEB_PORT and STHITAPRAJNA_BACKEND_PORT, validates port numbers,
and proxies HTTP and WebSocket traffic consistently. Python already supports --port
and --db. The new guide documents separate databases for concurrent SIM instances.

Validated: 37 new all-day/fallback regressions, frontend build and 14 frontend tests,
SIM servers on 8790/8791, and HTTP plus WebSocket proxy through frontend 5180. No live
orders or brokerage connections were used for this validation. See
docs/ALL_DAY_OPTIONS_AND_PORTS.md for usage and selection behavior.

Final validation: full Python regression run 300 passed / 2 skipped; dedicated
all-day suite 37 passed (including the subsequently added 15:00 expiry-day scalp
case); frontend 14 passed and production build passed. Skipped tests were not
counted as validated. SIM HTTP and WebSocket custom-port checks passed.


## 2026-11-04 — Dhan HTTP 400 diagnostics

A Windows PAPER startup and dhan-check both received HTTP 400 from market quotes.
The previous diagnostic inferred a software defect without exposing the provider's
reason. Error parsing now supports numeric-code maps under data/remarks/error and
string reasons, redacts both token and client ID before truncation, and avoids
printing arbitrary non-JSON bodies. Failed/error statuses in HTTP 200 responses
are also treated as failures. OHLC fallback is limited to explicit DH-905 input
errors; an unexplained 400 no longer silently switches endpoints. The check command
isolates index and equity segments after an input/unknown failure using the same
rate limiter. Account/rate failures stop further probes.

Validation: 42 diagnostic, market-data and order regression tests passed against
mock APIs/local test servers. The user's actual Dhan response is still needed;
this update improves diagnosis and does not claim live connectivity is fixed.
