import { useStore } from "../lib/store";
/** The mark: a circle (the whole), a level horizon (equanimity), a steady point above it (attention). */
export function BrandMark({ size = 26 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <circle cx="16" cy="16" r="13" fill="none" stroke="currentColor" strokeWidth="2.2" />
      <path d="M5 18h22" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
      <circle cx="16" cy="11" r="2.6" fill="var(--accent)" />
    </svg>
  );
}

export function Brand({ onClick }: { onClick?: () => void }) {
  const cash = useStore((s) => s.snap?.product === "intraday500");
  return (
    <a className="brand" href="#/dashboard" onClick={onClick} aria-label="Sthitaprajna 2.0 home">
      <BrandMark />
      <span className="brand-word">STHITAPRAJNA</span>
      <span className="brand-badge" title={cash ? "NIFTY 500 cash intraday" : "Index and stock options"}>{cash ? "INTRADAY 500" : "2.0 OPTIONS"}</span>
    </a>
  );
}
