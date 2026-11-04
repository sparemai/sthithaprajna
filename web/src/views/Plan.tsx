import { useEffect, useState } from "react";
import { get } from "../lib/api";
import { pct, rMult, rupees, signedRupees, title } from "../lib/format";
import { useStore } from "../lib/store";
import { Card, Delta, Label, Stat } from "../components/ui";

function PeriodRow({ period, t }: { period: string; t: any }) {
  return (
    <div>
      <div className="row between" style={{ fontSize: 12.5 }}>
        <span style={{ fontWeight: 500 }}>
          {title(period)}
          {t.locked && <span className="chip" style={{ marginLeft: 6 }}>LOCKED</span>}
          {t.ratchet.active && <span className="chip chip-accent" style={{ marginLeft: 6 }}>RATCHET</span>}
        </span>
        <span className="num">
          <Delta v={t.pnl}>{signedRupees(t.pnl)}</Delta>
          {t.target ? <span className="muted"> / {rupees(t.target.rupees)}</span> : <span className="faint"> · no target</span>}
        </span>
      </div>
      {t.target && <div className="meter" style={{ marginTop: 6 }}><span style={{ width: `${Math.max(0, Math.min(1, t.progress ?? 0)) * 100}%` }} /></div>}
    </div>
  );
}

function PeriodReviewTable({ rows, period }: { rows: any[]; period: string }) {
  return (
    <table className="t">
      <thead><tr><th>{title(period)}</th><th>Closed</th><th className="r">P&amp;L</th><th className="r">Target</th><th>Result</th></tr></thead>
      <tbody>
        {rows.map((r) => {
          const goal = r.target?.target?.rupees ?? null;
          const hit = goal != null && r.pnl >= goal;
          return (
            <tr key={r.period_key}>
              <td><b>{r.period_key}</b></td>
              <td className="muted">{r.closed_at.slice(0, 10)}</td>
              <td className="r num"><Delta v={r.pnl}>{signedRupees(r.pnl)}</Delta></td>
              <td className="r num muted">{goal != null ? rupees(goal) : "–"}</td>
              <td>{goal == null ? <span className="faint">no target set</span> : hit ? <span className="chip chip-up">Hit</span> : <span className="chip chip-down">Missed</span>}</td>
            </tr>
          );
        })}
        {!rows.length && <tr><td colSpan={5}><div className="empty">No {period} has closed yet.</div></td></tr>}
      </tbody>
    </table>
  );
}

