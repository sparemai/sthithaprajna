import { useEffect, useMemo, useState } from "react";
import { get } from "../lib/api";
import { compact, dirClass, glyph, num, signedPct } from "../lib/format";
import { asWatch, useStore } from "../lib/store";
import { CandleChart, RegimeStrip, type ChartMarker, type PriceLineSpec, type StopStep } from "../components/charts";
import { DecisionDrawer } from "../components/Overlays";
import { Card, Grade, KV, Seg, Toggle } from "../components/ui";
import type { FunnelRecord } from "../lib/types";

const toT = (iso: string) => Math.floor(Date.parse(iso) / 1000);

/** The stop's own value over time for one position: its initial stop at entry, every recorded
 * tightening in `stop_history` (D-058), and a final point holding the latest stop so the step line
 * visibly reaches the exit (or now, if still open). */
function stopStepsFor(p: any): StopStep[] {
  if (!p) return [];
  const steps: StopStep[] = [{ t: toT(p.opened_at), stop: p.initial_stop }];
  for (const h of p.stop_history ?? []) {
    if (typeof h.stop === "number") steps.push({ t: toT(h.ts), stop: h.stop });
  }
  const endT = p.closed_at ? toT(p.closed_at) : Math.floor(Date.now() / 1000);
  if (steps[steps.length - 1].t < endT) steps.push({ t: endT, stop: p.stop });
  return steps;
}

/** D-058: closed trades on this instrument, for the exit-history markers/stop-steps below -- the live
 * `pos` list only ever covers what's still open. Capped at a recent window; this is explainability, not
 * a full trade browser (that's the Plan page's job). */
function useClosedTrades(symbol: string | undefined) {
  const [trades, setTrades] = useState<any[]>([]);
  useEffect(() => {
    if (!symbol) { setTrades([]); return; }
    let alive = true;
    get("/api/trades?limit=400").then((d) => {
      if (alive) setTrades((d.trades ?? []).filter((t: any) => t.symbol === symbol));
    }).catch(() => undefined);
    return () => { alive = false; };
  }, [symbol]);
  return trades;
}

const FIB_PCTS = [23.6, 38.2, 50.0, 61.8, 78.6]; // D-062: skip 0/100 as lines -- the candles already show the day's own high/low

/** D-062: display-only technical analysis for the selected instrument -- reversal patterns, OI-wall
 * support/resistance (index/F&O-stock underlyings only), and day-range Fibonacci retracement. Never
 * read by any strategy; purely for the operator's own reading, same spirit as the Chain page's figures
 * before some of them became Confirmations. */
function useChartOverlays(key: string | undefined, tf: number) {
  const [data, setData] = useState<{ patterns: any[]; support_resistance: any; fibonacci: any } | null>(null);
  const tick = useStore((s) => s.snap?.clock.now);
  useEffect(() => {
    if (!key) { setData(null); return; }
    let alive = true;
    get(`/api/chart_overlays?key=${encodeURIComponent(key)}&tf=${tf}&limit=320`).then((d) => {
      if (alive) setData(d);
    }).catch(() => undefined);
    return () => { alive = false; };
  }, [key, tf, tick?.slice(0, 16)]);
  return data;
}

