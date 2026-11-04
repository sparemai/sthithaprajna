import {
  AreaSeries, CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries,
  LineSeries, LineStyle, LineType,
  type IChartApi, type IPriceLine, type ISeriesApi, type ISeriesMarkersPluginApi, type LineWidth,
  type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { get } from "../lib/api";
import { num, rupees } from "../lib/format";
import { useStore } from "../lib/store";

const IST = 19800; // lightweight-charts renders UTC; shift so axis labels read in IST

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

function themeColors() {
  return {
    bg: cssVar("--surface"), text: cssVar("--muted"), grid: cssVar("--grid"), axis: cssVar("--line"),
    up: cssVar("--up"), down: cssVar("--down"), accent: cssVar("--accent"), ink: cssVar("--ink"),
    warn: cssVar("--g-guard"), crit: cssVar("--critical"), faint: cssVar("--faint"),
  };
}

function baseChart(el: HTMLElement, c: ReturnType<typeof themeColors>): IChartApi {
  return createChart(el, {
    autoSize: true,
    layout: { background: { type: ColorType.Solid, color: c.bg }, textColor: c.text, fontFamily: getComputedStyle(document.body).fontFamily, fontSize: 11, attributionLogo: false },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    rightPriceScale: { borderColor: c.axis },
    timeScale: { borderColor: c.axis, timeVisible: true, secondsVisible: false },
    crosshair: { mode: CrosshairMode.Normal },
  });
}

export interface PriceLineSpec {
  price: number; label: string;
  kind: "entry" | "stop" | "broker" | "target" | "resistance" | "support" | "fib";
}
export interface StopStep { t: number; stop: number }
export interface ChartMarker { t: number; position: "aboveBar" | "belowBar"; shape: "arrowUp" | "arrowDown" | "circle"; color: string; text?: string }

export function CandleChart({ instrument, tf, showVwap = true, lines = [], stopSteps = [], markers = [], height = 460 }: {
  instrument: string; tf: number; showVwap?: boolean; lines?: PriceLineSpec[]; stopSteps?: StopStep[];
  markers?: ChartMarker[]; height?: number;
}) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volume = useRef<ISeriesApi<"Histogram"> | null>(null);
  const vwap = useRef<ISeriesApi<"Line"> | null>(null);
  const stopLine = useRef<ISeriesApi<"Line"> | null>(null);
  const markersApi = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const plines = useRef<IPriceLine[]>([]);
  const theme = useStore((s) => s.theme);
  const snapTs = useStore((s) => s.snap?.clock.now);
  const [err, setErr] = useState<string | null>(null);
  const fitted = useRef("");

  useEffect(() => {
    if (!el.current) return;
    const c = themeColors();
    const ch = baseChart(el.current, c);
    // hollow up / filled down: shape is the secondary cue beside red/green (dataviz CVD rule)
    candles.current = ch.addSeries(CandlestickSeries, {
      upColor: "rgba(0,0,0,0)", downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down,
    });
    volume.current = ch.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", color: c.faint, lastValueVisible: false, priceLineVisible: false });
    ch.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    vwap.current = ch.addSeries(LineSeries, { color: c.accent, lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
    // the position's actual stop, over time (D-058): a step line, not the single flat line `lines` draws,
    // so a string of tightenings (or the lack of one) is something to look at, not just read in a log.
    stopLine.current = ch.addSeries(LineSeries, {
      color: c.crit, lineWidth: 1, lineStyle: LineStyle.Dashed, lineType: LineType.WithSteps,
      lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
    });
    markersApi.current = createSeriesMarkers(candles.current, []);
    chart.current = ch;
    fitted.current = "";
    return () => { ch.remove(); chart.current = null; plines.current = []; markersApi.current = null; };
  }, [theme]);

  useEffect(() => {
    let alive = true;
    get(`/api/candles?key=${encodeURIComponent(instrument)}&tf=${tf}&limit=320`).then((d) => {
      if (!alive || !candles.current) return;
      setErr(null);
      const c = themeColors();
      candles.current.setData(d.candles.map((b: any) => ({ time: (b.t + IST) as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })));
      volume.current?.setData(d.candles.map((b: any) => ({ time: (b.t + IST) as UTCTimestamp, value: b.v, color: b.c >= b.o ? c.up + "55" : c.down + "55" })));
      vwap.current?.setData(showVwap ? d.vwap.map((p: any) => ({ time: (p.t + IST) as UTCTimestamp, value: p.v })) : []);
      const k = `${instrument}:${tf}:${theme}`;
      if (fitted.current !== k) {
        const n = d.candles.length;
        if (n > 140) chart.current?.timeScale().setVisibleLogicalRange({ from: n - 130, to: n + 4 });
        else chart.current?.timeScale().fitContent();
        fitted.current = k;
      }
    }).catch((e) => alive && setErr(String(e)));
    return () => { alive = false; };
  }, [instrument, tf, snapTs?.slice(0, 16), showVwap, theme]);

  useEffect(() => {
    const s = candles.current;
    if (!s) return;
    plines.current.forEach((p) => s.removePriceLine(p));
    const c = themeColors();
    // D-062: resistance/support (OI-wall derived) and fib (day-range Fibonacci) share this same
    // generic price-line mechanism -- distinct, muted colors and a thinner/dashed style so they read
    // as background analysis, not the position-critical entry/stop/broker/target lines.
    const col = { entry: c.accent, stop: c.crit, broker: c.warn, target: c.up, resistance: c.down, support: c.up, fib: c.faint };
    const width: Record<PriceLineSpec["kind"], LineWidth> = { entry: 2, stop: 2, broker: 1, target: 2, resistance: 1, support: 1, fib: 1 };
    const style: Record<PriceLineSpec["kind"], LineStyle> = {
      entry: LineStyle.Solid, stop: LineStyle.Solid, broker: LineStyle.Dotted, target: LineStyle.Solid,
      resistance: LineStyle.Dashed, support: LineStyle.Dashed, fib: LineStyle.Dotted,
    };
    plines.current = lines.map((l) => s.createPriceLine({ price: l.price, color: col[l.kind], lineWidth: width[l.kind], lineStyle: style[l.kind], axisLabelVisible: true, title: l.label }));
  }, [JSON.stringify(lines), theme]);

  useEffect(() => {
    stopLine.current?.setData(stopSteps.map((p) => ({ time: (p.t + IST) as UTCTimestamp, value: p.stop })));
  }, [JSON.stringify(stopSteps), theme]);

  useEffect(() => {
    const ms: SeriesMarker<Time>[] = markers
      .map((m) => ({ time: (m.t + IST) as UTCTimestamp, position: m.position, shape: m.shape, color: m.color, text: m.text }))
      .sort((a, b) => (a.time as number) - (b.time as number));
    markersApi.current?.setMarkers(ms);
  }, [JSON.stringify(markers), theme]);

  return (
    <div className="chart-box" style={{ height }}>
      <div ref={el} style={{ position: "absolute", inset: 0 }} aria-label={`${instrument} ${tf}-minute candles`} />
      {err && <div className="banner crit" style={{ position: "absolute", top: 8, left: 8 }}>{err}</div>}
    </div>
  );
}

const REGIME_COLORS: Record<string, string> = {
  TREND_UP: "var(--up)", TREND_DOWN: "var(--down)", RANGE: "var(--faint)",
  GAMMA_ACCELERATION: "var(--critical)", BREAKOUT_EXPIRY: "var(--accent)", VOLATILITY_CRUSH: "var(--g-guard)",
  TREND_EXPIRY: "var(--up)", PINNING: "var(--faint)", RANGE_EXPIRY: "var(--faint)",
};

/** D-058: a compact strip of colored segments showing the market regime over the session -- "why no
 * trade was taken" or "why no Zero-to-Hero on expiry day" is often simply "the regime wasn't eligible,"
 * and this makes that visible next to the price action instead of buried in the event log. */
export function RegimeStrip({ field, height = 28 }: { field: "trend" | "expiry"; height?: number }) {
  const [points, setPoints] = useState<{ ts: number; value: string | null }[]>([]);
  const tick = useStore((s) => s.snap?.clock.now);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    get("/api/regime_history").then((d) => {
      setPoints((d.points ?? []).map((p: any) => ({ ts: Date.parse(p.ts), value: p[field] })));
    }).catch(() => undefined);
  }, [field, tick?.slice(0, 10)]);
  if (points.length === 0) return null;
  const now = Date.now();
  const from = points[0].ts, to = Math.max(now, points[points.length - 1].ts);
  const span = Math.max(1, to - from);
  const segs = points.map((p, i) => {
    const start = p.ts, end = i + 1 < points.length ? points[i + 1].ts : to;
    return { value: p.value, start, end, pct: ((end - start) / span) * 100 };
  });
  return (
    <div style={{ marginBottom: 8 }}>
      <div className="row" style={{ height, borderRadius: 4, overflow: "hidden", position: "relative" }}>
        {segs.map((s, i) => (
          <div key={i} onPointerEnter={() => setHover(i)} onPointerLeave={() => setHover(null)}
               style={{ width: `${s.pct}%`, height: "100%", background: (s.value && REGIME_COLORS[s.value]) || "var(--grid)", opacity: hover === null || hover === i ? 0.85 : 0.4, cursor: "default" }} />
        ))}
      </div>
      {hover !== null && (
        <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
          {new Date(segs[hover].start).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}
          {" – "}
          {new Date(segs[hover].end).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}:{" "}
          <b>{segs[hover].value ?? "none"}</b>
        </div>
      )}
    </div>
  );
}

