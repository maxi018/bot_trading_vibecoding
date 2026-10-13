// Conexión WebSocket en vivo con reconexión automática y detección de datos viejos.
//
// Reglas de "frescura" (si cualquiera falla, la UI muestra DATOS DESACTUALIZADOS):
//  - el socket está abierto y autenticado,
//  - llegó un snapshot hace menos de STALE_AFTER_MS,
//  - el propio bot no reporta sus datos de mercado como viejos (health.stale).
import { useSyncExternalStore } from "react";
import type { BotEvent, Snapshot } from "./types";

export const STALE_AFTER_MS = 3500;
const ZOMBIE_AFTER_MS = 10_000;
const MAX_EVENTS = 300;

export type ConnState = "connecting" | "open" | "closed" | "unauthorized";

export interface LiveState {
  conn: ConnState;
  snapshot: Snapshot | null;
  lastSnapshotAt: number | null; // Date.now() local
  stale: boolean;
  staleReason: string;
  events: ReceivedEvent[]; // más nuevo primero
  attempts: number;
}

type Listener = () => void;

/** Evento + hora LOCAL de recepción (en demo el ts del bot es tiempo simulado). */
export interface ReceivedEvent {
  at: number;
  ev: BotEvent;
}

export class LiveConnection {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private tick: ReturnType<typeof setInterval> | null = null;
  private stopped = false;
  private lastMsgAt = 0;
  private eventListeners = new Set<(e: BotEvent) => void>();
  state: LiveState = {
    conn: "connecting",
    snapshot: null,
    lastSnapshotAt: null,
    stale: true,
    staleReason: "Conectando…",
    events: [],
    attempts: 0,
  };

  constructor(
    private url: string,
    private token: string,
    private now: () => number = () => Date.now(),
    private WS: typeof WebSocket = WebSocket,
  ) {}

  start(): void {
    this.stopped = false;
    this.connect();
    this.tick = setInterval(() => this.recompute(), 500);
  }

  stop(): void {
    this.stopped = true;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.tick) clearInterval(this.tick);
    this.ws?.close();
  }

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  onEvent(fn: (e: BotEvent) => void): () => void {
    this.eventListeners.add(fn);
    return () => this.eventListeners.delete(fn);
  }

  getState = (): LiveState => this.state;

  private set(patch: Partial<LiveState>): void {
    this.state = { ...this.state, ...patch };
    this.recompute(false);
    this.listeners.forEach((fn) => fn());
  }

  /** Recalcula "stale"; si cambia, notifica. */
  private recompute(notify = true): void {
    const s = this.state;
    let reason = "";
    if (s.conn === "unauthorized") reason = "Sesión inválida";
    else if (s.conn !== "open") reason = "Sin conexión con el bot — reconectando…";
    else if (s.lastSnapshotAt === null) reason = "Esperando datos…";
    else if (this.now() - s.lastSnapshotAt > STALE_AFTER_MS)
      reason = `Sin actualizaciones hace ${Math.round((this.now() - s.lastSnapshotAt) / 1000)} s`;
    else if (s.snapshot?.health.stale) reason = describeBotStale(s.snapshot);
    // Socket "zombie": abierto pero mudo. Se fuerza la reconexión.
    if (s.conn === "open" && this.ws && this.now() - this.lastMsgAt > ZOMBIE_AFTER_MS) {
      this.ws.close();
    }
    const stale = reason !== "";
    if (stale !== s.stale || reason !== s.staleReason) {
      this.state = { ...this.state, stale, staleReason: reason };
      if (notify) this.listeners.forEach((fn) => fn());
    }
  }

  private connect(): void {
    if (this.stopped) return;
    this.set({ conn: "connecting" });
    let ws: WebSocket;
    try {
      ws = new this.WS(this.url);
    } catch {
      this.scheduleRetry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: this.token }));
    ws.onmessage = (msg) => {
      let data: { type: string } & Record<string, unknown>;
      try {
        data = JSON.parse(String(msg.data));
      } catch {
        return;
      }
      this.lastMsgAt = this.now();
      if (data.type === "hello") {
        this.set({ conn: "open", attempts: 0 });
      } else if (data.type === "snapshot") {
        this.set({ snapshot: data as unknown as Snapshot, lastSnapshotAt: this.now() });
      } else if (data.type === "event") {
        const ev = data.event as BotEvent;
        this.eventListeners.forEach((fn) => fn(ev));
        if (ev.type !== "bar") {
          const events = [{ at: this.now(), ev }, ...this.state.events].slice(0, MAX_EVENTS);
          this.set({ events });
        }
      }
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (e.code === 4401) {
        this.set({ conn: "unauthorized" });
        return; // no reintentar con un token malo
      }
      this.set({ conn: "closed" });
      this.scheduleRetry();
    };
    ws.onerror = () => ws.close();
  }

  private scheduleRetry(): void {
    if (this.stopped) return;
    const attempts = this.state.attempts + 1;
    // backoff exponencial con jitter: 0.5s, 1s, 2s… hasta 10s
    const base = Math.min(500 * 2 ** (attempts - 1), 10_000);
    const delay = base / 2 + Math.random() * (base / 2);
    this.set({ attempts });
    this.retryTimer = setTimeout(() => this.connect(), delay);
  }
}

function describeBotStale(s: Snapshot): string {
  const h = s.health;
  if (!h.feed_connected) return "El bot perdió la conexión con el mercado";
  if (h.queue_backlog) return "El bot está atrasado procesando datos";
  if (h.stale_sources.length) return `Fuente de datos caída: ${h.stale_sources.join(", ")}`;
  if (h.last_data_age_s === null) return "El bot todavía no recibió datos de mercado";
  return `Último dato de mercado hace ${h.last_data_age_s.toFixed(0)} s`;
}

export function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}

export function useLive(conn: LiveConnection): LiveState {
  return useSyncExternalStore(conn.subscribe, conn.getState, conn.getState);
}
