import { CircleCheck, Lock, OctagonX, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { act, get, post } from "../lib/api";
import { GEAR_COLOR, hm, pct, rupees, signedRupees, time } from "../lib/format";
import { useStore } from "../lib/store";
import { Card, Delta, KV, Label, Meter } from "../components/ui";

function NumField({ label, value, onChange, step = 0.01, pctOf100 = false }:
    { label: string; value: number; onChange: (v: number) => void; step?: number; pctOf100?: boolean }) {
  return (
    <label className="row" style={{ justifyContent: "space-between", fontSize: 13, gap: 8 }}>
      <span className="muted">{label}</span>
      <input className="input num" type="number" step={step} value={pctOf100 ? +(value * 100).toFixed(3) : value}
        onChange={(e) => onChange(pctOf100 ? (+e.target.value || 0) / 100 : (+e.target.value || 0))}
        style={{ width: 100 }} />
    </label>
  );
}

function PartDEditor() {
  const toast = useStore((s) => s.toast);
  const [cfg, setCfg] = useState<any>(null);
  const [staging, setStaging] = useState<any>(null);
  const [limits, setLimits] = useState<any>(null);
  const [sleeve, setSleeve] = useState<any>(null);
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    get("/api/config").then((c) => {
      setCfg(c);
      setStaging({ ...c.part_d.staging });
      setLimits({ ...c.part_d.limits });
      setSleeve({ ...c.part_d.sleeve });
    });
  }, []);

  async function save() {
    setSaving(true);
    setNote(null);
    try {
      const r = await post("/api/operator/config", { staging, limits, sleeve });
      if (r.ok) {
        setNote(r.note);
        toast("Part D saved — restart the server to apply it");
      } else {
        setNote(r.error);
        toast(`Rejected: ${r.error}`);
      }
    } finally {
      setSaving(false);
    }
  }

  if (!cfg || !staging || !limits || !sleeve) return null;
  const c = cfg.ceilings;
  return (
    <Card title="Edit risk settings (Part D)" sub="D-044: validated against the hard ceilings below before anything is saved; nothing here can loosen them" className="span-8">
      <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
        Hard ceilings (never editable, code-only): {pct(c.max_risk_per_trade, 1)} per trade · {pct(c.max_open_risk, 1)} open risk ·
        halt at {pct(c.halt_drawdown, 0)} drawdown · ruin floor max(₹{c.ruin_floor_abs_min.toLocaleString("en-IN")}, {pct(c.ruin_floor_pct_min, 0)} of peak).
      </div>
      <div className="grid g3" style={{ gap: 16 }}>
        <div className="stack" style={{ gap: 8 }}>
          <div className="eyebrow">Profit-lock tiers (D-042)</div>
          <NumField label="Breakeven at (R)" value={staging.breakeven_at_r ?? 0} onChange={(v) => setStaging({ ...staging, breakeven_at_r: v })} />
          <NumField label="Early lock at (R)" value={staging.early_lock_at_r ?? 0} onChange={(v) => setStaging({ ...staging, early_lock_at_r: v })} />
          <NumField label="Early lock fraction" value={staging.early_lock_fraction ?? 0} pctOf100 onChange={(v) => setStaging({ ...staging, early_lock_fraction: v })} />
          <NumField label="Harvest at (R)" value={staging.harvest_at_r ?? 0} onChange={(v) => setStaging({ ...staging, harvest_at_r: v })} />
          <NumField label="Harvest fraction" value={staging.harvest_fraction ?? 0} pctOf100 onChange={(v) => setStaging({ ...staging, harvest_fraction: v })} />
          <div className="eyebrow" style={{ marginTop: 6 }}>On the contract's own expiry day (D-045)</div>
          <NumField label="Breakeven at (R)" value={staging.expiry_breakeven_at_r ?? 0} onChange={(v) => setStaging({ ...staging, expiry_breakeven_at_r: v })} />
          <NumField label="Early lock at (R)" value={staging.expiry_early_lock_at_r ?? 0} onChange={(v) => setStaging({ ...staging, expiry_early_lock_at_r: v })} />
        </div>
        <div className="stack" style={{ gap: 8 }}>
          <div className="eyebrow">Loss limits (of equity)</div>
          <NumField label="Daily" value={limits.daily_loss ?? 0} pctOf100 onChange={(v) => setLimits({ ...limits, daily_loss: v })} />
          <NumField label="Weekly" value={limits.weekly_loss ?? 0} pctOf100 onChange={(v) => setLimits({ ...limits, weekly_loss: v })} />
          <NumField label="Monthly" value={limits.monthly_loss ?? 0} pctOf100 onChange={(v) => setLimits({ ...limits, monthly_loss: v })} />
        </div>
        <div className="stack" style={{ gap: 8 }}>
          <div className="eyebrow">Zero-to-Hero sleeve</div>
          <NumField label="Per expiry" value={sleeve.per_expiry ?? 0} pctOf100 onChange={(v) => setSleeve({ ...sleeve, per_expiry: v })} />
          <NumField label="Per week" value={sleeve.per_week ?? 0} pctOf100 onChange={(v) => setSleeve({ ...sleeve, per_week: v })} />
          <NumField label="Per month" value={sleeve.per_month ?? 0} pctOf100 onChange={(v) => setSleeve({ ...sleeve, per_month: v })} />
          <NumField label="Kill switch (cum. loss)" value={sleeve.kill_cumulative_loss ?? 0} pctOf100 onChange={(v) => setSleeve({ ...sleeve, kill_cumulative_loss: v })} />
        </div>
      </div>
      <div className="row" style={{ marginTop: 14, gap: 10 }}>
        <button className="btn btn-primary" disabled={saving} onClick={save}>{saving ? "Saving…" : "Save to config/operator.yaml"}</button>
      </div>
      {note && <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>{note}</div>}
    </Card>
  );
}

