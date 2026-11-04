import { useMemo } from "react";
import { GEAR_COLOR, pct, rMult } from "../lib/format";
import type { GearName } from "../lib/types";

const GEARS: GearName[] = ["ATTACK", "PRESS", "GUARD", "PRESERVE", "HALTED"];

/** Tesla-style drive selector: the active gear is lit; the rest recede. Gears move only by rule. */
export function GearSelector({ gear, effective, note }: { gear: GearName; effective: GearName; note?: string }) {
  return (
    <div className="gearsel" aria-label={`Gear ${effective}`}>
      <div className="eyebrow ttl">Gear</div>
      {GEARS.map((g) => (
        <div key={g} className={`gear-opt ${g === effective ? "on" : ""}`} style={{ ["--gc" as string]: GEAR_COLOR[g] }}>
          {g}<span className="g-dot" />
        </div>
      ))}
      <div className="gear-note">{effective !== gear ? `Ladder ${gear}; lowered today by rule.` : note}</div>
    </div>
  );
}

/** Headroom to the 25% halt, shown like a Tesla battery range. Ticks mark each downshift threshold. */
export function HeadroomBattery({ drawdown, halt = 0.25, ladder }: { drawdown: number; halt?: number; ladder: { gear: string; dd_below: number }[] }) {
  const head = Math.max(0, halt - drawdown);
  const f = head / halt;
  const color = f > 0.6 ? "var(--up)" : f > 0.4 ? "var(--g-guard)" : f > 0.2 ? "var(--g-preserve)" : "var(--critical)";
  return (
    <div className="battery-wrap">
      <div className="eyebrow">Headroom to halt</div>
      <div className="range num" style={{ marginTop: 8 }}>{(head * 100).toFixed(1)}<small>%</small></div>
      <div className="battery" role="meter" aria-valuenow={head} aria-valuemin={0} aria-valuemax={halt} aria-label="Drawdown headroom">
        <div className="battery-fill" style={{ width: `${f * 100}%`, background: color }} />
        {ladder.filter((l) => l.dd_below < halt).map((l) => (
          <span key={l.gear} className="battery-tick" style={{ left: `calc(4px + ${(1 - l.dd_below / halt)} * (100% - 8px))` }} />
        ))}
      </div>
      <div className="battery-ticks" aria-hidden="true">
        {ladder.filter((l) => l.dd_below < halt).map((l) => (
          <span key={l.gear} style={{ left: `${(1 - l.dd_below / halt) * 100}%` }}>{(l.dd_below * 100).toFixed(0)}%</span>
        ))}
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>Drawdown {pct(drawdown)} · ticks mark downshifts</div>
    </div>
  );
}

/** Hero landscape: today's equity curve drawn as a calm horizon. */
/** x = time of day (09:15 -> 15:30), so the horizon grows across the session like a sunrise path. */
export function Horizon({ points }: { points: { t: number; v: number }[] }) {
  const d = useMemo(() => {
    if (points.length < 2) return null;
    const w = 1000, h = 220;
    const vals = points.map((p) => p.v);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const span = hi - lo || Math.max(1, Math.abs(hi) * 0.002);
    const tod = (t: number) => ((t + 19800) % 86400 - (9 * 3600 + 15 * 60)) / (375 * 60);
    const xy = [...points].sort((a, b) => a.t - b.t)
      .map((p) => [Math.max(0, Math.min(1, tod(p.t))) * w, h - 30 - ((p.v - lo) / span) * (h * 0.55)] as const);
    let path = `M ${xy[0][0]} ${xy[0][1]}`;
    for (let i = 1; i < xy.length; i++) {
      const [x0, y0] = xy[i - 1], [x1, y1] = xy[i];
      const cx = (x0 + x1) / 2;
      path += ` C ${cx} ${y0}, ${cx} ${y1}, ${x1} ${y1}`;
    }
    return { line: path, area: `${path} L ${xy[xy.length - 1][0]} ${h} L ${xy[0][0]} ${h} Z` };
  }, [points]);
  return (
    <svg className="hero-horizon" viewBox="0 0 1000 220" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id="hz" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.14" />
          <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient>
      </defs>
      {d ? (
        <>
          <path d={d.area} fill="url(#hz)" />
          <path d={d.line} fill="none" stroke="var(--accent)" strokeOpacity="0.55" strokeWidth="2" vectorEffect="non-scaling-stroke" />
        </>
      ) : (
        <path d="M0 170 C 250 160, 500 180, 750 165 S 1000 170, 1000 170 L 1000 220 L 0 220 Z" fill="url(#hz)" />
      )}
    </svg>
  );
}

const STAGES = [
  { id: "PROBE", label: "PROBE" }, { id: "CONFIRM_ADD", label: "CONFIRM" }, { id: "PRESS_1", label: "PRESS" },
  { id: "HARVEST", label: "HARVEST" }, { id: "EXIT", label: "EXIT" },
];

export function StageStepper({ stage, history }: { stage: string; history?: { to: string }[] }) {
  const seen = new Set((history ?? []).map((h) => (h.to === "PRESS_2" ? "PRESS_1" : h.to)));
  const cur = stage === "PRESS_2" ? "PRESS_1" : stage;
  const curIdx = STAGES.findIndex((s) => s.id === cur);
  return (
    <div className="stepper" aria-label={`Stage ${stage}`}>
      {STAGES.map((s, i) => {
        const done = (seen.has(s.id) || i < curIdx) && s.id !== cur;
        return (
          <div key={s.id} className={`step ${s.id === cur ? "cur" : done ? "done" : ""}`}>
            <span className="dot" />
            <span className="lbl">{s.label}{s.id === "PRESS_1" && stage === "PRESS_2" ? " ×2" : ""}</span>
          </div>
        );
      })}
    </div>
  );
}

