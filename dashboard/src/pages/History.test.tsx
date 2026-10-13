import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Trade } from "../api/types";
import { TradeRow } from "./History";

const base: Trade = {
  id: 1, symbol: "ETH/USDT", side: "long", qty: 0.5, entry_ts: 0, exit_ts: 7_200_000,
  entry_price: 3000, exit_price: 2950, pnl: -26.5, fees: 1.5, return_pct: -0.0167,
  exit_reason: "stop", slippage_bps: 12.5, expected_slippage_bps: 5,
};
const cell = (c: HTMLElement, label: string) => c.querySelector(`[data-label="${label}"]`) as HTMLElement;

describe("TradeRow", () => {
  it("slippage peor que lo esperado se marca en rojo con el desvío", () => {
    const { container } = render(<table><tbody><TradeRow t={base} /></tbody></table>);
    expect(cell(container, "vs esperado").textContent).toBe("+7.5 bps");
    expect(cell(container, "vs esperado").className).toContain("down");
    expect(cell(container, "Salida por").textContent).toBe("Stop");
    expect(cell(container, "Duración").textContent).toBe("2 h 0 min");
    expect(cell(container, "PnL neto").className).toContain("down");
  });
  it("slippage mejor que lo esperado en verde; sin medición muestra guion", () => {
    const { container } = render(<table><tbody>
      <TradeRow t={{ ...base, id: 2, slippage_bps: 2 }} />
      <TradeRow t={{ ...base, id: 3, slippage_bps: null }} />
    </tbody></table>);
    const rows = container.querySelectorAll("tr");
    expect(cell(rows[0]!, "vs esperado").textContent).toBe("−3.0 bps");
    expect(cell(rows[0]!, "vs esperado").className).toContain("up");
    expect(cell(rows[1]!, "vs esperado").textContent).toBe("—");
  });
});
