// Shapes of the engine snapshot (src/sthitaprajna/engine/snapshot.py). Kept loose where the backend
// passes through nested diagnostic dicts.
export type GearName = "ATTACK" | "PRESS" | "GUARD" | "PRESERVE" | "HALTED";
export type GradeName = "A+" | "A" | "B" | "C";

export interface Watch {
  key: string; symbol: string; name: string; kind: string; exchange: string; sector: string;
  ltp: number; prev_close: number; change: number; change_pct: number; open: number; high: number; low: number;
  volume: number; bid: number | null; ask: number | null; stale: boolean; lot: number;
}

export interface Leg { qty: number; price: number; ts: string; stage: string }
export interface Position {
  id: string; symbol: string; strategy: string; underlying: string; direction: string; exposure: string; segment: string;
  grade: GradeName; gear_at_entry: GearName; planned_risk: number; unit_risk: number; initial_stop: number; stop: number;
  broker_stop: number; broker_stop_id: string; staging_mode: string; full_qty_target: number; stage: string; presses: number;
  harvested: boolean; qty: number; opened_qty: number; avg_entry: number; last_price: number; unrealized: number;
  realized: number; charges: number; net_pnl: number; r_multiple: number; move_r: number; open_risk: number; mfe_r: number;
  mae_r: number; opened_at: string; exit_reason: string; is_reentry: boolean; sleeve: boolean; confirm_trigger: number;
  confirm_deadline: string; legs: Leg[]; closed_legs: (Leg & { reason: string; pnl: number })[];
  stage_history: { ts: string; to: string; qty?: number; price?: number; rule?: string; note?: string; reason?: string }[];
  stop_history: { ts: string; stop: number; reason: string }[];   // D-058
  instrument: { key: string; lot_size: number; kind: string };
  target: number;
}
export interface ClosedTrade {
  id: string; symbol: string; strategy: string; grade: string; direction: string; exposure: string; net: number; r: number;
  reason: string; opened: string; closed: string; stages: string[]; charges: number; label: string; sleeve: boolean; segment: string;
}
export interface Order {
  id: string; position_id: string; symbol: string; side: string; qty: number; type: string; purpose: string; price: number;
  trigger: number; status: string; filled_qty: number; avg_price: number; created: string; updated: string | null;
  algo_id: string; correlation_id: string; reject_reason: string; slippage_bps: number | null; charges: number;
}
export interface Approval {
  id: string; kind: string; status: string; seconds_left: number; setup_id: string | null; position_id: string | null;
  summary: Record<string, any>;
}
export interface Check { name: string; ok: boolean; detail: string }
export interface FunnelRecord {
  id: string; setup_id: string; strategy: string; symbol: string; instrument: string; underlying: string; direction: string;
  exposure: string; segment: string; entry: number; stop: number; target_r: number; p_win: number; p_calibrated: boolean;
  half_life: string; trigger: boolean; confirmations: { name: string; factor: string; fired: boolean; detail: string }[];
  independent_confirmations: number; stage: string; status: string; reason: string; grade: GradeName | null;
  ev: { p_win: number; win_r: number; loss_r: number; cost_r: number; gross_r: number; net_r: number; margin_ok: boolean; reason: string } | null;
  quality: number | null; quality_pct: number | null;
  confidence: { model: number; data: number; regime: number; execution: number; effective: number; weakest: string } | null;
  sizing: Record<string, any> | null; plan: Record<string, any> | null; verdict: { approved: boolean; reason: string; checks: Check[] } | null;
  utility: number; first_seen: string; expires_at: string; explanation: string; is_reentry: boolean; sleeve: boolean;
}
export interface EventItem { ts: string; kind: string; msg: string }

export interface MarketInfo {
  source: "SIM" | "DHAN" | string; label: string; session: "OPEN" | "PRE_OPEN" | "CLOSED" | string; day: string; now: string;
  next_open: string | null; secondary: string | null; secondary_configured: boolean; instruments: number; backfill_days: number;
  health: { last_ok?: string | null; last_error?: string | null; error_kind?: string | null; polls?: number; errors?: number };
  problems: string[]; betas_estimated: number;
}

export interface ScreenRow {
  key: string; symbol: string; name: string; sector: string; fno: boolean; lot: number; ltp: number; chg_pct: number;
  gap_pct: number | null; rvol: number | null; vwap_dist_pct: number | null; range_pos: number | null;
  w52_dist_pct: number | null; turnover_cr: number; imbalance: number | null; spread_bps: number | null; score: number;
  score_up: number; score_down: number; direction: "UP" | "DOWN"; flags: string[]; tradable: boolean; why_not: string;
  active: boolean;
}
export interface ScannerSummary {
  universe?: number; fresh?: number; baselines?: number; baselines_pending?: number; promoted?: string[]; fno?: number;
  ranked: number; booming: number; weights: string;
}
export interface SearchHit {
  key: string; symbol: string; name: string; ltp: number; prev_close: number; change_pct: number | null; fno: boolean;
  series: string; active: boolean;
}

