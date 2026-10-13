import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, auth, commands } from "../api/client";
import type { LiveConnection, LiveState } from "../api/live";
import type { BotEvent, Breaker } from "../api/types";
import { StatusPill } from "../components/Badges";
import { Dialog } from "../components/Dialog";
import { useToast } from "../components/Toasts";
import { num, pct } from "../lib/format";
import { classify, fromDisplay, GROUPS, META, toDisplay, UNIT_SUFFIX, type ChangeKind } from "../lib/riskParams";

interface Limit { name: string; value: number; editable: boolean; conservative_direction: "lower" | "higher" | null }
interface RiskView {
  status: "RUNNING" | "PAUSED" | "HALTED" | "KILLED";
  paused: boolean;
  killed: boolean;
  halt_reasons: string[];
  size_multiplier: number;
  veto_count: number;
  breakers: Breaker[];
  limits: Limit[];
}

const BREAKER_FMT: Record<string, (v: number | null) => string> = {
  total_drawdown: (v) => pct(v), daily_drawdown: (v) => pct(v), reject_rate: (v) => pct(v, { digits: 0 }),
  slippage: (v) => `${num(v, 1)} bps`, data_age: (v) => `${num(v, 1)} s`,
  losing_streak: (v) => num(v, 0), desync: (v) => num(v, 0),
};

export function RiskPage({ live, conn }: { live: LiveState; conn: LiveConnection }) {
  const [risk, setRisk] = useState<RiskView | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    api.get<RiskView>("/api/risk").then((r) => { setRisk(r); setError(""); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "No se pudo cargar el risk engine"));
  }, []);
  useEffect(() => { load(); }, [load]);
  // El estado (pausa/kill) cambia por comandos: se recarga cuando cambia en el snapshot.
  const status = live.snapshot?.overview.status;
  useEffect(() => { if (status) load(); }, [status, load]);

  if (error) return <div className="card error-text" role="alert">{error}</div>;
  if (!risk) return <div className="placeholder">Cargando risk engine…</div>;
  const breakers = live.snapshot?.breakers ?? risk.breakers;
  const ov = live.snapshot?.overview;

  return (
    <div className="grid" style={{ gap: 16 }}>
      <section className="card risk-head">
        <div style={{ fontSize: 22 }}><StatusPill status={ov?.status ?? risk.status} /></div>
        <div className="secondary">
          Tamaño ×{num(ov?.size_multiplier ?? risk.size_multiplier, 1)} · {risk.veto_count} vetos desde el arranque
        </div>
        {(ov?.halt_reasons ?? risk.halt_reasons).length > 0 && (
          <ul className="mono" style={{ margin: 0, paddingLeft: 18, fontSize: 12, flexBasis: "100%" }}>
            {(ov?.halt_reasons ?? risk.halt_reasons).map((r) => <li key={r}>{r}</li>)}
          </ul>
        )}
      </section>

      <section aria-labelledby="br-title">
        <h2 className="card-title" id="br-title" style={{ margin: "0 0 8px" }}>
          Circuit breakers <span className="stale-chip">congelado</span>
        </h2>
        <div className="grid breakers live-value">{breakers.map((b) => <BreakerCard key={b.key} b={b} />)}</div>
      </section>

      <LimitsEditor risk={risk} mode={ov?.mode ?? null} onApplied={load} />

      <EventLog conn={conn} />
    </div>
  );
}

const STATUS_TEXT = { green: ["✓", "OK"], yellow: ["!", "Atención"], red: ["▲", "Cerca del umbral"] } as const;

export function BreakerCard({ b }: { b: Breaker }) {
  const fmt = BREAKER_FMT[b.key] ?? ((v: number | null) => num(v));
  const [icon, text] = b.tripped ? ["▲", "DISPARADO"] : STATUS_TEXT[b.status];
  const level = Math.max(0, Math.min(1, b.level ?? 0));
  return (
    <div className={`card breaker sev-${b.tripped ? "red" : b.status}`}>
      <div className="breaker-head">
        <span className="stat-label">{b.label}</span>
        <span className="breaker-status"><span aria-hidden>{icon}</span> {text}</span>
      </div>
      <div className="breaker-value">
        <span className="stat-value">{fmt(b.value)}</span>
        <span className="secondary num">umbral {fmt(b.threshold)}</span>
      </div>
      <div className="breaker-track" aria-hidden><div style={{ width: `${level * 100}%` }} /></div>
      <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>Al dispararse: {b.action}</div>
    </div>
  );
}

