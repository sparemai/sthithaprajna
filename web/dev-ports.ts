export function devPorts(env: Record<string, string | undefined>) {
  function port(name: string, fallback: number): number {
    const raw = env[name] ?? String(fallback);
    if (!/^\d+$/.test(raw) || Number(raw) < 1 || Number(raw) > 65535) {
      throw new Error(`${name} must be an integer from 1 to 65535`);
    }
    return Number(raw);
  }
  const frontend = port("STHITAPRAJNA_WEB_PORT", 5173);
  const backend = port("STHITAPRAJNA_BACKEND_PORT", 8788);
  if (frontend === backend) throw new Error("Frontend and backend ports must be different");
  return {
    port: frontend,
    strictPort: true,
    proxy: {
      "/api": `http://127.0.0.1:${backend}`,
      "/ws": { target: `ws://127.0.0.1:${backend}`, ws: true },
    },
  };
}