/** Live R on a -1R .. +3R track with the MFE marker (capture ratio at a glance). */
export function RBar({ r, mfe }: { r: number; mfe?: number }) {
  const lo = -1.2, hi = 3.2;
  const pos = (x: number) => `${((Math.max(lo, Math.min(hi, x)) - lo) / (hi - lo)) * 100}%`;
  const zero = pos(0);
  const color = r >= 0 ? "var(--up)" : "var(--down)";
  return (
    <div>
      <div className="rbar" aria-label={`Current ${rMult(r)}`}>
        <div className="track" />
        <div className="fill" style={{ left: r >= 0 ? zero : pos(r), width: `calc(${r >= 0 ? pos(r) : zero} - ${r >= 0 ? zero : pos(r)})`, background: color }} />
        <div className="zero" style={{ left: zero }} />
        {mfe !== undefined && mfe > 0 && <div className="mfe" style={{ left: pos(mfe) }} title={`Best ${rMult(mfe)}`} />}
        <div className="mark" style={{ left: pos(r), background: color }} />
      </div>
      <div className="rbar-scale" style={{ position: "relative", height: 14 }}>
        {[-1, 0, 1, 2, 3].map((t) => (
          <span key={t} style={{ position: "absolute", left: pos(t), transform: "translateX(-50%)" }}>{t === 0 ? "0" : `${t > 0 ? "+" : "−"}${Math.abs(t)}R`}</span>
        ))}
      </div>
    </div>
  );
}

/** Unbounded mode: free capital shown like battery range; the charge is what is not tied up in open trades. */
export function CapitalGauge({ equity, deployed, free, perTrade }: { equity: number; deployed: number; free: number; perTrade: number }) {
  const f = equity > 0 ? Math.max(0, Math.min(1, free / equity)) : 0;
  return (
    <div className="battery-wrap">
      <div className="eyebrow">Free capital</div>
      <div className="range num" style={{ marginTop: 8 }}>{rupeesShort(free)}</div>
      <div className="battery" role="meter" aria-valuenow={free} aria-valuemin={0} aria-valuemax={equity} aria-label="Free capital">
        <div className="battery-fill" style={{ width: `${f * 100}%`, background: "var(--up)" }} />
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>{rupeesShort(deployed)} in trades · {rupeesShort(perTrade)} per trade</div>
    </div>
  );
}

function rupeesShort(x: number): string {
  return `₹${Math.round(x).toLocaleString("en-IN")}`;
}

/** The scanner's top names in the hero's right column. */
export function BoomingList({ rows, onPick }: { rows: { key: string; symbol: string; direction: string; score: number; chg_pct: number; fno: boolean }[]; onPick: (key: string) => void }) {
  return (
    <div className="booming" aria-label="Booming now">
      <div className="eyebrow ttl">Booming now</div>
      {rows.length === 0 && <div className="muted" style={{ fontSize: 12.5 }}>Scanning the universe…</div>}
      {rows.slice(0, 6).map((r) => (
        <button key={r.key} className="boom-row" onClick={() => onPick(r.key)} title={`Score ${(r.score * 100).toFixed(0)}`}>
          <span className="sym">{r.symbol}{r.fno && <small>F&amp;O</small>}</span>
          <span className={`num ${r.direction === "UP" ? "up" : "down"}`}>{r.direction === "UP" ? "▲" : "▼"} {Math.abs(r.chg_pct).toFixed(1)}%</span>
          <span className="num faint">{(r.score * 100).toFixed(0)}</span>
        </button>
      ))}
    </div>
  );
}

/** Stop-loss -> entry -> target on one track, drawn in R from the initial stop (so shorts read the same way and a stop
 * trailed to entry or beyond stays visible as its own tick), with the live price as the dot. */
export function TradeBar({ direction, initialStop, stop, entry, target, price }: { direction: string; initialStop: number; stop: number; entry: number; target: number; price: number }) {
  const s = direction === "SHORT" ? -1 : 1;
  const unit = Math.max(s * (entry - initialStop), Math.abs(entry) * 1e-4);
  const reward = target && s * (target - entry) > 0 ? s * (target - entry) : 2 * unit;
  const x = (p: number) => Math.max(0, Math.min(1, (s * (p - entry) + unit) / (unit + reward)));
  const e = x(entry), px = x(price), st = x(stop);
  const winning = s * (price - entry) >= 0;
  const moved = s * (stop - initialStop) > 1e-9;
  return (
    <div className="tradebar" aria-label={`Stop ${stop}, entry ${entry}, target ${target || "none"}, price ${price}`}>
      <div className="tb-track">
        <span className="tb-loss" style={{ width: `${e * 100}%` }} />
        <span className="tb-win" style={{ left: `${e * 100}%`, width: `${(1 - e) * 100}%` }} />
        <span className="tb-entry" style={{ left: `${e * 100}%` }} />
        {moved && <span className="tb-stop" style={{ left: `${st * 100}%` }} title={`stop-loss now ${stop.toFixed(2)}`} />}
        <span className={`tb-price ${winning ? "win" : "loss"}`} style={{ left: `${px * 100}%` }} />
      </div>
      <div className="tb-labels num">
        <span>SL {stop.toFixed(2)}{moved ? (Math.abs(stop - entry) < 1e-6 ? " (at entry)" : " (trailed)") : ""}</span>
        <span>{target ? `Target ${target.toFixed(2)}` : "no target"}</span>
      </div>
    </div>
  );
}
