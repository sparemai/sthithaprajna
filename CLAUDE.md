# CLAUDE.md: Sthitaprajna 2.0 (Options Hero)

Read this first in every session, then `docs/progress.md`, and continue from the last incomplete step. This is a
fork of Sthitaprajna v3.0 (`docs/MASTER_PROMPT.md`, the full multi-asset spec), narrowed to **pure index + stock
options**. Where this file is silent, MASTER_PROMPT.md's mechanics (Aggression Layer, Risk Guardian, execution,
data layer) still apply unchanged; where the two conflict on scope or numbers, this file and `docs/decisions.md`
D-036 onward win. Precedence: **A2 Honesty Contract → Part D (`config/operator.yaml`) → hard ceilings
(`risk/ceilings.py`) → Aggression Layer → everything else.**

## Identity (A1)
Lead architect, quant, risk officer and engineer for an aggressive but risk-bounded **options-only** trading system
for Indian markets. Dhan handles execution and primary data; Upstox is the secondary data feed. Starting capital is
₹1,00,000. The stance is **full force, zero attachment**: attack validated opportunities, never hope, never average
down. Long premium only -- naked option selling is disabled (D0) and does not change without the operator explicitly
enabling and hedging it.

## Scope (D-036, D-041, D-043, D-048): options-only
Six strategies trade: `IndexOptionMomentum` and `StockOptionMomentum` (opening-range breakout/momentum, need a
trend), `ZeroToHero` (the far-OTM expiry sleeve, D8, needs an expiry-day regime), `IndexRangeReversion` (D-041,
fades range extremes back to the mean -- wants RANGE regime and calm volatility, where the trend strategies stand
down), `DayRangeBreakout` (D-043, a later second-leg break of the day's own high/low once that level has
already carried past the opening range -- distinct from the first-25-minutes OR breaks the momentum strategies
trade), and `ScalpMomentum` (D-048, a short 1-3 minute directional burst traded on its own, anywhere in the
session and in any regime except EXTREME_VOL including RANGE, tight stop/tight target_r=1.0, higher-delta
near-ATM strike so the option actually responds to a small move -- the strategy that keeps trading small, quick
moves when the other five are waiting for a bigger structural setup). All six at every tier, in both risk modes.
No cash-equity strategy runs. The whole-market scanner still screens every NSE stock (it's how F&O names get
found and ranked), but only options ever get ordered; non-F&O names are informational only.
`strategies/intraday.py` (the v1 cash-equity strategies) was deleted, not merely disabled. `market/patterns.py`
(D-041) adds candlestick/chart pattern detection (engulfing, hammer/shooting star, range-compression breakouts)
that feeds these strategies' existing Confirmation lists rather than trading on its own. Below +2R,
`aggression/staging.py`'s `trailing_stop()` also runs two earlier profit-protection tiers
(D-042: breakeven at +0.5R, a partial lock at +1.0R) so a fast move that reverses before the +2R harvest
threshold doesn't give back a position's entire open profit; those tiers fire even earlier on a position's
own expiry day (D-045: 0.3R/0.6R, since extrinsic value decays fastest into the close). Part D's
gears/grades/staging/limits/sleeve/edge_brake/targets sections are editable from the Monk page (D-044),
validated against the hard ceilings before anything is written, restart required to apply. `Engine.reversal_watch()`
(D-045) adds one extra Confirmation to a candidate that already independently qualifies, when it sits opposite a
recent stop-out on the same underlying -- evidence only, never a forced flip.

## Honesty Contract (A2), non-negotiable
- No guaranteed returns. Every performance number is labelled **SIMULATED / BACKTEST / WALK-FORWARD-OOS / PAPER / LIVE**.
- Targets never change risk. Expectancy matters more than win rate. Never fabricate endpoints, rates or results: write `UNKNOWN: VERIFY`.
- If a strategy shows no edge after costs, say so and disable it. The operator's enthusiasm is not evidence.
- No manipulation of any kind. **No LLM in the live order path (A5.3).**

## Aggression Layer (AGG-1/2) in one paragraph
Gears: ATTACK (default) → PRESS → GUARD → PRESERVE → HALTED, driven only by drawdown (D12.3) and the Edge
Brake (D12.4). Planned risk = gear R × grade multiplier (A+ 1.2 / A 1.0 / B 0.5), and it is only ever
reduced after that. Staging runs probe (50%) → confirm-add (≥ +0.5R plus a confirmation) → press (≤ 2 adds, lock
≥ 50% of open profit from 2R) → harvest (40% at +2R) → trail. Never add to a loser. The Opportunity Maximiser picks
only among candidates that passed every gate; it never lowers a threshold. The Zero-to-Hero sleeve is separate and
ring-fenced (D8): its own budget, its own cumulative-loss kill switch (D-039), never staged or pressed.

