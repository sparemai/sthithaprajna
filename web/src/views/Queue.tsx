import { useMemo, useState } from "react";
import { rMult, title } from "../lib/format";
import { useStore } from "../lib/store";
import type { FunnelRecord } from "../lib/types";
import { ApprovalCard, DecisionDrawer } from "../components/Overlays";
import { Card, Grade, Seg } from "../components/ui";

const STAGE_LABEL: Record<string, string> = {
  S1_SCAN: "Scan", S2_QUALIFY: "Qualify", S3_GRADE: "Grade", S4_CONFIRM: "Confirm", S5_ARM: "Arm", S6_RISK: "Risk", S7_PROBE: "Probe",
};

export function Queue() {
  const snap = useStore((s) => s.snap);
  const [rec, setRec] = useState<FunnelRecord | null>(null);
  const [f, setF] = useState<"live" | "all" | "dropped">("live");
  const records = useMemo(() => (snap?.funnel.records ?? []).filter((r) =>
    f === "all" ? true : f === "live" ? ["ACTIVE", "ARMED", "READY", "PENDING_APPROVAL", "PROBED"].includes(r.status) : ["DROPPED", "REJECTED"].includes(r.status)), [snap?.funnel.records, f]);
  if (!snap) return null;
  const counts = snap.funnel.counts;
  const max = Math.max(1, ...Object.values(counts));
  const drops = Object.entries(snap.funnel.drop_reasons).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const maxDrop = Math.max(1, ...drops.map((d) => d[1]));
  const live = rec ? snap.funnel.records.find((r) => r.id === rec.id) ?? rec : null;
  return (
    <div className="view-enter">
      <h1 className="section-title">Opportunity queue</h1>
      <p className="section-sub">Every candidate passes S1 to S7 in order. The Opportunity Maximiser only chooses among candidates that have already passed every gate. It never lowers a threshold to create a trade (AGG-1).</p>

      {snap.approvals.length > 0 && <div className="grid g2" style={{ marginBottom: 16 }}>{snap.approvals.map((a) => <ApprovalCard key={a.id} a={a} />)}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, minmax(0,1fr))", gap: 10 }}>
          {Object.entries(counts).map(([st, n], i) => (
            <div key={st}>
              <div className="eyebrow">S{i + 1} · {STAGE_LABEL[st]}</div>
              <div style={{ fontSize: 28, fontWeight: 600, letterSpacing: "-0.02em", margin: "4px 0 8px" }}>{n}</div>
              <div className="meter" style={{ height: 6 }}><span style={{ width: `${(n / max) * 100}%` }} /></div>
            </div>
          ))}
        </div>
      </div>

      <div className="grid g-12">
        <Card title="Candidates" sub="Select a row to see the full decision trail (A18)" className="span-8"
          right={<Seg value={f} onChange={setF} options={[{ v: "live", label: "Live" }, { v: "dropped", label: "Dropped" }, { v: "all", label: "All" }]} />}>
          {records.length === 0 ? <div className="empty">{f === "live" ? "Nothing in play. No setup qualifies right now, and cash is a position." : "Nothing here yet."}</div> : (
            <div className="table-wrap" style={{ maxHeight: "60vh" }}>
              <table className="t">
                <thead><tr><th>Instrument</th><th>Strategy</th><th>Bias</th><th>Grade</th><th>Stage</th><th className="r">EV</th><th className="r">TQS</th><th className="r">Conf</th><th>Status · reason</th></tr></thead>
                <tbody>
                  {records.map((r) => (
                    <tr key={r.id} className="clickable" onClick={() => setRec(r)}>
                      <td><b>{r.symbol}</b>{r.is_reentry && <span className="chip chip-warn" style={{ marginLeft: 6 }}>RE</span>}</td>
                      <td>{title(r.strategy)}</td><td>{r.exposure}</td><td><Grade g={r.grade} /></td>
                      <td><span className="chip">{STAGE_LABEL[r.stage] ?? r.stage}</span></td>
                      <td className="r num">{r.ev ? rMult(r.ev.net_r) : "–"}</td>
                      <td className="r num">{r.quality?.toFixed(0) ?? "–"}</td>
                      <td className="r num" title={r.confidence ? `weakest: ${r.confidence.weakest}` : ""}>{r.confidence?.effective.toFixed(2) ?? "–"}</td>
                      <td style={{ whiteSpace: "normal", minWidth: 220 }}><span className="chip chip-accent" style={{ marginRight: 6 }}>{r.status}</span><span className="muted" style={{ fontSize: 12 }}>{r.reason}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        <div className="span-4 stack" style={{ gap: 16 }}>
          <Card title="Why candidates dropped" sub="Today, by blocking rule">
            {drops.length === 0 ? <div className="muted">No drops yet.</div> : (
              <div className="funnel">
                {drops.map(([k, n]) => (
                  <div className="f-row" key={k} style={{ gridTemplateColumns: "140px 1fr 36px" }}>
                    <span className="f-name" title={k}>{k.replace(/_/g, " ")}</span>
                    <div className="f-track"><div className="f-bar" style={{ width: `${(n / maxDrop) * 100}%` }} /></div>
                    <span className="f-val">{n}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>
          <Card title="Missed A/A+ by stage" sub="Diagnosis only: controls are never weakened because a missed trade later worked (A13)">
            {Object.keys(snap.kpis.missed_a_by_stage ?? {}).length === 0 ? <div className="muted">None today.</div> :
              Object.entries(snap.kpis.missed_a_by_stage).map(([k, v]) => <div key={k} className="row between" style={{ padding: "4px 0" }}><span>{STAGE_LABEL[k] ?? k}</span><b>{String(v)}</b></div>)}
          </Card>
          <Card title="Grading bands" sub={snap.tqs_status}>
            <div className="stack" style={{ gap: 6, fontSize: 12.5 }}>
              <div className="row"><Grade g="A+" /><span>Top 10% quality, EV ≥ 0.30R, confidence ≥ 0.70 · ×1.2</span></div>
              <div className="row"><Grade g="A" /><span>Top 30% quality, EV ≥ 0.20R · ×1.0</span></div>
              <div className="row"><Grade g="B" /><span>Passes the margin of safety · ×0.5, probe only</span></div>
              <div className="row"><Grade g="C" /><span>Below margin of safety: no trade</span></div>
            </div>
          </Card>
        </div>
      </div>
      <DecisionDrawer rec={live} onClose={() => setRec(null)} />
    </div>
  );
}
