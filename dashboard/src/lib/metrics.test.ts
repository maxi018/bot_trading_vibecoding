import { describe, expect, it } from "vitest";
import { alignCurves, guide, type Point } from "./metrics";

const H = 3_600_000;

describe("alignCurves", () => {
  it("normaliza a retorno y velas transcurridas, recortando el backtest al horizonte live", () => {
    const live: Point[] = [[100 * H, 1000, 0], [101 * H, 1100, 0], [102 * H, 1050, 0.045]];
    const bt: Point[] = [[0, 50], [H, 55], [2 * H, 60], [3 * H, 70], [4 * H, 80]];
    const out = alignCurves(live, bt, H);
    expect(out.map((p) => p.x)).toEqual([0, 1, 2]);
    expect(out[1]!.live).toBeCloseTo(0.1);
    expect(out[2]!.bt).toBeCloseTo(0.2); // 60/50 - 1
    expect(out[2]!.dd).toBeCloseTo(0.045);
  });
  it("sin backtest de referencia, bt es null", () => {
    expect(alignCurves([[0, 1, 0]], null, H)[0]!.bt).toBeNull();
  });
  it("backtest más corto que live: no inventa valores más allá de su fin", () => {
    const out = alignCurves([[0, 1, 0], [H, 1, 0], [5 * H, 1, 0]], [[0, 1], [H, 2]], H);
    expect(out[1]!.bt).toBe(1);
    expect(out[2]!.bt).toBeNull();
  });
});

describe("guide", () => {
  it("banderas rojas de la spec", () => {
    expect(guide("sharpe", 3.5, 200).warn).toMatch(/bug/);
    expect(guide("sharpe", 1.5, 200).warn).toBeUndefined();
    expect(guide("profit_factor", 4, 80).warn).toBeTruthy();
    expect(guide("profit_factor", 4, 20).warn).toBeUndefined();
    expect(guide("n_trades", 30, 30).warn).toMatch(/anecdótico/);
    expect(guide("expectancy", -2, 10).warn).toBeTruthy();
  });
});
