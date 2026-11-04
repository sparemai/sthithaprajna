import { describe, expect, it } from "vitest";
import { devPorts } from "./dev-ports";

describe("development ports", () => {
  it("preserves defaults", () => {
    expect(devPorts({})).toMatchObject({ port: 5173, strictPort: true,
      proxy: { "/api": "http://127.0.0.1:8788", "/ws": { target: "ws://127.0.0.1:8788", ws: true } } });
  });
  it("routes HTTP and WebSockets to the same selected backend", () => {
    expect(devPorts({ STHITAPRAJNA_WEB_PORT: "5180", STHITAPRAJNA_BACKEND_PORT: "8790" }))
      .toMatchObject({ port: 5180, proxy: { "/api": "http://127.0.0.1:8790",
        "/ws": { target: "ws://127.0.0.1:8790" } } });
  });
  it.each(["0", "65536", "abc", "8788.5", "-1", ""])("rejects invalid port %s", value => {
    expect(() => devPorts({ STHITAPRAJNA_BACKEND_PORT: value })).toThrow();
    expect(() => devPorts({ STHITAPRAJNA_WEB_PORT: value })).toThrow();
  });
  it("rejects port collisions", () => {
    expect(() => devPorts({ STHITAPRAJNA_WEB_PORT: "8788" })).toThrow();
  });
});
