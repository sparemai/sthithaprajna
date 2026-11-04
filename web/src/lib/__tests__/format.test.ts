import { describe, expect, it } from "vitest";
import { compact, glyph, pct, rMult, rupees, signedPct, signedRupees } from "../format";

describe("Indian-locale formatting", () => {
  it("groups rupees in lakhs and crores", () => {
    expect(rupees(102340)).toBe("₹1,02,340");
    expect(rupees(12345678.9, 2)).toBe("₹1,23,45,678.90");
    expect(rupees(-5000)).toBe("−₹5,000");
  });
  it("signs deltas explicitly so direction never depends on colour", () => {
    expect(signedRupees(1500)).toBe("+₹1,500");
    expect(signedRupees(-250)).toBe("−₹250");
    expect(signedRupees(0)).toBe("₹0");
    expect(glyph(1)).toBe("▲");
    expect(glyph(-1)).toBe("▼");
    expect(glyph(0)).toBe("•");
  });
  it("formats percentages and R multiples", () => {
    expect(pct(0.025)).toBe("2.50%");
    expect(signedPct(-0.0162)).toBe("−1.62%");
    expect(signedPct(1.23, 2, false)).toBe("+1.23%");
    expect(rMult(0.75)).toBe("+0.75R");
    expect(rMult(-1)).toBe("−1.00R");
  });
  it("compacts large numbers the Indian way", () => {
    expect(compact(12_50_00_000)).toBe("12.50 Cr");
    expect(compact(3_40_000)).toBe("3.40 L");
    expect(compact(4_200)).toBe("4.2 K");
  });
  it("handles missing values", () => {
    expect(rupees(null)).toBe("–");
    expect(pct(undefined)).toBe("–");
  });
});
