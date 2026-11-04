// Thin API client. When the server runs with STHITAPRAJNA_API_TOKEN, the operator enters it once and it is
// kept in this browser only (localStorage), sent as `x-operator-token`.
export function getToken(): string {
  try { return localStorage.getItem("sp-token") || ""; } catch { return ""; }
}
export function setToken(t: string) {
  try { localStorage.setItem("sp-token", t); } catch { /* private mode */ }
}

function headers(): HeadersInit {
  const t = getToken();
  return t ? { "Content-Type": "application/json", "x-operator-token": t } : { "Content-Type": "application/json" };
}

export class AuthError extends Error {}

export async function get<T = any>(path: string): Promise<T> {
  const r = await fetch(path, { headers: headers() });
  if (r.status === 401) throw new AuthError("operator token required");
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export async function post<T = any>(path: string, body: unknown = {}): Promise<T> {
  const r = await fetch(path, { method: "POST", headers: headers(), body: JSON.stringify(body) });
  if (r.status === 401) throw new AuthError("operator token required");
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export const act = (action: string, body: unknown = {}) => post(`/api/actions/${action}`, body);

export function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const t = getToken();
  return `${proto}://${location.host}/ws${t ? `?token=${encodeURIComponent(t)}` : ""}`;
}
