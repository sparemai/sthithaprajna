import { CandlestickChart, Layers, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { get } from "../lib/api";
import { dirClass, glyph, num, signedPct } from "../lib/format";
import { asWatch, useStore } from "../lib/store";
import type { SearchHit, Watch } from "../lib/types";

const INDEX_LABEL: Record<string, string> = { NIFTY: "NIFTY 50", SENSEX: "SENSEX", BANKNIFTY: "BANK NIFTY", INDIAVIX: "INDIA VIX" };
const CHAIN_OK = new Set(["NIFTY", "SENSEX", "BANKNIFTY"]);

export function Watchlist() {
  const { snap, select, selected, setChain, setRoute, flash, watchOpen } = useStore();
  const [q, setQ] = useState("");
  const [tab, setTab] = useState("All");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const watch = snap?.watch ?? [];
  const booming = (snap?.scanner.top ?? []).filter((r) => r.tradable);
  const indices = watch.filter((w) => w.kind === "INDEX");
  const sectors = useMemo(() => ["All", ...(snap?.scanner.top.length ? ["Booming"] : []), "Indices",
    ...Array.from(new Set(watch.filter((w) => w.kind !== "INDEX").map((w) => w.sector))).sort()], [watch.length, !!snap?.scanner.top.length]);
  useEffect(() => {                     // the whole market (every stock the scanner knows), not just this list
    const t = q.trim();
    if (t.length < 2) { setHits([]); return; }
    const id = setTimeout(() => { get<SearchHit[]>(`/api/search?q=${encodeURIComponent(t)}`).then(setHits).catch(() => setHits([])); }, 250);
    return () => clearTimeout(id);
  }, [q]);
  const inList = new Set(watch.map((w) => w.key));
  const more = hits.filter((h) => !inList.has(h.key));
  const rows = watch.filter((w) => {
    if (tab === "Indices" && w.kind !== "INDEX") return false;
    if (tab !== "All" && tab !== "Indices" && w.sector !== tab) return false;
    if (!q) return true;
    const s = q.toLowerCase();
    return w.symbol.toLowerCase().includes(s) || w.name.toLowerCase().includes(s);
  });
  const up = watch.filter((w) => w.kind !== "INDEX" && w.change > 0).length;
  const dn = watch.filter((w) => w.kind !== "INDEX" && w.change < 0).length;

  const cell = (w: Watch | undefined) => w && (
    <div className="index-cell" onClick={() => select(w.key, "terminal")} role="button" tabIndex={0}>
      <div className="n">{INDEX_LABEL[w.symbol] ?? w.symbol}</div>
      <div className="p num">{num(w.ltp)}</div>
      <div className={`c num ${dirClass(w.change)}`}>{glyph(w.change)} {num(w.change)} ({signedPct(w.change_pct, 2, false)})</div>
    </div>
  );

  return (
    <aside className={`watch ${watchOpen ? "open" : ""}`} aria-label="Watchlist">
      <div className="index-strip">
        {cell(indices.find((i) => i.symbol === "NIFTY"))}
        {cell(indices.find((i) => i.symbol === "SENSEX"))}
        {cell(indices.find((i) => i.symbol === "BANKNIFTY"))}
        {cell(indices.find((i) => i.symbol === "INDIAVIX"))}
      </div>
      <div className="watch-search">
        <label className="row" style={{ position: "relative" }}>
          <Search size={15} style={{ position: "absolute", left: 11, color: "var(--faint)" }} />
          <input className="input" style={{ paddingLeft: 32, width: "100%" }} placeholder="Search any stock: infy, tata, bank" value={q}
            onChange={(e) => setQ(e.target.value)} aria-label="Search instruments" />
        </label>
        <div className="watch-tabs" role="tablist">
          {sectors.map((s) => <button key={s} className={tab === s ? "on" : ""} onClick={() => setTab(s)}>{s === "All" ? `All ${watch.length}` : s === "Booming" ? `Booming ${booming.length}` : s[0] + s.slice(1).toLowerCase()}</button>)}
        </div>
      </div>
      <div className="watch-list">
        {tab === "Booming" && booming.map((r) => (
          <div key={r.key} className={`w-row ${selected === r.key ? "sel" : ""}`} onClick={() => select(r.key, "terminal", asWatch(r))} role="button" tabIndex={0}>
            <div>
              <div className="sym">{r.symbol}{r.fno && <small>F&amp;O</small>}</div>
              <div className="sub">{r.flags[0]?.toLowerCase() ?? r.direction.toLowerCase()} · score {(r.score * 100).toFixed(0)}</div>
            </div>
            <div className={`ltp ${dirClass(r.chg_pct)}`}>{num(r.ltp)}</div>
            <div className={`chg ${dirClass(r.chg_pct)}`}>{glyph(r.chg_pct)} {signedPct(r.chg_pct, 2, false)}</div>
          </div>
        ))}
        {tab === "Booming" && !booming.length && <div className="empty">Nothing booming right now.</div>}
        {tab !== "Booming" && rows.map((w) => (
          <div key={w.key} className={`w-row ${selected === w.key ? "sel" : ""} ${flash[w.key] ? `flash-${flash[w.key]}` : ""}`}
            onClick={() => select(w.key, "terminal")} role="button" tabIndex={0}>
            <div>
              <div className="sym">{w.symbol}<small>{w.exchange}</small></div>
              <div className="sub">{w.kind === "INDEX" ? "Index" : w.sector.toLowerCase()}</div>
            </div>
            <div className={`ltp ${dirClass(w.change)}`}>{num(w.ltp)}</div>
            <div className={`chg ${dirClass(w.change)}`}>{glyph(w.change)} {signedPct(w.change_pct, 2, false)}</div>
            <div className="hover-actions">
              <button onClick={(e) => { e.stopPropagation(); select(w.key, "terminal"); }} title="Chart"><CandlestickChart size={13} /></button>
              {CHAIN_OK.has(w.symbol) && (
                <button onClick={(e) => { e.stopPropagation(); setChain(w.symbol); setRoute("chain"); }} title="Option chain"><Layers size={13} /></button>
              )}
            </div>
          </div>
        ))}
        {tab !== "Booming" && !rows.length && !more.length && <div className="empty">No instruments match “{q}”.</div>}
        {more.length > 0 && (
          <div className="search-hits">
            <div className="eyebrow">Whole market</div>
            {more.map((h) => (
              <div key={h.key} className={`w-row ${selected === h.key ? "sel" : ""}`} role="button" tabIndex={0}
                onClick={() => select(h.key, "terminal", asWatch({ key: h.key, symbol: h.symbol, name: h.name, ltp: h.ltp, chg_pct: h.change_pct }))}>
                <div>
                  <div className="sym">{h.symbol}{h.fno && <small>F&amp;O</small>}</div>
                  <div className="sub">{h.name.toLowerCase()}{h.series !== "EQ" ? ` · ${h.series}` : ""}</div>
                </div>
                <div className={`ltp ${dirClass(h.change_pct)}`}>{h.ltp ? num(h.ltp) : "–"}</div>
                <div className={`chg ${dirClass(h.change_pct)}`}>{h.change_pct == null ? "" : `${glyph(h.change_pct)} ${signedPct(h.change_pct, 2, false)}`}</div>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="watch-foot">
        <span><span className="up">▲ {up}</span> · <span className="down">▼ {dn}</span> breadth</span>
        <span title="Spec D6/A15: the operator never opens or enlarges positions">No manual orders · D6</span>
      </div>
    </aside>
  );
}
