// Metadatos y reglas del editor de parámetros de riesgo.
// La validación DEFINITIVA la hace el backend (risk/params.py); esto solo guía al operador.

export type Unit = "pct" | "bps" | "x" | "s" | "n" | "usd";
export type Group = "Sizing" | "Límites de exposición" | "Circuit breakers" | "Fat finger" | "Ejecución";

export interface ParamMeta {
  label: string;
  unit: Unit;
  group: Group;
  int?: boolean;
}

export const META: Record<string, ParamMeta> = {
  target_vol_annual: { label: "Volatilidad objetivo anual", unit: "pct", group: "Sizing" },
  vol_ewma_span: { label: "Ventana de volatilidad (EWMA)", unit: "n", group: "Sizing", int: true },
  kelly_fraction: { label: "Fracción de Kelly (techo)", unit: "x", group: "Sizing" },
  kelly_min_trades: { label: "Trades mínimos para usar Kelly", unit: "n", group: "Sizing", int: true },
  max_leverage_per_asset: { label: "Apalancamiento máx. por activo", unit: "x", group: "Sizing" },
  rebalance_threshold: { label: "Banda de no-rebalanceo", unit: "pct", group: "Sizing" },
  max_risk_per_trade: { label: "Riesgo máx. por trade", unit: "pct", group: "Límites de exposición" },
  max_position_pct: { label: "Peso máx. por activo", unit: "pct", group: "Límites de exposición" },
  max_gross_leverage: { label: "Apalancamiento bruto máx.", unit: "x", group: "Límites de exposición" },
  max_correlated_exposure: { label: "Exposición correlacionada máx.", unit: "pct", group: "Límites de exposición" },
  correlation_threshold: { label: "Umbral de correlación", unit: "x", group: "Límites de exposición" },
  min_order_notional: { label: "Notional mínimo por orden", unit: "usd", group: "Límites de exposición" },
  daily_drawdown_halt: { label: "Pérdida diaria → HALT", unit: "pct", group: "Circuit breakers" },
  total_drawdown_kill: { label: "Drawdown total → KILL", unit: "pct", group: "Circuit breakers" },
  expected_slippage_bps: { label: "Slippage esperado", unit: "bps", group: "Circuit breakers" },
  slippage_anomaly_multiplier: { label: "Slippage anómalo (× esperado)", unit: "x", group: "Circuit breakers" },
  slippage_window: { label: "Ventana de slippage (fills)", unit: "n", group: "Circuit breakers", int: true },
  max_slippage_bps: { label: "Slippage estimado máx. por orden", unit: "bps", group: "Circuit breakers" },
  consecutive_losses_reduce: { label: "Pérdidas seguidas → achicar", unit: "n", group: "Circuit breakers", int: true },
  size_reduction_factor: { label: "Factor de reducción", unit: "x", group: "Circuit breakers" },
  max_data_age_s: { label: "Antigüedad máx. de datos", unit: "s", group: "Circuit breakers" },
  max_reject_rate: { label: "Tasa máx. de rechazos", unit: "pct", group: "Circuit breakers" },
  reject_window: { label: "Ventana de rechazos (órdenes)", unit: "n", group: "Circuit breakers", int: true },
  min_orders_for_reject_rate: { label: "Órdenes mín. para medir rechazos", unit: "n", group: "Circuit breakers", int: true },
  max_price_deviation_pct: { label: "Desvío máx. de precio", unit: "pct", group: "Fat finger" },
  max_order_notional_pct: { label: "Notional máx. por orden", unit: "pct", group: "Fat finger" },
  max_orders_per_minute: { label: "Órdenes máx. por minuto", unit: "n", group: "Fat finger", int: true },
  max_order_size_vs_avg: { label: "Tamaño máx. vs. promedio", unit: "x", group: "Fat finger" },
  market_protection_bps: { label: "Protección de precio (entradas)", unit: "bps", group: "Ejecución" },
  exit_protection_bps: { label: "Protección de precio (salidas)", unit: "bps", group: "Ejecución" },
};

export const GROUPS: Group[] = ["Límites de exposición", "Circuit breakers", "Sizing", "Fat finger", "Ejecución"];

export const UNIT_SUFFIX: Record<Unit, string> = { pct: "%", bps: "bps", x: "×", s: "s", n: "", usd: "USD" };

/** Valor interno -> valor que ve el operador (0.01 -> 1 para porcentajes). */
export function toDisplay(name: string, v: number): number {
  return META[name]?.unit === "pct" ? Math.round(v * 1e6) / 1e4 : v;
}

/** Valor tipeado -> valor interno. null si no es un número válido. */
export function fromDisplay(name: string, text: string): number | null {
  const t = text.trim().replace(",", ".");
  if (t === "" || !/^-?\d*\.?\d+$/.test(t)) return null;
  const n = Number(t);
  if (!Number.isFinite(n) || n < 0) return null;
  const m = META[name];
  if (m?.int && !Number.isInteger(n)) return null;
  return m?.unit === "pct" ? n / 100 : n;
}

export type ChangeKind = "tightens" | "relaxes" | "same";

/** Misma regla que el backend: según la dirección conservadora declarada por el bot. */
export function classify(direction: "lower" | "higher" | null, oldV: number, newV: number): ChangeKind {
  if (Math.abs(newV - oldV) < 1e-12) return "same";
  const up = newV > oldV;
  if (direction === "lower") return up ? "relaxes" : "tightens";
  return up ? "tightens" : "relaxes";
}
