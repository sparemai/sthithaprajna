# Assumptions

Interpretations made where the spec leaves room. Each one is conservative (never looser than the spec) and open to
operator review. Items tagged **[VERIFY]** depend on facts not yet confirmed from primary sources.

## R conventions (AGG-2.2)
- **unit_risk** = |probe fill − initial stop| in price terms.
- "**Probe ≥ +0.5R in favour**" means the price has moved 0.5 × unit_risk from the average entry.
- "**Open profit ≥ 1R**" (press trigger) and "**from 2R**" (profit lock) are measured as unrealised ₹ ÷ planned-risk ₹.
- "**+2R harvest**" means the price has moved 2 × unit_risk from the average entry.
- Trade R in journals and KPIs = net ₹ ÷ planned risk ₹. A failed 50% probe therefore records about −0.5R.

## Sizing and staging
- Costs per unit = round-trip charges (market_rules) + about one spread of slippage, included before lots are computed.
- B grade is probe only: the position is the probe, at 50% of B's planned risk (D12.5 "probe only").
- A single-lot fit means the probe is the full position, and confirm-add is skipped (AGG-2.2).
- When the confirm window expires without confirmation, the probe is **kept** (strategy default), with no further adds.
- Before the fill, the entry zone allows 0.15R of drift. Beyond that the order is skipped (no chasing).
- A fill that lands outside the plan (gap on entry) is pulled back inside by tightening the stop, or exited at once.

## Risk
- The weekly and monthly loss bases are equity at the start of the week and the month.
- The profit give-back trigger compares today's MTM P&L peak with 1.5 × gear R ₹ (D4).
- Margin: long premium = the full premium outlay. Cash intraday = 20% of notional **[VERIFY Dhan MIS margin]**.
- "Unusual entry" (needs approval in LIVE-FULL) = a detected change point or a VIX expansion tag.
- Sleeve budgets count the probe's ₹ risk against the expiry, week and month budgets.

## Data and simulation
- SIM data is synthetic: no NSE/BSE levels, OI or volumes are claimed to be real. The Kubera 2026 holiday list is used and flagged **[VERIFY]**.
- The paper broker fills MARKET orders at the opposite side of the book on the first quote newer than the order (next tick).
- The Black-76 risk-free rate is 6.5% **[VERIFY: RBI T-bill]**. Option IV is VIX-linked with a symmetric-plus-skew smile (SIM only).
- The Dhan index-underlying security id for the option-chain API is unresolved in Kubera **[VERIFY in Phase 0]**.

## Charges **[VERIFY with a Dhan contract note]**
- STT follows Union Budget 2026 (futures 0.05%, options 0.15% of premium, from 1 Apr 2026). These rates come from secondary sources.
- NSE exchange charges (₹2.97 / 1.73 / 35.03 per lakh) and BSE Sensex options (₹3,250/cr) come from secondary sources.
- Stamp duty, SEBI fee, IPFT and GST are background knowledge. They are marked VERIFY in `market_rules.yaml`.

## Market structure
- Nifty weekly expiry is Tuesday and Sensex is Thursday (VERIFIED in the master prompt). Bank Nifty monthly on the last Tuesday is **[VERIFY]**.
- Lot sizes are 65 / 30 / 20 (VERIFIED, as fallbacks). Freeze quantities are placeholders **[UNKNOWN]**.
- The system exits intraday at 15:10. Dhan's MIS auto square-off time is taken as 15:20 **[VERIFY]**.