export function EquityChart({ points, height = 260, baseline }: { points: { t: number; equity: number }[]; height?: number; baseline?: number }) {
  const el = useRef<HTMLDivElement>(null);
  const series = useRef<ISeriesApi<"Area"> | null>(null);
  const chart = useRef<IChartApi | null>(null);
  const theme = useStore((s) => s.theme);
  useEffect(() => {
    if (!el.current) return;
    const c = themeColors();
    const ch = baseChart(el.current, c);
    series.current = ch.addSeries(AreaSeries, { lineColor: c.accent, topColor: c.accent + "26", bottomColor: c.accent + "00", lineWidth: 2, priceLineVisible: false });
    if (baseline) series.current.createPriceLine({ price: baseline, color: c.faint, lineWidth: 1, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: "start" });
    chart.current = ch;
    return () => { ch.remove(); chart.current = null; };
  }, [theme, baseline]);
  useEffect(() => {
    if (!series.current) return;
    const seen = new Set<number>();
    const data = points.filter((p) => (seen.has(p.t) ? false : (seen.add(p.t), true))).map((p) => ({ time: (p.t + IST) as UTCTimestamp, value: p.equity }));
    series.current.setData(data);
    chart.current?.timeScale().fitContent();
  }, [points, theme]);
  return <div className="chart-box" style={{ height }}><div ref={el} style={{ position: "absolute", inset: 0 }} /></div>;
}

