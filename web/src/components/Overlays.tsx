import { ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { act } from "../lib/api";
import { num, pct, rMult, rupees } from "../lib/format";
import { ROUTES, useStore } from "../lib/store";
import type { Approval, FunnelRecord } from "../lib/types";
import { Checklist, CountdownRing, Drawer, Grade, KV } from "./ui";

export function ApprovalCard({ a, compact = false }: { a: Approval; compact?: boolean }) {
  const toast = useStore((s) => s.toast);
  const [busy, setBusy] = useState(false);
  const s = a.summary;
  const isAdd = a.kind === "CONFIRM_ADD" || a.kind === "PRESS";
  async function answer(ok: boolean) {
    setBusy(true);
    const r = await act("approve", { id: a.id, approved: ok });
    toast(r.ok ? `${a.kind} ${s.symbol}: ${r.result}` : `Not executed: ${r.error}`);
    setBusy(false);
  }
  return (
    <div className="approval">
      <div className="row" style={{ alignItems: "flex-start" }}>
        <CountdownRing seconds={a.seconds_left} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row" style={{ gap: 8 }}>
            <span className="eyebrow" style={{ color: "var(--accent)" }}>Approval · {a.kind.replace("_", " ")}</span>
            {!isAdd && <Grade g={s.grade} />}
            {!isAdd && !s.p_calibrated && <span className="chip chip-warn" title="P(win) is a prior until Phase 5 calibration">UNCALIBRATED</span>}
          </div>
          <div style={{ fontSize: 17, fontWeight: 600, marginTop: 4, letterSpacing: "-0.01em" }}>
            {s.symbol} <span className="muted" style={{ fontWeight: 500, fontSize: 13 }}>{isAdd ? s.stage : `${s.strategy} · ${s.exposure}`}</span>
          </div>
          {isAdd ? (
            <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
              Add {s.qty} @ ~{num(s.price)} · new stop {num(s.new_stop)} · worst case after {rupees(s.worst_case_after)} ≤ plan {rupees(s.planned_risk)} · {s.rule}
            </div>
          ) : (
            <>
              <div className="row-wrap" style={{ marginTop: 8, fontSize: 12.5 }}>
                <span className="pill">Qty {s.qty}{s.full_qty !== s.qty ? ` / ${s.full_qty}` : ""}</span>
                <span className="pill">Entry {num(s.entry)}</span>
                <span className="pill">Stop {num(s.stop)}</span>
                <span className="pill">At risk {rupees(s.risk_rupees)}</span>
                <span className="pill">EV {rMult(s.ev_r)}</span>
                {!compact && <span className="pill">TQS {s.quality}</span>}
              </div>
              {!compact && <div className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>{s.reason}</div>}
            </>
          )}
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => answer(true)}>Approve</button>
            <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => answer(false)}>Deny</button>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: 11 }}>Timeout = skip (safe default)</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function ApprovalDock() {
  const approvals = useStore((s) => s.snap?.approvals ?? []);
  const route = useStore((s) => s.route);
  if (!approvals.length || route === "queue") return null;
  return (
    <div className="approval-dock" aria-live="assertive">
      {approvals.slice(0, 2).map((a) => <ApprovalCard key={a.id} a={a} compact />)}
    </div>
  );
}

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return <div className="toasts" aria-live="polite">{toasts.map((t) => <div key={t.id} className="toast">{t.text}</div>)}</div>;
}

const COMMANDS = ["/status", "/gear", "/positions", "/pnl", "/queue", "/target month 8%", "/target day 1500", "/lock day", "/help"];

