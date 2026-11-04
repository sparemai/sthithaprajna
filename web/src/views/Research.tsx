import { Check, Play } from "lucide-react";
import { useEffect, useState } from "react";
import { act, get, post } from "../lib/api";
import { pct, rMult, rupees, signedPct } from "../lib/format";
import { useStore } from "../lib/store";
import { EquityChart, FanChart, Histogram } from "../components/charts";
import { Card, KV, Label, Seg, Stat } from "../components/ui";

const SPEC_D12: Record<string, [number, number]> = {
  "Real edge|BASE 1.5%, halt 15%": [1.78, 0.95], "Real edge|ATTACK day 1, halt 25%": [2.49, 0.97], "Real edge|ATTACK day 1 + edge brake": [2.44, 0.99],
  "Weak edge|BASE 1.5%, halt 15%": [0.95, 0.85], "Weak edge|ATTACK day 1, halt 25%": [0.90, 0.75], "Weak edge|ATTACK day 1 + edge brake": [0.91, 0.75],
  "No edge|BASE 1.5%, halt 15%": [0.91, 0.85], "No edge|ATTACK day 1, halt 25%": [0.84, 0.75], "No edge|ATTACK day 1 + edge brake": [0.84, 0.75],
};

function MonteCarlo() {
  const [p, setP] = useState(0.45);
  const [b, setB] = useState(1.8);
  const [gap, setGap] = useState(0.04);
  const [t, setT] = useState(250);
  const [policy, setPolicy] = useState<"baseline" | "attack" | "attack_brake">("attack");
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true);
    try { setRes(await post("/api/research/mc", { p, b, gap_p: gap, t, policy, n: 20000 })); } finally { setBusy(false); }
  }
  useEffect(() => { run(); }, []);
  const ev = p * b - (1 - p) * (1 + gap * 1.5);
  const s = res?.summary;
  return (
    <Card title="Monte Carlo lab" sub="20,000 paths with the gear ladder, gap losses and halts. A simulation, not a forecast." right={<Label text="SIMULATION" />} className="span-12">
      <div className="grid g-12">
        <div className="span-4 stack" style={{ gap: 14 }}>
          <div><label className="label">Win rate · {pct(p, 0)}</label><input type="range" min={0.3} max={0.6} step={0.01} value={p} onChange={(e) => setP(+e.target.value)} /></div>
          <div><label className="label">Payoff (win in R) · {b.toFixed(2)}R</label><input type="range" min={1} max={3} step={0.05} value={b} onChange={(e) => setB(+e.target.value)} /></div>
          <div><label className="label">Losses that gap through the stop (−2.5R) · {pct(gap, 0)}</label><input type="range" min={0} max={0.15} step={0.01} value={gap} onChange={(e) => setGap(+e.target.value)} /></div>
          <div><label className="label">Trades · {t}</label><input type="range" min={50} max={500} step={10} value={t} onChange={(e) => setT(+e.target.value)} /></div>
          <div><label className="label">Policy</label>
            <Seg value={policy} onChange={setPolicy} options={[{ v: "baseline", label: "Baseline 1.5%" }, { v: "attack", label: "ATTACK" }, { v: "attack_brake", label: "ATTACK + brake" }]} /></div>
          <div className="row between">
            <span className="muted" style={{ fontSize: 13 }}>EV per trade incl. gaps <b className={ev > 0 ? "up" : "down"}>{rMult(ev)}</b></span>
            <button className="btn btn-primary" disabled={busy} onClick={run}><Play size={14} /> {busy ? "Running…" : "Run"}</button>
          </div>
          {s && (
            <div className="kpis" style={{ gridTemplateColumns: "1fr 1fr" }}>
              <Stat label="Median year" value={`${s.median.toFixed(2)}×`} />
              <Stat label="5th percentile" value={`${s.p5.toFixed(2)}×`} />
              <Stat label="P(halt)" value={pct(s.p_halt, 1)} />
              <Stat label="P(loss)" value={pct(s.p_loss, 1)} />
              <Stat label="P(DD ≥ 20%) in-year" value={pct(s.p_dd_20, 1)} />
              <Stat label="Mean worst drawdown" value={pct(s.mean_max_dd, 1)} />
            </div>
          )}
          {s && s.p_dd_20 > 0.4 && (
            <div className="banner warn" style={{ fontSize: 12.5, marginBottom: 0 }}>
              Expect deep drawdowns even with an edge. Under these inputs, {pct(s.p_dd_20, 0)} of years pass through a 20% drawdown on the way
              to their final result, so GUARD and PRESERVE gears are a normal part of an aggressive year, not a malfunction.
            </div>
          )}
        </div>
        <div className="span-8" style={{ opacity: busy ? 0.55 : 1, transition: "opacity .2s" }}>
          {res && <FanChart bands={res.bands} height={300} />}
          {res && <div style={{ marginTop: 18 }}><div className="card-title" style={{ marginBottom: 6 }}>Where the year ends</div><Histogram counts={res.histogram.counts} edges={res.histogram.edges} height={180} /></div>}
        </div>
      </div>
    </Card>
  );
}

