import type { LiveState } from "../api/live";
import type { BotEvent } from "../api/types";
import { StatusPill, statusHelp } from "../components/Badges";
import { Meter } from "../components/Meter";
import { age, clock, dirClass, duration, money, ms, num, pct } from "../lib/format";

function Stat({ label, value, delta, deltaClass }: { label: string; value: string; delta?: string; deltaClass?: string }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className="stat-value live-value">{value}</div>
      {delta !== undefined && <div className={`stat-delta live-value ${deltaClass ?? ""}`}>{delta}</div>}
    </div>
  );
}

function StaleChip() {
  return <span className="stale-chip">congelado</span>;
}

export function Overview({ live }: { live: LiveState }) {
  const snap = live.snapshot;
  if (!snap) {
    return <div className="placeholder">Esperando el primer snapshot del bot…</div>;
  }
  const o = snap.overview;
  const h = snap.health;
  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="grid two-col">
        <section className="card" aria-labelledby="eq-title">
          <h2 className="card-title" id="eq-title">Equity <StaleChip /></h2>
          <div className="hero-value live-value">{money(o.equity)}</div>
          <div className="live-value" style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 8 }}>
            <span className={dirClass(o.pnl_total)}>
              {money(o.pnl_total, { signed: true })} ({pct(o.pnl_total_pct, { signed: true })}) total
            </span>
            <span className={dirClass(o.pnl_day)}>
              {money(o.pnl_day, { signed: true })} ({pct(o.pnl_day_pct, { signed: true })}) hoy
            </span>
          </div>
          <div className="muted live-value" style={{ marginTop: 8, fontSize: 12 }}>
            Máximo histórico {money(o.peak_equity)} · caja {money(o.cash)}
          </div>
        </section>

        <section className="card" aria-labelledby="st-title">
          <h2 className="card-title" id="st-title">Estado del bot <StaleChip /></h2>
          <div style={{ fontSize: 26 }}><StatusPill status={o.status} /></div>
          <p className="secondary" style={{ margin: "6px 0 8px" }}>{statusHelp(o.status)}</p>
          {o.kill && (
            <p className="down" style={{ margin: "0 0 8px" }}>
              Kill: <span className="mono">{o.kill.reason}</span>
            </p>
          )}
          {o.halt_reasons.length > 0 && (
            <ul style={{ margin: "0 0 8px", paddingLeft: 18 }} className="secondary">
              {o.halt_reasons.map((r) => <li key={r} className="mono" style={{ fontSize: 12 }}>{r}</li>)}
            </ul>
          )}
          <dl className="kv live-value">
            <dt>Símbolos</dt><dd>{o.symbols.join(", ")} · {o.timeframe}</dd>
            <dt>Posiciones abiertas</dt><dd>{o.open_positions}</dd>
            <dt>Tamaño (rachas)</dt><dd>×{num(o.size_multiplier, 1)}</dd>
            <dt>Encendido hace</dt><dd>{duration(o.uptime_s)}</dd>
          </dl>
        </section>
      </div>

      <div className="grid kpis">
        <Stat label="PnL del día" value={money(o.pnl_day, { signed: true })}
          delta={pct(o.pnl_day_pct, { signed: true })} deltaClass={dirClass(o.pnl_day)} />
        <Stat label="PnL total" value={money(o.pnl_total, { signed: true })}
          delta={pct(o.pnl_total_pct, { signed: true })} deltaClass={dirClass(o.pnl_total)} />
        <Stat label="PnL no realizado" value={money(o.unrealized_pnl, { signed: true })} />
        <Stat label="Fees pagados" value={money(o.fees_paid)} />
        <Stat label="Pérdidas seguidas" value={String(o.consecutive_losses)} />
      </div>

      <div className="grid two-col">
        <section className="card" aria-labelledby="risk-title">
          <h2 className="card-title" id="risk-title">Riesgo vs. límites <StaleChip /></h2>
          <Meter label="Drawdown desde el máximo" value={o.drawdown} limit={o.drawdown_limit}
            format={(v) => pct(v)} hint="al límite: KILL + cierre" />
          <Meter label="Pérdida del día" value={o.pnl_day_pct !== null ? Math.max(0, -o.pnl_day_pct) : null}
            limit={o.daily_loss_limit} format={(v) => pct(v)} hint="al límite: HALT hasta mañana" />
          <Meter label="Apalancamiento bruto" value={o.gross_leverage} limit={o.max_gross_leverage}
            format={(v) => `${num(v, 2)}×`} />
        </section>

        <section className="card" aria-labelledby="hl-title">
          <h2 className="card-title" id="hl-title">Salud de la conexión</h2>
          <dl className="kv">
            <dt>Dashboard ↔ bot</dt>
            <dd className={live.conn === "open" ? "up" : "down"}>
              {live.conn === "open" ? "● en vivo" : `○ reconectando (intento ${live.attempts})`}
            </dd>
            <dt>Bot ↔ mercado</dt>
            <dd className={h.feed_connected ? "up" : "down"}>{h.feed_connected ? "● conectado" : "○ caído"}</dd>
            <dt>Último dato de mercado</dt>
            <dd className={h.last_data_age_s !== null && h.last_data_age_s > h.stale_threshold_s ? "down" : ""}>
              hace {age(h.last_data_age_s)}
            </dd>
            <dt>Latencia al exchange (RTT)</dt><dd>{ms(h.exchange_rtt_ms)}</dd>
            <dt>Lag del feed</dt><dd>{ms(h.feed_lag_ms)}</dd>
            <dt>Reconexiones del feed</dt><dd>{h.reconnects}</dd>
            <dt>Cola de eventos</dt><dd className={h.queue_backlog ? "down" : ""}>{h.queue_size}</dd>
            <dt>Último snapshot</dt>
            <dd>{live.lastSnapshotAt ? clock(live.lastSnapshotAt) : "—"}</dd>
          </dl>
        </section>
      </div>

      <section className="card" aria-labelledby="ev-title">
        <h2 className="card-title" id="ev-title">Actividad reciente</h2>
        {live.events.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>Sin eventos desde que abriste el dashboard.</p>
        ) : (
          <ul className="events">
            {live.events.slice(0, 12).map((r, i) => <EventRow key={`${r.at}-${i}`} e={r.ev} at={r.at} />)}
          </ul>
        )}
      </section>
    </div>
  );
}

function EventRow({ e, at }: { e: BotEvent; at: number }) {
  if (e.type === "bar") return null;
  const t = clock(at);
  switch (e.type) {
    case "fill":
      return (
        <li><time>{t}</time><span className="tag tag-fill">FILL</span>
          <span>{e.side.toUpperCase()} {num(e.qty, 4)} {e.symbol} @ {num(e.price, 2)} <span className="muted">({e.tag})</span></span></li>
      );
    case "veto":
      return (
        <li><time>{t}</time><span className="tag tag-veto">VETO</span>
          <span>{e.symbol} {e.direction}: <span className="mono secondary">{e.reason}</span></span></li>
      );
    case "alert":
      return (
        <li><time>{t}</time><span className="tag tag-alert">{e.severity}</span><span>{e.title}</span></li>
      );
    case "command":
      return (
        <li><time>{t}</time><span className="tag tag-command">CMD</span><span>{e.action} por {e.operator}</span></li>
      );
  }
}