## Decision hierarchy (A5.2), implemented in `src/sthitaprajna/engine/runtime.py`
data (primary + secondary) → feed arbitration + data quality → regime → strategies → pickup funnel S1–S5
→ queue → Aggression Layer → Risk Guardian (S6) → operator approval → execution (S7, Algo-ID, broker-side
stop) → Position Stage Manager (every add re-runs the guardian) → ledger → gears / edge brake → journal.

## Risk mode and hard ceilings (D-037/D-049: aggressive-tuned for 2.0, gate (e))
`config/operator.yaml` `risk_mode: unbounded` (the current default, D-049, 5 Oct 2026 -- reversing D-036's 28 Sep
`bounded` default) is v1's D-031/D-035 design: capital-based sizing, one full entry with stop-loss and target,
`unbounded.capital_per_trade` 25% of equity per trade, no gears/ceilings/loss-limits/staging/approvals. `risk_mode:
bounded` (the full gear/grade/staging/loss-limit machinery below) is still selectable -- see `docs/decisions.md`
D-037 and D-049 for the numbers and why the default flipped. Unbounded mode also skips the mandatory
operator-approval-per-entry gate on LIVE orders, not only the risk ceilings -- irrelevant in PAPER/SIM, binding
the moment `--mode live` is used with this config. Unbounded mode has no correlation check of its own (that's
bounded-only, `risk/guardian.py`'s
`check_entry`), so `Engine._deploy()` caps it at one bet per underlying+direction per deploy cycle (D-046) --
otherwise several strategies agreeing on the same move in the same cycle would each independently commit a full
`capital_per_trade` slice to what is really one correlated bet. PAPER/SIM equity now also survives a process
restart (D-047): `Ledger.restore_from_history()` replays the SQLite store's closed-trade history to rebuild
realized P&L and the all-time peak at startup -- only realized P&L carries forward, a position still open at the
previous shutdown is not resumed. Hard ceilings, constants in `risk/ceilings.py`, never runtime-configurable:

Planned risk ≤ **5.0%** per trade · open risk ≤ **10.0%** · no new trades at ≥ **28%** drawdown ·
ruin floor **max(₹70,000, 70% of all-time peak)** (unchanged from v1 -- capital survival doesn't scale with
aggression) · orders/sec cap stays below SEBI's 10.

ATTACK gear R is 4.17% of equity at T1 (A+ trades reach the 5% ceiling); T1 allows 5 concurrent positions and 10
trades/day. Daily/weekly/monthly loss limits are 8/15/22% of equity. The Zero-to-Hero sleeve budget is 1.5% of
equity per expiry (3%/week, 7%/month), with a 10%-of-equity cumulative-loss kill switch that needs an explicit
`/resume(reviewed=True)` to clear, same as a gear HALT. Full numbers and the reasoning: `docs/decisions.md` D-037.

## LIVE trading (D-040)
`sthitaprajna serve --mode live` is reachable, not hardcoded off -- but gated behind two things the operator sets in
`.env`, both required: real `DHAN_CLIENT_ID`/`DHAN_TRADING_TOKEN`, and `STHITAPRAJNA_LIVE_TRADING_CONFIRMED=yes` (a
deliberate second switch; credentials alone never turn it on). The first live session defaults to operator approval
on every entry and stage-add (D9). PAPER and SIM never read `DHAN_EXCHANGE_ALGO_ID`; they tag orders with a dummy
`PAPER-ALGO-0000`/`SIM-ALGO-0000` so a paper decision log can never look like a real exchange Algo-ID. Full detail:
`docs/LIVE_TRADING.md`.

## Where things are
| Need | Go to |
|---|---|
| Operator config (Part D) | `config/operator.yaml` (validated so it can only be stricter than the ceilings) |
| Market rules with sources | `config/market_rules.yaml` (`sthitaprajna rules` audits freshness) |
| Decisions and assumptions | `docs/decisions.md` (D-036 onward = 2.0-specific), `docs/assumptions.md` |
| Status and next steps | `docs/progress.md`, `docs/phase_reports/` |
| Architecture | `docs/ARCHITECTURE.md` |
| Going live | `docs/LIVE_TRADING.md` |
| Red team | `docs/red_team.md` (Part C run against v1's build; re-run after any 2.0 strategy change) |

## Working rules
- Test before claiming done: `pytest` (Python), `cd web && npm run build && npm test` (UI), `node web/e2e/*.cjs` (E2E).
- Human gates (B0): credentials, paid data, go-live, real money per strategy, **any Part D or ceilings change**, or
  an exit criterion failed twice. Stop and ask at these; otherwise proceed and log decisions.
- Never commit secrets. Credentials live only in the environment (`.env.example` lists the names).
- End every session by updating `docs/progress.md`.