export interface Snapshot {
  type: "snapshot";
  clock: { now: string; day: string; session: string; minutes_into: number; speed: number; paused: boolean; weekday: string };
  mode: string; label: string; gates_missing: string[]; sim_regime: string; market: MarketInfo;
  risk_mode: "bounded" | "unbounded";
  capital: { equity: number; deployed: number; free: number; per_trade_fraction: number; per_trade: number;
    intraday_leverage: number; breakeven_at_r: number; deployed_pct: number };
  scanner: { summary: ScannerSummary; top: ScreenRow[] };
  broker: { live: boolean; name: string; resting_stops: number; working: number; available_funds?: number | null;
    health?: { polls?: number; last_ok?: string | null; error?: string | null }; rejects_1m?: number };
  mandatory_checks: { name: string; what: string }[];
  account: { equity: number; mtm: number; equity_sod: number; unrealized: number; open_charges: number; day_pnl: number;
    week_pnl: number; month_pnl: number; day_pnl_pct: number; peak: number; ruin_floor: number; starting: number; total_return_pct: number };
  gear: { gear: GearName; effective: GearName; ref_peak: number; drawdown: number; clean_days: number; upshift_needs: number;
    halted_review_pending: boolean; day_drop: boolean; evidence_drop: boolean; expectancy_cap: boolean; next_downshift_in: number | null;
    ladder: { gear: GearName; dd_below: number; r: number; open_risk: number }[]; events: { ts: string; from: string; to: string; reason: string }[];
    time_in_gear: Record<string, number>; effective_r: number; headroom_to_halt: number };
  tier: { tier: string; attack_r: number; attack_r_cap: number | null; max_positions: number; max_trades_per_day: number;
    days_above_next: number; breach_free_days: number };
  risk: { open_risk: number; open_risk_limit: number; planned_by_grade: Record<string, number>; per_trade_ceiling: number;
    day_loss_limit: number; week_loss_limit: number; month_loss_limit: number; trades_today: number; max_trades: number;
    positions: number; max_positions: number; losing_streak: number; cooldown_until: string | null; margin_used: number;
    margin_limit: number; kill_switch: boolean; ruined: boolean; halts: string[]; blocked_instruments: number; day_peak_pnl: number;
    bucket_cap: number; entry_window_open: boolean; target_blocks: string[]; b_allowed: boolean };
  ceilings: { max_risk_per_trade: number; max_open_risk: number; halt_drawdown: number; ruin_floor_abs: number; ruin_floor_pct: number; ops_cap: number };
  edge_brake: { n: number; active: boolean; ceiling_r: number | null; win_rate: number | null; payoff: number | null; kelly: number | null;
    expectancy_r: number | null; cap_at_guard: boolean; note: string };
  calibration: { n: number; ece: number | null; calibrated: boolean; out_of_band: boolean; status: string };
  tqs_status: string;
  positions: Position[]; closed: ClosedTrade[]; orders: Order[]; approvals: Approval[];
  funnel: { counts: Record<string, number>; drop_reasons: Record<string, number>; records: FunnelRecord[] };
  kpis: Record<string, any>;
  targets: Record<"day" | "week" | "month", { target: { value: number; unit: string; rupees: number } | null; pnl: number; progress: number | null;
    ratchet: { active: boolean; peak: number; floor: number | null; floor_hit: boolean }; locked: boolean }>;
  regime: { trend: string; trend_strength: number; volatility: string; vol_tags: string[]; expiry: string | null; change_point: boolean;
    confidence: number; breadth_ad: number; vix: number; vix_change: number; latency: string };
  feeds: { situation: string; primary: Record<string, any>; secondary: Record<string, any>; disagreeing: Record<string, number>;
    data_confidence: number; entries_allowed: boolean; alerts: { ts: string; msg: string }[] };
  quality: { blocked: Record<string, string>; halts: Record<string, string>; events: { ts: string; key: string; msg: string }[] };
  process: { score: number; items: Record<string, number>; manual_exits: number };
  events: EventItem[]; state_check: Record<string, any> | null; pilot_approvals: boolean;
  manual_exits: Record<string, any>[]; journal_latest: Record<string, any> | null; faults: { ts: string; msg: string }[]; ops_peak: number;
  watch: Watch[];
}