export function CommandPalette() {
  const { paletteOpen, setPalette, snap, select, setRoute } = useStore();
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const [reply, setReply] = useState("");
  const inp = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette(!useStore.getState().paletteOpen); }
      if (e.key === "Escape") setPalette(false);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [setPalette]);
  useEffect(() => { if (paletteOpen) { setQ(""); setReply(""); setIdx(0); setTimeout(() => inp.current?.focus(), 20); } }, [paletteOpen]);
  const items = useMemo(() => {
    const s = q.trim().toLowerCase();
    const out: { label: string; hint: string; run: () => void }[] = [];
    if (s.startsWith("/")) {
      out.push({ label: q.trim(), hint: "run command", run: () => run(q.trim()) });
      COMMANDS.filter((c) => c.startsWith(s) && c !== s).forEach((c) => out.push({ label: c, hint: "command", run: () => setQ(c + " ") }));
      return out;
    }
    ROUTES.filter((r) => !s || r.label.toLowerCase().includes(s)).forEach((r) => out.push({ label: r.label, hint: "go to", run: () => { setRoute(r.id); setPalette(false); } }));
    (snap?.watch ?? []).filter((w) => s && (w.symbol.toLowerCase().includes(s) || w.name.toLowerCase().includes(s))).slice(0, 8)
      .forEach((w) => out.push({ label: `${w.symbol} · ${num(w.ltp)}`, hint: w.name, run: () => { select(w.key, "terminal"); setPalette(false); } }));
    if (!s) COMMANDS.slice(0, 5).forEach((c) => out.push({ label: c, hint: "command", run: () => run(c) }));
    return out;
  }, [q, snap?.watch]);
  async function run(text: string) {
    const r = await act("command", { text });
    setReply(r.reply ?? JSON.stringify(r));
  }
  if (!paletteOpen) return null;
  return (
    <>
      <div className="scrim" onClick={() => setPalette(false)} />
      <div className="modal" role="dialog" aria-label="Command palette">
        <input ref={inp} className="palette-input" placeholder="Search instruments, pages, or type a /command…" value={q}
          onChange={(e) => { setQ(e.target.value); setIdx(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setIdx((i) => Math.min(items.length - 1, i + 1)); }
            if (e.key === "ArrowUp") { e.preventDefault(); setIdx((i) => Math.max(0, i - 1)); }
            if (e.key === "Enter" && items[idx]) items[idx].run();
          }} />
        <div className="palette-list">
          {items.map((it, i) => (
            <div key={it.label + i} className={`palette-item ${i === idx ? "on" : ""}`} onMouseEnter={() => setIdx(i)} onClick={it.run}>
              <span style={{ fontWeight: 500 }}>{it.label}</span><span className="faint" style={{ fontSize: 12 }}>{it.hint}</span>
            </div>
          ))}
          {!items.length && <div className="empty">Nothing matches.</div>}
        </div>
        {reply && <div className="palette-reply">{reply}</div>}
        <div className="row between" style={{ padding: "8px 16px", borderTop: "1px solid var(--line)", fontSize: 11 }}>
          <span className="faint"><kbd>↑</kbd> <kbd>↓</kbd> move · <kbd>Enter</kbd> run · <kbd>Esc</kbd> close</span>
          <span className="faint row" style={{ gap: 4 }}><ShieldCheck size={12} /> No command can increase risk</span>
        </div>
      </div>
    </>
  );
}