function D12Table() {
  const [rows, setRows] = useState<any[] | null>(null);
  useEffect(() => { get("/api/research/d12").then((d) => setRows(d.rows)).catch(() => setRows([])); }, []);
  return (
    <Card title="D12.1 reproduced by this engine" sub="The same seed and RNG order as research/aggressive_day1_sim.py. ✓ means it matches the master prompt's table." className="span-7" right={<Label text="SIMULATION" />}>
      {!rows ? <div className="empty">Running 180,000 paths…</div> : (
        <table className="t">
          <thead><tr><th>Scenario</th><th>Policy</th><th className="r">Median</th><th className="r">5th pct</th><th className="r">P(halt)</th><th className="r">P(loss)</th><th /></tr></thead>
          <tbody>
            {rows.map((r) => {
              const spec = SPEC_D12[`${r.scenario}|${r.policy}`];
              const ok = spec && Math.abs(spec[0] - r.median) < 0.006 && Math.abs(spec[1] - r.p5) < 0.006;
              return (
                <tr key={r.scenario + r.policy}><td>{r.scenario}</td><td>{r.policy}</td><td className="r num"><b>{r.median.toFixed(2)}×</b></td><td className="r num">{r.p5.toFixed(2)}×</td>
                  <td className="r num">{r.p_halt.toFixed(1)}%</td><td className="r num">{r.p_loss.toFixed(1)}%</td><td>{ok && <Check size={15} color="var(--up)" aria-label="matches spec" />}</td></tr>
              );
            })}
          </tbody>
        </table>
      )}
      <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>The trade-off you accepted: with a real edge, day-1 aggression lifts the typical year from about 1.8× to about 2.5×. With a weak edge or none, the bad year deepens from −15% to −25%, and gaps can overshoot. Phase 5 must re-run this with real out-of-sample statistics.</div>
    </Card>
  );
}

