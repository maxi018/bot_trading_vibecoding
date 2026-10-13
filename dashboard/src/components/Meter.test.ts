import { describe, expect, it } from "vitest";
import { money, pct } from "../lib/format";
import { severityFor } from "./Meter";

describe("severidad del medidor", () => {
  it("se pone roja cerca del límite", () => {
    expect(severityFor(0.2)).toBe("good");
    expect(severityFor(0.5)).toBe("warning");
    expect(severityFor(0.79)).toBe("warning");
    expect(severityFor(0.8)).toBe("critical");
    expect(severityFor(1.3)).toBe("critical");
  });
});

describe("formato", () => {
  it("nunca inventa números", () => {
    expect(money(null)).toBe("—");
    expect(pct(Number.NaN)).toBe("—");
  });
  it("signo explícito", () => {
    expect(money(-12.5)).toBe("−$12.50");
    expect(money(3, { signed: true })).toBe("+$3.00");
    expect(pct(0.0123, { signed: true })).toBe("+1.23%");
  });
});