/** Full A18 decision trail for one candidate: EV, weakest-link confidence, confirmations, gates. */
export function DecisionDrawer({ rec, onClose }: { rec: FunnelRecord | null; onClose: () => void }) {
  if (!rec) return <Drawer open={false} onClose={onClose}>{null}</Drawer>;
  const c = rec.confidence;
  return (
    <Drawer open onClose={onClose}>
      <div className="eyebrow">{rec.strategy} · {rec.segment.replace("_", " ")}</div>
      <h2 style={{ margin: "6px 0 2px", fontSize: 24, letterSpacing: "-0.02em" }}>{rec.symbol}</h2>
      <div className="row-wrap" style={{ margin: "8px 0 14px" }}>
        <Grade g={rec.grade} /><span className="chip">{rec.stage}</span><span className="chip chip-accent">{rec.status}</span>
        <span className="chip">{rec.exposure}</span>{rec.is_reentry && <span className="chip chip-warn">RE-ENTRY</span>}
        {rec.sleeve && <span className="chip chip-warn">SLEEVE</span>}
      </div>
      <p className="muted" style={{ fontSize: 13 }}>{rec.explanation}</p>
      <div className="banner" style={{ fontSize: 12.5 }}>{rec.reason}</div>

      <h4 className="card-title" style={{ margin: "18px 0 8px" }}>Expected value (A9.6)</h4>
      {rec.ev ? (
        <>
          <KV k="P(win)" v={`${pct(rec.ev.p_win, 1)} ${rec.p_calibrated ? "(calibrated)" : "(prior, UNCALIBRATED)"}`} />
          <KV k="Expected win" v={rMult(rec.ev.win_r)} />
          <KV k="Expected loss (incl. gap allowance)" v={rMult(-rec.ev.loss_r)} />
          <KV k="Costs (charges + slippage)" v={rMult(-rec.ev.cost_r)} />
          <KV k="Net EV" v={<b>{rMult(rec.ev.net_r)}</b>} />
          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>{rec.ev.reason}</div>
        </>
      ) : <div className="muted">Not graded (dropped at {rec.stage}).</div>}

      {c && (
        <>
          <h4 className="card-title" style={{ margin: "18px 0 8px" }}>Confidence: capped by the weakest link (A9.5)</h4>
          {(["model", "data", "regime", "execution"] as const).map((k) => (
            <div key={k} style={{ marginBottom: 8 }}>
              <div className="row between" style={{ fontSize: 12 }}><span className={k === c.weakest ? "" : "muted"} style={{ fontWeight: k === c.weakest ? 700 : 500 }}>{k}{k === c.weakest ? " · weakest" : ""}</span><span className="num">{c[k].toFixed(2)}</span></div>
              <div className="meter" style={{ height: 6 }}><span style={{ width: `${c[k] * 100}%`, background: k === c.weakest ? "var(--g-guard)" : "var(--accent)" }} /></div>
            </div>
          ))}
          <KV k="Effective confidence" v={<b>{c.effective.toFixed(2)}</b>} />
        </>
      )}

      <h4 className="card-title" style={{ margin: "18px 0 8px" }}>Confirmations: {rec.independent_confirmations} independent (needs 2)</h4>
      <Checklist checks={rec.confirmations.map((x) => ({ name: `${x.name} [${x.factor}]`, ok: x.fired, detail: x.detail }))} />

      {rec.sizing && (
        <>
          <h4 className="card-title" style={{ margin: "18px 0 8px" }}>Conviction sizing (AGG-1)</h4>
          <KV k="Planned risk" v={rupees(rec.sizing.planned_risk)} />
          <KV k="Staging" v={rec.sizing.staging_mode} />
          <KV k="Probe / full qty" v={`${rec.sizing.probe_qty} / ${rec.sizing.full_qty}`} />
          <KV k="Risk at full size" v={rupees(rec.sizing.risk_at_full)} />
          {Object.entries(rec.sizing.adjustments ?? {}).map(([k, v]) => <KV key={k} k={`adjust · ${k}`} v={`×${Number(v).toFixed(2)}`} />)}
        </>
      )}
      {rec.plan && (
        <>
          <h4 className="card-title" style={{ margin: "18px 0 8px" }}>Plan, computed before entry (S5)</h4>
          <KV k="Stop / broker-side stop" v={`${num(rec.plan.stop)} / ${num(rec.plan.broker_stop)}`} />
          <KV k="Confirm-add trigger" v={num(rec.plan.confirm_trigger)} />
          <KV k="Harvest" v={`${pct(rec.plan.harvest_fraction, 0)} at +${rec.plan.harvest_at_r}R`} />
        </>
      )}
      {rec.verdict && (
        <>
          <h4 className="card-title" style={{ margin: "18px 0 8px" }}>The Monk's checklist (S6)</h4>
          <Checklist checks={rec.verdict.checks} />
        </>
      )}
    </Drawer>
  );
}