function TargetPlan() {
  const [period, setPeriod] = useState<"day" | "week" | "month">("month");
  const [value, setValue] = useState(8);
  const [unit, setUnit] = useState<"PCT" | "RUPEES">("PCT");
  const [res, setRes] = useState<any>(null);
  const toast = useStore((s) => s.toast);
  async function preview() { setRes(await post("/api/research/target", { period, value, unit })); }
  async function setIt() { const r = await act("target", { period, value, unit }); setRes(r.feasibility); toast(`Target set (${period}): ${r.feasibility.label}`); }
  return (
    <Card title="Target-to-Plan" sub="A target is always accepted, because it never changes risk. Behind target, nothing changes (D5)." className="span-5">
      <div className="stack" style={{ gap: 12 }}>
        <Seg value={period} onChange={setPeriod} options={[{ v: "day", label: "Day" }, { v: "week", label: "Week" }, { v: "month", label: "Month" }]} />
        <div className="row">
          <input className="input num" type="number" value={value} min={0} step={unit === "PCT" ? 0.5 : 100} onChange={(e) => setValue(+e.target.value)} style={{ width: 120 }} aria-label="Target value" />
          <Seg value={unit} onChange={setUnit} options={[{ v: "PCT", label: "%" }, { v: "RUPEES", label: "₹" }]} />
        </div>
        <div className="row"><button className="btn btn-secondary" onClick={preview}>Check feasibility</button><button className="btn btn-primary" onClick={setIt}>Set target</button></div>
        {res && (
          <div className="card card-flat" style={{ padding: 14 }}>
            <div className="eyebrow">Verdict · {res.source}</div>
            <div style={{ fontSize: 18, fontWeight: 600, margin: "6px 0 10px", letterSpacing: "-0.01em" }}>{res.label}</div>
            <KV k="Per period" v={signedPct(res.math.per_period)} />
            <KV k="Compounded per year" v={signedPct(res.math.annualised, 0)} />
            <KV k="Effect on risk" v={res.risk_effect} />
            {res.mc && <><KV k="P(hit)" v={pct(res.mc.p_hit, 1)} /><KV k="Median outcome" v={signedPct(res.mc.median)} /><KV k="Best target at 50% confidence" v={signedPct(res.mc.best_at_50pct)} /></>}
            {res.illustration && (
              <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>Illustration on {res.illustration.sample_trades} {res.source} trades (not validated): P(hit) {pct(res.illustration.p_hit, 0)}, median {signedPct(res.illustration.median)}.</div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function Backtest() {
  const [days, setDays] = useState(3);
  const [seed, setSeed] = useState(7);
  const [compare, setCompare] = useState(false);
  const [job, setJob] = useState<any>(null);
  useEffect(() => {
    if (!job || job.status !== "running") return;
    const id = setInterval(async () => setJob(await get(`/api/research/jobs/${job.id}`)), 1500);
    return () => clearInterval(id);
  }, [job?.id, job?.status]);
  async function start() { const r = await post("/api/research/backtest", { days, seed, compare_staging: compare }); setJob({ id: r.job, status: "running" }); }
  const res = job?.result;
  const rep = res?.report;
  return (
    <Card title="Backtest" sub="The same engine, strategies, funnel, gears and guardian in fast-forward: identical code path (A5.4)" className="span-12" right={<Label text="SIMULATED" />}>
      <div className="row-wrap" style={{ gap: 12, marginBottom: 12 }}>
        <label className="row">Days <input className="input num" type="number" min={1} max={30} value={days} onChange={(e) => setDays(+e.target.value)} style={{ width: 80 }} /></label>
        <label className="row">Seed <input className="input num" type="number" value={seed} onChange={(e) => setSeed(+e.target.value)} style={{ width: 90 }} /></label>
        <label className="row" style={{ fontSize: 13 }}><input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} /> Compare staged vs single entry (AGG-2.2)</label>
        <button className="btn btn-primary" onClick={start} disabled={job?.status === "running"}><Play size={14} /> {job?.status === "running" ? "Running…" : "Run backtest"}</button>
        <span className="faint" style={{ fontSize: 12 }}>About 4 s per simulated day.</span>
      </div>
      {job?.status === "error" && <div className="banner crit">{job.error}</div>}
      {rep && (
        <>
          <div className="kpis" style={{ marginBottom: 14 }}>
            <Stat label="Trades" value={rep.trades} /><Stat label="Net P&L" value={rupees(rep.net_pnl)} />
            <Stat label="Win rate" value={rep.win_rate === null ? "–" : pct(rep.win_rate, 0)} />
            <Stat label="Expectancy" value={rep.expectancy_r === null ? "–" : rMult(rep.expectancy_r)} hint={rep.expectancy_r_ci95 ? `95% CI ${rMult(rep.expectancy_r_ci95[0])} … ${rMult(rep.expectancy_r_ci95[1])}` : ""} />
            <Stat label="Profit factor" value={rep.profit_factor?.toFixed(2) ?? "–"} /><Stat label="Max drawdown" value={pct(rep.max_drawdown, 1)} />
          </div>
          <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>{rep.note} Synthetic market: this validates mechanics and costs, not edge.</div>
          {res.equity_curve?.length > 1 && <EquityChart points={res.equity_curve} height={220} baseline={100000} />}
        </>
      )}
      {res?.rows && (
        <table className="t">
          <thead><tr><th>Seed</th><th>Variant</th><th className="r">Trades</th><th className="r">Net</th><th className="r">Expectancy</th><th className="r">Max DD</th></tr></thead>
          <tbody>{res.rows.flatMap((r: any) => [["staged", r.staged], ["single", r.single]].map(([v, x]: any) => (
            <tr key={r.seed + v}><td>{r.seed}</td><td>{v}</td><td className="r num">{x.trades}</td><td className="r num">{rupees(x.net_pnl)}</td><td className="r num">{x.expectancy_r === null ? "–" : rMult(x.expectancy_r)}</td><td className="r num">{pct(x.max_drawdown, 1)}</td></tr>)))}</tbody>
        </table>
      )}
    </Card>
  );
}

function Feasibility() {
  const [d, setD] = useState<any>(null);
  useEffect(() => { get("/api/feasibility").then(setD).catch(() => undefined); }, []);
  if (!d) return null;
  return (
    <Card title="The ₹1 lakh reality at ATTACK gear" sub={d.label} className="span-12">
      <div className="row-wrap" style={{ marginBottom: 10 }}>{Object.entries(d.budgets).map(([g, v]) => <span key={g} className="pill">{g}: {rupees(v as number)}</span>)}</div>
      <div className="table-wrap">
        <table className="t">
          <thead><tr><th>Structure</th><th>Stop</th><th className="r">Lot</th><th className="r">Premium / price</th><th className="r">Risk per lot</th><th>A+</th><th>A</th><th>B</th><th>2-lot staging (A)</th></tr></thead>
          <tbody>{d.rows.map((r: any, i: number) => (
            <tr key={i}><td>{r.structure}</td><td className="muted">{r.stop}</td><td className="r num">{r.lot}</td><td className="r num">{r.premium.toFixed(2)}</td><td className="r num">{rupees(r.one_lot_risk)}</td>
              {["fits_A+", "fits_A", "fits_B", "two_lot_staging_fits_A"].map((k) => <td key={k}>{r[k] ? <span className="chip chip-up">fits</span> : <span className="chip">skip</span>}</td>)}</tr>
          ))}</tbody>
        </table>
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>At ₹1 lakh, most index-option setups fit one lot for A/A+ only, and two-lot staging rarely fits -- it needs either a higher gear R or a cheap enough premium. Skipping when a lot doesn't fit is correct behaviour, not a malfunction (D2).</div>
    </Card>
  );
}

export function Research() {
  return (
    <div className="view-enter">
      <h1 className="section-title">Research</h1>
      <p className="section-sub">Evidence before conviction. Every number here is labelled with its source. Simulations are not forecasts, and synthetic backtests are not edge (A2).</p>
      <div className="grid g-12">
        <MonteCarlo />
        <D12Table />
        <TargetPlan />
        <Backtest />
        <Feasibility />
      </div>
    </div>
  );
}