// ------------------------------------------------------------ editor de límites
export function LimitsEditor({ risk, mode, onApplied }: { risk: RiskView; mode: string | null; onApplied: () => void }) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState(false);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const byName = useMemo(() => Object.fromEntries(risk.limits.map((l) => [l.name, l])), [risk.limits]);

  const changes = useMemo(() => {
    const out: { name: string; old: number; new: number | null; kind: ChangeKind | "invalid" }[] = [];
    for (const [name, text] of Object.entries(drafts)) {
      const l = byName[name];
      if (!l) continue;
      const v = fromDisplay(name, text);
      if (v === null) out.push({ name, old: l.value, new: null, kind: "invalid" });
      else {
        const kind = classify(l.conservative_direction, l.value, v);
        if (kind !== "same") out.push({ name, old: l.value, new: v, kind });
      }
    }
    return out;
  }, [drafts, byName]);
  const invalid = changes.some((c) => c.kind === "invalid");
  const relaxing = changes.filter((c) => c.kind === "relaxes");
  const blockedByPause = relaxing.length > 0 && !risk.paused;

  async function apply() {
    setBusy(true);
    try {
      const payload = Object.fromEntries(changes.map((c) => [c.name, c.new]));
      await api.patch("/api/risk/params", {
        operator: auth.operator || "dashboard", changes: payload, confirm_relax: relaxing.length > 0,
      });
      toast("ok", `${changes.length} parámetro(s) aplicado(s)`);
      setDrafts({}); setConfirm(false); setAck(false);
      onApplied();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "No se pudo aplicar");
    } finally {
      setBusy(false);
    }
  }

  const fmtVal = (name: string, v: number) =>
    `${num(toDisplay(name, v), META[name]?.int ? 0 : 2)} ${UNIT_SUFFIX[META[name]?.unit ?? "n"]}`.trim();

  return (
    <section className="card" aria-labelledby="lim-title">
      <h2 className="card-title" id="lim-title">Límites configurados</h2>
      <p className="secondary" style={{ marginTop: 0, fontSize: 13 }}>
        En caliente solo se puede <strong>endurecer</strong>. Relajar un límite exige el bot <strong>PAUSADO</strong> y
        confirmación. Los techos duros del archivo de config siguen mandando y todo cambio queda auditado.
      </p>
      {GROUPS.map((g) => {
        const rows = risk.limits.filter((l) => (META[l.name]?.group ?? "Ejecución") === g);
        if (!rows.length) return null;
        return (
          <div key={g} className="limit-group">
            <h3>{g}</h3>
            <table className="rtable limits">
              <thead><tr><th>Parámetro</th><th className="r">Actual</th><th>Nuevo valor</th><th>Más conservador</th><th>Cambio</th></tr></thead>
              <tbody>
                {rows.map((l) => {
                  const m = META[l.name];
                  const c = changes.find((x) => x.name === l.name);
                  return (
                    <tr key={l.name}>
                      <td data-label="Parámetro">{m?.label ?? l.name}<div className="mono muted" style={{ fontSize: 11 }}>{l.name}</div></td>
                      <td data-label="Actual" className="r num">{fmtVal(l.name, l.value)}</td>
                      <td data-label="Nuevo valor">
                        {l.editable ? (
                          <span className="limit-input">
                            <input className="input num" inputMode="decimal" aria-label={`Nuevo valor de ${m?.label ?? l.name}`}
                              value={drafts[l.name] ?? ""} placeholder={String(toDisplay(l.name, l.value))}
                              onChange={(e) => setDrafts((d) => ({ ...d, [l.name]: e.target.value }))} />
                            <span className="muted">{UNIT_SUFFIX[m?.unit ?? "n"]}</span>
                          </span>
                        ) : <span className="muted" title="Requiere reiniciar con un archivo de config revisado">🔒 solo con reinicio</span>}
                      </td>
                      <td data-label="Más conservador" className="secondary">
                        {l.conservative_direction === "lower" ? "↓ bajar" : l.conservative_direction === "higher" ? "↑ subir" : "—"}
                      </td>
                      <td data-label="Cambio">
                        {c?.kind === "tightens" && <span className="chg ok">✓ endurece</span>}
                        {c?.kind === "relaxes" && <span className="chg warn">⚠ relaja</span>}
                        {c?.kind === "invalid" && <span className="chg bad">✕ inválido</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })}

      <div className="limits-footer">
        <span className="secondary">
          {changes.length === 0 ? "Sin cambios pendientes" :
            `${changes.length} cambio(s): ${changes.filter((c) => c.kind === "tightens").length} endurecen, ${relaxing.length} relajan`}
        </span>
        {blockedByPause && (
          <span className="chg warn">
            Para relajar hay que pausar el bot{" "}
            <button className="btn" onClick={() => commands.pause("relajar límites de riesgo").then(onApplied)
              .catch((e) => toast("error", e instanceof ApiError ? e.message : "Error"))}>Pausar ahora</button>
          </span>
        )}
        <button className="btn" onClick={() => setDrafts({})} disabled={!Object.keys(drafts).length}>Descartar</button>
        <button className="btn primary" disabled={!changes.length || invalid || blockedByPause} onClick={() => setConfirm(true)}>
          Revisar y aplicar
        </button>
      </div>

      {confirm && (
        <Dialog title={relaxing.length ? "⚠ Relajar límites de riesgo" : "Aplicar límites más conservadores"}
          onClose={() => { setConfirm(false); setAck(false); }} danger={relaxing.length > 0}>
          {mode === "live" && <p className="down"><strong>Modo LIVE: afecta la operación con dinero real.</strong></p>}
          <ul className="mono" style={{ fontSize: 13 }}>
            {changes.map((c) => (
              <li key={c.name}>
                {META[c.name]?.label ?? c.name}: {fmtVal(c.name, c.old)} → {c.new === null ? "?" : fmtVal(c.name, c.new)}{" "}
                {c.kind === "relaxes" ? <span className="chg warn">relaja</span> : <span className="chg ok">endurece</span>}
              </li>
            ))}
          </ul>
          {relaxing.length > 0 && (
            <label className="ack">
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} data-autofocus />
              Entiendo que estoy aumentando el riesgo que puede tomar el bot.
            </label>
          )}
          <div className="dialog-actions">
            <button className="btn" onClick={() => { setConfirm(false); setAck(false); }}>Cancelar</button>
            <button className={`btn ${relaxing.length ? "warn" : "primary"}`} disabled={busy || (relaxing.length > 0 && !ack)} onClick={apply}>
              {busy ? "Aplicando…" : "Aplicar"}
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}

// ------------------------------------------------------------ log de vetos / alertas
interface LogRow { key: string; ts: number; kind: string; severity: string; title: string; body: string }
const TABS = [
  { id: "", label: "Todo" }, { id: "veto", label: "Vetos" }, { id: "alert", label: "Alertas" }, { id: "command", label: "Auditoría" },
] as const;

function fromLive(ev: BotEvent): LogRow | null {
  const key = `live-${Math.random()}`;
  switch (ev.type) {
    case "veto": return { key, ts: ev.ts, kind: "veto", severity: "INFO", title: `VETO ${ev.symbol} ${ev.direction}`, body: ev.reason };
    case "alert": return { key, ts: ev.ts, kind: "alert", severity: ev.severity, title: ev.title, body: ev.body };
    case "command": return { key, ts: ev.ts, kind: "command", severity: "WARNING", title: `${ev.action} por ${ev.operator}`, body: typeof ev.details === "string" ? ev.details : JSON.stringify(ev.details) };
    default: return null;
  }
}

/** Los cambios de parámetros llegan como JSON {nombre: {old, new, relaxes}}: legible. */
export function formatBody(body: string): string {
  if (!body.startsWith("{")) return body;
  try {
    const obj = JSON.parse(body) as Record<string, unknown>;
    const parts = Object.entries(obj).map(([k, v]) => {
      if (v && typeof v === "object" && "old" in v && "new" in v) {
        const c = v as { old: number; new: number; relaxes?: boolean };
        const fmt = (x: number) => `${num(toDisplay(k, x), META[k]?.int ? 0 : 2)}${META[k]?.unit === "pct" ? "%" : ""}`;
        return `${META[k]?.label ?? k}: ${fmt(c.old)} → ${fmt(c.new)} (${c.relaxes ? "relaja" : "endurece"})`;
      }
      return `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`;
    });
    return parts.join(" · ");
  } catch {
    return body;
  }
}

function EventLog({ conn }: { conn: LiveConnection }) {
  const [tab, setTab] = useState<string>("");
  const [rows, setRows] = useState<LogRow[]>([]);
  useEffect(() => {
    let alive = true;
    api.get<{ events: { id: number; ts: number; kind: string; severity: string; title: string; body: string }[] }>(
      `/api/events?limit=300${tab ? `&kind=${tab}` : ""}`,
    ).then((r) => alive && setRows(r.events.map((e) => ({ ...e, key: `db-${e.id}` })))).catch(() => {});
    const off = conn.onEvent((ev) => {
      const row = fromLive(ev);
      if (row && (!tab || row.kind === tab)) setRows((rs) => [row, ...rs].slice(0, 300));
    });
    return () => { alive = false; off(); };
  }, [conn, tab]);

  const topVetos = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of rows) if (r.kind === "veto") {
      const k = r.body.split(":")[0] ?? r.body;
      counts.set(k, (counts.get(k) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6);
  }, [rows]);

  return (
    <section className="card" aria-labelledby="log-title">
      <h2 className="card-title" id="log-title">Vetos, alertas y auditoría</h2>
      <div className="seg" role="tablist" style={{ marginBottom: 12 }}>
        {TABS.map((t) => <button key={t.id} role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>{t.label}</button>)}
      </div>
      {topVetos.length > 0 && (
        <div className="veto-reasons">
          <span className="stat-label">Motivos de veto más frecuentes:</span>
          {topVetos.map(([k, n]) => <span key={k} className="pill mono">{k} · {n}</span>)}
        </div>
      )}
      {rows.length === 0 ? <p className="muted" style={{ margin: 0 }}>Sin eventos.</p> : (
        <ul className="events log">
          {rows.slice(0, 150).map((r) => (
            <li key={r.key}>
              <time>{new Date(r.ts).toLocaleString("es-AR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}</time>
              <span className={`tag tag-${r.kind}`}>{r.kind === "veto" ? "VETO" : r.kind === "command" ? "CMD" : r.severity}</span>
              <span>{r.title}{r.body && <span className="mono secondary log-body"> {formatBody(r.body)}</span>}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
