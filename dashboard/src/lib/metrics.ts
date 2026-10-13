// Lógica pura de la pantalla de métricas: alinear curvas live vs backtest y guías de lectura.

export type Point = [number, number, ...number[]]; // [ts_ms, equity, (drawdown)]

export interface AlignedPoint {
  x: number; // velas transcurridas desde el inicio de cada curva
  live: number; // retorno acumulado (0.05 = +5%)
  bt: number | null;
  dd: number; // drawdown live (fracción positiva)
}

/**
 * Pone ambas curvas en la MISMA escala: retorno acumulado vs. velas desde el inicio.
 * El backtest se recorta al horizonte live (comparar 6000 velas contra 300 no dice nada)
 * y se remuestrea sobre la grilla live (último punto conocido <= x).
 */
export function alignCurves(live: Point[], bt: Point[] | null, tfMs: number): AlignedPoint[] {
  if (!live.length) return [];
  const [t0, e0] = live[0]!;
  const btPts = bt?.length ? bt.map(([t, e]) => ({ x: (t - bt[0]![0]) / tfMs, r: e / bt[0]![1] - 1 })) : [];
  let j = 0;
  return live.map(([t, e, dd]) => {
    const x = Math.round((t - t0) / tfMs);
    while (j + 1 < btPts.length && btPts[j + 1]!.x <= x) j++;
    const b = btPts[j];
    const inRange = b !== undefined && btPts.length > 0 && x <= btPts[btPts.length - 1]!.x;
    return { x, live: e / e0 - 1, bt: inRange ? b.r : null, dd: dd ?? 0 };
  });
}

export interface Guide {
  note: string; // "referencia sana" de la spec (§6.2)
  warn?: string; // bandera roja concreta, si aplica
}

export function guide(key: string, value: number | null, nTrades: number): Guide {
  const v = value ?? NaN;
  switch (key) {
    case "sharpe":
      return { note: "> 1 bueno · > 2 excelente", warn: v > 3 ? "> 3: asumir que hay un bug" : undefined };
    case "sortino":
      return { note: "como Sharpe, solo penaliza la volatilidad bajista" };
    case "max_drawdown":
      return { note: "¿tolerás verlo sin apagar el bot?" };
    case "calmar":
      return { note: "retorno anual / max DD · > 1 bueno" };
    case "profit_factor":
      return {
        note: "1.3 a 2 es realista",
        warn: v > 3 && nTrades > 50 ? "> 3 con muchos trades: sospechar" : undefined,
      };
    case "expectancy":
      return { note: "tiene que ser positiva DESPUÉS de costos", warn: nTrades > 0 && v <= 0 ? "expectancy no positiva" : undefined };
    case "win_rate":
      return { note: "solo no significa nada (ver ganancia/pérdida)" };
    case "n_trades":
      return { note: "significancia estadística", warn: nTrades < 100 ? "< 100 trades: anecdótico" : undefined };
    case "recovery":
      return { note: "un DD que dura mucho destruye la confianza" };
    case "deflated_sharpe":
      return { note: "prob. de edge real descontando las pruebas hechas", warn: !Number.isNaN(v) && v < 0.5 ? "< 50%: no distinguible de suerte" : undefined };
    default:
      return { note: "" };
  }
}
