import { ChevronDown, ChevronUp } from "lucide-react";
import { useState } from "react";
import { act } from "../lib/api";
import { hm, num, rMult, rupees, signedRupees, time, title } from "../lib/format";
import { useStore } from "../lib/store";
import type { Position } from "../lib/types";
import { RBar, StageStepper, TradeBar } from "../components/Signature";
import { Card, Delta, Grade, Label, Stat } from "../components/ui";

function PositionCard({ p }: { p: Position }) {
  const toast = useStore((s) => s.toast);
  const ub = useStore((s) => s.snap?.risk_mode === "unbounded");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  async function run(kind: "close" | "half" | "trail") {
    setBusy(kind);
    const r = kind === "trail" ? await act("trail", { position_id: p.id }) : await act("close", { position_id: p.id, fraction: kind === "half" ? 0.5 : 1 });
    toast(r.ok ? `${p.symbol}: ${kind === "trail" ? `stop → ${num(r.stop)}` : kind === "half" ? "closing 50%" : "closing"}` : `${p.symbol}: ${r.error}`);
    setBusy(null);
  }
  return (
    <div className="card">
      <div className="pos-card">
        <div>
          <div className="row" style={{ gap: 8 }}>
            <span className="pos-sym">{p.symbol}</span><Grade g={p.grade} />
            {p.is_reentry && <span className="chip chip-warn">RE-ENTRY</span>}{p.sleeve && <span className="chip chip-warn">SLEEVE</span>}
          </div>
          <div className="muted" style={{ fontSize: 12.5, marginTop: 2 }}>{title(p.strategy)} · {p.exposure}{ub ? "" : ` · ${p.staging_mode} staging`} · since {hm(p.opened_at)}</div>
          <div className="row-wrap" style={{ marginTop: 8 }}>
            <span className="pill num">Qty {p.qty}{p.full_qty_target > p.qty ? ` / ${p.full_qty_target}` : ""}</span>
            <span className="pill num">Avg {num(p.avg_entry)}</span>
            <span className="pill num">LTP {num(p.last_price)}</span>
          </div>
        </div>
        <div className="stack" style={{ gap: 14 }}>
          {ub ? <TradeBar direction={p.direction} initialStop={p.initial_stop} stop={p.stop} entry={p.avg_entry} target={p.target} price={p.last_price} />
            : <StageStepper stage={p.stage} history={p.stage_history} />}
          <RBar r={p.r_multiple} mfe={p.mfe_r} />
        </div>
        <div className="stack" style={{ gap: 4 }}>
          <div className="pos-pnl"><Delta v={p.net_pnl}>{signedRupees(p.net_pnl)}</Delta></div>
          <div className="muted num" style={{ fontSize: 12 }}>{rMult(p.r_multiple)} of {rupees(p.planned_risk)} {ub ? "at the stop" : "plan"} · charges {rupees(p.charges, 2)}</div>
          <div className="num" style={{ fontSize: 12.5 }}>Stop-loss <b>{num(p.stop)}</b> <span className="muted">· broker {num(p.broker_stop)}</span></div>
          {ub && p.target > 0 && <div className="num" style={{ fontSize: 12.5 }}>Target <b>{num(p.target)}</b></div>}
          <div className="muted" style={{ fontSize: 12 }}>Open risk {rupees(p.open_risk)}{p.open_risk === 0 ? " (profit locked)" : ""}</div>
        </div>
        <div className="stack" style={{ gap: 8, minWidth: 150 }}>
          <button className="btn btn-danger btn-sm" disabled={!!busy} onClick={() => run("close")}>Close now</button>
          <button className="btn btn-secondary btn-sm" disabled={!!busy} onClick={() => run("half")}>Close 50%</button>
          <button className="btn btn-secondary btn-sm" disabled={!!busy} onClick={() => run("trail")}>Trail tighter</button>
        </div>
      </div>
      <button className="btn btn-sm" style={{ background: "transparent", paddingLeft: 0, marginTop: 8, color: "var(--muted)" }} onClick={() => setOpen(!open)}>
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />} Legs and stage history
      </button>
      {open && (
        <div className="grid g2" style={{ marginTop: 6 }}>
          <table className="t"><thead><tr><th>Time</th><th>Leg</th><th className="r">Qty</th><th className="r">Price</th></tr></thead>
            <tbody>
              {p.legs.map((l, i) => <tr key={i}><td className="num">{time(l.ts)}</td><td>{l.stage}</td><td className="r num">{l.qty}</td><td className="r num">{num(l.price)}</td></tr>)}
              {p.closed_legs.map((l, i) => <tr key={`c${i}`}><td className="num">{time(l.ts)}</td><td>exit · {l.reason}</td><td className="r num">−{l.qty}</td><td className="r num">{num(l.price)}</td></tr>)}
            </tbody></table>
          <div className="feed">
            {p.stage_history.map((h, i) => <div className="feed-item" key={i}><span className="ts">{time(h.ts)}</span><span><span className="k">{h.to}</span>{h.rule ?? h.note ?? h.reason ?? (h.qty ? `${h.qty} @ ${num(h.price ?? 0)}` : "")}</span></div>)}
          </div>
        </div>
      )}
    </div>
  );
}

