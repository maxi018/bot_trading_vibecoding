// Tipos que espejan las respuestas de quantbot/api (state.py / server.py).

export type Mode = "paper" | "live" | "demo";
export type BotStatus = "RUNNING" | "PAUSED" | "HALTED" | "KILLED";
export type BreakerStatus = "green" | "yellow" | "red";

export interface Overview {
  mode: Mode;
  testnet: boolean | null;
  status: BotStatus;
  halt_reasons: string[];
  kill: { reason: string; at: number; flatten: boolean } | null;
  equity: number | null;
  cash: number | null;
  pnl_day: number | null;
  pnl_day_pct: number | null;
  pnl_total: number | null;
  pnl_total_pct: number | null;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  fees_paid: number | null;
  drawdown: number | null;
  drawdown_limit: number;
  daily_loss_limit: number;
  peak_equity: number | null;
  gross_leverage: number | null;
  max_gross_leverage: number;
  open_positions: number;
  consecutive_losses: number;
  size_multiplier: number;
  symbols: string[];
  timeframe: string;
  sim_time: number;
  uptime_s: number;
}

export interface Health {
  feed_connected: boolean;
  reconnects: number;
  last_data_age_s: number | null;
  stale_threshold_s: number;
  stale: boolean;
  stale_sources: string[];
  feed_lag_ms: number | null;
  exchange_rtt_ms: number | null;
  queue_size: number;
  queue_backlog: boolean;
  server_time: number;
}

export interface Position {
  symbol: string;
  side: "long" | "short";
  qty: number;
  avg_entry: number | null;
  mark: number | null;
  notional: number | null;
  unrealized_pnl: number | null;
  unrealized_pct: number | null;
  stop: number | null;
  trailing_stop: number | null;
  distance_to_stop_pct: number | null;
  risk_to_stop: number | null;
  protected: boolean;
  entry_ts: number | null;
}

export interface OpenOrder {
  client_order_id: string;
  symbol: string;
  side: "buy" | "sell";
  type: string;
  qty: number;
  filled_qty: number;
  price: number | null;
  stop_price: number | null;
  status: string;
  tag: string;
  reduce_only: boolean;
  created_ts: number;
}

export interface Breaker {
  key: string;
  label: string;
  value: number | null;
  threshold: number;
  level: number | null;
  status: BreakerStatus;
  tripped: boolean;
  action: string;
}

export interface Snapshot {
  type: "snapshot";
  overview: Overview;
  health: Health;
  positions: Position[];
  orders: OpenOrder[];
  breakers: Breaker[];
  stops: Record<string, { stop: number | null; trailing: number | null }>;
}

export type BotEvent =
  | { type: "bar"; symbol: string; bar: Candle }
  | { type: "fill"; ts: number; symbol: string; side: string; qty: number; price: number; tag: string; slippage_bps: number | null }
  | { type: "veto"; ts: number; symbol: string; direction: string; reason: string }
  | { type: "alert"; ts: number; severity: string; title: string; body: string }
  | { type: "command"; ts: number; operator: string; action: string; details: unknown };

export interface Candle {
  time: number; // segundos
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}