export function Plan() {
  const snap = useStore((s) => s.snap);
  const [review, setReview] = useState<any>(null);
  const [cal, setCal] = useState<any>(null);
  const [trades, setTrades] = useState<any>(null);
  const day = snap?.clock.day;
  useEffect(() => {
    get("/api/review").then(setReview).catch(() => undefined);
    get("/api/calendar").then(setCal).catch(() => undefined);
    get("/api/trades?limit=150").then(setTrades).catch(() => undefined);
  }, [day, snap?.closed.length]);
  if (!snap) return null;
  const zth = cal?.zero_to_hero;
  return (
    <div className="view-enter">
      <h1 className="section-title">Plan</h1>
      <p className="section-sub">Daily, weekly and monthly plan against what actually happened, the expiry calendar, every
        Zero-to-Hero capture ever closed, and the full trade history — synchronising and reviewing, not just watching the
        live number (A2 Honesty Contract: every figure here carries its own label).</p>
      <div className="grid g-12">
        <Card title="This period" sub="Live, from the running engine (D5)" className="span-4">
          <div className="stack" style={{ gap: 14 }}>
            {(["day", "week", "month"] as const).map((p) => <PeriodRow key={p} period={p} t={snap.targets[p]} />)}
          </div>
        </Card>

        <Card title="Weekly review" sub="Plan vs actual, persisted at each week's close (D-054)" className="span-4"
          right={<Label text={snap.label} />}>
          {review ? <PeriodReviewTable rows={review.completed.week} period="week" /> : <div className="empty">Loading…</div>}
        </Card>

        <Card title="Monthly review" sub="Plan vs actual, persisted at each month's close (D-054)" className="span-4"
          right={<Label text={snap.label} />}>
          {review ? <PeriodReviewTable rows={review.completed.month} period="month" /> : <div className="empty">Loading…</div>}
        </Card>

        <Card title="Expiry calendar" sub="NIFTY, SENSEX and BANKNIFTY, nearest first" className="span-6">
          {!cal ? <div className="empty">Loading…</div> : (
            <div className="row-wrap">
              {cal.expiries.map((e: any) => (
                <span key={`${e.underlying}-${e.expiry}`} className={`pill ${e.is_today ? "chip-accent" : ""}`}>
                  <b>{e.underlying}</b> {e.expiry}{e.is_today ? " · today" : ` · ${e.days_to_expiry}d`}
                </span>
              ))}
              {!cal.expiries.length && <div className="empty">No upcoming expiries found.</div>}
            </div>
          )}
        </Card>

        <Card title="Zero-to-Hero captures" sub="Every sleeve trade ever closed, grouped by expiry (D8)" className="span-6"
          right={<Label text={snap.label} />}>
          {!zth ? <div className="empty">Loading…</div> : (
            <>
              <div className="kpis" style={{ marginBottom: 12 }}>
                <Stat label="Total captures" value={zth.total_trades} />
                <Stat label="Total net P&amp;L" value={<Delta v={zth.total_net_pnl}>{signedRupees(zth.total_net_pnl)}</Delta>} />
                <Stat label="Cumulative since last review" value={<Delta v={zth.current.cumulative_pnl}>{signedRupees(zth.current.cumulative_pnl)}</Delta>} hint="D-039 kill switch tracks this" />
              </div>
              <table className="t">
                <thead><tr><th>Expiry</th><th className="r">Trades</th><th className="r">Win rate</th><th className="r">Net P&amp;L</th></tr></thead>
                <tbody>
                  {zth.captures_by_expiry.map((r: any) => (
                    <tr key={r.expiry}>
                      <td>{r.expiry}</td><td className="r num">{r.trades}</td>
                      <td className="r num">{r.win_rate != null ? pct(r.win_rate, 0) : "–"}</td>
                      <td className="r num"><Delta v={r.net_pnl}>{signedRupees(r.net_pnl)}</Delta></td>
                    </tr>
                  ))}
                  {!zth.captures_by_expiry.length && <tr><td colSpan={4}><div className="empty">No Zero-to-Hero trade has closed yet.</div></td></tr>}
                </tbody>
              </table>
            </>
          )}
        </Card>

        <Card title="Full trade history" sub={trades ? `${trades.total} closed trades, newest first` : "Loading…"} className="span-12"
          right={<Label text={snap.label} />}>
          <div className="table-wrap" style={{ maxHeight: 460 }}>
            <table className="t">
              <thead><tr><th>Closed</th><th>Symbol</th><th>Strategy</th><th>Exit</th><th className="r">R</th><th className="r">Net</th></tr></thead>
              <tbody>
                {(trades?.trades ?? []).map((t: any) => (
                  <tr key={t.id}>
                    <td className="muted">{t.closed_at?.slice(0, 16).replace("T", " ") ?? "–"}</td>
                    <td><b>{t.symbol}</b>{t.sleeve && <span className="chip" style={{ marginLeft: 6 }}>ZTH</span>}</td>
                    <td className="muted">{title(t.strategy ?? "")}</td>
                    <td className="muted">{t.exit_reason}</td>
                    <td className="r num"><Delta v={t.r_multiple}>{rMult(t.r_multiple)}</Delta></td>
                    <td className="r num"><Delta v={t.net_pnl}>{signedRupees(t.net_pnl)}</Delta></td>
                  </tr>
                ))}
                {trades && !trades.trades.length && <tr><td colSpan={6}><div className="empty">No trade has closed yet.</div></td></tr>}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  );
}
