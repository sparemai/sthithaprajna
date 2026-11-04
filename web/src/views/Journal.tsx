import { useEffect, useState } from "react";
import { get } from "../lib/api";
import { pct, rMult, signedRupees, time } from "../lib/format";
import { useStore } from "../lib/store";
import { EquityChart } from "../components/charts";
import { Card, Delta, Drawer, Label, Seg } from "../components/ui";

export function Journal() {
  const snap = useStore((s) => s.snap);
  const [j, setJ] = useState<any>(null);
  const [eq, setEq] = useState<any>(null);
  const [decs, setDecs] = useState<any[]>([]);
  const [action, setAction] = useState<"ALL" | "ENTER" | "EXIT" | "NO_TRADE">("ALL");
  const [open, setOpen] = useState<any>(null);
  const day = snap?.clock.day;
  useEffect(() => {
    get("/api/journal").then(setJ).catch(() => undefined);
    get("/api/equity").then(setEq).catch(() => undefined);
  }, [day, snap?.closed.length]);
  useEffect(() => { get(`/api/decisions?limit=300${action !== "ALL" ? `&action=${action}` : ""}`).then(setDecs).catch(() => undefined); }, [action, snap?.clock.now.slice(0, 16)]);
  if (!snap) return null;
  const manual = (j?.manual_exits ?? []).reduce((a: number, m: any) => a + m.manual_minus_system, 0);
  return (
    <div className="view-enter">
      <h1 className="section-title">Journal</h1>
      <p className="section-sub">Svadhyaya, self-study. Each day is judged on process first and P&amp;L second (A15). Every proposed or rejected trade leaves a decision object (A18).</p>
      <div className="grid g-12">
        <Card title="Equity" right={<Label text={snap.label} />} className="span-8">
          {eq?.curve?.length > 1 ? <EquityChart points={eq.curve} height={260} baseline={snap.account.starting} /> : <div className="empty">The curve appears as the session runs.</div>}
        </Card>
        <Card title="Manual exits vs the system" sub="Monthly report line (D6)" className="span-4">
          <div style={{ fontSize: 28, fontWeight: 600, letterSpacing: "-0.02em" }}><Delta v={manual}>{signedRupees(manual)}</Delta></div>
          <div className="muted" style={{ fontSize: 13 }}>Your manual exits have {manual >= 0 ? "added" : "cost"} this versus the system's own exits, over {(j?.manual_exits ?? []).length} closed counterfactuals.</div>
          <div style={{ marginTop: 16 }}>
            <div className="card-title" style={{ fontSize: 13 }}>Calibration (A9.4)</div>
            <div className="muted" style={{ fontSize: 12.5 }}>{j?.calibration?.status ?? "–"} · n={j?.calibration?.n ?? 0}{j?.calibration?.ece != null ? ` · ECE ${j.calibration.ece.toFixed(3)}` : ""}. A 70% prediction must win about 70% of the time before anything is trusted.</div>
          </div>
        </Card>
        <div className="span-12 grid g3">
          {(j?.days ?? []).slice(0, 6).map((d: any) => (
            <div key={d.date} className="card">
              <div className="row between"><div className="eyebrow">{d.date}</div><Label text={d.label} /></div>
              <div className="row" style={{ alignItems: "baseline", gap: 14, margin: "8px 0" }}>
                <div><div className="muted" style={{ fontSize: 11 }}>Process</div><div style={{ fontSize: 30, fontWeight: 600, letterSpacing: "-0.02em" }}>{pct(d.process.score, 0)}</div></div>
                <div><div className="muted" style={{ fontSize: 11 }}>P&amp;L</div><div style={{ fontSize: 18, fontWeight: 600 }}><Delta v={d.pnl}>{signedRupees(d.pnl)}</Delta></div></div>
              </div>
              <div className="row-wrap" style={{ marginBottom: 8 }}>
                <span className="pill">{d.trades} trades · {d.wins} wins</span><span className="pill">{d.gear}</span><span className="pill">{String(d.regime).replace("_", " ")}</span>
                {d.best !== null && <span className="pill">best {rMult(d.best)}</span>}
              </div>
              <div className="muted" style={{ fontSize: 12.5 }}>{d.reflection}</div>
              <div className="faint" style={{ fontSize: 11, marginTop: 8 }}>{Object.entries(d.process.items).map(([k, v]: any) => `${k.replace(/_/g, " ")} ${pct(v, 0)}`).join(" · ")}</div>
            </div>
          ))}
          {!(j?.days ?? []).length && <div className="card"><div className="empty">The first journal entry is written at the close.</div></div>}
        </div>
        <Card title="Decision log" sub="A18 decision objects, newest first" className="span-12"
          right={<Seg value={action} onChange={setAction} options={[{ v: "ALL", label: "All" }, { v: "ENTER", label: "Enter" }, { v: "EXIT", label: "Exit" }, { v: "NO_TRADE", label: "No trade" }]} />}>
          <div className="table-wrap" style={{ maxHeight: 460 }}>
            <table className="t">
              <thead><tr><th>Time</th><th>Action</th><th>Symbol</th><th>Strategy</th><th>Grade</th><th>Stage</th><th className="r">EV</th><th>Risk</th><th>Explanation</th></tr></thead>
              <tbody>
                {decs.map((d) => (
                  <tr key={d.decision_id} className="clickable" onClick={() => setOpen(d)}>
                    <td className="num">{time(d.timestamp)}</td><td><span className={`chip ${d.action === "ENTER" ? "chip-accent" : d.action === "EXIT" ? "" : "chip-warn"}`}>{d.action}</span></td>
                    <td><b>{d.symbol}</b></td><td>{d.strategy}</td><td>{d.grade}</td><td className="muted">{d.position_stage || d.pickup_stage_reached}</td>
                    <td className="r num">{d.expected_value ? rMult(d.expected_value) : "–"}</td><td>{d.risk_approval || "–"}</td>
                    <td className="muted" style={{ whiteSpace: "normal", minWidth: 260 }}>{d.rejection_reason || d.explanation}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
      <Drawer open={!!open} onClose={() => setOpen(null)}>
        <div className="eyebrow">Decision object · A18</div>
        <h2 style={{ margin: "6px 0 12px", fontSize: 20 }}>{open?.action} {open?.symbol}</h2>
        <pre className="mono" style={{ whiteSpace: "pre-wrap", background: "var(--surface-2)", padding: 14, borderRadius: 10, fontSize: 11.5 }}>{open && JSON.stringify(open, null, 2)}</pre>
      </Drawer>
    </div>
  );
}
