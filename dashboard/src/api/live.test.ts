import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveConnection, STALE_AFTER_MS } from "./live";

class FakeWS {
  static instances: FakeWS[] = [];
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) { FakeWS.instances.push(this); }
  send(s: string) { this.sent.push(s); }
  close(code = 1006) { this.onclose?.({ code }); }
  // helpers del test
  open() { this.onopen?.(); }
  msg(o: unknown) { this.onmessage?.({ data: JSON.stringify(o) }); }
}

const snapshot = (stale = false) => ({
  type: "snapshot",
  overview: { status: "RUNNING" },
  health: { stale, feed_connected: true, queue_backlog: false, stale_sources: [], last_data_age_s: 0.2 },
  positions: [], orders: [], breakers: [], stops: {},
});

describe("LiveConnection", () => {
  let now = 0;
  beforeEach(() => { vi.useFakeTimers(); now = 1_000_000; FakeWS.instances = []; });
  afterEach(() => vi.useRealTimers());

  function make() {
    const c = new LiveConnection("ws://x/ws", "TOKEN", () => now, FakeWS as unknown as typeof WebSocket);
    c.start();
    return c;
  }

  it("autentica en el primer mensaje, no en la URL", () => {
    make();
    const ws = FakeWS.instances[0]!;
    expect(ws.url).not.toContain("TOKEN");
    ws.open();
    expect(JSON.parse(ws.sent[0]!)).toEqual({ type: "auth", token: "TOKEN" });
  });

  it("está stale hasta recibir datos y vuelve a stale si dejan de llegar", () => {
    const c = make();
    const ws = FakeWS.instances[0]!;
    expect(c.state.stale).toBe(true);
    ws.open(); ws.msg({ type: "hello" }); ws.msg(snapshot());
    expect(c.state.stale).toBe(false);
    now += STALE_AFTER_MS + 100;
    vi.advanceTimersByTime(600);
    expect(c.state.stale).toBe(true);
    expect(c.state.staleReason).toMatch(/Sin actualizaciones/);
  });

  it("respeta el stale reportado por el bot aunque el socket esté vivo", () => {
    const c = make();
    const ws = FakeWS.instances[0]!;
    ws.open(); ws.msg({ type: "hello" }); ws.msg(snapshot(true));
    expect(c.state.stale).toBe(true);
  });

  it("reconecta con backoff tras un corte y marca stale mientras tanto", () => {
    const c = make();
    FakeWS.instances[0]!.open(); FakeWS.instances[0]!.msg({ type: "hello" }); FakeWS.instances[0]!.msg(snapshot());
    FakeWS.instances[0]!.close();
    expect(c.state.conn).toBe("closed");
    expect(c.state.stale).toBe(true);
    vi.advanceTimersByTime(600);
    expect(FakeWS.instances.length).toBe(2);
    FakeWS.instances[1]!.close();
    vi.advanceTimersByTime(1100);
    expect(FakeWS.instances.length).toBe(3);
  });

  it("no reintenta con un token rechazado (4401)", () => {
    const c = make();
    FakeWS.instances[0]!.close(4401);
    vi.advanceTimersByTime(30_000);
    expect(c.state.conn).toBe("unauthorized");
    expect(FakeWS.instances.length).toBe(1);
  });

  it("fuerza reconexión si el socket queda mudo (zombie)", () => {
    const c = make();
    const ws = FakeWS.instances[0]!;
    ws.open(); ws.msg({ type: "hello" }); ws.msg(snapshot());
    now += 11_000;
    vi.advanceTimersByTime(600);
    expect(c.state.conn).not.toBe("open");
    vi.advanceTimersByTime(1000);
    expect(FakeWS.instances.length).toBe(2);
  });

  it("guarda eventos con hora local, sin las velas", () => {
    const c = make();
    const ws = FakeWS.instances[0]!;
    ws.open(); ws.msg({ type: "hello" });
    ws.msg({ type: "event", event: { type: "bar", symbol: "X", bar: {} } });
    ws.msg({ type: "event", event: { type: "alert", ts: 1, severity: "INFO", title: "hola", body: "" } });
    expect(c.state.events).toHaveLength(1);
    expect(c.state.events[0]!.at).toBe(now);
  });
});
