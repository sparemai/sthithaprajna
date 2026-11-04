# Architecture

```
                      ┌──────────────────────── web terminal (React/TS, built into api/static) ─────────────┐
                      │  Dashboard · Terminal · Chain · Positions · Orders · Queue · Monk · Research · ...   │
                      └──────────────▲───────────────────────────────┬───────────────────────────────────────┘
                                     │ WebSocket snapshot (2 Hz)     │ REST actions (approve/close/trail/lock/kill)
                      ┌──────────────┴───────────────────────────────▼───────────────────────────────────────┐
 Discord bot (D10) ──►│ api/server.py (FastAPI)  ·  api/commands.py (operator whitelist, rate limit)          │
                      └──────────────▲───────────────────────────────────────────────────────────────────────┘
                                     │ same process, one asyncio loop, deterministic ticks
┌────────────────────────────────────┴─────────────────────────────────────────────────────────────────────────┐
│ engine/runtime.py: the A5.2 hierarchy, one deterministic loop                                                 │
│                                                                                                               │
│  data/feeds ─► data/arbitration (A5.5 table) ─► data/quality ─► intelligence/regime ─► strategies/*           │
│      │                                                                                     │ candidates (S1)  │
│      │                                                           engine/funnel.py  S2 qualify → S3 grade      │
│      │                                                           (signal/ev, grading) → S4 confirm → S5 arm   │
│      │                                                                                     │ ranked queue     │
│      │                         aggression/sizer · staging · tiers · gears · edge_brake ◄───┘                  │
│      │                                                     │ proposal                                        │
│      │                                  risk/guardian.py (S6, named checks, absolute veto) ─► approval gate   │
│      │                                                     │                                                 │
│      └──────────► execution/paper.py (exchange emulation: fills, broker-side SL, gap-through) ◄─ orders.py    │
│                                                     │ fills                                                   │
│                  Position Stage Manager (probe → confirm → press → harvest → trail/exit; every add re-checked)│
│                                                     │                                                        │
│                  portfolio/ledger ─► gears/edge brake/tiers ─► kpis ─► journal ─► engine/store (SQLite)       │
└───────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Module map

| Package | Responsibility | Spec |
|---|---|---|
| `config.py` + `config/*.yaml` | Part D (validated against the ceilings) and market rules (value/status/source/verified_on, freshness) | D, A4 |
| `risk/ceilings.py` | Hard ceilings as constants: 5% / 10% / 28% / ruin floor / OPS (2.0, D-037; v1 was 3%/6%/25%) | D12.2 |
| `risk/guardian.py` | Risk Guardian: entry and add verdicts with named checks; exits never blocked | A10.12, D4 |
| `risk/targets.py` | Targets, Ratchet Mode, `/lock` (targets never change risk) | D5 |
| `aggression/gears.py` | Gear controller: immediate downshifts, recovery-only upshifts, HALTED → GUARD | D12.3 |
| `aggression/edge_brake.py` | Quarter-Kelly ceiling from live stats; expectancy ≤ 0 caps at GUARD | D12.4 |
| `aggression/sizer.py` | Gear R × grade multiplier, decreases only, lot-granularity skips, leverage caps | A10.4, D2 |
| `aggression/staging.py` | Confirm-add / press / harvest / trailing math; whole-position worst case within plan | AGG-2.2 |
| `aggression/tiers.py` | Capital tiers as state; operator-approved promotion; no R rise after a losing demotion | D3 |
| `aggression/kpis.py` | Idle risk budget, capture ratio, probe success, expectancy by grade/stage | AGG-1 |
| `engine/funnel.py` | Pickup funnel S1–S5, opportunity queue, A18 decision objects | AGG-2.1, A18 |
| `engine/runtime.py` | The loop: deploy, approvals, orders, fills, stage manager, counterfactuals, day boundaries | A5.2 |
| `signal/*` | EV engine + margin of safety, weakest-link confidence, TQS, grading, calibration tracking | A9 |
| `strategies/*` | Three hypothesis-card strategies, options-only (2.0, D-036): index-option momentum, Zero-to-Hero, stock-option momentum. The v1 cash-equity strategies (ORB, VWAP reclaim, trend pullback) were deleted, not disabled | A7, A8 |
| `market/*` | Calendar/expiries (Tue/Thu, holiday shift), Indian charges, Black-76 + Greeks + IV, indicators, chain analytics | A4, A6 |
| `data/adapters/*` | Dhan and Upstox read-only adapters (ported, live-verified shapes) behind `BrokerDataAdapter` | A5.5 |
| `data/sim.py`, `data/feeds.py` | Synthetic market + two simulated feeds + failure injection | Phase 3 tests |
| `execution/*` | Order state machine, Algo-ID, OPS limiter, freeze slicing, paper broker, fail-closed live client | A11, A5.5 r4 |
| `research/*` | Monte Carlo (reproduces D12.1), Target-to-Plan, metrics (PSR), backtest on the same engine, feasibility | A16, D12.1 |
| `api/*`, `discord_bot.py`, `cli.py` | Terminal server, operator commands, Discord, CLI | D6, D9, D10 |

## Invariants and where they're proven

| Invariant | Enforced in | Proven by |
|---|---|---|
| Per-trade risk ≤ 5%, open risk ≤ 10%, no trades past a 28% drawdown (2.0, D-037) | `ceilings`, `sizer`, `guardian` | `test_planned_risk_never_exceeds_ceiling`, `test_runtime_invariants_hold_every_tick` |
| No code path raises risk after a loss | `gears` (D-004), `tiers` (D-017), `sizer` (sizing from the previous close) | `test_no_code_path_raises_risk_after_a_loss` (Hypothesis) |
| Gears never step up on wins or targets | `gears.on_equity` can only downshift | `test_on_equity_never_raises_the_gear`, `test_targets_have_zero_effect_on_gear_and_size` |
| No stage adds to a loser; whole position within plan | `staging`, `guardian.check_add`, post-fill pull-back | `test_confirm_add_never_adds_to_a_loser_and_stays_in_plan`, `test_press_rules` |
| Every position has a broker-side stop that works when feeds or the order API are down | `runtime._place_broker_stop`, `paper.PaperBroker` (D-018) | `test_order_api_down_broker_stop_still_protects...`, invariant hook |
| Kill switch flattens and halts; resume needs review | `runtime.kill/resume` | `test_kill_switch_end_to_end`, E2E |
| No command can increase risk; no open/enlarge endpoint | `api/commands.py`, `api/server.py` | `test_no_command_can_raise_risk`, `test_actions_cannot_open_positions` |
| Deterministic replay | seeded RNGs, sorted iteration (D-020) | `test_replay_is_deterministic` |
| No LLM in the live path | no LLM imports in `src/` | `grep -ri "openai\|anthropic" src/` returns nothing |

## Credential isolation
Data adapters read `DHAN_DATA_TOKEN` / `UPSTOX_ANALYTICS_TOKEN` only. `execution/live.py` is the only module that reads
`DHAN_TRADING_TOKEN`, and it refuses to construct unless every live gate flag is set (`resolve_mode` fails closed to PAPER).