function Depth({ instrument }: { instrument: string }) {
  const [d, setD] = useState<{ bids: any[]; asks: any[] }>({ bids: [], asks: [] });
  const tick = useStore((s) => s.snap?.clock.now);
  useEffect(() => { get(`/api/depth?key=${encodeURIComponent(instrument)}`).then(setD).catch(() => undefined); }, [instrument, tick?.slice(0, 18)]);
  const max = Math.max(1, ...d.bids.map((b) => b.qty), ...d.asks.map((a) => a.qty));
  const tb = d.bids.reduce((s, b) => s + b.qty, 0), ta = d.asks.reduce((s, a) => s + a.qty, 0);
  return (
    <div>
      <div className="depth">
        <div>
          <div className="depth-row muted" style={{ fontSize: 11 }}><span>Bid</span><span style={{ textAlign: "right" }}>Qty</span></div>
          {d.bids.map((b, i) => (
            <div className="depth-row" key={i}><div className="bar" style={{ right: 0, width: `${(b.qty / max) * 100}%`, background: "var(--up)" }} />
              <span className="up">{num(b.price)}</span><span style={{ textAlign: "right" }}>{b.qty.toLocaleString("en-IN")}</span></div>
          ))}
        </div>
        <div>
          <div className="depth-row muted" style={{ fontSize: 11 }}><span>Ask</span><span style={{ textAlign: "right" }}>Qty</span></div>
          {d.asks.map((a, i) => (
            <div className="depth-row" key={i}><div className="bar" style={{ left: 0, width: `${(a.qty / max) * 100}%`, background: "var(--down)" }} />
              <span className="down">{num(a.price)}</span><span style={{ textAlign: "right" }}>{a.qty.toLocaleString("en-IN")}</span></div>
          ))}
        </div>
      </div>
      <div className="row between muted" style={{ fontSize: 11, marginTop: 6 }}>
        <span>Total bids {tb.toLocaleString("en-IN")}</span><span>Imbalance {tb + ta ? ((tb - ta) / (tb + ta) * 100).toFixed(0) : 0}%</span><span>Total asks {ta.toLocaleString("en-IN")}</span>
      </div>
    </div>
  );
}

