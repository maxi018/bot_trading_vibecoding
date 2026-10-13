import type { LiveState } from "../api/live";
import type { OpenOrder, Position } from "../api/types";
import { clock, dirClass, duration, money, num, pct } from "../lib/format";

/** Distancia al stop bajo la cual se avisa (el precio está "encima" del stop). */
export const NEAR_STOP = 0.005;

const ORDER_TYPE: Record<string, string> = { market: "Mercado", limit: "Límite", stop_market: "Stop" };
const ORDER_TAG: Record<string, string> = {
  entry: "Entrada", exit: "Salida", reduce: "Reducción", stop: "Stop protectivo", flatten: "Cierre (kill)",
};

export function PositionsPage({ live }: { live: LiveState }) {
  const snap = live.snapshot;
  if (!snap) return <div className="placeholder">Esperando el primer snapshot del bot…</div>;
  const equity = snap.overview.equity ?? 0;
  const positions = snap.positions;
  const orders = [...snap.orders].sort((a, b) => b.created_ts - a.created_ts);
  const gross = positions.reduce((s, p) => s + (p.notional ?? 0), 0);
  const upnl = positions.reduce((s, p) => s + (p.unrealized_pnl ?? 0), 0);
  const risk = positions.reduce((s, p) => s + (p.risk_to_stop ?? 0), 0);
  const unprotected = positions.filter((p) => !p.protected).length;

  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="grid kpis">
        <Kpi label="Posiciones abiertas" value={String(positions.length)} />
        <Kpi label="Exposición bruta" value={money(gross)} sub={equity ? `${pct(gross / equity)} del equity` : undefined} />
        <Kpi label="PnL no realizado" value={money(upnl, { signed: true })} cls={dirClass(upnl)} />
        <Kpi label="Riesgo hasta los stops" value={money(risk)}
          sub={equity ? `${pct(risk / equity)} del equity` : undefined} />
        <Kpi label="Sin stop protectivo" value={String(unprotected)} cls={unprotected ? "down" : ""}
          sub={unprotected ? "⚠ revisar ya" : "✓ todas protegidas"} />
      </div>

      <section className="card" aria-labelledby="pos-title">
        <h2 className="card-title" id="pos-title">Posiciones <span className="stale-chip">congelado</span></h2>
        {positions.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>Sin posiciones abiertas.</p>
        ) : (
          <table className="rtable live-value">
            <thead>
              <tr>
                <th>Símbolo</th><th>Lado</th><th className="r">Tamaño</th><th className="r">Entrada</th>
                <th className="r">Precio</th><th className="r">Notional</th><th className="r">PnL no realizado</th>
                <th className="r">Stop</th><th className="r">Distancia al stop</th><th className="r">Riesgo al stop</th>
                <th className="r">Abierta</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => <PositionRow key={p.symbol} p={p} equity={equity} now={snap.overview.sim_time} />)}
            </tbody>
          </table>
        )}
      </section>

      <section className="card" aria-labelledby="ord-title">
        <h2 className="card-title" id="ord-title">
          Órdenes abiertas <span className="muted" style={{ textTransform: "none", letterSpacing: 0 }}>
            (las crea solo el bot, a través del risk engine)</span>
          <span className="stale-chip">congelado</span>
        </h2>
        {orders.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>Sin órdenes abiertas.</p>
        ) : (
          <table className="rtable live-value">
            <thead>
              <tr>
                <th>Creada</th><th>Símbolo</th><th>Lado</th><th>Tipo</th><th>Propósito</th>
                <th className="r">Cantidad</th><th className="r">Ejecutada</th><th className="r">Precio</th>
                <th>Estado</th>
              </tr>
            </thead>
            <tbody>{orders.map((o) => <OrderRow key={o.client_order_id} o={o} />)}</tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function Kpi({ label, value, sub, cls }: { label: string; value: string; sub?: string; cls?: string }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value live-value ${cls ?? ""}`}>{value}</div>
      {sub && <div className="stat-delta secondary live-value">{sub}</div>}
    </div>
  );
}

export function PositionRow({ p, equity, now }: { p: Position; equity: number; now: number }) {
  const near = p.distance_to_stop_pct !== null && p.distance_to_stop_pct < NEAR_STOP;
  return (
    <tr className={!p.protected ? "row-alert" : undefined}>
      <td data-label="Símbolo" className="mono strong">{p.symbol}</td>
      <td data-label="Lado"><span className={`side side-${p.side}`}>{p.side === "long" ? "▲ LONG" : "▼ SHORT"}</span></td>
      <td data-label="Tamaño" className="r num">{num(p.qty, 5)}</td>
      <td data-label="Entrada" className="r num">{num(p.avg_entry)}</td>
      <td data-label="Precio" className="r num">{num(p.mark)}</td>
      <td data-label="Notional" className="r num">{money(p.notional)}</td>
      <td data-label="PnL no realizado" className={`r num ${dirClass(p.unrealized_pnl)}`}>
        {money(p.unrealized_pnl, { signed: true })} <small>({pct(p.unrealized_pct, { signed: true })})</small>
      </td>
      <td data-label="Stop" className="r num">
        {p.protected ? num(p.stop) : <strong className="down">⚠ SIN STOP</strong>}
        {p.trailing_stop !== null && p.stop !== null && Math.abs(p.trailing_stop - p.stop) > 1e-9 && (
          <div className="muted" style={{ fontSize: 11 }}>trailing → {num(p.trailing_stop)}</div>
        )}
      </td>
      <td data-label="Distancia al stop" className={`r num ${near ? "down" : ""}`}>
        {near && <span aria-hidden>⚠ </span>}{pct(p.distance_to_stop_pct)}
      </td>
      <td data-label="Riesgo al stop" className="r num">
        {money(p.risk_to_stop)}
        {equity > 0 && p.risk_to_stop !== null && <small className="muted"> ({pct(p.risk_to_stop / equity)})</small>}
      </td>
      <td data-label="Abierta" className="r num secondary">
        {p.entry_ts !== null ? `hace ${duration(Math.max(0, (now - p.entry_ts) / 1000))}` : "—"}
      </td>
    </tr>
  );
}

function OrderRow({ o }: { o: OpenOrder }) {
  const price = o.type === "stop_market" ? o.stop_price : o.price;
  return (
    <tr>
      <td data-label="Creada" className="num secondary">{clock(o.created_ts)}</td>
      <td data-label="Símbolo" className="mono strong">{o.symbol}</td>
      <td data-label="Lado"><span className={`side side-${o.side === "buy" ? "long" : "short"}`}>{o.side === "buy" ? "COMPRA" : "VENTA"}</span></td>
      <td data-label="Tipo">{ORDER_TYPE[o.type] ?? o.type}{o.reduce_only && <span className="pill">reduce-only</span>}</td>
      <td data-label="Propósito">{ORDER_TAG[o.tag] ?? o.tag}</td>
      <td data-label="Cantidad" className="r num">{num(o.qty, 5)}</td>
      <td data-label="Ejecutada" className="r num">{num(o.filled_qty, 5)}</td>
      <td data-label="Precio" className="r num">{num(price)}</td>
      <td data-label="Estado" className="mono">{o.status}</td>
    </tr>
  );
}
