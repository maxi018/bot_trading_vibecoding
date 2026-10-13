import type { Trade } from "../api/types";

export interface TradeSummary {
  n: number;
  wins: number;
  winRate: number | null;
  netPnl: number;
  fees: number;
  avgWin: number | null;
  avgLoss: number | null;
  avgSlippage: number | null; // bps, solo trades con slippage medido
  expectedSlippage: number | null;
  slippageRatio: number | null; // real / esperado
}

const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

export function summarize(trades: Trade[]): TradeSummary {
  const wins = trades.filter((t) => t.pnl > 0);
  const losses = trades.filter((t) => t.pnl <= 0);
  const slips = trades.map((t) => t.slippage_bps).filter((s): s is number => s !== null);
  const avgSlippage = mean(slips);
  const expectedSlippage = mean(trades.map((t) => t.expected_slippage_bps));
  return {
    n: trades.length,
    wins: wins.length,
    winRate: trades.length ? wins.length / trades.length : null,
    netPnl: trades.reduce((s, t) => s + t.pnl, 0),
    fees: trades.reduce((s, t) => s + t.fees, 0),
    avgWin: mean(wins.map((t) => t.pnl)),
    avgLoss: mean(losses.map((t) => t.pnl)),
    avgSlippage,
    expectedSlippage,
    slippageRatio: avgSlippage !== null && expectedSlippage ? avgSlippage / expectedSlippage : null,
  };
}

const CSV_COLS: (keyof Trade)[] = [
  "id", "symbol", "side", "qty", "entry_ts", "exit_ts", "entry_price", "exit_price",
  "pnl", "fees", "return_pct", "exit_reason", "slippage_bps", "expected_slippage_bps",
];

function csvCell(v: unknown): string {
  const s = v === null || v === undefined ? "" : String(v);
  // Escapar comillas, comas y saltos; y neutralizar fórmulas (inyección CSV en Excel).
  const safe = /^[=+\-@]/.test(s) && Number.isNaN(Number(s)) ? `'${s}` : s;
  return /[",\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

export function toCsv(trades: Trade[]): string {
  const rows = trades.map((t) => CSV_COLS.map((c) => csvCell(t[c])).join(","));
  return [CSV_COLS.join(","), ...rows].join("\n");
}