export function Positions() {
  const snap = useStore((s) => s.snap);
  if (!snap) return null;
  const open = snap.positions;
  const unreal = open.reduce((s, p) => s + p.unrealized, 0);
  const realizedToday = snap.closed.filter((t) => t.closed.slice(0, 10) === snap.clock.day).reduce((s, t) => s + t.net, 0);
  const ub = snap.risk_mode === "unbounded";
  return (
    <div className="view-enter">
      <h1 className="section-title">Positions</h1>
      <p className="section-sub">You can always close, reduce or tighten, instantly and without approval. You can never open, add, enlarge or widen a stop (D6). Every manual exit gets a counterfactual.</p>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="kpis">
          <Stat label="Open P&L" value={<Delta v={unreal}>{signedRupees(unreal)}</Delta>} />
          <Stat label="Realised today" value={<Delta v={realizedToday}>{signedRupees(realizedToday)}</Delta>} hint={<Label text={snap.label} />} />
          {ub ? <Stat label="In trades" value={rupees(snap.capital.deployed)} hint={`${rupees(snap.capital.free)} free`} />
            : <Stat label="Open risk" value={rupees(snap.risk.open_risk)} hint={`limit ${rupees(snap.risk.open_risk_limit)}`} />}
          <Stat label="Positions" value={ub ? open.length : `${open.length} / ${snap.risk.max_positions}`} />
        </div>
      </div>
      <div className="stack" style={{ gap: 14, marginBottom: 24 }}>
        {open.length === 0 ? <Card><div className="empty">No open positions. Cash is a position.</div></Card> : open.map((p) => <PositionCard key={p.id} p={p} />)}
        {open.length > 1 && <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn btn-danger" onClick={() => act("close_all")}>Close all</button></div>}
      </div>
      <Card title="Closed trades" right={<Label text={snap.label} />}>
        {snap.closed.length === 0 ? <div className="empty">No closed trades yet.</div> : (
          <div className="table-wrap" style={{ maxHeight: 460 }}>
            <table className="t">
              <thead><tr><th>Closed</th><th>Symbol</th><th>Strategy</th>{!ub && <th>Grade</th>}{!ub && <th>Stages</th>}<th>Exit</th><th className="r">Charges</th><th className="r">R</th><th className="r">Net</th></tr></thead>
              <tbody>
                {snap.closed.map((t) => (
                  <tr key={t.id}>
                    <td className="num">{t.closed.slice(5, 10)} {time(t.closed)}</td><td><b>{t.symbol}</b></td><td>{title(t.strategy)}</td>{!ub && <td><Grade g={t.grade} /></td>}
                    {!ub && <td className="muted" style={{ fontSize: 12 }}>{t.stages.filter((s) => s !== "EXIT").join(" → ")}</td>}<td>{t.reason}</td>
                    <td className="r num">{rupees(t.charges, 2)}</td><td className="r num"><Delta v={t.r}>{rMult(t.r)}</Delta></td><td className="r num"><Delta v={t.net}>{signedRupees(t.net)}</Delta></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {snap.manual_exits.length > 0 && (
        <Card title="Manual exits vs system exits (D6 counterfactual)" className="" sub="What the system's own exit would have produced">
          <table className="t"><thead><tr><th>Symbol</th><th className="r">Manual</th><th className="r">System</th><th className="r">You added / cost</th></tr></thead>
            <tbody>{snap.manual_exits.map((m, i) => <tr key={i}><td>{m.symbol}</td><td className="r num">{signedRupees(m.manual_pnl)}</td><td className="r num">{signedRupees(m.system_pnl)}</td><td className="r num"><Delta v={m.manual_minus_system}>{signedRupees(m.manual_minus_system)}</Delta></td></tr>)}</tbody></table>
        </Card>
      )}
    </div>
  );
}
