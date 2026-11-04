import { useEffect, useState } from "react";
import { act, get } from "../lib/api";
import { time } from "../lib/format";
import { useStore } from "../lib/store";
import { Card, KV, Toggle } from "../components/ui";

const ARB_ROWS = [
  { s: "BOTH_AGREE", sit: "Both healthy and agree", conf: "High", entries: "Allowed", open: "Normal" },
  { s: "DISAGREE", sit: "Both healthy, disagree > max(2 ticks, 5 bps) for > 3 s", conf: "Low on that instrument", entries: "Blocked on it", open: "Managed on primary; alert" },
  { s: "SECONDARY_DOWN", sit: "Dhan healthy, Upstox down", conf: "Capped at 0.8", entries: "Allowed", open: "Normal; alert" },
  { s: "PRIMARY_DOWN", sit: "Dhan down, Upstox healthy", conf: "Low", entries: "Blocked", open: "Monitored via Upstox; exits via Dhan" },
  { s: "BOTH_DOWN", sit: "Both feeds down", conf: "None", entries: "Blocked", open: "Broker-side stops; flatten after 5 min" },
];

const FAULTS = [
  ["primary_down", "Kill primary feed"], ["secondary_down", "Kill secondary feed"], ["both_down", "Kill both feeds"],
  ["disagree", "Feeds disagree on NIFTY"], ["order_api_down", "Order API down"], ["reject_storm", "Reject storm"],
  ["gap_down", "Gap down 1.5%"], ["gap_up", "Gap up 1.5%"], ["clear", "Clear all faults"],
] as const;

const GATES = [
  ["(a) Credentials", "Dhan + Upstox API keys, Discord bot token, static-IP registration, Algo-ID onboarding"],
  ["(b) Paid data / cloud", "Any vendor, data or Mumbai-VM purchase"],
  ["(c) Go-live", "PAPER → LIVE-PILOT, then LIVE-PILOT → LIVE-FULL, via the go-live dossier (B9)"],
  ["(d) Real money per strategy", "Enabling any strategy or the Zero-to-Hero sleeve live"],
  ["(e) Part D", "Any change to config/operator.yaml"],
  ["(f) Failed exit criterion", "Reported honestly after two attempts; standards are never lowered"],
];