export function Terminal() {
  const { snap, selected, select, extra } = useStore();
  const [tf, setTf] = useState<"1" | "5" | "15">("5");
  const [vwap, setVwap] = useState(true);
  const [analysis, setAnalysis] = useState(true);
  const [rec, setRec] = useState<FunnelRecord | null>(null);
  const row = snap?.scanner.top.find((x) => x.key === selected);
  const w = snap?.watch.find((x) => x.key === selected) ?? (row ? asWatch(row) : extra[selected]) ?? snap?.watch[0];
  const pos = snap?.positions.filter((p) => p.underlying === selected || p.instrument.key === selected) ?? [];
  const openOnSel = pos.filter((p) => p.instrument.key === selected);
  const closedOnSel = useClosedTrades(w?.symbol);
  const lines: PriceLineSpec[] = useMemo(() => openOnSel.flatMap((p) => [
    { price: p.avg_entry, label: `${p.symbol} entry`, kind: "entry" as const },
    { price: p.stop, label: "stop", kind: "stop" as const },
    { price: p.broker_stop, label: "broker stop", kind: "broker" as const },
    ...(p.target > 0 ? [{ price: p.target, label: "target", kind: "target" as const }] : []),
  ]), [JSON.stringify(openOnSel.map((p) => [p.avg_entry, p.stop, p.broker_stop, p.target]))]);
  // D-058: the position's own stop drawn as a step line (not just the flat `lines` above), plus trade
  // entry/exit markers, so "was the exit perfect" is something to look at on this instrument's own
  // chart -- the most recent position on it (the open one if there is one, else the latest closed trade).
  const chartPos = openOnSel[0] ?? closedOnSel[0];
  const stopSteps: StopStep[] = useMemo(() => stopStepsFor(chartPos),
    [chartPos?.id, chartPos?.stop, JSON.stringify(chartPos?.stop_history)]);
  const tradeHistory = useMemo(() => [...openOnSel, ...closedOnSel], [openOnSel, closedOnSel]);
  const markers: ChartMarker[] = useMemo(() => tradeHistory.flatMap((p: any) => {
    const firstLeg = (p.legs ?? [])[0];
    const entry = firstLeg ? [{ t: toT(firstLeg.ts), position: "belowBar" as const, shape: "arrowUp" as const,
      color: "var(--accent)", text: `entry @ ${num(firstLeg.price)}` }] : [];
    const exits = (p.closed_legs ?? []).map((l: any) => ({
      t: toT(l.ts), position: "aboveBar" as const, shape: "arrowDown" as const,
      color: l.pnl >= 0 ? "var(--up)" : "var(--down)",
      text: `${l.reason} @ ${num(l.price)} (${l.pnl >= 0 ? "+" : ""}${Math.round(l.pnl)})`,
    }));
    return [...entry, ...exits];
  }), [JSON.stringify(tradeHistory.map((p: any) => p.id))]);
  // D-062: display-only technical analysis -- resistance/support (OI walls), Fibonacci (day range) as
  // price lines alongside the position lines above, pattern detections merged into the same markers the
  // trade entry/exit arrows use (a different shape so they're not confused for an actual fill).
  const overlays = useChartOverlays(w?.key, Number(tf));
  const levels: PriceLineSpec[] = useMemo(() => {
    if (!analysis || !overlays) return [];
    const out: PriceLineSpec[] = [];
    const sr = overlays.support_resistance;
    if (sr) {
      sr.resistance.forEach((r: any, i: number) => out.push({ price: r.strike, label: `R${i + 1} ${num(r.strike)}`, kind: "resistance" as const }));
      sr.support.forEach((r: any, i: number) => out.push({ price: r.strike, label: `S${i + 1} ${num(r.strike)}`, kind: "support" as const }));
    }
    const fib = overlays.fibonacci;
    if (fib) {
      for (const pct of FIB_PCTS) {
        const price = fib.levels[String(pct)] ?? fib.levels[pct.toFixed(1)];
        if (typeof price === "number") out.push({ price, label: `fib ${pct}%`, kind: "fib" as const });
      }
    }
    return out;
  }, [analysis, JSON.stringify(overlays?.support_resistance), JSON.stringify(overlays?.fibonacci)]);
  const patternMarkers: ChartMarker[] = useMemo(() => {
    if (!analysis || !overlays) return [];
    const byT = new Map<number, { bullish: boolean; labels: string[] }>();
    for (const p of overlays.patterns ?? []) {
      const e = byT.get(p.t) ?? { bullish: p.bullish, labels: [] as string[] };
      e.labels.push(...p.labels);
      byT.set(p.t, e);
    }
    return [...byT.entries()].map(([t, e]) => ({
      t, position: e.bullish ? "belowBar" as const : "aboveBar" as const, shape: "circle" as const,
      color: e.bullish ? "var(--up)" : "var(--down)", text: e.labels.join(", ").replace(/_/g, " "),
    }));
  }, [analysis, JSON.stringify(overlays?.patterns)]);
  const allLines = useMemo(() => [...lines, ...levels], [lines, levels]);
  const allMarkers = useMemo(() => [...markers, ...patternMarkers], [markers, patternMarkers]);
  const cands = (snap?.funnel.records ?? []).filter((r) => r.underlying === selected || r.instrument === selected);
  if (!snap || !w) return null;
  return (
    <div className="view-enter">
      <div className="row between" style={{ marginBottom: 14, flexWrap: "wrap", gap: 12 }}>
        <div>
          <div className="eyebrow">{w.exchange} · {w.kind === "INDEX" ? "Index" : w.sector}</div>
          <div className="row" style={{ gap: 14, alignItems: "baseline", flexWrap: "wrap" }}>
            <h1 style={{ margin: 0, fontSize: 30, letterSpacing: "-0.02em" }}>{w.symbol}</h1>
            <span style={{ fontSize: 26, fontWeight: 600 }} className="num">{num(w.ltp)}</span>
            <span className={`num ${dirClass(w.change)}`} style={{ fontSize: 15, fontWeight: 500 }}>{glyph(w.change)} {num(w.change)} ({signedPct(w.change_pct, 2, false)})</span>
          </div>
          <div className="muted" style={{ fontSize: 13 }}>{w.name}</div>
        </div>
        <div className="row" style={{ gap: 12 }}>
          <span className="row muted" style={{ fontSize: 12, gap: 8 }}>VWAP <Toggle on={vwap} onChange={setVwap} label="VWAP overlay" /></span>
          <span className="row muted" style={{ fontSize: 12, gap: 8 }}>Analysis <Toggle on={analysis} onChange={setAnalysis} label="Resistance/support, patterns, Fibonacci" /></span>
          <Seg value={tf} onChange={setTf} options={[{ v: "1", label: "1m" }, { v: "5", label: "5m" }, { v: "15", label: "15m" }]} />
        </div>
      </div>
      <div className="grid g-12">
        <div className="card span-9" style={{ padding: 10 }}>
          <div style={{ padding: "2px 8px 0" }}>
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>Expiry regime (today)</div>
            {snap.product !== "intraday500" && <RegimeStrip field="expiry" />}
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>Trend regime</div>
            <RegimeStrip field="trend" />
          </div>
          <CandleChart instrument={w.key} tf={Number(tf)} showVwap={vwap} lines={allLines} stopSteps={stopSteps} markers={allMarkers} height={500} />
          <div className="legend" style={{ padding: "8px 8px 2px" }}>
            <span><i className="box" style={{ boxShadow: "inset 0 0 0 2px var(--up)" }} />up bar (hollow)</span>
            <span><i className="box" style={{ background: "var(--down)" }} />down bar (filled)</span>
            {vwap && <span><i style={{ background: "var(--accent)" }} />session VWAP</span>}
            {stopSteps.length > 0 && <span><i style={{ background: "var(--critical)" }} />stop over time</span>}
            {markers.length > 0 && <span><i style={{ background: "var(--accent)" }} />trade entry / exit</span>}
            {analysis && levels.some((l) => l.kind === "resistance") && <span><i style={{ background: "var(--down)" }} />resistance (OI wall)</span>}
            {analysis && levels.some((l) => l.kind === "support") && <span><i style={{ background: "var(--up)" }} />support (OI wall)</span>}
            {analysis && levels.some((l) => l.kind === "fib") && <span><i style={{ background: "var(--faint)" }} />Fibonacci (day range)</span>}
            {analysis && patternMarkers.length > 0 && <span><i className="box" style={{ boxShadow: "inset 0 0 0 2px var(--muted)", borderRadius: "50%" }} />candlestick pattern</span>}
            <span className="faint">The last bar may still be forming (display only). Strategies act on completed bars. Analysis is display-only, never read by any strategy.</span>
          </div>
        </div>
        <div className="span-3 stack" style={{ gap: 16 }}>
          <Card title="Market depth" sub="Top 5 levels">
            <Depth instrument={w.key} />
          </Card>
          <Card title="Session">
            <KV k="Open" v={num(w.open)} /><KV k="High" v={num(w.high)} /><KV k="Low" v={num(w.low)} />
            <KV k="Prev close" v={num(w.prev_close)} /><KV k="Volume" v={compact(w.volume)} />
          </Card>
        </div>
        <Card title="Why (or why not) a trade here" sub="Every candidate records the last stage it reached (AGG-2.1)" className="span-12">
          {cands.length === 0 ? <div className="empty" style={{ padding: 16 }}>No setups forming on {w.symbol} right now.</div> : (
            <div className="table-wrap">
              <table className="t">
                <thead><tr><th>Strategy</th><th>Instrument</th><th>Exposure</th><th>Grade</th><th>Stage</th><th>Status</th><th className="r">EV</th><th>Reason</th></tr></thead>
                <tbody>
                  {cands.map((r) => (
                    <tr key={r.id} className="clickable" onClick={() => setRec(r)}>
                      <td>{r.strategy}</td><td>{r.symbol}</td><td>{r.exposure}</td><td><Grade g={r.grade} /></td><td><span className="chip">{r.stage}</span></td>
                      <td>{r.status}</td><td className="r num">{r.ev ? r.ev.net_r.toFixed(2) + "R" : "–"}</td><td className="muted" style={{ whiteSpace: "normal", minWidth: 240 }}>{r.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        {pos.length > 0 && (
          <Card title="Positions on this underlying" className="span-12">
            {pos.map((p) => <div key={p.id} className="row between" style={{ padding: "6px 0" }}><b>{p.symbol}</b><span>{p.stage}</span><span className="num">{p.qty} @ {num(p.avg_entry)}</span><span className={p.net_pnl >= 0 ? "up" : "down"}>{num(p.net_pnl)}</span>
              <button className="btn btn-ghost btn-sm" onClick={() => select(p.instrument.key, "terminal")}>Chart {p.symbol}</button></div>)}
          </Card>
        )}
      </div>
      <DecisionDrawer rec={rec} onClose={() => setRec(null)} />
    </div>
  );
}
