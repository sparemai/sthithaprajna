import { useEffect, useRef, useState } from "react";
import { get } from "../lib/api";
import { compact, num, pct } from "../lib/format";
import { useStore } from "../lib/store";
import { Card, Label, Seg, Stat, Toggle } from "../components/ui";

interface ChainData {
  underlying: string; expiry: string; expiries: string[]; spot: number; dte_minutes: number; lot: number; label: string;
  summary: { atm: number; pcr: { oi: number; volume: number }; max_pain: number; atm_iv: number; rr25: number;
    straddle: { straddle: number; pct: number }; walls: { resistance: { strike: number; oi: number }[]; support: { strike: number; oi: number }[] };
    buildup: { underlying_chg_pct: number | null; aggregate: { ce: string; pe: string };
      per_strike: { strike: number; ce: string; pe: string }[] };
    iv_rank: number | null; iv_percentile: number | null; realized_vol: number | null; iv_rv_spread: number | null;
    pin_risk?: { active: boolean; strike: number | null; distance_pct: number | null } };
  rows: any[]; source?: string; note?: string | null;
}

const BUILDUP_LABEL: Record<string, string> = {
  LONG_BUILDUP: "Long build-up", SHORT_BUILDUP: "Short build-up",
  SHORT_COVERING: "Short covering", LONG_UNWINDING: "Long unwinding", FLAT: "Flat",
};
const BUILDUP_CLASS: Record<string, string> = {
  LONG_BUILDUP: "up", SHORT_COVERING: "up", SHORT_BUILDUP: "down", LONG_UNWINDING: "down", FLAT: "muted",
};

