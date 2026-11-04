# Phase 3: Second Feed and Intelligence Engines

**Status: partial. Arbitration and failure injection are done on SIM; real-feed parts need gate (a).**

## Built
- **Feed arbitration** (`data/arbitration.py`): exactly the A5.5 table (agree / disagree > max(2 ticks, 5 bps) for > 3 s /
  secondary down (cap 0.8) / primary down (entries blocked, monitor via secondary) / both down (broker stops, flatten after 5 min)).
  Feeds are never averaged.
- **Data Quality Guardian**: bad prints, crossed quotes, stale ticks, missing symbols, clock-drift halt.
- **Failure injection** (SIM): primary down, secondary down, both down, disagreement, order API down, reject storm, ±1.5% gap.
- **Intelligence**: volatility regime (+ expansion/compression tags), trend regime (EMA + ADX), expiry regime
  (PINNING / TREND / BREAKOUT / VOLATILITY_CRUSH / GAMMA_ACCELERATION / RANGE), volatility change points, Black-76 Greeks,
  chain analytics (PCR, max pain, OI walls, straddle-implied move, 25Δ risk reversal), breadth A/D, sector alignment,
  relative volume vs time of day. Each outputs a confidence and states input latency as "real-time (bar close)".

## Exit criteria
| Criterion | Result |
|---|---|
| Failure-injection tests pass (kill each feed, both, the order API mid-session in PAPER) | **MET on SIM**: `test_failure_injection_primary_down...`, `test_order_api_down...`, `test_both_feeds_down_flattens...`, UI E2E |
| Primary per data type assigned from the benchmark | **NOT MET: needs real feeds (gate a)** |
| Each engine has tests, documented latency, confidence | **PARTIAL**: core engines tested; large-participant proxy, intermarket, cross-sectional ranking and episode memory are thin or not built |
| Latency report of capturable half-lives | **NOT MET: needs real feeds.** SECONDS-half-life signals are not used (A9.8). |
