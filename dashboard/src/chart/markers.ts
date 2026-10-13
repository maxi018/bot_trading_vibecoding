// Lógica pura del gráfico (testeable sin DOM): ubicar fills en velas y estilos.
import type { SeriesMarker, Time, UTCTimestamp } from "lightweight-charts";

export interface FillLike {
  ts: number; // ms
  side: string; // "buy" | "sell"
  qty: number;
  price: number;
  tag: string; // entry | exit | reduce | stop | flatten
}

export interface MarkerColors {
  entry: string;
  exit: string;
  stop: string;
}

/**
 * Vela (time en segundos, = apertura) a la que pertenece un fill.
 * Un fill con ts EXACTO en el cierre de la vela K (decisión al cierre) cae en K, no en
 * K+1 (que todavía no existe): por eso se usa ts-1. Nunca después de la última vela.
 */
export function snapToCandle(tsMs: number, tfMs: number, lastCandleTime: number | null): number {
  const open = Math.floor((tsMs - 1) / tfMs) * tfMs;
  const t = Math.floor(open / 1000);
  return lastCandleTime !== null ? Math.min(t, lastCandleTime) : t;
}

const LABEL: Record<string, string> = {
  entry: "ENTRADA",
  exit: "SALIDA",
  reduce: "REDUCE",
  stop: "STOP",
  flatten: "KILL",
};

export function toMarker(f: FillLike, tfMs: number, lastCandleTime: number | null, c: MarkerColors): SeriesMarker<Time> {
  const buy = f.side === "buy";
  const isEntry = f.tag === "entry";
  const color = f.tag === "stop" || f.tag === "flatten" ? c.stop : isEntry ? c.entry : c.exit;
  return {
    time: snapToCandle(f.ts, tfMs, lastCandleTime) as UTCTimestamp,
    position: buy ? "belowBar" : "aboveBar",
    shape: buy ? "arrowUp" : "arrowDown",
    color,
    text: `${LABEL[f.tag] ?? f.tag.toUpperCase()} ${f.qty.toPrecision(3)}`,
  };
}

/**
 * Ordena (lo exige lightweight-charts) y deja texto SOLO en el último marcador:
 * etiquetas en cada punto se pisan y tapan las velas. El detalle de cada fill se ve
 * en la leyenda al pasar el mouse por la vela.
 */
export function prepareMarkers(m: SeriesMarker<Time>[]): SeriesMarker<Time>[] {
  const sorted = [...m].sort((a, b) => (a.time as number) - (b.time as number));
  return sorted.map((x, i) => (i === sorted.length - 1 ? x : { ...x, text: "" }));
}
