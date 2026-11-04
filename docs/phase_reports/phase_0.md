# Phase 0: Audit, Rules, Data Foundation

**Status: mostly done. Blocked on gate (a) for the live-data parts.**

## Built
- **Gap analysis of existing code.** The `trade_like_a_monk` zip is identical to that repo's HEAD, and Kubera v5.5 is its
  evolved superset (19.4k vs 11.3k lines). Kubera is a linear pipeline (signal → risk → compliance → execution). It has no gears, grades,
  staging, funnel or hard-ceiling layer. Its *broker integrations* were the verified working parts, and they were ported (D-001):
  Dhan historical/intraday/OHLC/option-chain/expiry-list calls, the int-vs-string security-id quirk, the option-chain response
  shape verified on RELIANCE, the WebSocket ticker packet `<BHBIfI>`, Upstox instrument master / LTP / candles / option chain,
  paise tick-size normalisation, dropping the forming candle, and a retry policy of transport + 5xx + 429 only.
  Kubera's LLM parts (Sovereign, OpenAI adviser) were not ported into the live path (A5.3, D-012).
- **`CLAUDE.md`**, `docs/decisions.md` (D-001…D-020), `docs/assumptions.md`, `docs/progress.md`.
- **`config/market_rules.yaml`.** 45 rules, each with `value`, `status`, `source_url`, `verified_on`, plus a freshness audit
  (`sthitaprajna rules`) that flags anything older than 30 days or not verified.
- **Loaders.** Calendar with Nifty-Tuesday / Sensex-Thursday weeklies, monthly expiries, and holiday → previous-trading-day shift
  (tested: the Tue 20 Oct 2026 holiday moves the expiry to Mon 19 Oct). Holiday list, charges model, trading-minute time-to-expiry.
- **Adapters.** `DhanDataAdapter`, `UpstoxDataAdapter` behind the `BrokerDataAdapter` protocol; tested against the verified response
  shapes with mocked HTTP.

## Exit criteria
| Criterion | Result |
|---|---|
| Data inventory report (entitlements, depth levels, limits, token lifecycle) | **NOT MET: needs credentials (gate a).** The adapters are ready. |
| ₹1 lakh feasibility table at ATTACK gear | **MET (SIM model)**: `sthitaprajna feasibility` / Research page. Replace premiums with Dhan margin-API numbers before go-live. |
| All loaders tested | **MET**: `tests/test_market_core.py`, `tests/test_data_layer.py` |
| Every rule has `source_url` and `verified_on` | **PARTIAL**: every rule has a `source_url`; 22 rules are verified (9 VERIFIED + 13 VERIFIED_SECONDARY, dated 2026-09-27). 23 are `VERIFY`/`UNKNOWN` with `verified_on: null`, deliberately. They are listed below rather than given a fake date. |

## Feasibility table (SIM model, ATTACK gear, ₹1,00,000; budgets A+ ₹3,000 · A ₹2,500 · B ₹1,250)
| Structure | 1-lot risk (20% / 30% premium stop) | Fits |
|---|---|---|
| Nifty weekly CE ~0.50Δ (lot 65, ~₹121) | ₹1,629 / ₹2,411 | A+, A (1 lot) at both stops; B no; 2-lot staging no |
| Nifty weekly CE ~0.35Δ (~₹76) | ₹1,040 / ₹1,531 | A+, A, B at 20%; 2-lot staging only at 20% |
| Nifty weekly CE ~0.60Δ (~₹179) | ₹2,399 / ₹3,562 | A+, A at 20% only |
| Sensex weekly CE ~0.50Δ (lot 20, ~₹541) | ₹2,232 / ₹3,312 | A+, A at 20% only |
| Sensex weekly CE ~0.35Δ (~₹322) | ₹1,347 / ₹1,989 | A+, A at both stops |
| Sensex weekly CE ~0.60Δ (~₹771) | ₹3,161 / ₹4,700 | nothing fits: skipped, never forced |
| Bank Nifty monthly ~0.50Δ (lot 30, ~₹287) | ₹1,788 / ₹2,648 | A+, A at 20%; A+ only at 30% |
| Liquid cash stock, 0.8% stop | ₹1.4–12 per share | all grades; staging always fits (precise sizing) |

**Finding:** at ₹1 lakh, options staging mostly degenerates to a single lot. Cash equities carry the staging programme (consistent with AGG-2.2's own note).

## Open [VERIFY] items
freeze quantities (NIFTY/SENSEX/BANKNIFTY), strike steps, Bank Nifty monthly weekday, stock-F&O physical-settlement cut-off,
session times, T+1, Dhan MIS square-off time, IPFT, SEBI fee, stamp duty (4), GST, risk-free rate, Dhan index security id for
the option chain, Algo-ID payload field, permitted stop order types for options, the kill-switch endpoint, the static-IP process,
and the Upstox data-only static-IP rule and API-terms cross-broker use.

## Risks
- Secondary sources for STT and exchange charges: confirm against a Dhan contract note (Phase 1 exit).
- Budget 2026 raised STT. Anything validated on older cost assumptions would overstate edge.