/** Monte Carlo fan: p5-p95 and p25-p75 bands (one hue, two steps) + a 2px median, crosshair tooltip. */
export function FanChart({ bands, height = 280 }: { bands: Record<string, number[]>; height?: number }) {
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const W = 800, H = height, L = 48, R = 16, T = 12, B = 28;
  const n = bands.p50?.length ?? 0;
  const { lo, hi, x, y, ticks } = useMemo(() => {
    const all = [...(bands.p5 ?? []), ...(bands.p95 ?? [])];
    const lo = Math.min(1, ...all) * 0.98, hi = Math.max(1, ...all) * 1.02;
    const x = (i: number) => L + (i / Math.max(1, n - 1)) * (W - L - R);
    const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const step = (hi - lo) / 4;
    const ticks = [0, 1, 2, 3, 4].map((k) => lo + k * step);
    return { lo, hi, x, y, ticks };
  }, [bands, n, H]);
  if (!n) return null;
  const area = (a: number[], b: number[]) => `M ${a.map((v, i) => `${x(i)} ${y(v)}`).join(" L ")} L ${b.map((_, i) => `${x(n - 1 - i)} ${y(b[n - 1 - i])}`).join(" L ")} Z`;
  const line = (a: number[]) => `M ${a.map((v, i) => `${x(i)} ${y(v)}`).join(" L ")}`;
  const onMove = (e: React.PointerEvent) => {
    const r = ref.current!.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    setHover(Math.max(0, Math.min(n - 1, Math.round(((px - L) / (W - L - R)) * (n - 1)))));
  };
  return (
    <div className="chart-box">
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} width="100%" height={H} onPointerMove={onMove} onPointerLeave={() => setHover(null)} role="img" aria-label="Monte Carlo equity percentile bands">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="var(--grid)" strokeWidth={1} />
            <text x={L - 8} y={y(t) + 4} textAnchor="end" fontSize={11} fill="var(--muted)" className="num">{t.toFixed(2)}×</text>
          </g>
        ))}
        {lo < 1 && hi > 1 && <line x1={L} x2={W - R} y1={y(1)} y2={y(1)} stroke="var(--axis)" strokeWidth={1} />}
        <path d={area(bands.p95, bands.p5)} fill="var(--accent)" fillOpacity={0.1} />
        <path d={area(bands.p75, bands.p25)} fill="var(--accent)" fillOpacity={0.2} />
        <path d={line(bands.p50)} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round" />
        <text x={x(n - 1) - 4} y={y(bands.p50[n - 1]) - 8} textAnchor="end" fontSize={11} fill="var(--ink)" fontWeight={600}>median {bands.p50[n - 1].toFixed(2)}×</text>
        {[0, Math.floor(n / 2), n - 1].map((i, j) => <text key={i} x={x(i)} y={H - 8} textAnchor={j === 0 ? "start" : j === 2 ? "end" : "middle"} fontSize={11} fill="var(--muted)">trade {i + 1}</text>)}
        {hover !== null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} stroke="var(--ink)" strokeOpacity={0.35} strokeWidth={1} />
            <circle cx={x(hover)} cy={y(bands.p50[hover])} r={4} fill="var(--accent)" stroke="var(--surface)" strokeWidth={2} />
          </g>
        )}
      </svg>
      {hover !== null && (
        <div className="viz-tip" style={{ left: `calc(${(x(hover) / W) * 100}% + 12px)`, top: 10 }}>
          <div className="muted" style={{ marginBottom: 4 }}>After trade {hover + 1}</div>
          {(["p95", "p75", "p50", "p25", "p5"] as const).map((k) => (
            <div key={k} className="row between" style={{ gap: 16 }}><span className="muted">{k === "p50" ? "median" : k.replace("p", "") + "th pct"}</span><span className="tv">{bands[k][hover].toFixed(3)}×</span></div>
          ))}
        </div>
      )}
      <div className="legend" style={{ marginTop: 6 }}>
        <span><i style={{ background: "var(--accent)" }} />median path</span>
        <span><i className="box" style={{ background: "var(--accent)", opacity: 0.35 }} />25th–75th pct</span>
        <span><i className="box" style={{ background: "var(--accent)", opacity: 0.15 }} />5th–95th pct</span>
      </div>
    </div>
  );
}

