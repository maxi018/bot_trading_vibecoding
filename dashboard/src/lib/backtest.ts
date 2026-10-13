// Lógica pura del formulario de backtests (el backend vuelve a validar todo).

export const MAX_WF_BACKTESTS = 300; // mismo tope que quantbot/api/backtest_jobs.py

export interface BacktestForm {
  source: "synthetic" | "files";
  bars: number;
  seed: number;
  files: Record<string, string>; // símbolo -> ruta relativa ("" = no usar)
  strategy: Record<string, number | boolean>;
  riskPct: Record<string, number>; // en % (como lo escribe el operador)
  stress: number;
  montecarlo: boolean;
  mcSims: number;
  walkforward: boolean;
  grid: Record<string, string>; // "10, 20, 30"
  train: number;
  test: number;
  warmup: number;
  trials: number;
}

/** "10, 20,30" -> [10, 20, 30]; null si hay algo que no es número. "" -> []. */
export function parseList(text: string): number[] | null {
  const parts = text.split(",").map((s) => s.trim()).filter(Boolean);
  const nums = parts.map(Number);
  return nums.some((n) => !Number.isFinite(n)) ? null : nums;
}

export function parseGrid(grid: Record<string, string>): { grid: Record<string, number[]>; error: string | null } {
  const out: Record<string, number[]> = {};
  for (const [k, v] of Object.entries(grid)) {
    const list = parseList(v);
    if (list === null) return { grid: {}, error: `Grilla inválida en ${k}` };
    if (list.length) out[k] = list;
  }
  return { grid: out, error: Object.keys(out).length ? null : "La grilla está vacía" };
}

export function wfCost(grid: Record<string, number[]>, nBars: number, train: number, test: number) {
  const combos = Object.values(grid).reduce((a, v) => a * Math.max(1, v.length), 1);
  const windows = test > 0 ? Math.max(0, Math.floor((nBars - train) / test)) : 0;
  return { combos, windows, total: combos * windows };
}

export interface BuildResult { body: Record<string, unknown> | null; errors: string[]; trialsUsed: number }

export function buildRequest(f: BacktestForm, baseRisk: Record<string, number>): BuildResult {
  const errors: string[] = [];
  const fast = Number(f.strategy.fast_ema);
  const slow = Number(f.strategy.slow_ema);
  if (!(fast < slow)) errors.push("EMA rápida tiene que ser menor que la lenta");
  const body: Record<string, unknown> = {
    strategy: f.strategy,
    cost_stress: f.stress,
    montecarlo: f.montecarlo,
    mc_sims: f.mcSims,
    seed: f.seed,
  };
  // Riesgo: solo los que cambiaron respecto de la config del bot.
  const risk: Record<string, number> = {};
  for (const [k, pctValue] of Object.entries(f.riskPct)) {
    const v = pctValue / 100;
    if (!Number.isFinite(v) || v <= 0) errors.push(`Valor inválido en ${k}`);
    else if (Math.abs(v - (baseRisk[k] ?? NaN)) > 1e-12) risk[k] = v;
  }
  if (Object.keys(risk).length) body.risk = risk;

  let nBars = f.bars;
  if (f.source === "files") {
    const chosen = Object.entries(f.files).filter(([, p]) => p);
    if (!chosen.length) errors.push("Elegí al menos un archivo de datos");
    body.data_files = chosen.map(([symbol, path]) => ({ path, symbol }));
    body.symbols = chosen.map(([s]) => s);
    nBars = NaN; // se conoce recién al cargar los archivos
  } else {
    if (!(f.bars >= 500 && f.bars <= 50_000)) errors.push("Barras sintéticas: entre 500 y 50.000");
    body.synthetic_bars = f.bars;
  }

  let trialsUsed = Math.max(1, f.trials);
  if (f.walkforward) {
    const { grid, error } = parseGrid(f.grid);
    if (error) errors.push(error);
    else {
      body.walkforward = { grid, train: f.train, test: f.test, warmup: f.warmup };
      if (Number.isFinite(nBars)) {
        const c = wfCost(grid, nBars, f.train, f.test);
        if (c.windows < 1) errors.push("Train + test no entran en las barras disponibles");
        if (c.total > MAX_WF_BACKTESTS) errors.push(`Walk-forward de ${c.total} backtests (máx. ${MAX_WF_BACKTESTS})`);
        trialsUsed = Math.max(trialsUsed, c.total);
      }
    }
  }
  body.n_trials = trialsUsed; // honestidad: cuenta para el Deflated Sharpe
  return { body: errors.length ? null : body, errors, trialsUsed };
}
