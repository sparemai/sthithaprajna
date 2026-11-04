import { CandlestickChart, Home, KeyRound, LayoutGrid, ListChecks, Radar, Wallet } from "lucide-react";
import { useEffect, useState } from "react";
import { setToken } from "./lib/api";
import { connect, reconnect, routesFor, useStore } from "./lib/store";
import { BrandMark } from "./components/Brand";
import { ApprovalDock, CommandPalette, Toasts } from "./components/Overlays";
import { TopNav } from "./components/TopNav";
import { Watchlist } from "./components/Watchlist";
import { Chain } from "./views/Chain";
import { Dashboard } from "./views/Dashboard";
import { Journal } from "./views/Journal";
import { Monk } from "./views/Monk";
import { Orders } from "./views/Orders";
import { Plan } from "./views/Plan";
import { Positions } from "./views/Positions";
import { Queue } from "./views/Queue";
import { Research } from "./views/Research";
import { Scanner } from "./views/Scanner";
import { System } from "./views/System";
import { Terminal } from "./views/Terminal";

const VIEWS = { dashboard: Dashboard, terminal: Terminal, scanner: Scanner, chain: Chain, positions: Positions, orders: Orders,
  queue: Queue, monk: Monk, research: Research, journal: Journal, plan: Plan, system: System };

export function modeName(mode: string): string {
  return mode.startsWith("LIVE") ? "LIVE" : mode;
}

function Splash() {
  const conn = useStore((s) => s.conn);
  const [tok, setTok] = useState("");
  return (
    <div style={{ height: "100%", display: "grid", placeItems: "center", textAlign: "center", padding: 24 }}>
      <div>
        <div style={{ color: "var(--ink)" }}><BrandMark size={56} /></div>
        <div style={{ fontWeight: 600, letterSpacing: "0.34em", margin: "18px 0 6px" }}>STHITAPRAJNA</div>
        <div className="muted">{conn === "auth" ? "This terminal needs the operator token." : conn === "down" ? "Reconnecting to the engine…" : "Steady in wisdom. Connecting…"}</div>
        {conn === "auth" && (
          <form className="row" style={{ marginTop: 16, justifyContent: "center" }} onSubmit={(e) => { e.preventDefault(); setToken(tok); reconnect(); }}>
            <KeyRound size={16} /><input className="input" type="password" placeholder="Operator token" value={tok} onChange={(e) => setTok(e.target.value)} autoFocus />
            <button className="btn btn-primary">Unlock</button>
          </form>
        )}
      </div>
    </div>
  );
}

function BottomNav() {
  const { route, setRoute, setPalette, snap } = useStore();
  const pending = snap?.approvals.length ?? 0;       // five slots: the queue shows up only when it has something
  const items = [
    { id: "dashboard" as const, label: "Home", icon: <Home size={20} /> },
    pending ? { id: "queue" as const, label: "Queue", icon: <ListChecks size={20} />, badge: pending }
      : { id: "terminal" as const, label: "Chart", icon: <CandlestickChart size={20} /> },
    { id: "scanner" as const, label: "Scanner", icon: <Radar size={20} /> },
    { id: "positions" as const, label: "Positions", icon: <Wallet size={20} />, badge: snap?.positions.length },
  ];
  return (
    <nav className="bottom-nav" aria-label="Mobile">
      {items.map((it) => (
        <button key={it.id} className={route === it.id ? "on" : ""} onClick={() => setRoute(it.id)}>
          {it.icon}<span>{it.label}</span>{it.badge ? <i>{it.badge}</i> : null}
        </button>
      ))}
      <button onClick={() => setPalette(true)}><LayoutGrid size={20} /><span>More</span></button>
    </nav>
  );
}

export default function App() {
  const route = useStore((s) => s.route);
  const snap = useStore((s) => s.snap);
  const watchOpen = useStore((s) => s.watchOpen);
  const setWatchOpen = useStore((s) => s.setWatchOpen);
  useEffect(() => { connect(); }, []);
  const tag = snap ? (snap.risk_mode === "unbounded" ? modeName(snap.mode) : snap.gear.effective) : "";
  useEffect(() => { document.title = tag ? `${tag} · Sthitaprajna${snap?.product === "intraday500" ? " Intraday 500" : ""}` : "Sthitaprajna Terminal"; }, [tag, snap?.product]);
  if (!snap) return <Splash />;
  const View = routesFor(snap.product).some((r) => r.id === route) ? VIEWS[route] : Dashboard;
  return (
    <div className="shell">
      <TopNav />
      <div className="body">
        <Watchlist />
        {watchOpen && <div className="scrim" style={{ zIndex: 35 }} onClick={() => setWatchOpen(false)} />}
        <main className="main" id="main">
          <div className="main-inner" key={route}><View /></div>
        </main>
      </div>
      <BottomNav />
      <ApprovalDock />
      <CommandPalette />
      <Toasts />
    </div>
  );
}