function KillCard() {
  const snap = useStore((s) => s.snap);
  const toast = useStore((s) => s.toast);
  const [reviewed, setReviewed] = useState(false);
  if (!snap) return null;
  return (
    <Card title="Kill switch" sub={snap.broker?.live ? "Flattens every position at Dhan and halts new entries" : "One step: flatten everything and halt"} className="span-4">
      <div className="stack" style={{ gap: 12 }}>
        {snap.risk.kill_switch ? (
          <>
            <div className="banner crit">Kill switch is engaged.</div>
            <label className="row" style={{ fontSize: 13 }}><input type="checkbox" checked={reviewed} onChange={(x) => setReviewed(x.target.checked)} /> I have reviewed what happened</label>
            <button className="btn btn-primary" disabled={!reviewed} onClick={async () => { const x = await act("resume", { reviewed }); toast(x.ok ? x.result.join("; ") : x.error); }}>Resume</button>
          </>
        ) : (
          <button className="btn btn-kill" style={{ height: 52, fontSize: 14 }} onClick={async () => { if (confirm("Flatten every position and halt new entries?")) { await act("kill"); toast("KILL SWITCH engaged"); } }}>
            <OctagonX size={18} /> KILL: FLATTEN AND HALT
          </button>
        )}
        <div className="muted" style={{ fontSize: 12.5 }}>{snap.broker?.live ? "If this terminal is unreachable, close positions in the Dhan app: Sthitaprajna reconciles with Dhan and cancels its own stop-losses." : "Stops and exits never wait for approval."}</div>
      </div>
    </Card>
  );
}

