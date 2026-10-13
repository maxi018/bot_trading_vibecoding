import { describe, expect, it } from "vitest";
import type { Trade } from "../api/types";
import { summarize, toCsv } from "./trades";

const t = (pnl: number, slip: number | null, extra: Partial<Trade> = {}): Trade => ({
  id: 1, symbol: "BTC/USDT", side: "long", qty: 1, entry_ts: 0, exit_ts: 1, entry_price: 100,
  exit_price: 101, pnl, fees: 0.5, return_pct: 0.01, exit_reason: "stop", slippage_bps: slip,
  expected_slippage_bps: 5, ...extra,
});

describe("summarize", () => {
  it("calcula win rate, promedios y slippage real vs esperado", () => {
    const s = summarize([t(10, 4), t(-5, 12), t(20, null)]);
    expect(s.n).toBe(3);
    expect(s.winRate).toBeCloseTo(2 / 3);
    expect(s.netPnl).toBe(25);
    expect(s.avgWin).toBe(15);
    expect(s.avgLoss).toBe(-5);
    expect(s.avgSlippage).toBe(8); // ignora el trade sin medición
    expect(s.slippageRatio).toBeCloseTo(1.6);
  });
  it("sin trades no inventa números", () => {
    const s = summarize([]);
    expect(s.winRate).toBeNull();
    expect(s.avgSlippage).toBeNull();
  });
});

describe("toCsv", () => {
  it("escapa comas/comillas y neutraliza fórmulas", () => {
    const csv = toCsv([t(1, 2, { exit_reason: '=HYPERLINK("x")' }), t(1, 2, { exit_reason: "a,b" })]);
    const lines = csv.split("\n");
    expect(lines[0]).toContain("exit_reason");
    expect(lines[1]).toContain(`"'=HYPERLINK(""x"")"`);
    expect(lines[2]).toContain('"a,b"');
    expect(toCsv([t(-3, 1)]).split("\n")[1]).toContain(",-3,"); // negativos NO se tocan
  });
});
