import { Command, Menu, Moon, OctagonX, Sun, Wifi, WifiOff } from "lucide-react";
import { useState } from "react";
import { act } from "../lib/api";
import { GEAR_COLOR, hm, rupees } from "../lib/format";
import { routesFor, useStore } from "../lib/store";
import { Brand } from "./Brand";

export function TopNav() {
  const { route, setRoute, snap, theme, toggleTheme, conn, setPalette, setWatchOpen, watchOpen, toast } = useStore();
  const [confirmKill, setConfirmKill] = useState(false);
  const approvals = snap?.approvals.length ?? 0;
  const positions = snap?.positions.length ?? 0;
  const gear = snap?.gear.effective ?? "ATTACK";
  const unbounded = snap?.risk_mode === "unbounded";
  const live = snap?.mode.startsWith("LIVE") ?? false;
  const modeTip = live ? "LIVE: real orders on your Dhan account" : snap?.market?.source === "DHAN"
    ? "PAPER: live Dhan data, simulated fills" : snap?.gates_missing.length ? `Live refused: ${snap.gates_missing.join(", ")}` : "Mode";

  async function kill() {
    if (!confirmKill) { setConfirmKill(true); setTimeout(() => setConfirmKill(false), 4000); return; }
    setConfirmKill(false);
    await act("kill");
    toast("KILL SWITCH: flattening everything and halting new entries");
  }

  return (
    <nav className="nav" aria-label="Main">
      <button className="icon-btn menu-btn" aria-label="Watchlist" onClick={() => setWatchOpen(!watchOpen)}><Menu size={18} /></button>
      <Brand />
      <div className="nav-links">
        {routesFor(snap?.product).map((r) => (
          <button key={r.id} className={`nav-link ${route === r.id ? "active" : ""}`} onClick={() => setRoute(r.id)}>
            {r.label}
            {r.id === "queue" && approvals > 0 && <span className="badge-dot">{approvals}</span>}
            {r.id === "positions" && positions > 0 && <span className="badge-dot" style={{ background: "var(--ink)", color: "var(--bg)" }}>{positions}</span>}
          </button>
        ))}
      </div>
      <div className="nav-right">
        {snap && (
          <>
            <span className={`pill hide-sm ${live ? "pill-live" : ""}`} title={modeTip}>
              <span className="dot" style={{ color: live ? "var(--critical)" : snap.mode === "SIM" ? "var(--accent)" : "var(--up)" }} />{live ? "LIVE" : snap.mode}
            </span>
            {unbounded ? (
              <span className="pill hide-sm hide-md num" title="Capital deployed in open trades / free">
                {rupees(snap.capital.deployed)} in · {rupees(snap.capital.free)} free
              </span>
            ) : (
              <span className="pill hide-sm hide-md" title="Effective gear (AGG-1)">
                <span className="dot" style={{ color: GEAR_COLOR[gear] }} />{gear}
              </span>
            )}
            <span className="pill hide-sm hide-md num" title={`${snap.clock.weekday} ${snap.clock.day} (${snap.market?.source === "DHAN" ? "exchange" : "sim"} clock, IST)`}>
              {hm(snap.clock.now)} IST · {snap.clock.session === "OPEN" ? "Open" : snap.market?.session === "PRE_OPEN" ? "Pre-open" : "Closed"}
            </span>
          </>
        )}
        <span className="icon-btn hide-sm" title={`Stream: ${conn}`} aria-label={`Stream ${conn}`}>
          {conn === "live" ? <Wifi size={16} color="var(--up)" /> : <WifiOff size={16} color="var(--critical)" />}
        </span>
        <button className="icon-btn" onClick={() => setPalette(true)} aria-label="Command palette (Ctrl K)" title="Command palette (Ctrl K)"><Command size={17} /></button>
        <button className="icon-btn" onClick={toggleTheme} aria-label="Toggle theme">{theme === "light" ? <Moon size={17} /> : <Sun size={17} />}</button>
        <button className="btn btn-kill" onClick={kill} title="Flatten everything and halt (D10 /kill)">
          <OctagonX size={15} /> {confirmKill ? "CONFIRM" : "KILL"}
        </button>
      </div>
    </nav>
  );
}
