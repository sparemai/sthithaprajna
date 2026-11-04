# Part C red team, run against this build (2026-09-27, updated 2026-09-28)

Reviewer stance: hostile but fair. Every Part C check was run against the code, not the intentions. Severity is **for
real money**. On SIM nothing is at risk.

## Fixed during this review
| # | Finding | Sev | Fix | Proof |
|---|---|---|---|---|
| F1 | Upshift could fire at the close of a losing day, so risk rose right after a loss | High | D-004: no upshift on a losing day | Hypothesis `test_no_code_path_raises_risk_after_a_loss` |
| F2 | Tiers auto-promoted by equity; demotion raised R% after a loss | High | D-017: `TierState` with operator promotion and a losing-day hold | `test_tier_*`, property test |
| F3 | Exact 10% drawdown did not downshift (float noise) | High | D-016 rounding | `test_downshift_is_immediate...` |
| F4 | A position could live without a broker-side stop if placement was rejected | High | Retry every tick; exit after 60 s unprotected | `test_position_never_stays_without_a_broker_side_stop` |
| F5 | Discord-down rule (D10) not implemented | High | Heartbeat halt on new entries + SMTP alert | `test_silent_alert_channel_pauses_entries` |
| F6 | Paper fills depended on our own feeds (an outage froze exits) | High | D-018: the paper broker emulates the exchange | `test_both_feeds_down_flattens...` |
| F7 | Broker stop sat so close it acted as the primary stop | Med | D-019: max(4 ticks, 0.15R) buffer | invariant hook |
| F8 | Non-deterministic replay (set ordering) | Med | D-020 | `test_replay_is_deterministic` |
| F9 | Duplicate add proposals while an add was working | Med | Pending-add guard | e2e staged trade |
| F10 | Stale quotes could reach decisions | Med | Freshness gate in `Engine.quote` | `test_failure_injection_primary_down...` |
| F11 | Discord channel: `.env` never read; forwarding stalled after 400 events; one failed post stopped the heartbeat (false D10 halt); login failures silent | High | D-023, D-024 | `tests/test_discord_bot.py`, `tests/test_envfile.py` |
| F12 | With live data, a chain request on a closed market waited for the 30 s slow poll and timed out | Low | The poller wakes when a chain or contract is wanted | `test_closed_market_chain_request_wakes_the_poller` |
| F13 | First real `dhan-check`: HTTP 429 on back-to-back quote calls (spacing measured send-to-send, no retry) | High | D-030: spacing from the response, retry with backoff, one quote call in the check | `test_rate_limit_is_retried_with_backoff_then_explained`, `test_spacing_is_measured_from_the_response` |
| F14 | `TARGET` exits were not classed as exits (`EXIT_PURPOSES`): with a live broker the resting stop would not be cancelled first (double sell), and in a reject storm a target exit could be refused like an entry | **Critical (live)** | `TARGET` and `EXTERNAL` added to `EXIT_PURPOSES` | `test_live_equity_trade_places_a_stop_at_dhan_and_exits_at_target` |
| F15 | Race: the broker stop fills at Dhan while the engine sends its own exit on a stale order book | **Critical (live)** | Stop-first exits: cancel/shrink the resting stop at Dhan, send the exit only after Dhan confirms, never if the stop traded | `test_stop_that_fills_at_dhan_while_the_engine_exits_never_sells_twice` |
| F16 | A position closed in the Dhan app would leave Sthitaprajna's stop resting (it could later sell again) | High (live) | Reconciliation on two consecutive position snapshots; the stop is cancelled at Dhan | `test_a_position_closed_in_the_dhan_app_is_reconciled_and_its_stop_cancelled` |
| F17 | An entry filling after its cancel (kill switch, timeout) was dropped silently; at the close the engine invented fills for open positions | High (live) | Late fills are tracked and closed at once; LIVE never invents a fill (Dhan's positions decide) | `test_kill_switch_flattens_real_positions`, engine code review |
| F18 | Unbounded mode: the plan quantity went stale when free capital shrank before deployment (UI showed "6 / 9") | Low | The plan follows the size actually sent | visual check, SIM |

## Open findings (must close before the gates noted)
| # | Check (Part C) | Sev | Evidence | Required fix | Gate |
|---|---|---|---|---|---|
| O1 | Confidence not calibrated; grade bands not shown monotonic out of sample | **Critical** | All P(win) values are priors (`p_win_calibrated=False`); TQS weights are PLACEHOLDER | Phase 4–5 walk-forward + calibration. **Live is already blocked** by `calibrated_for_live` (D-015) | before PAPER→LIVE |
| O2 | No strategy has evidence of edge | **Critical** | All five are UNVALIDATED; SIM results are SIMULATED | Phase 4 on real history with red team and multiple-testing corrections; kill failures | before PAPER |
| O3 | Dhan↔Upstox canonical instrument mapping not built | High | Adapters normalise per broker; no cross-mapping table yet | Build the A5.5 rule 2 master (exchange token/ISIN/expiry/strike), exclude ambiguities, build orders only from Dhan's master | before PAPER on live data |
| O4 | No crash recovery of open state | High | Open positions, orders and paper equity live in memory; a restart begins again at ₹1,00,000 (history stays in SQLite) | Persist open state and the ledger; on restart reload it (PAPER) or reconcile with Dhan positions and orders (LIVE) | before the 6-week PAPER phase counts |
| O5 | Token expiry / refresh and live websocket loop not implemented | Med | PAPER now runs on a Dhan REST poll (D-025). An expired token is detected, explained and fails closed; renewal is manual, and there is no WebSocket or Upstox loop yet | Token renewal job, pre-market validation, WS reconnect with backoff per broker (independent failure domains) | before LIVE |
| O6 | Fill realism | Med | Next-tick fills at bid/ask; no partial fills, queue position or impact; options spread model is synthetic | Partial-fill and impact model calibrated from real depth; limit-order execution logic (A11) | Phase 1 real data |
| O7 | Charges from secondary sources | Med | STT, exchange charges VERIFIED_SECONDARY; stamp, GST, SEBI fee, IPFT VERIFY | Match a Dhan contract note | Phase 1 exit |
| O8 | Confirmations may share a factor | Med | Factor tags (volume/flow/breadth/trend/sector/oi) are coarse; trend, flow and sector co-move | Measure SIGNAL_CORRELATION empirically (A9.2); merge correlated confirmations | Phase 5 |
| O9 | Correlation beyond index bucket | Med | Stocks with beta < 1.2 aligned with the index are not counted; no sector exposure cap | Add a sector and aggregate-beta exposure limit | before PAPER |
| O10 | Staging never compared out of sample | Med | Tooling exists; a 10-day SIM comparison only (inconclusive) | Phase 4 staged vs single on real OOS data per strategy | Phase 4 |
| O11 | Remote access hardening | Med | Shared token over HTTP; no TLS | Reverse proxy with TLS on the VM; rotate the token; restrict by IP | before remote use |
| O13 | Live Dhan data path only partly verified against the real API | Med | The operator's first `dhan-check` confirmed credentials, master, all 40 ids, lots, IDX_I in quotes and the quote field names (and found F13). Still [VERIFY]: depth level fields, `ohlc.close` meaning, candle alignment, put-delta sign, DH-9xx table, tick unit | Re-run `dhan-check` after F13; then one PAPER session during market hours | first PAPER session |
| O12 | Unverified market rules | Med | 23 rules VERIFY/UNKNOWN (freeze qty, MIS time, Bank Nifty weekday…) | Verify against NSE/BSE/Dhan; freeze quantities loaded daily | before PAPER |
| O14 | Unbounded mode (D-031) removes every risk ceiling at the operator's instruction | **Critical (real money)** | 25% of equity per trade, no loss limits, no drawdown halt, no streak stops; strategies unvalidated (O1, O2) | Operator's decision, recorded. Recommended before real money: PAPER on live data first, a smaller `capital_per_trade`, and `risk_mode: bounded` restores the spec | before LIVE |
| O15 | Live broker never run against the real Dhan order API | High | Tested only against `tests/fake_dhan.py`; order paths, status words, SL-M in options, Algo-ID/static-IP needs are [VERIFY] (`docs/LIVE_TRADING.md`) | Read-only order check, then the single test order, then a watched first session | before LIVE |
| O16 | Scanner score is uncalibrated | Med | PLACEHOLDER weights (D-032); it only ranks and promotes, it never trades by itself | Measure which components predict follow-through on real data (Phase 4) | Phase 4 |

## Checks that pass
Look-ahead in the bar pipeline (completed bars only) · no microstructure or seconds-scale alpha claimed · no naked short
options · intraday flat by 15:10 (no physical-settlement exposure at T1) · lot granularity skips, never forces · Nifty+Sensex
same direction = one bet · no path exceeds 3% / 6% / trades past 25% (property + per-tick invariants) · gears never step up
on wins or targets · downshifts immediate and not overridable · no add to a loser; whole position within plan · the
Opportunity Maximiser cannot lower thresholds · re-entry: fresh trigger, one per day, probe size, gear-limited · no
auto-generated strategy path to live · **no LLM in `src/`** (grep) · no endpoint or command can open, enlarge, widen or raise risk.

## Verdict (28 Sep update)
The operator chose unbounded risk (D-031) and live order-taking (D-034). The live order path is now careful about the
mechanics that lose money through bugs rather than markets: stop-first exits, reconciliation with Dhan, no invented
fills, late-fill handling. None of that creates an edge. My view has not changed: **there is no evidence of edge yet**
(O1, O2), and at 25% of equity per trade with no loss limits, a strategy without an edge loses money fast. Going live is
the operator's call (the CLI keeps `--mode live` switched off until then). The honest recommendation is PAPER on live
Dhan data first, then live with a smaller capital share per trade.

## Verdict (original, 27 Sep, bounded mode)
Would I let this system trade real money today? **No, and it correctly refuses to.** The risk plumbing is strong and
proven: ceilings, gears, staging math, broker-side stops, kill switch and failure handling are tested far beyond a typical
retail system. But there is **no validated edge yet** (O1, O2), and live-data operations (O3–O5) are unbuilt. The honest next
size is **₹0 live and full-size PAPER**. That means running PAPER in ATTACK gear on real Dhan/Upstox data once gate (a) is
cleared, while Phase 4 either finds strategies that survive the red team or reports that none do. If validation passes and paper tracks
backtest within bands for 6 weeks, start LIVE-PILOT at the spec's ATTACK sizing with approval on every entry, having
accepted beforehand that the typical aggressive year includes a drawdown of about 20% even with a real edge.
