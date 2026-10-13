import { describe, expect, it } from "vitest";
import { buildRequest, parseGrid, parseList, wfCost, type BacktestForm } from "./backtest";

const form = (over: Partial<BacktestForm> = {}): BacktestForm => ({
  source: "synthetic", bars: 6000, seed: 42, files: {}, stress: 1, montecarlo: true, mcSims: 2000,
  strategy: { fast_ema: 20, slow_ema: 100, atr_stop_mult: 2.5, allow_short: false },
  riskPct: { max_risk_per_trade: 1 }, walkforward: false, grid: {}, train: 3000, test: 1000, warmup: 600, trials: 1,
  ...over,
});

describe("grilla", () => {
  it("parsea listas y detecta basura", () => {
    expect(parseList("10, 20,30")).toEqual([10, 20, 30]);
    expect(parseList("")).toEqual([]);
    expect(parseList("10, x")).toBeNull();
    expect(parseGrid({ fast_ema: "10,20", slow_ema: "" }).grid).toEqual({ fast_ema: [10, 20] });
    expect(parseGrid({ fast_ema: "" }).error).toMatch(/vacía/);
  });
  it("estima el costo del walk-forward", () => {
    expect(wfCost({ a: [1, 2], b: [1, 2, 3] }, 6000, 3000, 1000)).toEqual({ combos: 6, windows: 3, total: 18 });
  });
});

describe("buildRequest", () => {
  it("solo manda los riesgos que cambiaron, en fracción", () => {
    expect(buildRequest(form(), { max_risk_per_trade: 0.01 }).body?.risk).toBeUndefined();
    const r = buildRequest(form({ riskPct: { max_risk_per_trade: 0.5 } }), { max_risk_per_trade: 0.01 });
    expect(r.body?.risk).toEqual({ max_risk_per_trade: 0.005 });
  });
  it("valida EMAs, archivos y tope del walk-forward", () => {
    expect(buildRequest(form({ strategy: { fast_ema: 100, slow_ema: 50 } }), {}).errors[0]).toMatch(/EMA/);
    expect(buildRequest(form({ source: "files", files: { "BTC/USDT": "" } }), {}).errors).toContain("Elegí al menos un archivo de datos");
    const big = form({ walkforward: true, grid: { fast_ema: "1,2,3,4,5,6,7,8,9,10", slow_ema: "100,110,120,130,140,150,160,170,180,190" }, train: 1000, test: 1000 });
    expect(buildRequest(big, {}).errors.join()).toMatch(/máx. 300/);
  });
  it("el walk-forward cuenta como pruebas para el Deflated Sharpe", () => {
    const r = buildRequest(form({ walkforward: true, grid: { fast_ema: "10,20", slow_ema: "100,200" } }), {});
    expect(r.trialsUsed).toBe(12); // 4 combos x 3 ventanas
    expect(r.body?.n_trials).toBe(12);
    expect(r.body?.walkforward).toEqual({ grid: { fast_ema: [10, 20], slow_ema: [100, 200] }, train: 3000, test: 1000, warmup: 600 });
  });
  it("con archivos manda rutas y símbolos", () => {
    const r = buildRequest(form({ source: "files", files: { "BTC/USDT": "btc.csv", "ETH/USDT": "" } }), {});
    expect(r.body).toMatchObject({ data_files: [{ path: "btc.csv", symbol: "BTC/USDT" }], symbols: ["BTC/USDT"] });
    expect(r.body?.synthetic_bars).toBeUndefined();
  });
});
