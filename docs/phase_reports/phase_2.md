# Phase 2: Walking Skeleton with Aggression Plumbing

**Status: done on SIM. The Discord live test needs a bot token (gate a).**

## Built
The full A5.2 hierarchy in `engine/runtime.py`: data → arbitration → quality → regime → 5 strategies → funnel S1–S5 → queue →
Aggression Layer (gears, grades, sizing, staging, tiers, edge brake) → Risk Guardian (S6, named checks) → approval gate
(pilot mode, 60 s, timeout = skip) → staged paper execution (Algo-ID tags, OPS cap, broker-side SL) → Position Stage Manager →
reconciliation → ledger → KPIs → journal → A18 decision objects (SQLite). Kill switch, all D4/D12 limits, ratchet and lock,
manual-exit counterfactuals, the Discord bot module (`discord_bot.py`), the web terminal, and the operator command whitelist.

## Exit criteria
| Criterion | Result | Evidence |
|---|---|---|
| A complete paper trade runs probe → confirm-add → harvest → exit, with approval and reconciliation | **MET** | `test_full_staged_trade_with_approvals_and_reconciliation`; UI flow `web/e2e/approvals.e2e.cjs` |
| No code path raises risk after a loss | **MET** | Hypothesis `test_no_code_path_raises_risk_after_a_loss` found two spec-level paths, which were fixed (D-004 upshift-on-losing-day, D-017 tier demotion) |
| Nothing exceeds 3.0% per trade or 6.0% open risk, or trades past the 25% halt | **MET** | `test_planned_risk_never_exceeds_ceiling` (500 examples), `test_runtime_invariants_hold_every_tick` (3 seeds × 2 days, every tick) |
| Gears never step up because of wins or targets | **MET** | `test_on_equity_never_raises_the_gear`, `test_targets_have_zero_effect_on_gear_and_size`, `test_no_command_can_raise_risk` |
| No stage adds to a losing position | **MET** | `test_confirm_add_never_adds_to_a_loser_and_stays_in_plan` (400 examples), `test_press_rules` |
| Every position has a broker-side stop in the paper model | **MET** | Invariant hook on every tick; the stop fires with the order API down (`test_order_api_down...`) |
| Kill switch tested end to end | **MET** | `test_kill_switch_end_to_end`; UI E2E (kill → flatten → review → resume) |
| Discord approval | **PARTIAL** | Bot, buttons and routing are built; formatting and the operator gate are tested. A live run needs a token. |

## Bugs found and fixed by the tests (the point of testing)
Exact 10% drawdown failed to downshift (float noise, D-016). Upshift at the close of a losing day (D-004). Tier auto-promotion
and a risk increase on demotion (D-017). Set-order nondeterminism (D-020). The broker stop was effectively the primary stop (D-019).
The paper broker depended on our own feeds (D-018). Duplicate add proposals while an add was pending. Stale quotes leaking
into decisions. Simulator OI compounding exponentially.

## Metrics
All SIM numbers are SIMULATED; see phase_1 for the staged vs single comparison. Aggression KPIs are computed live (idle risk
budget, capture ratio, probe success, expectancy by grade and stage) and shown on the Dashboard.
