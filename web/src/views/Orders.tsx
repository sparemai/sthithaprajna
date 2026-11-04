import { useState } from "react";
import { num, rupees, time } from "../lib/format";
import { useStore } from "../lib/store";
import { Card, Seg, Stat } from "../components/ui";

const STATUS_CLASS: Record<string, string> = { FILLED: "chip-up", OPEN: "chip-accent", REJECTED: "chip-down", CANCELLED: "", SUBMITTED: "chip-warn", NEW: "" };

export function Orders() {
  const snap = useStore((s) => s.snap);
  const [f, setF] = useState<"all" | "entries" | "exits" | "stops" | "rejected">("all");
  if (!snap) return null;
  const orders = snap.orders.filter((o) => {
    if (f === "entries") return ["PROBE", "CONFIRM_ADD", "PRESS"].includes(o.purpose);
    if (f === "exits") return !["PROBE", "CONFIRM_ADD", "PRESS", "BROKER_STOP"].includes(o.purpose);
    if (f === "stops") return o.purpose === "BROKER_STOP";
    if (f === "rejected") return o.status === "REJECTED";
    return true;
  });
  const resting = snap.orders.filter((o) => o.purpose === "BROKER_STOP" && o.status === "OPEN");
  const slips = snap.orders.map((o) => o.slippage_bps).filter((x): x is number => x !== null && x !== undefined);
  const avgSlip = slips.length ? slips.reduce((a, b) => a + b, 0) / slips.length : null;
  const charges = snap.orders.reduce((a, o) => a + (o.charges || 0), 0);
  return (
    <div className="view-enter">
      <h1 className="section-title">Orders</h1>
      <p className="section-sub">Every order is Algo-ID tagged with an idempotent correlation id and capped far below SEBI's 10 orders/sec. Exits always go ahead of entries. Broker-side stops rest at the exchange as the backstop (A5.5).</p>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="kpis">
          <Stat label="Orders (recent)" value={snap.orders.length} />
          <Stat label="Resting broker stops" value={resting.length} hint={`${snap.positions.length} open positions`} />
          <Stat label="Avg slippage vs model" value={avgSlip === null ? "–" : `${avgSlip.toFixed(1)} bps`} />
          <Stat label="Charges (recent)" value={rupees(charges, 2)} hint="STT, exchange, GST, stamp, brokerage" />
          <Stat label="Peak orders/sec" value={`${snap.ops_peak} / ${snap.ceilings.ops_cap}`} hint="system cap < SEBI 10" />
        </div>
      </div>
      <Card title="Order book" right={<Seg value={f} onChange={setF} options={[{ v: "all", label: "All" }, { v: "entries", label: "Entries" }, { v: "exits", label: "Exits" }, { v: "stops", label: "Broker stops" }, { v: "rejected", label: "Rejected" }]} />}>
        {orders.length === 0 ? <div className="empty">No orders.</div> : (
          <div className="table-wrap" style={{ maxHeight: "64vh" }}>
            <table className="t">
              <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Purpose</th><th>Type</th><th className="r">Qty</th><th className="r">Avg / trigger</th><th>Status</th><th className="r">Slip</th><th className="r">Charges</th><th>Algo tag</th></tr></thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.id} title={o.reject_reason || o.id}>
                    <td className="num">{time(o.updated ?? o.created)}</td><td><b>{o.symbol}</b></td>
                    <td><span className={`chip ${o.side === "BUY" ? "chip-accent" : ""}`}>{o.side}</span></td>
                    <td>{o.purpose.replace("_", " ")}</td><td className="muted">{o.type}</td><td className="r num">{o.qty}</td>
                    <td className="r num">{o.status === "FILLED" ? num(o.avg_price) : o.trigger ? `@ ${num(o.trigger)}` : "–"}</td>
                    <td><span className={`chip ${STATUS_CLASS[o.status] ?? ""}`}>{o.status}</span>{o.reject_reason && <span className="muted" style={{ fontSize: 11, marginLeft: 6 }}>{o.reject_reason}</span>}</td>
                    <td className="r num">{o.slippage_bps === null ? "–" : `${o.slippage_bps.toFixed(1)}`}</td>
                    <td className="r num">{o.charges ? rupees(o.charges, 2) : "–"}</td>
                    <td className="mono muted">{o.correlation_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