function MonkUnbounded() {
  const snap = useStore((s) => s.snap);
  if (!snap) return null;
  const cap = snap.capital, b = snap.broker;
  const rs = snap.closed.map((t) => t.r);
  const wins = rs.filter((r) => r > 0), losses = rs.filter((r) => r <= 0);
  const avg = (xs: number[]) => (xs.length ? xs.reduce((x, y) => x + y, 0) / xs.length : null);
  const payoff = wins.length && losses.length && avg(losses) ? (avg(wins) as number) / Math.abs(avg(losses) as number) : null;
  const expectancy = avg(rs);
  return (
    <div className="view-enter">
      <h1 className="section-title">The Monk</h1>
      <p className="section-sub">Unbounded mode, your decision (D-031): capital-based sizing, one full entry with a stop-loss and a target on
        every trade. There are no risk ceilings, gears, loss limits, streak stops, staging or approvals. Only the checks below can stop an entry.</p>
      <div className="grid g-12">
        <Card title="What can stop an entry" sub="Market, broker and exchange rules, your kill switch, and broken trades" className="span-8">
          <div className="checks-static">
            {snap.mandatory_checks.map((c) => (
              <div className="item" key={c.name}><CircleCheck size={16} color="var(--up)" style={{ flex: "none", marginTop: 1 }} />
                <span><b style={{ fontWeight: 600 }}>{c.what}</b><div className="faint" style={{ fontSize: 11 }}>{c.name.replace(/_/g, " ")}</div></span></div>
            ))}
          </div>
        </Card>
        <KillCard />
        <Card title="Capital" className="span-4">
          <KV k="Equity" v={rupees(cap.equity)} />
          <KV k="In open trades" v={rupees(cap.deployed)} />
          <KV k="Free for new trades" v={rupees(cap.free)} />
          <KV k="Each trade gets" v={`${rupees(cap.per_trade)} (${pct(cap.per_trade_fraction, 0)})`} />
          {b?.live && <KV k="Dhan funds available" v={b.available_funds == null ? "–" : rupees(b.available_funds)} />}
        </Card>
        <Card title="Edge so far" sub={<>Information only: nothing here changes size <Label text={snap.label} /></>} className="span-4">
          <KV k="Closed trades (recent)" v={rs.length} />
          <KV k="Win rate / payoff" v={rs.length ? `${pct(wins.length / rs.length, 0)} / ${payoff === null ? "–" : payoff.toFixed(2)}` : "–"} />
          <KV k="Expectancy per trade" v={expectancy === null ? "–" : <Delta v={expectancy}>{expectancy.toFixed(3)}R</Delta>} />
          <div className="faint" style={{ fontSize: 12, marginTop: 6 }}>A strategy with no edge after costs loses money faster at full size. Watch this number.</div>
        </Card>
        <Card title="Operational safety" className="span-4">
          <KV k="Broker" v={b?.live ? "Dhan (real orders)" : "paper (simulated fills)"} />
          <KV k="Stop-losses resting at the broker" v={`${b?.resting_stops ?? 0} for ${snap.positions.length} positions`} />
          <KV k="Feed situation" v={snap.feeds.situation.replace("_", " ")} />
          <KV k="Operational halts" v={snap.risk.halts.length ? snap.risk.halts.join(" · ") : "none"} />
          {b?.live && <KV k="Order rejects (1 min)" v={b.rejects_1m ?? 0} />}
        </Card>
      </div>
    </div>
  );
}