export function Chain() {
  const { chainUnderlying, setChain } = useStore();
  const tick = useStore((s) => s.snap?.clock.now);
  const [expiry, setExpiry] = useState<string | null>(null);
  const [greeks, setGreeks] = useState(false);
  const [d, setD] = useState<ChainData | null>(null);
  const [err, setErr] = useState("");
  const wrap = useRef<HTMLDivElement>(null);
  const centred = useRef("");
  useEffect(() => { setExpiry(null); }, [chainUnderlying]);
  useEffect(() => {
    get<ChainData>(`/api/chain?underlying=${chainUnderlying}${expiry ? `&expiry=${expiry}` : ""}`).then((x) => { setD(x); setErr(""); }).catch((e) => setErr(String(e)));
  }, [chainUnderlying, expiry, tick?.slice(0, 17)]);
  useEffect(() => {
    if (!d || !wrap.current) return;
    const key = `${d.underlying}:${d.expiry}`;
    if (centred.current === key) return;
    const atm = wrap.current.querySelector("tr.atm") as HTMLElement | null;
    if (atm) { wrap.current.scrollTop = atm.offsetTop - wrap.current.clientHeight / 2; centred.current = key; }
  }, [d]);
  if (!d) return <div className="empty">{err || "Loading chain…"}</div>;
  const s = d.summary;
  const maxOi = Math.max(1, ...d.rows.map((r) => Math.max(r.ce_oi, r.pe_oi)));
  const res = new Set(s.walls.resistance.map((w) => w.strike));
  const sup = new Set(s.walls.support.map((w) => w.strike));
  const spotIdx = d.rows.findIndex((r) => r.strike > d.spot);
  const days = d.dte_minutes / 375;
  const oiChg = (v: number) => <span className={v > 0 ? "up" : v < 0 ? "down" : "muted"}>{v > 0 ? "+" : v < 0 ? "−" : ""}{compact(Math.abs(v))}</span>;
  return (
    <div className="view-enter">
      <div className="row between" style={{ flexWrap: "wrap", gap: 12, marginBottom: 14 }}>
        <div>
          <h1 className="section-title" style={{ margin: 0 }}>Option chain</h1>
          <div className="muted" style={{ fontSize: 13 }}>Long premium only: naked selling is disabled (D0). Lot {d.lot} ({d.source === "DHAN" ? "from the Dhan instrument master" : "loaded from rules, never hard-coded"}). <Label text={d.label} /></div>
          {d.note && <div className="chip chip-warn" style={{ marginTop: 6 }}>{d.note}</div>}
        </div>
        <div className="row-wrap" style={{ gap: 10 }}>
          <Seg value={chainUnderlying} onChange={setChain} options={[{ v: "NIFTY", label: "NIFTY" }, { v: "SENSEX", label: "SENSEX" }, { v: "BANKNIFTY", label: "BANK NIFTY" }]} />
          <span className="row muted" style={{ fontSize: 12, gap: 8 }}>Greeks <Toggle on={greeks} onChange={setGreeks} label="Show greeks" /></span>
        </div>
      </div>
      <div className="row-wrap" style={{ marginBottom: 14 }}>
        {d.expiries.map((e) => (
          <button key={e} className={`btn btn-sm ${e === d.expiry ? "btn-primary" : "btn-secondary"}`} onClick={() => setExpiry(e)}>
            {new Date(e + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", weekday: "short" })}
          </button>
        ))}
      </div>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="kpis">
          <Stat label="Spot" value={num(d.spot)} />
          <Stat label="ATM IV" value={pct(s.atm_iv, 1)} hint={`25Δ RR ${(s.rr25 * 100).toFixed(1)} vol pts`} />
          <Stat label="PCR (OI)" value={num(s.pcr.oi, 2)} hint={`volume ${num(s.pcr.volume, 2)}`} />
          <Stat label="Max pain" value={num(s.max_pain, 0)} hint="weak evidence: tested, not trusted" />
          <Stat label="Straddle-implied move" value={`±${pct(s.straddle.pct, 2)}`} hint={`₹${num(s.straddle.straddle)} to expiry`} />
          <Stat label="Time to expiry" value={days >= 1 ? `${days.toFixed(1)} d` : `${Math.round(d.dte_minutes)} min`} hint="trading time" />
          <Stat label="OI walls" value={<span style={{ fontSize: 17 }}>R {s.walls.resistance[0]?.strike ?? "–"} · S {s.walls.support[0]?.strike ?? "–"}</span>} hint="largest call / put OI" />
          <Stat label="IV rank / percentile" value={s.iv_rank == null ? "–" : `${Math.round(s.iv_rank * 100)}% / ${Math.round((s.iv_percentile ?? 0) * 100)}%`}
               hint={s.iv_rank == null ? "building history (needs 5+ sessions)" : "vs trailing sessions (A6.5)"} />
          <Stat label="IV − RV (vol premium)" value={s.iv_rv_spread == null ? "–" : `${s.iv_rv_spread > 0 ? "+" : ""}${(s.iv_rv_spread * 100).toFixed(1)} pts`}
               hint={s.realized_vol == null ? "realized vol needs 6+ sessions" : `realized ${(s.realized_vol * 100).toFixed(1)}%`} />
          {s.pin_risk && (
            <Stat label="Pin risk" value={s.pin_risk.active ? `pinning ${s.pin_risk.strike}` : "not active"}
                 hint={s.pin_risk.active ? "spot is hugging the peak-OI strike (proxy, not certainty)" : "expiry day only"} />
          )}
        </div>
        <div className="row-wrap" style={{ marginTop: 10, gap: 14, fontSize: 12.5 }}>
          <span className="muted">OI build-up:</span>
          <span>Calls <b className={BUILDUP_CLASS[s.buildup.aggregate.ce]}>{BUILDUP_LABEL[s.buildup.aggregate.ce]}</b></span>
          <span>Puts <b className={BUILDUP_CLASS[s.buildup.aggregate.pe]}>{BUILDUP_LABEL[s.buildup.aggregate.pe]}</b></span>
          {s.buildup.underlying_chg_pct == null && <span className="faint">(underlying change unknown this tick -- flat until known)</span>}
        </div>
      </div>
      <Card>
        <div className="legend" style={{ marginBottom: 8 }}>
          <span><i className="box" style={{ background: "var(--call)", opacity: 0.5 }} />Call OI</span>
          <span><i className="box" style={{ background: "var(--put)", opacity: 0.5 }} />Put OI</span>
          <span><i className="box" style={{ background: "var(--accent)", opacity: 0.12 }} />in the money</span>
          <span className="faint">Bold = OI wall</span>
        </div>
        <div className="table-wrap" style={{ maxHeight: "68vh" }} ref={wrap}>
          <table className="t chain">
            <thead>
              <tr>
                {greeks && <><th className="r">Vega</th><th className="r">Theta</th><th className="r">Gamma</th></>}
                <th className="r">Delta</th><th className="r">OI</th><th className="r">OI chg</th><th className="r">Volume</th><th className="r">IV</th><th className="r">LTP</th>
                <th style={{ textAlign: "center" }}>Strike</th>
                <th>LTP</th><th>IV</th><th>Volume</th><th>OI chg</th><th>OI</th><th>Delta</th>
                {greeks && <><th>Gamma</th><th>Theta</th><th>Vega</th></>}
              </tr>
            </thead>
            <tbody>
              {d.rows.map((r, i) => {
                const ceItm = r.strike < d.spot, peItm = r.strike > d.spot;
                return [
                  i === spotIdx ? (
                    <tr key="spot" className="spot-row"><td colSpan={greeks ? 19 : 13}><div className="spot-line"><span>Spot {num(d.spot)}</span></div></td></tr>
                  ) : null,
                  <tr key={r.strike} className={r.strike === s.atm ? "atm" : ""}>
                    {greeks && <><td className={`r num ${ceItm ? "itm" : ""}`}>{r.ce_vega.toFixed(2)}</td><td className={`r num ${ceItm ? "itm" : ""}`}>{r.ce_theta.toFixed(1)}</td><td className={`r num ${ceItm ? "itm" : ""}`}>{r.ce_gamma.toFixed(4)}</td></>}
                    <td className={`r num ${ceItm ? "itm" : ""}`}>{r.ce_delta.toFixed(2)}</td>
                    <td className={`r num oi-cell ce ${ceItm ? "itm" : ""} ${res.has(r.strike) ? "wall" : ""}`}><div className="oi-bar" style={{ width: `${(r.ce_oi / maxOi) * 80}%` }} /><span>{compact(r.ce_oi)}</span></td>
                    <td className={`r num ${ceItm ? "itm" : ""}`}>{oiChg(r.ce_oi_chg)}</td>
                    <td className={`r num ${ceItm ? "itm" : ""}`}>{compact(r.ce_vol)}</td>
                    <td className={`r num ${ceItm ? "itm" : ""}`}>{(r.ce_iv * 100).toFixed(1)}</td>
                    <td className={`r num ${ceItm ? "itm" : ""}`}><b>{num(r.ce_ltp)}</b></td>
                    <td className="strike num">{r.strike}</td>
                    <td className={`num ${peItm ? "itm" : ""}`}><b>{num(r.pe_ltp)}</b></td>
                    <td className={`num ${peItm ? "itm" : ""}`}>{(r.pe_iv * 100).toFixed(1)}</td>
                    <td className={`num ${peItm ? "itm" : ""}`}>{compact(r.pe_vol)}</td>
                    <td className={`num ${peItm ? "itm" : ""}`}>{oiChg(r.pe_oi_chg)}</td>
                    <td className={`num oi-cell pe ${peItm ? "itm" : ""} ${sup.has(r.strike) ? "wall" : ""}`}><div className="oi-bar" style={{ width: `${(r.pe_oi / maxOi) * 80}%` }} /><span>{compact(r.pe_oi)}</span></td>
                    <td className={`num ${peItm ? "itm" : ""}`}>{r.pe_delta.toFixed(2)}</td>
                    {greeks && <><td className={`num ${peItm ? "itm" : ""}`}>{r.pe_gamma.toFixed(4)}</td><td className={`num ${peItm ? "itm" : ""}`}>{r.pe_theta.toFixed(1)}</td><td className={`num ${peItm ? "itm" : ""}`}>{r.pe_vega.toFixed(2)}</td></>}
                  </tr>,
                ];
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
