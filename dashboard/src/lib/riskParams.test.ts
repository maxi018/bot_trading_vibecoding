import { describe, expect, it } from "vitest";
import { classify, fromDisplay, META, toDisplay } from "./riskParams";

describe("conversión de unidades", () => {
  it("porcentajes se muestran x100 y vuelven a fracción", () => {
    expect(toDisplay("max_risk_per_trade", 0.01)).toBe(1);
    expect(fromDisplay("max_risk_per_trade", "0,5")).toBe(0.005);
    expect(toDisplay("max_slippage_bps", 30)).toBe(30);
  });
  it("rechaza texto inválido, negativos y decimales en enteros", () => {
    expect(fromDisplay("max_risk_per_trade", "abc")).toBeNull();
    expect(fromDisplay("max_risk_per_trade", "-1")).toBeNull();
    expect(fromDisplay("max_risk_per_trade", "1e3")).toBeNull();
    expect(fromDisplay("max_orders_per_minute", "2.5")).toBeNull();
    expect(fromDisplay("max_orders_per_minute", "12")).toBe(12);
  });
});

describe("classify", () => {
  it("espeja la regla del backend", () => {
    expect(classify("lower", 0.01, 0.005)).toBe("tightens");
    expect(classify("lower", 0.01, 0.02)).toBe("relaxes");
    expect(classify("higher", 5, 10)).toBe("tightens");
    expect(classify("lower", 1, 1)).toBe("same");
  });
});

it("todos los parámetros tienen metadatos", () => {
  const fields = ["target_vol_annual", "max_risk_per_trade", "total_drawdown_kill", "exit_protection_bps"];
  for (const f of fields) expect(META[f]).toBeTruthy();
  expect(Object.keys(META)).toHaveLength(30);
});
