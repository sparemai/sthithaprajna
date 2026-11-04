import { ArrowDownRight, ArrowUpRight, CandlestickChart, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { get } from "../lib/api";
import { compact, num, signedPct } from "../lib/format";
import { asWatch, useStore } from "../lib/store";
import type { ScannerSummary, ScreenRow } from "../lib/types";
import { Card, Seg, Stat } from "../components/ui";

type SegF = "all" | "fno" | "cash";
type DirF = "all" | "up" | "down";

function ScoreBar({ v }: { v: number }) {
  return (
    <div className="score" title={`Booming score ${(v * 100).toFixed(0)} / 100`}>
      <div className="score-track"><span style={{ width: `${Math.max(2, Math.min(1, v) * 100)}%` }} /></div>
      <span className="num">{(v * 100).toFixed(0)}</span>
    </div>
  );
}

export function Scanner() {
  const snap = useStore((s) => s.snap);
  const select = useStore((s) => s.select);
  const [seg, setSeg] = useState<SegF>("all");
  const [dir, setDir] = useState<DirF>("all");
  const [tradable, setTradable] = useState(true);
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<ScreenRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<ScannerSummary | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    const load = () => {
      const p = new URLSearchParams({ segment: seg, direction: dir, tradable: tradable ? "yes" : "all", q, limit: "300" });
      get(`/api/scanner?${p}`).then((d) => {
        if (!alive) return;
        setRows(d.rows); setTotal(d.total); setSummary(d.summary); setErr("");
      }).catch((e) => alive && setErr(String(e.message ?? e)));
    };
    load();
    const t = setInterval(load, 5000);
    return () => { alive = false; clearInterval(t); };
  }, [seg, dir, tradable, q]);

  if (!snap) return null;
  const s = summary ?? snap.scanner.summary;
  const live = snap.market?.source !== "SIM";
  return (
    <div className="view-enter">
      <h1 className="section-title">Scanner</h1>
      <p className="section-sub">
        Every {live ? "NSE stock in Dhan's master" : "stock in the simulated universe"} is screened continuously for booming
        (or breaking-down) moves: move against its normal range, relative volume for the time of day, breakouts (day,
        previous day, 52-week), VWAP, momentum and order-book pressure. Sthitaprajna 2.0 is options-only: the best
        F&amp;O-eligible ones join the strategy universe automatically and are traded through their options.
        Non-F&amp;O names are shown for context and are never traded here.
      </p>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="kpis">
          <Stat label="Universe" value={s.universe ?? "–"} hint={`${s.fresh ?? 0} with live prices`} />
          <Stat label="Ranked now" value={s.ranked} hint={`${s.baselines ?? 0} with daily baselines${s.baselines_pending ? `, ${s.baselines_pending} loading` : ""}`} />
          <Stat label="Booming (tradable)" value={s.booming} hint="score ≥ 55" />
          <Stat label="In the strategy universe" value={s.promoted?.length ?? 0} hint={(s.promoted ?? []).slice(0, 6).map((k) => k.split(":")[1]).join(", ") || "none promoted yet"} />
          <Stat label="With options" value={s.fno ?? "–"} hint="F&O stocks" />
        </div>
        <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>Score weights: {s.weights}. The score ranks candidates; strategies still decide each entry on their own analysis.</div>
      </div>

      <Card title="Ranked universe" sub={`${total} match`}
        right={
          <div className="row-wrap" style={{ gap: 8 }}>
            <label className="row" style={{ position: "relative" }}>
              <Search size={14} style={{ position: "absolute", left: 10, color: "var(--faint)" }} />
              <input className="input" style={{ paddingLeft: 30, width: 170, height: 32 }} placeholder="Symbol or name" value={q}
                onChange={(e) => setQ(e.target.value)} aria-label="Filter scanner" />
            </label>
            <Seg value={seg} onChange={setSeg} options={[{ v: "all", label: "All" }, { v: "fno", label: "Options-eligible" }, { v: "cash", label: "No options" }]} />
            <Seg value={dir} onChange={setDir} options={[{ v: "all", label: "Both" }, { v: "up", label: "Up" }, { v: "down", label: "Down" }]} />
            <Seg value={tradable ? "yes" : "all"} onChange={(v) => setTradable(v === "yes")} options={[{ v: "yes", label: "Tradable" }, { v: "all", label: "Everything" }]} />
          </div>
        }>
        {err && <div className="banner warn" style={{ marginBottom: 10 }}>Scanner unavailable: {err}</div>}
        {rows === null ? <div className="empty">Loading the scanner…</div> : rows.length === 0 ? (
          <div className="empty">{s.ranked ? "Nothing matches these filters." : "The scanner fills in once prices arrive (it needs the market's first quotes)."}</div>
        ) : (
          <div className="table-wrap" style={{ maxHeight: "66vh" }}>
            <table className="t scanner-t">
              <thead><tr>
                <th>Symbol</th><th className="r">LTP</th><th className="r">Change</th><th>Score</th><th>Move</th>
                <th className="r">RVOL</th><th className="r">Turnover</th><th>Why it is on the list</th><th>Options</th><th>Status</th><th />
              </tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.key} className="clickable" onClick={() => select(r.key, "terminal", asWatch(r))}>
                    <td><b>{r.symbol}</b><div className="faint" style={{ fontSize: 11, maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>{r.name}</div></td>
                    <td className="r num">{num(r.ltp)}</td>
                    <td className={`r num ${r.chg_pct > 0 ? "up" : r.chg_pct < 0 ? "down" : ""}`}>{signedPct(r.chg_pct, 2, false)}</td>
                    <td><ScoreBar v={r.score} /></td>
                    <td>{r.direction === "UP"
                      ? <span className="up row" style={{ gap: 2 }}><ArrowUpRight size={14} />Up</span>
                      : <span className="down row" style={{ gap: 2 }}><ArrowDownRight size={14} />Down</span>}</td>
                    <td className="r num">{r.rvol === null ? "–" : `${r.rvol.toFixed(1)}×`}</td>
                    <td className="r num">₹{compact(r.turnover_cr * 1e7)}</td>
                    <td className="flags"><div className="row-wrap" style={{ gap: 4 }}>{r.flags.slice(0, 3).map((f) => <span key={f} className="chip">{f}</span>)}</div></td>
                    <td>{r.fno ? <span className="chip chip-accent">lot {r.lot}</span> : <span className="faint" title="No options on this name: never traded in 2.0">no options</span>}</td>
                    <td>{r.tradable
                      ? (r.active ? <span className="chip chip-up" title="In the strategy universe: strategies evaluate it bar by bar">active</span> : <span className="chip">tradable</span>)
                      : <span className="chip chip-warn" title={r.why_not}>{r.why_not.length > 26 ? `${r.why_not.slice(0, 26)}…` : r.why_not}</span>}</td>
                    <td><button className="icon-btn" title="Chart" aria-label={`Chart ${r.symbol}`} onClick={(e) => { e.stopPropagation(); select(r.key, "terminal", asWatch(r)); }}><CandlestickChart size={14} /></button></td>
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
