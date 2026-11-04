// Indian-locale formatting: Rs 1,02,340.50 (lakh grouping), signed deltas with a glyph so direction
// never depends on colour alone.
const inr0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const inr2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  return digits === 0 ? inr0.format(x) : new Intl.NumberFormat("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(x);
}

export function rupees(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  const s = digits === 0 ? inr0.format(Math.abs(x)) : inr2.format(Math.abs(x));
  return `${x < 0 ? "−" : ""}₹${s}`;
}

export function signedRupees(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  const s = digits === 0 ? inr0.format(Math.abs(x)) : inr2.format(Math.abs(x));
  return `${x > 0 ? "+" : x < 0 ? "−" : ""}₹${s}`;
}

export function pct(x: number | null | undefined, digits = 2, fromFraction = true): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  const v = fromFraction ? x * 100 : x;
  return `${v.toFixed(digits)}%`;
}

export function signedPct(x: number | null | undefined, digits = 2, fromFraction = true): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  const v = fromFraction ? x * 100 : x;
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(digits)}%`;
}

export function rMult(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  return `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(digits)}R`;
}

export function dirClass(x: number | null | undefined): string {
  if (!x) return "";
  return x > 0 ? "up" : "down";
}

export function glyph(x: number | null | undefined): string {
  if (!x) return "•";
  return x > 0 ? "▲" : "▼";
}

export function compact(x: number | null | undefined): string {
  if (x === null || x === undefined) return "–";
  const a = Math.abs(x);
  if (a >= 1e7) return `${(x / 1e7).toFixed(2)} Cr`;
  if (a >= 1e5) return `${(x / 1e5).toFixed(2)} L`;
  if (a >= 1e3) return `${(x / 1e3).toFixed(1)} K`;
  return x.toFixed(0);
}

export function time(iso: string | null | undefined): string {
  if (!iso) return "–";
  return iso.slice(11, 19);
}

export function hm(iso: string | null | undefined): string {
  if (!iso) return "–";
  return iso.slice(11, 16);
}

export function title(s: string): string {
  return s.replace(/_/g, " ").toLowerCase().replace(/(^|\s)\S/g, (m) => m.toUpperCase());
}

export function gradeClass(g: string | null | undefined): string {
  if (!g) return "grade grade-C";
  return `grade grade-${g === "A+" ? "Aplus" : g}`;
}

export const GEAR_COLOR: Record<string, string> = {
  ATTACK: "var(--g-attack)", PRESS: "var(--g-press)", GUARD: "var(--g-guard)", PRESERVE: "var(--g-preserve)", HALTED: "var(--g-halted)",
};