export function Monk() {
  const snap = useStore((s) => s.snap);
  const toast = useStore((s) => s.toast);
  const [reviewed, setReviewed] = useState(false);
  if (!snap) return null;
  if (snap.risk_mode === "unbounded") return <MonkUnbounded />;
  const c = snap.ceilings, r = snap.risk, g = snap.gear, a = snap.account, e = snap.edge_brake;
  const ladder = g.ladder.filter((l) => l.gear !== "HALTED");
  const scale = (x: number) => `${Math.min(1, x / 0.3) * 100}%`;
  return (
    <div className="view-enter">
      <h1 className="section-title">The Monk</h1>
      <p className="section-sub">The Risk Guardian has absolute veto. Aggression is the accelerator and the Monk is the brake, and the brake is never removed. Nothing on this page can increase risk.</p>

      <div className="grid g4" style={{ marginBottom: 16 }}>
        {[
          { l: "Per-trade ceiling", v: pct(c.max_risk_per_trade, 1), d: `${rupees(r.per_trade_ceiling)} today` },
          { l: "Open-risk ceiling", v: pct(c.max_open_risk, 1), d: `gear limit ${rupees(r.open_risk_limit)}` },
          { l: "Halt at drawdown", v: pct(c.halt_drawdown, 0), d: "no new trades beyond" },
          { l: "Ruin floor", v: rupees(a.ruin_floor), d: `max(₹70,000, 70% of peak ${rupees(a.peak)})` },
        ].map((x) => (
          <div className="ceiling" key={x.l}>
            <Lock size={16} className="lock" />
            <div className="eyebrow">{x.l}</div>
            <div className="v">{x.v}</div>
            <div className="muted" style={{ fontSize: 12 }}>{x.d}</div>
          </div>
        ))}
      </div>
      <div className="faint" style={{ fontSize: 12, marginTop: -8, marginBottom: 16 }}>Enforced in code (risk/ceilings.py). No layer can change them at runtime, and operator.yaml is validated so it can only be stricter.</div>

      <div className="grid g-12">
        <Card title="Gear ladder" sub="Downshifts are immediate. Upshifts go one gear at a time, only after recovery plus 5 clean days, never at the close of a losing day (D-004)." className="span-8">
          <div className="ladder">
            {ladder.map((l, i) => {
              const from = i === 0 ? 0 : ladder[i - 1].dd_below;
              return <div key={l.gear} className="seg-g" style={{ left: scale(from), width: `calc(${scale(l.dd_below)} - ${scale(from)})`, background: GEAR_COLOR[l.gear], opacity: l.gear === g.effective ? 1 : 0.35 }} title={`${l.gear}: R ${pct(l.r, 1)}, open risk ${pct(l.open_risk, 1)}`} />;
            })}
            <div className="seg-g" style={{ left: scale(0.25), width: `calc(100% - ${scale(0.25)})`, background: GEAR_COLOR.HALTED, opacity: 0.2, borderRadius: "0 7px 7px 0" }} />
            <div className="marker" style={{ left: scale(g.drawdown) }}><span>DD {pct(g.drawdown, 1)}</span></div>
            {[0, 0.1, 0.15, 0.2, 0.25].map((t) => <span key={t} className="tick" style={{ left: scale(t) }}>{(t * 100).toFixed(0)}%</span>)}
          </div>
          <div className="grid g4" style={{ marginTop: 16 }}>
            {g.ladder.map((l) => (
              <div key={l.gear} style={{ opacity: l.gear === g.effective ? 1 : 0.6 }}>
                <div className="row" style={{ gap: 6 }}><span style={{ width: 8, height: 8, borderRadius: 4, background: GEAR_COLOR[l.gear] }} /><b style={{ fontSize: 12, letterSpacing: "0.06em" }}>{l.gear}</b></div>
                <div className="muted" style={{ fontSize: 12 }}>{l.gear === "HALTED" ? "≥ 25% · R 0" : `< ${pct(l.dd_below, 0)} · R ${pct(l.r, 1)} · open ${pct(l.open_risk, 1)}`}</div>
              </div>
            )).slice(0, 4)}
          </div>
          <div className="row-wrap" style={{ marginTop: 14 }}>
            <span className="pill">Ladder {g.gear} → effective {g.effective}</span>
            <span className="pill">Clean days {g.clean_days}/{g.upshift_needs}</span>
            {g.day_drop && <span className="pill">State check: −1 gear today</span>}
            {g.evidence_drop && <span className="pill">Calibration out of band: −1 gear</span>}
            {g.expectancy_cap && <span className="pill">Edge review: capped at GUARD</span>}
          </div>
          {g.events.length > 0 && <div className="feed" style={{ marginTop: 10 }}>{g.events.slice(-4).reverse().map((ev, i) => <div key={i} className="feed-item"><span className="ts">{time(ev.ts)}</span><span>{ev.from} → {ev.to}: {ev.reason}</span></div>)}</div>}
        </Card>

        <Card title="Kill switch" sub="One step: flatten everything and halt (D10)" className="span-4">
          <div className="stack" style={{ gap: 12 }}>
            {r.kill_switch || g.gear === "HALTED" ? (
              <>
                <div className="banner crit">{r.kill_switch ? "Kill switch is engaged." : "HALTED at the drawdown ceiling."}</div>
                <label className="row" style={{ fontSize: 13 }}><input type="checkbox" checked={reviewed} onChange={(x) => setReviewed(x.target.checked)} /> I have reviewed what happened (required)</label>
                <button className="btn btn-primary" disabled={!reviewed} onClick={async () => { const x = await act("resume", { reviewed }); toast(x.ok ? x.result.join("; ") : x.error); }}>Resume after review</button>
                <div className="faint" style={{ fontSize: 12 }}>After a halt the system restarts in GUARD. The ruin floor never resets.</div>
              </>
            ) : (
              <>
                <button className="btn btn-kill" style={{ height: 52, fontSize: 14 }} onClick={async () => { if (confirm("Flatten every position and halt new entries?")) { await act("kill"); toast("KILL SWITCH engaged"); } }}>
                  <OctagonX size={18} /> KILL: FLATTEN AND HALT
                </button>
                <div className="muted" style={{ fontSize: 12.5 }}>Stops, time exits, harvests, halts and downshifts never wait for approval. They are always immediate (D9).</div>
              </>
            )}
            <div className="row-wrap">
              {(["day", "week", "month"] as const).map((p) => <button key={p} className="btn btn-secondary btn-sm" onClick={async () => { await act("lock", { period: p }); toast(`/lock ${p}: no new entries this ${p}`); }}>/lock {p}</button>)}
            </div>
          </div>
        </Card>

        <Card title="Loss limits" sub="In every gear (D4)" className="span-4">
          <div className="stack" style={{ gap: 14 }}>
            <Meter label="Day (5%)" right={`${signedRupees(a.day_pnl)} / −${rupees(r.day_loss_limit)}`} value={Math.max(0, -a.day_pnl)} max={r.day_loss_limit} warnAt={0.5} critAt={0.8} />
            <Meter label="Week (10%): breach means rest of week PAPER" right={`${signedRupees(a.week_pnl)} / −${rupees(r.week_loss_limit)}`} value={Math.max(0, -a.week_pnl)} max={r.week_loss_limit} warnAt={0.5} critAt={0.8} />
            <Meter label="Month (15%): breach means rest of month PAPER" right={`${signedRupees(a.month_pnl)} / −${rupees(r.month_loss_limit)}`} value={Math.max(0, -a.month_pnl)} max={r.month_loss_limit} warnAt={0.5} critAt={0.8} />
            <KV k="Profit give-back guard" v={r.day_peak_pnl > 0 ? `peak ${rupees(r.day_peak_pnl)}` : "not armed"} />
            <KV k="Losing streak" v={`${r.losing_streak} (3 → 30-min cooldown, 4 → done)`} />
            {r.cooldown_until && <KV k="Cooldown until" v={hm(r.cooldown_until)} />}
          </div>
        </Card>

        <Card title="Exposure" className="span-4">
          <div className="stack" style={{ gap: 14 }}>
            <Meter label="Open risk" right={`${rupees(r.open_risk)} / ${rupees(r.open_risk_limit)}`} value={r.open_risk} max={r.open_risk_limit} />
            <Meter label="Margin (≤ 50%)" right={`${rupees(r.margin_used)} / ${rupees(r.margin_limit)}`} value={r.margin_used} max={r.margin_limit} />
            <Meter label="Trades" right={`${r.trades_today} / ${r.max_trades}`} value={r.trades_today} max={r.max_trades} />
            <KV k="Same-direction index bucket cap" v={rupees(r.bucket_cap)} />
            <div className="faint" style={{ fontSize: 12 }}>Nifty and Sensex in the same direction, plus aligned high-beta stocks, count as ONE bet (A7.3).</div>
          </div>
        </Card>

        <Card title="Edge Brake and calibration" sub="Live evidence caps the gear and never raises it (D12.4)" className="span-4">
          <KV k="Status" v={e.active ? "active" : "inactive"} />
          <KV k="Trades counted" v={e.n} />
          <KV k="Win rate / payoff" v={e.win_rate === null ? "–" : `${pct(e.win_rate, 0)} / ${e.payoff?.toFixed(2) ?? "–"}`} />
          <KV k="Quarter-Kelly ceiling" v={e.ceiling_r === null ? "none" : pct(e.ceiling_r, 2)} />
          <KV k="Expectancy" v={e.expectancy_r === null ? "–" : <Delta v={e.expectancy_r}>{e.expectancy_r.toFixed(3)}R</Delta>} />
          <KV k="Calibration" v={`${snap.calibration.status} (n=${snap.calibration.n}${snap.calibration.ece !== null ? `, ECE ${snap.calibration.ece.toFixed(3)}` : ""})`} />
          <div className="faint" style={{ fontSize: 12, marginTop: 6 }}>{e.note}</div>
        </Card>

        <Card title="Operational safety" className="span-6">
          <KV k="Feed situation (A5.5)" v={snap.feeds.situation.replace("_", " ")} />
          <KV k="Data confidence" v={snap.feeds.data_confidence.toFixed(2)} />
          <KV k="Instruments blocked by data quality" v={r.blocked_instruments} />
          <KV k="Operational halts" v={r.halts.length ? r.halts.join(" · ") : "none"} />
          <KV k="Target blocks" v={r.target_blocks.length ? r.target_blocks.join(" · ") : "none"} />
          <KV k="Broker-side stops resting" v={`${snap.orders.filter((o) => o.purpose === "BROKER_STOP" && o.status === "OPEN").length} for ${snap.positions.length} positions`} />
        </Card>

        <PartDEditor />

        <Card title="Capital tier" sub="Promotion is never automatic (D3, D-017)" className="span-6">
          <KV k="Current tier" v={snap.tier.tier} />
          <KV k="ATTACK R" v={`${pct(snap.tier.attack_r, 2)}${snap.tier.attack_r_cap !== null ? " (held after demotion)" : ""}`} />
          <KV k="Max positions / trades per day" v={`${snap.tier.max_positions} / ${snap.tier.max_trades_per_day}`} />
          <KV k="Days above next floor" v={`${snap.tier.days_above_next} / 20`} />
          <KV k="Breach-free days" v={`${snap.tier.breach_free_days} / 20`} />
          <div className="row" style={{ gap: 6, marginTop: 10, fontSize: 12 }}><ShieldCheck size={14} color="var(--up)" /> <span className="muted">Promotion also needs ≥ 100 live trades with positive expectancy, plus your approval.</span></div>
        </Card>
      </div>
    </div>
  );
}
