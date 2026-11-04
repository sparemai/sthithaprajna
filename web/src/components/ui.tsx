import { Check as CheckIcon, X } from "lucide-react";
import { useEffect, type ReactNode } from "react";
import { dirClass, glyph, gradeClass } from "../lib/format";
import type { Check } from "../lib/types";

export function Card({ title, sub, right, children, className = "", flat = false }: {
  title?: ReactNode; sub?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; flat?: boolean;
}) {
  return (
    <section className={`card ${flat ? "card-flat" : ""} ${className}`}>
      {(title || right) && (
        <header className="card-head">
          <div>
            {title && <h3 className="card-title">{title}</h3>}
            {sub && <div className="card-sub">{sub}</div>}
          </div>
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, delta, deltaValue, hint }: {
  label: ReactNode; value: ReactNode; delta?: ReactNode; deltaValue?: number | null; hint?: ReactNode;
}) {
  return (
    <div className="stat">
      <div className="l">{label}</div>
      <div className="v">{value}</div>
      {delta !== undefined && <div className={`d ${dirClass(deltaValue ?? 0)}`}>{deltaValue ? glyph(deltaValue) + " " : ""}{delta}</div>}
      {hint && <div className="d muted">{hint}</div>}
    </div>
  );
}

/** Meter: fill carries severity; the unfilled track is a lighter step of the same ramp. */
export function Meter({ value, max, warnAt = 0.7, critAt = 0.9, label, right }: {
  value: number; max: number; warnAt?: number; critAt?: number; label?: ReactNode; right?: ReactNode;
}) {
  const f = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  const cls = f >= critAt ? "crit" : f >= warnAt ? "warn" : "";
  return (
    <div className="meter-row">
      {label !== undefined && <span className="muted" style={{ fontSize: 12, fontWeight: 500 }}>{label}</span>}
      {right !== undefined && <span className="num" style={{ fontSize: 12, fontWeight: 600 }}>{right}</span>}
      <div className={`meter ${cls}`} role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
        <span style={{ width: `${f * 100}%` }} />
      </div>
    </div>
  );
}

export function Seg<T extends string>({ value, options, onChange }: {
  value: T; options: { v: T; label: ReactNode }[]; onChange: (v: T) => void;
}) {
  return (
    <div className="seg" role="tablist">
      {options.map((o) => (
        <button key={o.v} role="tab" aria-selected={o.v === value} className={o.v === value ? "on" : ""} onClick={() => onChange(o.v)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return <button className={`toggle ${on ? "on" : ""}`} role="switch" aria-checked={on} aria-label={label} onClick={() => onChange(!on)} />;
}

export function Grade({ g }: { g: string | null | undefined }) {
  return <span className={gradeClass(g)}>{g ?? "–"}</span>;
}

/** Every performance number carries its source (A2.1). */
export function Label({ text }: { text: string }) {
  return <span className="chip chip-label" title="Source of this number (Honesty Contract A2.1)">{text}</span>;
}

export function Delta({ v, children }: { v: number | null | undefined; children: ReactNode }) {
  return <span className={dirClass(v)}>{v ? glyph(v) + " " : ""}{children}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Checklist({ checks }: { checks: Check[] }) {
  if (!checks?.length) return <div className="muted" style={{ fontSize: 12 }}>No checks recorded.</div>;
  return (
    <div className="checks">
      {checks.map((c) => (
        <div className="check" key={c.name}>
          <span className="ic" style={{ color: c.ok ? "var(--up)" : "var(--critical)" }} aria-label={c.ok ? "pass" : "fail"}>
            {c.ok ? <CheckIcon size={15} strokeWidth={2.6} /> : <X size={15} strokeWidth={2.6} />}
          </span>
          <div>
            <div className="nm">{c.name.replace(/_/g, " ")}</div>
            {c.detail && <div className="dt">{c.detail}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

export function CountdownRing({ seconds, total = 60, size = 44 }: { seconds: number; total?: number; size?: number }) {
  const r = size / 2 - 4;
  const c = 2 * Math.PI * r;
  const f = Math.max(0, Math.min(1, seconds / total));
  const color = f < 0.25 ? "var(--critical)" : f < 0.5 ? "var(--warning)" : "var(--accent)";
  return (
    <div style={{ position: "relative", width: size, height: size, flex: "none" }} aria-label={`${Math.ceil(seconds)} seconds left`}>
      <svg width={size} height={size} className="ring">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-3)" strokeWidth={4} />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={4} strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - f)} style={{ transition: "stroke-dashoffset .5s linear, stroke .3s" }} />
      </svg>
      <span className="num" style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", fontSize: 12, fontWeight: 700 }}>
        {Math.ceil(seconds)}
      </span>
    </div>
  );
}

export function Drawer({ open, onClose, children }: { open: boolean; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true">
        <button className="icon-btn" style={{ position: "absolute", right: 14, top: 14 }} onClick={onClose} aria-label="Close"><X size={18} /></button>
        {children}
      </aside>
    </>
  );
}

export function KV({ k, v }: { k: ReactNode; v: ReactNode }) {
  return (
    <div className="row between" style={{ padding: "6px 0", borderBottom: "1px solid var(--line)", fontSize: 13 }}>
      <span className="muted">{k}</span>
      <span className="num" style={{ fontWeight: 500, textAlign: "right" }}>{v}</span>
    </div>
  );
}
