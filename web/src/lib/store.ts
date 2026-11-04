import { create } from "zustand";
import { wsUrl } from "./api";
import type { ScreenRow, Snapshot, Watch } from "./types";

export type Route = "dashboard" | "terminal" | "scanner" | "chain" | "positions" | "orders" | "queue" | "monk" | "research"
  | "journal" | "plan" | "system";
export const ROUTES: { id: Route; label: string }[] = [
  { id: "dashboard", label: "Dashboard" }, { id: "terminal", label: "Terminal" }, { id: "scanner", label: "Scanner" },
  { id: "chain", label: "Option Chain" },
  { id: "positions", label: "Positions" }, { id: "orders", label: "Orders" }, { id: "queue", label: "Queue" },
  { id: "monk", label: "The Monk" }, { id: "research", label: "Research" }, { id: "journal", label: "Journal" },
  { id: "plan", label: "Plan" }, { id: "system", label: "System" },
];

interface Toast { id: number; text: string }

interface State {
  snap: Snapshot | null;
  conn: "connecting" | "live" | "down" | "auth";
  route: Route;
  selected: string;            // instrument key for the terminal
  chainUnderlying: string;
  theme: "light" | "dark";
  watchOpen: boolean;
  paletteOpen: boolean;
  toasts: Toast[];
  flash: Record<string, "up" | "down">;
  extra: Record<string, Watch>;        // instruments picked from the scanner or search (not in the watchlist)
  setRoute: (r: Route) => void;
  select: (key: string, go?: Route, info?: Watch) => void;
  setChain: (u: string) => void;
  toggleTheme: () => void;
  setWatchOpen: (v: boolean) => void;
  setPalette: (v: boolean) => void;
  toast: (text: string) => void;
}

function initialRoute(): Route {
  const h = location.hash.replace(/^#\/?/, "") as Route;
  return ROUTES.some((r) => r.id === h) ? h : "dashboard";
}

function initialTheme(): "light" | "dark" {
  try {
    const t = localStorage.getItem("sp-theme");
    if (t === "dark" || t === "light") return t;
  } catch { /* ignore */ }
  return "light";   // operator decision D-003: Tesla-light by default
}

let toastSeq = 1;

export const useStore = create<State>((set, get) => ({
  snap: null, conn: "connecting", route: initialRoute(), selected: "NSE:NIFTY", chainUnderlying: "NIFTY",
  theme: initialTheme(), watchOpen: false, paletteOpen: false, toasts: [], flash: {}, extra: {},
  setRoute: (r) => { location.hash = `/${r}`; set({ route: r, watchOpen: false }); },
  select: (key, go, info) => {
    set(info ? { selected: key, extra: { ...get().extra, [key]: info } } : { selected: key });
    if (go) get().setRoute(go);
  },
  setChain: (u) => set({ chainUnderlying: u }),
  toggleTheme: () => {
    const t = get().theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("sp-theme", t); } catch { /* ignore */ }
    set({ theme: t });
  },
  setWatchOpen: (v) => set({ watchOpen: v }),
  setPalette: (v) => set({ paletteOpen: v }),
  toast: (text) => {
    const id = toastSeq++;
    set({ toasts: [...get().toasts.slice(-3), { id, text }] });
    setTimeout(() => set({ toasts: get().toasts.filter((t) => t.id !== id) }), 4200);
  },
}));

/** A scanner row (or search hit) as a watchlist row, so the terminal can chart any stock in the universe. */
export function asWatch(r: Pick<ScreenRow, "key" | "symbol" | "name" | "ltp"> & { chg_pct?: number | null; sector?: string; lot?: number }): Watch {
  const pc = r.chg_pct != null && r.ltp > 0 ? r.ltp / (1 + r.chg_pct / 100) : r.ltp;
  return { key: r.key, symbol: r.symbol, name: r.name, kind: "EQUITY", exchange: "NSE", sector: r.sector || "", ltp: r.ltp,
    prev_close: pc, change: r.ltp - pc, change_pct: r.chg_pct ?? 0, open: 0, high: 0, low: 0, volume: 0, bid: null, ask: null,
    stale: false, lot: r.lot ?? 1 };
}

// ------------------------------------------------------------------ live stream
let ws: WebSocket | null = null;
let retry = 0;
let lastEventTs = "";

export function connect() {
  useStore.setState({ conn: "connecting" });
  ws = new WebSocket(wsUrl());
  ws.onopen = () => { retry = 0; useStore.setState({ conn: "live" }); };
  ws.onmessage = (m) => {
    const data = JSON.parse(m.data);
    if (data.type !== "snapshot") return;
    const prev = useStore.getState().snap;
    const flash: Record<string, "up" | "down"> = {};
    if (prev?.watch) {
      const old = new Map(prev.watch.map((w) => [w.key, w.ltp]));
      for (const w of (data as Snapshot).watch) {
        const o = old.get(w.key);
        if (o !== undefined && o !== w.ltp) flash[w.key] = w.ltp > o ? "up" : "down";
      }
    }
    // surface trade-relevant events as toasts (entries, exits, gear changes, risk)
    const evs = (data as Snapshot).events;
    if (lastEventTs && evs?.length) {
      const fresh = evs.filter((e) => e.ts > lastEventTs && ["entry", "exit", "gear", "risk", "tier"].includes(e.kind));
      fresh.slice(0, 2).reverse().forEach((e) => useStore.getState().toast(e.msg));
    }
    if (evs?.length) lastEventTs = evs[0].ts;
    useStore.setState({ snap: data as Snapshot, flash });
  };
  ws.onclose = (ev) => {
    if (ev.code === 4401) { useStore.setState({ conn: "auth" }); return; }
    useStore.setState({ conn: "down" });
    retry = Math.min(retry + 1, 6);
    setTimeout(connect, 400 * 2 ** retry);
  };
  ws.onerror = () => ws?.close();
}

export function reconnect() {
  try { ws?.close(); } catch { /* ignore */ }
  retry = 0;
  connect();
}

window.addEventListener("hashchange", () => {
  const r = initialRoute();
  if (r !== useStore.getState().route) useStore.setState({ route: r });
});