/** Terminal-equity histogram: single series (slot 1), <=24px bars, 2px gaps, per-bar hover. */
export function Histogram({ counts, edges, height = 200, marker = 1 }: { counts: number[]; edges: number[]; height?: number; marker?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 800, H = height, L = 40, R = 10, T = 10, B = 26;
  const max = Math.max(1, ...counts);
  const bw = (W - L - R) / counts.length;
  const barW = Math.min(24, bw - 2);
  const xv = (v: number) => L + ((v - edges[0]) / (edges[edges.length - 1] - edges[0])) * (W - L - R);
  const total = counts.reduce((a, b) => a + b, 0);
  return (
    <div className="chart-box">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="Distribution of terminal equity">
        <line x1={L} x2={W - R} y1={H - B} y2={H - B} stroke="var(--axis)" strokeWidth={1} />
        {counts.map((c, i) => {
          const h = (c / max) * (H - T - B);
          const x = L + i * bw + (bw - barW) / 2;
          const y0 = H - B - h;
          const loss = edges[i + 1] <= marker;
          return (
            <g key={i} onPointerEnter={() => setHover(i)} onPointerLeave={() => setHover(null)}>
              <rect x={L + i * bw} y={T} width={bw} height={H - T - B} fill="transparent" />
              {h > 0 && <path d={`M ${x} ${H - B} V ${y0 + Math.min(4, h)} Q ${x} ${y0} ${x + Math.min(4, barW / 2)} ${y0} H ${x + barW - Math.min(4, barW / 2)} Q ${x + barW} ${y0} ${x + barW} ${y0 + Math.min(4, h)} V ${H - B} Z`}
                fill={loss ? "var(--faint)" : "var(--accent)"} opacity={hover === null || hover === i ? 1 : 0.55} />}
            </g>
          );
        })}
        <line x1={xv(marker)} x2={xv(marker)} y1={T} y2={H - B} stroke="var(--ink)" strokeOpacity={0.5} strokeWidth={1} />
        <text x={xv(marker) + 4} y={T + 10} fontSize={11} fill="var(--muted)">break-even</text>
        {[0, 1, 2, 3, 4, 5, 6].filter((v) => v >= edges[0] && v <= edges[edges.length - 1]).map((v) => (
          <text key={v} x={xv(v)} y={H - 8} fontSize={11} textAnchor="middle" fill="var(--muted)">{v}×</text>
        ))}
      </svg>
      {hover !== null && (
        <div className="viz-tip" style={{ left: `${((L + hover * bw) / W) * 100}%`, top: 4 }}>
          <div className="tv">{counts[hover].toLocaleString("en-IN")} paths ({((counts[hover] / total) * 100).toFixed(1)}%)</div>
          <div className="muted">{edges[hover].toFixed(2)}× – {edges[hover + 1].toFixed(2)}×</div>
        </div>
      )}
      <div className="legend" style={{ marginTop: 6 }}>
        <span><i className="box" style={{ background: "var(--accent)" }} />ended above start</span>
        <span><i className="box" style={{ background: "var(--faint)" }} />ended at or below start</span>
        <span className="faint">rightmost bar includes every path at or above {edges[edges.length - 1]}×</span>
      </div>
    </div>
  );
}

export function Sparkline({ values, width = 120, height = 32 }: { values: number[]; width?: number; height?: number }) {
  if (values.length < 2) return <svg width={width} height={height} />;
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const d = values.map((v, i) => `${i ? "L" : "M"} ${(i / (values.length - 1)) * (width - 4) + 2} ${height - 3 - ((v - lo) / span) * (height - 6)}`).join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={width} height={height} aria-hidden="true">
      <path d={d} fill="none" stroke={up ? "var(--up)" : "var(--down)"} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export function money(v: number) { return rupees(v); }
export function price(v: number) { return num(v); }
