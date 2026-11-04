import { AlertTriangle, ArrowRight, Moon as Sleep, Smile, Target } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { act, get } from "../lib/api";
import { GEAR_COLOR, hm, num, pct, rMult, rupees, signedPct, signedRupees, time, title } from "../lib/format";
import { useStore } from "../lib/store";
import type { Snapshot } from "../lib/types";
import { ApprovalCard } from "../components/Overlays";
import { BoomingList, CapitalGauge, GearSelector, HeadroomBattery, Horizon, RBar, StageStepper, TradeBar } from "../components/Signature";
import { Card, Delta, Grade, Label, Meter, Stat } from "../components/ui";

function useTodayCurve(snap: Snapshot | null): { t: number; v: number }[] {
  const [pts, setPts] = useState<{ t: number; v: number }[]>([]);
  const day = useRef("");
  useEffect(() => {
    if (!snap) return;
    if (day.current !== snap.clock.day) {
      day.current = snap.clock.day;
      setPts([]);
      get("/api/equity").then((d) => {
        const start = new Date(`${snap.clock.day}T09:15:00+05:30`).getTime() / 1000;
        setPts((live) => [...d.curve.filter((p: any) => p.t >= start).map((p: any) => ({ t: p.t, v: p.equity })), ...live.filter((p) => p.t >= start)]);
      }).catch(() => setPts([]));
    } else {
      const t = Math.floor(new Date(snap.clock.now).getTime() / 1000);
      setPts((p) => (p.length && p[p.length - 1].v === snap.account.mtm ? p : [...p.slice(-800), { t, v: snap.account.mtm }]));
    }
  }, [snap?.clock.now]);
  return pts;
}

function StateCheck() {
  const [a, setA] = useState({ slept_well: true, calm: true, undistracted: true });
  const toast = useStore((s) => s.toast);
  const Q = [
    { k: "slept_well" as const, q: "Slept well?", icon: <Sleep size={16} /> },
    { k: "calm" as const, q: "Calm?", icon: <Smile size={16} /> },
    { k: "undistracted" as const, q: "Undistracted?", icon: <Target size={16} /> },
  ];
  return (
    <Card title="Pre-market state check" sub="The operator is part of the system (A15). Any “no” lowers the gear by one for today. It can never raise it." className="span-12">
      <div className="row-wrap" style={{ gap: 12 }}>
        {Q.map((x) => (
          <div key={x.k} className="row" style={{ gap: 8, background: "var(--bg-subtle)", borderRadius: 10, padding: "8px 12px" }}>
            {x.icon}<span style={{ fontWeight: 500 }}>{x.q}</span>
            <div className="seg" style={{ marginLeft: 6 }}>
              <button className={a[x.k] ? "on" : ""} onClick={() => setA({ ...a, [x.k]: true })}>Yes</button>
              <button className={!a[x.k] ? "on" : ""} onClick={() => setA({ ...a, [x.k]: false })}>No</button>
            </div>
          </div>
        ))}
        <span className="spacer" />
        <button className="btn btn-primary" onClick={async () => { const r = await act("state_check", a); toast(`State check recorded: effective gear ${r.effective_gear}`); }}>Submit check</button>
      </div>
    </Card>
  );
}

