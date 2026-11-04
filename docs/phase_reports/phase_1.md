# Phase 1: Backtest Engine

**Status: partial. The engine and mechanics are done. The historical-data criteria are blocked on data (gate a).**

## Built
- `research/backtest.py` drives the **same `Engine`** in deterministic fast-forward (A5.4: identical code path; no separate
  "backtest version" of any strategy, sizing, guardian or staging logic).
- Supported: staged entries, confirm-adds, presses, partial harvests, trailing stops, time and EOD exits, **gap-through-stop
  fills at the gapped market**, broker-side stops, next-tick fills, bid/ask spread and slippage, every Indian charge by
  segment (`market/charges.py`, rates from `market_rules.yaml`), lot sizes and weekly/monthly expiries.
- The staged vs single-entry research variant (`single_entry_variant`, `compare_staging`) serves AGG-2.2's "staging is a hypothesis".
- Metrics (`research/metrics.py`): expectancy with a 95% CI, profit factor, max DD and duration, Sharpe, Sortino, Calmar, PSR, streaks.

## Exit criteria
| Criterion | Result |
|---|---|
| Tests prove no look-ahead | **MET for the bar pipeline.** Minute bars are only moved into history once their minute has ended (`test_sim_bars_are_complete_minutes_without_lookahead`); strategies read completed bars only; UI charts label the forming bar as display-only; Upstox's forming candle is dropped (ported). A full "truncate the future and compare decisions" harness needs historical data. |
| One known historical day replays within tolerance of actual prices | **NOT MET: needs historical data (gate a/b).** |
| Charges match a real broker contract note | **NOT MET: needs a Dhan contract note.** Hand-computed cases pass (`test_option_charges_by_hand` etc.). |
| Staged and single-entry trades give correct P&L in hand-checked cases | **MET**: `test_full_staged_trade_with_approvals_and_reconciliation` reconciles legs − charges to the ledger to ₹0.01; `test_order_api_down...` checks gap fills. |

## Evidence [SIMULATED: synthetic market, n < 100, inconclusive by A12.9]
10 simulated sessions, seed 7:

| Variant | Trades | Net | Win rate | Expectancy (95% CI) | Profit factor | Max DD |
|---|---|---|---|---|---|---|
| Staged | 21 | +₹2,546 | 42.9% | +0.02R (−0.23, +0.27) | 1.31 | 3.8% |
| Single entry | 27 | +₹2,259 | 44.4% | +0.10R (−0.35, +0.54) | 1.15 | 4.7% |

Staging cut drawdown and trade count in this sample, as AGG-2.2 anticipates. It proves nothing about real markets: both CIs span zero.

## Risks
- The synthetic market has an unrealistic, stationary structure. Real validation must use historical data plus walk-forward (Phase 4).
