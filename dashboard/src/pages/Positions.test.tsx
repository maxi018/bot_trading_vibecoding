import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Position } from "../api/types";
import { PositionRow } from "./Positions";

const base: Position = {
  symbol: "BTC/USDT", side: "long", qty: 0.1, avg_entry: 20000, mark: 21000, notional: 2100,
  unrealized_pnl: 100, unrealized_pct: 0.05, stop: 20500, trailing_stop: 20500,
  distance_to_stop_pct: 0.0238, risk_to_stop: 50, protected: true, entry_ts: 0,
};

function row(p: Position) {
  return render(<table><tbody><PositionRow p={p} equity={10000} now={3_600_000} /></tbody></table>);
}

describe("PositionRow", () => {
  it("muestra datos clave con etiquetas para el modo tarjeta", () => {
    const { container } = row(base);
    expect(screen.getByText("BTC/USDT")).toBeTruthy();
    expect(container.querySelector('[data-label="Distancia al stop"]')?.textContent).toContain("2.38%");
    expect(container.querySelector('[data-label="Abierta"]')?.textContent).toBe("hace 1 h 0 min");
  });
  it("una posición sin stop se marca en rojo y bien visible", () => {
    const { container } = row({ ...base, protected: false, stop: null, distance_to_stop_pct: null, risk_to_stop: null });
    expect(screen.getByText("⚠ SIN STOP")).toBeTruthy();
    expect(container.querySelector("tr")?.className).toBe("row-alert");
  });
  it("avisa cuando el precio está muy cerca del stop", () => {
    const { container } = row({ ...base, distance_to_stop_pct: 0.003 });
    expect(container.querySelector('[data-label="Distancia al stop"]')?.className).toContain("down");
  });
  it("muestra el trailing cuando difiere del stop colocado", () => {
    row({ ...base, trailing_stop: 20700 });
    expect(screen.getByText(/trailing → 20,700.00/)).toBeTruthy();
  });
});