export function Banners({ snap }: { snap: Snapshot }) {
  const out = [];
  if (snap.risk.kill_switch) out.push(<div key="k" className="banner crit"><AlertTriangle size={16} /> Kill switch engaged: everything flattened and new entries halted. Resume from The Monk after review.</div>);
  if (snap.gear.gear === "HALTED") out.push(<div key="h" className="banner crit"><AlertTriangle size={16} /> HALTED at the 25% drawdown ceiling. No new trades until an operator review; the system resumes in GUARD.</div>);
  if (snap.risk.ruined) out.push(<div key="r" className="banner crit"><AlertTriangle size={16} /> Ruin floor breached: full shutdown.</div>);
  if (snap.risk.halts.length && !snap.risk.kill_switch) out.push(<div key="o" className="banner warn"><AlertTriangle size={16} /> Operational halt on new entries: {snap.risk.halts.join(" · ")}</div>);
  const m = snap.market;
  if (snap.broker?.live) {
    out.push(<div key="live" className="banner crit" style={{ fontSize: 12.5 }}>
      <span className="chip chip-down">LIVE</span>
      <span>Real orders on your Dhan account: every entry, stop-loss and exit is sent to the exchange.
        {snap.broker.available_funds != null && <> Dhan funds available <b>{rupees(snap.broker.available_funds)}</b>.</>}
        {" "}The strategies are <b>unvalidated</b>: there is no evidence of an edge yet.
        {snap.broker.health?.error && <> Order-book poll problem: {snap.broker.health.error}</>}</span>
    </div>);
  }
  if (m && m.source !== "SIM" && !snap.broker?.live) {
    const next = m.next_open ? new Date(m.next_open).toLocaleString("en-IN", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" }) : null;
    if (m.health?.error_kind) out.push(<div key="d" className={`banner ${m.health.error_kind === "AUTH" || m.health.error_kind === "NOT_SUBSCRIBED" ? "crit" : "warn"}`}>
      <AlertTriangle size={16} /> Dhan data problem ({m.health.error_kind}): {m.health.last_error}. See the event log for the fix; new entries pause while data is stale (A5.5).
    </div>);
    out.push(<div key="p" className="banner" style={{ fontSize: 12.5 }}>
      <span className="chip chip-up">PAPER</span>
      <span>Live Dhan market data. Fills are simulated at the live bid/ask and nothing is sent to the exchange.{" "}
        {m.session === "OPEN" ? <b>Market open.</b> : <><b>Market closed</b>{next ? `: next session ${next} IST` : ""}. Showing the last traded prices.</>}
        {!m.secondary_configured && " Upstox not configured: single feed, data confidence capped at 0.8 (A5.5)."}</span>
    </div>);
  }
  if (snap.mode === "SIM") out.push(<div key="s" className="banner" style={{ fontSize: 12.5 }}>
    <span className="chip chip-accent">SIM</span>
    <span>Synthetic market ({snap.sim_regime.replace("_", " ")} day). It exercises every rule end to end, but results here are <b>SIMULATED</b> and never evidence of edge (D-006).</span>
  </div>);
  return <>{out}</>;
}

export function Dashboard() {
  const snap = useStore((s) => s.snap);
  const setRoute = useStore((s) => s.setRoute);
  const select = useStore((s) => s.select);
  const curve = useTodayCurve(snap);
  if (!snap) return null;
  const a = snap.account, g = snap.gear, r = snap.risk;
  const fc = snap.funnel.counts;
  const maxF = Math.max(1, ...Object.values(fc));
  const k = snap.kpis;
  const lastTrades = snap.closed.slice(0, 5);
  const ub = snap.risk_mode === "unbounded";
  const cap = snap.capital;
  const today = snap.closed.filter((t) => t.closed.slice(0, 10) === snap.clock.day);
  const winRate = today.length ? today.filter((t) => t.net > 0).length / today.length : null;
  return (
    <div className="view-enter">
      <Banners snap={snap} />
      <section className="hero" aria-label="Account overview">
        <Horizon points={curve} />
        <div className="hero-grid">
          {ub ? <CapitalGauge equity={cap.equity} deployed={cap.deployed} free={cap.free} perTrade={cap.per_trade} />
            : <HeadroomBattery drawdown={g.drawdown} ladder={g.ladder} />}
          <div className="hero-center">
            <div className="hero-eyebrow">Equity <Label text={snap.label} /></div>
            <div className="hero-number">{rupees(a.mtm)}</div>
            <div className="hero-sub">
              <span>Process <b>{pct(snap.process.score, 0)}</b></span>
              <span>Today <b><Delta v={a.day_pnl}>{signedRupees(a.day_pnl)} ({signedPct(a.day_pnl_pct, 2, false)})</Delta></b></span>
              {ub ? <span>Per trade <b>{rupees(cap.per_trade)}</b> ({pct(cap.per_trade_fraction, 0)})</span>
                : <span>R per trade <b>{pct(g.effective_r, 2)}</b></span>}
            </div>
            <div className="hero-cta">
              {ub ? <button className="btn btn-primary" onClick={() => setRoute("scanner")}>Open scanner <ArrowRight size={15} /></button>
                : <button className="btn btn-primary" onClick={() => setRoute("queue")}>Review queue <ArrowRight size={15} /></button>}
              <button className="btn btn-secondary" onClick={() => setRoute("terminal")}>Open terminal</button>
            </div>
            <div className="hero-quote">Full force, zero attachment.</div>
          </div>
          {ub ? <BoomingList rows={snap.scanner.top.filter((x) => x.tradable)} onPick={(key) => select(key, "terminal")} />
            : <GearSelector gear={g.gear} effective={g.effective}
              note={g.next_downshift_in !== null ? `Next downshift in ${pct(g.next_downshift_in, 1)}. Upshifts only by recovery (${g.clean_days}/${g.upshift_needs} clean days).` : "Operator review required."} />}
        </div>
      </section>

      {snap.approvals.length > 0 && (
        <div className="grid g2" style={{ marginBottom: 16 }}>{snap.approvals.map((ap) => <ApprovalCard key={ap.id} a={ap} />)}</div>
      )}

      <div className="grid g-12">
        {!ub && !snap.state_check && snap.clock.session === "OPEN" && <StateCheck />}

        {ub ? (
          <div className="card span-12">
            <div className="kpis">
              <Stat label="In trades" value={rupees(cap.deployed)} hint={`${num(cap.deployed_pct, 0)}% of equity`} />
              <Stat label="Free capital" value={rupees(cap.free)} hint={snap.broker?.live ? "capped by Dhan's available funds" : "for the next entries"} />
              <Stat label="Per trade" value={rupees(cap.per_trade)} hint={`${pct(cap.per_trade_fraction, 0)} of equity, whole lots`} />
              <Stat label="Trades today" value={r.trades_today} hint={`${r.positions} open now`} />
              <Stat label="Win rate today" value={winRate === null ? "–" : pct(winRate, 0)} hint={`${today.length} closed`} />
              <Stat label="Week" value={<Delta v={a.week_pnl}>{signedRupees(a.week_pnl)}</Delta>} hint={<Label text={snap.label} />} />
            </div>
          </div>
        ) : (
        <div className="card span-12">
          <div className="kpis">
            <Stat label="Open risk" value={rupees(r.open_risk)} hint={`of ${rupees(r.open_risk_limit)} (${g.effective})`} />
            <Stat label="Week" value={<Delta v={a.week_pnl}>{signedRupees(a.week_pnl)}</Delta>} hint={`limit −${rupees(r.week_loss_limit)}`} />
            <Stat label="Month" value={<Delta v={a.month_pnl}>{signedRupees(a.month_pnl)}</Delta>} hint={`limit −${rupees(r.month_loss_limit)}`} />
            <Stat label="Trades today" value={`${r.trades_today} / ${r.max_trades}`} hint={`${r.positions}/${r.max_positions} open`} />
            <Stat label="A+ · A · B risk" value={<span style={{ fontSize: 16 }}>{rupees(r.planned_by_grade["A+"])} · {rupees(r.planned_by_grade.A)} · {rupees(r.planned_by_grade.B)}</span>} hint="planned per trade today" />
            <Stat label="Tier" value={snap.tier.tier} hint={`ATTACK R ${pct(snap.tier.attack_r, 1)}`} />
          </div>
        </div>
        )}

        <Card title="Open positions" sub={ub ? "Every trade: stop-loss at the broker and a target" : "Staged: probe → confirm → press → harvest → exit"} className="span-7"
          right={<button className="btn btn-ghost btn-sm" onClick={() => setRoute("positions")}>All positions</button>}>
          {snap.positions.length === 0 ? (
            <div className="empty" style={{ padding: 20 }}>No open positions. <b>Cash is a position.</b></div>
          ) : (
            <div className="stack" style={{ gap: 14 }}>
              {snap.positions.slice(0, 4).map((p) => (
                <div key={p.id} style={{ display: "grid", gridTemplateColumns: "1.1fr 1.6fr 1.2fr", gap: 16, alignItems: "center", cursor: "pointer" }}
                  onClick={() => select(p.underlying, "terminal")}>
                  <div>
                    <div className="row" style={{ gap: 6 }}><b>{p.symbol}</b><Grade g={p.grade} /></div>
                    <div className="muted" style={{ fontSize: 12 }}>{p.qty} @ {num(p.avg_entry)} · stop {num(p.stop)}</div>
                  </div>
                  {ub ? <TradeBar direction={p.direction} initialStop={p.initial_stop} stop={p.stop} entry={p.avg_entry} target={p.target} price={p.last_price} />
                    : <StageStepper stage={p.stage} history={p.stage_history} />}
                  <div>
                    <div className="row between"><span className="muted" style={{ fontSize: 12 }}>{title(p.strategy)}</span><b className={p.net_pnl >= 0 ? "up" : "down"}>{signedRupees(p.net_pnl)}</b></div>
                    <RBar r={p.r_multiple} mfe={p.mfe_r} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        {ub ? (
          <Card title="Capital" sub="Sizing comes from capital, not from a risk ceiling" className="span-5"
            right={<button className="btn btn-ghost btn-sm" onClick={() => setRoute("monk")}>The Monk</button>}>
            <div className="stack" style={{ gap: 14 }}>
              <Meter label="In open trades" right={`${rupees(cap.deployed)} / ${rupees(cap.equity)}`} value={cap.deployed} max={Math.max(1, cap.equity)} warnAt={0.95} critAt={1.01} />
              <div className="stack" style={{ gap: 6, fontSize: 13 }}>
                <div className="row between"><span className="muted">Each trade gets</span><b className="num">{rupees(cap.per_trade)} ({pct(cap.per_trade_fraction, 0)})</b></div>
                <div className="row between"><span className="muted">Entry</span><span>all at once, whole lots</span></div>
                <div className="row between"><span className="muted">Stop-loss</span><span>from the setup, resting at the broker</span></div>
                <div className="row between"><span className="muted">Target</span><span>from the setup (usually 2R)</span></div>
                <div className="row between"><span className="muted">Stop moves to entry at</span><b className="num">+{num(cap.breakeven_at_r, 1)}R</b></div>
                <div className="row between"><span className="muted">Stock leverage</span><b className="num">{num(cap.intraday_leverage, 1)}×</b></div>
              </div>
              <div className="row-wrap">
                <span className="pill">{r.entry_window_open ? "Entry window open" : "Entry window closed"}</span>
                <span className="pill">{snap.broker?.live ? `Dhan · ${snap.broker.resting_stops} stops resting` : `${snap.broker?.resting_stops ?? 0} broker stops resting`}</span>
              </div>
            </div>
          </Card>
        ) : (
        <Card title="Risk budget" sub="Ceilings 3% trade · 6% open · 25% halt" className="span-5"
          right={<button className="btn btn-ghost btn-sm" onClick={() => setRoute("monk")}>The Monk</button>}>
          <div className="stack" style={{ gap: 14 }}>
            <Meter label="Open risk" right={`${rupees(r.open_risk)} / ${rupees(r.open_risk_limit)}`} value={r.open_risk} max={r.open_risk_limit} />
            <Meter label="Daily loss used" right={`${rupees(Math.max(0, -a.day_pnl))} / ${rupees(r.day_loss_limit)}`} value={Math.max(0, -a.day_pnl)} max={r.day_loss_limit} warnAt={0.5} critAt={0.8} />
            <Meter label="Trades" right={`${r.trades_today} / ${r.max_trades}`} value={r.trades_today} max={r.max_trades} warnAt={0.84} critAt={1} />
            <Meter label="Margin" right={`${rupees(r.margin_used)} / ${rupees(r.margin_limit)}`} value={r.margin_used} max={r.margin_limit} />
            <div className="row-wrap">
              <span className="pill">Losing streak {r.losing_streak}</span>
              {r.cooldown_until && <span className="pill" style={{ color: "var(--warning-text)" }}>Cooldown until {hm(r.cooldown_until)}</span>}
              <span className="pill">{r.entry_window_open ? "Entry window open" : "Entry window closed"}</span>
              {!r.b_allowed && <span className="pill">Ratchet: B probes off</span>}
            </div>
          </div>
        </Card>
        )}

        <Card title="Pickup funnel" sub="Today · S1 scan → S7 probe" className="span-4"
          right={<button className="btn btn-ghost btn-sm" onClick={() => setRoute("queue")}>Queue</button>}>
          <div className="funnel">
            {Object.entries(fc).map(([st, n]) => (
              <div className="f-row" key={st}>
                <span className="f-name">{st.replace(/^S\d_/, "").toLowerCase()}</span>
                <div className="f-track"><div className="f-bar" style={{ width: `${(n / maxF) * 100}%` }} /></div>
                <span className="f-val">{n}</span>
              </div>
            ))}
          </div>
          <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            Top drop: {Object.entries(snap.funnel.drop_reasons).sort((x, y) => y[1] - x[1])[0]?.[0]?.replace(/_/g, " ") ?? "none yet"}
          </div>
        </Card>

        <Card title="Market pulse" sub={`Inputs ${snap.regime.latency}`} className="span-4">
          <div className="stack" style={{ gap: 10 }}>
            <div className="row between"><span className="muted">Trend (Nifty)</span><span className="chip chip-accent">{snap.regime.trend.replace("_", " ")}</span></div>
            <div className="row between"><span className="muted">Volatility</span><span className="chip">{snap.regime.volatility.replace("_", " ")} {snap.regime.vol_tags.join(" ")}</span></div>
            <div className="row between"><span className="muted">India VIX</span><span className="num"><b>{num(snap.regime.vix)}</b> <Delta v={snap.regime.vix_change}>{signedPct(snap.regime.vix_change)}</Delta></span></div>
            <div className="row between"><span className="muted">Breadth A/D</span><b className="num">{num(snap.regime.breadth_ad, 2)}</b></div>
            <div className="row between"><span className="muted">Expiry regime</span><span className="chip">{snap.regime.expiry ?? "not an expiry day"}</span></div>
            <div className="row between"><span className="muted">Feeds</span><span className="chip" style={{ color: snap.feeds.entries_allowed ? "var(--up-text)" : "var(--down-text)" }}>{snap.feeds.situation.replace("_", " ")} · {snap.feeds.data_confidence.toFixed(1)}</span></div>
          </div>
        </Card>

        <Card title="Targets" sub="A target never changes risk (D5)" className="span-4">
          <div className="stack" style={{ gap: 12 }}>
            {(["day", "week", "month"] as const).map((p) => {
              const t = snap.targets[p];
              return (
                <div key={p}>
                  <div className="row between" style={{ fontSize: 12.5 }}>
                    <span style={{ fontWeight: 500 }}>{title(p)}{t.locked && <span className="chip" style={{ marginLeft: 6 }}>LOCKED</span>}{t.ratchet.active && <span className="chip chip-accent" style={{ marginLeft: 6 }}>RATCHET</span>}</span>
                    <span className="num"><Delta v={t.pnl}>{signedRupees(t.pnl)}</Delta>{t.target ? <span className="muted"> / {rupees(t.target.rupees)}</span> : <span className="faint"> · no target</span>}</span>
                  </div>
                  {t.target && <div className="meter" style={{ marginTop: 6 }}><span style={{ width: `${Math.max(0, Math.min(1, t.progress ?? 0)) * 100}%` }} /></div>}
                  {t.ratchet.floor !== null && <div className="faint" style={{ fontSize: 11, marginTop: 3 }}>Profit floor {rupees(t.ratchet.floor)}</div>}
                </div>
              );
            })}
            <button className="btn btn-ghost btn-sm" onClick={() => setRoute("research")}>Set or test a target</button>
          </div>
        </Card>

        <Card title={ub ? "Trade quality" : "Aggression KPIs"} sub={<>{ub ? "How the trades are going" : "Measured, never used to raise risk"} <Label text={snap.label} /></>} className="span-6">
          <div className="kpis">
            <Stat label="Idle risk budget" value={pct(k.idle_risk_budget_pct ?? 0, 0)} hint="time A/A+ queued with capacity free" />
            <Stat label="Capture ratio" value={k.capture_ratio == null ? "–" : pct(k.capture_ratio, 0)} hint="realised ÷ max favourable" />
            <Stat label="Avg winner / loser" value={<span style={{ fontSize: 20 }}>{rMult(k.avg_winner_r, 2)} / {rMult(k.avg_loser_r, 2)}</span>} />
            {ub ? <Stat label="Win rate today" value={winRate === null ? "–" : pct(winRate, 0)} hint={`${today.length} closed trades`} />
              : <Stat label="Probe success" value={k.probe_success_rate == null ? "–" : pct(k.probe_success_rate, 0)} hint="probes reaching confirm-add" />}
          </div>
        </Card>

        <Card title="Recent closed trades" className="span-6" right={<Label text={snap.label} />}>
          {lastTrades.length === 0 ? <div className="empty" style={{ padding: 16 }}>No closed trades yet.</div> : (
            <table className="t">
              <tbody>
                {lastTrades.map((t) => (
                  <tr key={t.id}><td><b>{t.symbol}</b> <Grade g={t.grade} /></td><td className="muted">{t.reason}</td>
                    <td className="r num"><Delta v={t.r}>{rMult(t.r)}</Delta></td><td className="r num"><Delta v={t.net}>{signedRupees(t.net)}</Delta></td></tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="The Monk says" sub="Live decisions and rule actions" className="span-12">
          <div className="feed">
            {snap.events.filter((e) => e.kind !== "regime").slice(0, 8).map((e, i) => (
              <div key={i} className="feed-item"><span className="ts">{time(e.ts)}</span><span><span className="k" style={{ color: e.kind === "risk" || e.kind === "gear" ? GEAR_COLOR.HALTED : undefined }}>{e.kind}</span>{e.msg}</span></div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