export function System() {
  const snap = useStore((s) => s.snap);
  const toast = useStore((s) => s.toast);
  const [rules, setRules] = useState<any>(null);
  const [cfg, setCfg] = useState<any>(null);
  useEffect(() => { get("/api/rules").then(setRules).catch(() => undefined); get("/api/config").then(setCfg).catch(() => undefined); }, []);
  if (!snap) return null;
  const f = snap.feeds;
  const sim = snap.mode === "SIM";
  return (
    <div className="view-enter">
      <h1 className="section-title">System</h1>
      <p className="section-sub">Feeds, failure injection, rule freshness, human gates and the operator configuration. Plumbing and safety come before strategies, and paper comes before live (B0).</p>
      <div className="grid g-12">
        <Card title="Mode" className="span-4">
          <KV k="Mode" v={snap.mode.startsWith("LIVE") ? "LIVE (real orders)" : snap.mode} /><KV k="Performance label" v={snap.label} />
          <KV k="Risk mode" v={snap.risk_mode === "unbounded" ? "unbounded (D-031)" : "bounded (v3.0 spec)"} />
          <KV k="Orders go to" v={snap.broker?.live ? "Dhan (real orders)" : "paper broker (simulated fills)"} />
          {snap.broker?.live && <KV k="Dhan order book" v={snap.broker.health?.error ? `problem: ${snap.broker.health.error}` : `ok, ${snap.broker.health?.polls ?? 0} polls`} />}
          {snap.gates_missing.length > 0 && <KV k="LIVE refused, missing" v={snap.gates_missing.join(", ")} />}
          <div className="row between" style={{ padding: "10px 0" }}>
            <div><div style={{ fontWeight: 500, fontSize: 13 }}>Approve each entry</div><div className="muted" style={{ fontSize: 12 }}>{snap.risk_mode === "unbounded" ? "Off: the system takes every entry that passes the checks. On: each entry waits 60 s for your OK." : "Approve every entry and add, as in LIVE-PILOT (D9)"}</div></div>
            <Toggle on={snap.pilot_approvals} onChange={async (v) => { const r = await act("pilot_approvals", { on: v }); toast(r.ok ? `Pilot approvals ${v ? "ON" : "OFF"}` : r.error); }} label="Pilot approvals" />
          </div>
          {sim && (
            <>
              <label className="label">Simulation speed · {snap.clock.speed}×</label>
              <input type="range" min={1} max={240} step={1} defaultValue={snap.clock.speed} onMouseUp={(e) => act("speed", { speed: +(e.target as HTMLInputElement).value })} onTouchEnd={(e) => act("speed", { speed: +(e.target as HTMLInputElement).value })} aria-label="Simulation speed" />
              <button className="btn btn-secondary btn-sm" style={{ marginTop: 10 }} onClick={() => act("pause", { paused: !snap.clock.paused })}>{snap.clock.paused ? "Resume" : "Pause"} simulation</button>
            </>
          )}
        </Card>

        <Card title="Feeds" sub="Dhan primary · Upstox secondary · never averaged (A5.5)" className="span-8">
          <div className="grid g2" style={{ marginBottom: 12 }}>
            {[f.primary, f.secondary].map((x: any) => (
              <div key={x.name} className="card card-flat" style={{ padding: 14 }}>
                <div className="row between"><b>{x.name}{sim ? " (sim)" : ""}</b>{!sim && x.name === f.secondary.name && !snap.market?.secondary_configured
                  ? <span className="chip chip-warn">not configured</span>
                  : <span className="chip">{x.updates.toLocaleString("en-IN")} updates</span>}</div>
                <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>Latency p50 {x.latency_p50_ms?.toFixed(0) ?? "–"} ms · p95 {x.latency_p95_ms?.toFixed(0) ?? "–"} ms · disconnects {x.disconnects}</div>
              </div>
            ))}
          </div>
          <div className="table-wrap"><table className="t t-wrap">
            <thead><tr><th>Situation</th><th>Data confidence</th><th>New entries</th><th>Open positions</th></tr></thead>
            <tbody>{ARB_ROWS.map((r) => (
              <tr key={r.s} style={r.s === f.situation ? { background: "var(--accent-wash)", fontWeight: 600 } : undefined}>
                <td>{r.s === f.situation ? "▶ " : ""}{r.sit}</td><td>{r.conf}</td><td>{r.entries}</td><td>{r.open}</td>
              </tr>))}</tbody>
          </table></div>
          {f.alerts.length > 0 && <div className="feed" style={{ marginTop: 10 }}>{f.alerts.slice(-5).reverse().map((a, i) => <div className="feed-item" key={i}><span className="ts">{time(a.ts)}</span><span>{a.msg}</span></div>)}</div>}
        </Card>

        {!sim && snap.market && (
          <Card title="Live market data" sub="Dhan REST polling within rate limits · minute bars built from quotes · history backfilled on start" className="span-12">
            <div className="grid g4">
              <KV k="Source" v={snap.market.label} /><KV k="Session" v={snap.market.session} />
              <KV k="Instruments" v={snap.market.instruments} /><KV k="History backfilled" v={`${snap.market.backfill_days} sessions`} />
              <KV k="Quote polls" v={(snap.market.health.polls ?? 0).toLocaleString("en-IN")} /><KV k="Errors" v={snap.market.health.errors ?? 0} />
              <KV k="Last good poll" v={snap.market.health.last_ok ? time(snap.market.health.last_ok) : "–"} /><KV k="Betas estimated from data" v={snap.market.betas_estimated} />
            </div>
            {snap.market.health.last_error && <div className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>Last error: {snap.market.health.last_error}</div>}
            {snap.market.problems.length > 0 && <div className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>Notes: {snap.market.problems.join(" · ")}</div>}
          </Card>
        )}

        {sim && (
          <Card title="Failure injection" sub="Phase 3 exit test: kill each feed, both feeds, and the order API mid-session" className="span-12">
            <div className="row-wrap">
              {FAULTS.map(([k, label]) => <button key={k} className={`btn btn-sm ${k === "clear" ? "btn-primary" : "btn-secondary"}`} onClick={async () => { const r = await act("inject", { fault: k, seconds: 120 }); toast(r.msg ?? r.error); }}>{label}</button>)}
            </div>
            {snap.faults.length > 0 && <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>Recent: {snap.faults.slice(-4).map((x) => x.msg).join(" · ")}</div>}
          </Card>
        )}

        <Card title="Human gates (B0)" sub="The system stops and asks only here" className="span-5">
          {GATES.map(([g, d]) => (
            <div key={g} className="row between" style={{ padding: "8px 0", borderBottom: "1px solid var(--line)", alignItems: "flex-start" }}>
              <div><div style={{ fontWeight: 500, fontSize: 13 }}>{g}</div><div className="muted" style={{ fontSize: 12 }}>{d}</div></div>
              <span className="chip chip-warn">pending</span>
            </div>
          ))}
        </Card>

        <Card title="Market rules freshness" sub={rules ? `${rules.open_items.length} open items (UNVERIFIED or STALE) will be listed in the go-live dossier` : ""} className="span-7">
          <div className="table-wrap" style={{ maxHeight: 420 }}>
            <table className="t">
              <thead><tr><th>Rule</th><th>Value</th><th>Status</th><th>Verified</th><th>Source</th></tr></thead>
              <tbody>{(rules?.rules ?? []).map((r: any) => (
                <tr key={r.name}>
                  <td style={{ fontWeight: 500 }}>{r.name}</td><td className="num">{typeof r.value === "object" ? JSON.stringify(r.value) : String(r.value)}</td>
                  <td><span className={`chip ${r.freshness === "FRESH" ? "chip-up" : r.freshness === "STALE" ? "chip-down" : "chip-warn"}`}>{r.status}</span></td>
                  <td className="num muted">{r.verified_on ?? "–"}</td>
                  <td><a href={r.source_url.startsWith("http") ? r.source_url : undefined} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>{r.source_url.replace(/^https?:\/\//, "").slice(0, 38)}</a></td>
                </tr>))}</tbody>
            </table>
          </div>
        </Card>

        <Card title="Strategy hypothesis cards (A7.2)" sub="Every strategy is a hypothesis. None is validated yet." className="span-12">
          <div className="grid g3">
            {(cfg?.strategies ?? []).map((s: any) => (
              <div key={s.name} className="card card-flat" style={{ padding: 14 }}>
                <div className="row between"><b>{s.name}</b><span className="chip chip-warn">{s.card.validation_status}</span></div>
                <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}><b>Why:</b> {s.card.why}</div>
                <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}><b>Who loses:</b> {s.card.counterparty}</div>
                <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}><b>Fails when:</b> {s.card.fails_when}</div>
                <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}><b>Kill:</b> {s.card.kill_criteria}</div>
              </div>
            ))}
          </div>
        </Card>

        <Card title="Part D: operator configuration" sub="Read-only here. Changes are gate (e), made in config/operator.yaml." className="span-12">
          <pre className="mono" style={{ whiteSpace: "pre-wrap", maxHeight: 360, overflow: "auto", background: "var(--surface-2)", padding: 14, borderRadius: 10, fontSize: 11.5 }}>{cfg && JSON.stringify(cfg.part_d, null, 2)}</pre>
        </Card>
      </div>
    </div>
  );
}
