import { useCallback, useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, ApiError } from "../api/client";
import type { LiveConnection } from "../api/live";
import type { Trade } from "../api/types";
import { dirClass, duration, money, num, pct } from "../lib/format";
import { summarize, toCsv } from "../lib/trades";

const REASON: Record<string, string> = { stop: "Stop", exit: "Señal", flatten: "Kill", reduce: "Reducción" };
const BREAKER_MULT = 3; // risk.slippage_anomaly_multiplier

interface Filters { symbol: string; side: string; result: string; from: string; to: string }
const EMPTY: Filters = { symbol: "", side: "", result: "", from: "", to: "" };

function dateTime(ms: number): string {
  return new Date(ms).toLocaleString("es-AR", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function query(f: Filters): string {
  const q = new URLSearchParams({ limit: "1000" });
  if (f.symbol) q.set("symbol", f.symbol);
  if (f.side) q.set("side", f.side);
  if (f.result) q.set("result", f.result);
  if (f.from) q.set("since", String(new Date(`${f.from}T00:00:00`).getTime()));
  if (f.to) q.set("until", String(new Date(`${f.to}T23:59:59.999`).getTime()));
  return q.toString();
}

export function HistoryPage({ conn, symbols }: { conn: LiveConnection; symbols: string[] }) {
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [trades, setTrades] = useState<Trade[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api.get<{ trades: Trade[] }>(`/api/trades?${query(filters)}`)
      .then((r) => { setTrades(r.trades); setError(""); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "No se pudo cargar el historial"));
  }, [filters]);

  useEffect(() => { load(); }, [load]);

  // Un fill de salida cierra un trade: recargar (con un pequeño debounce).
  useEffect(() => {
    let t: ReturnType<typeof setTimeout> | null = null;
    const off = conn.onEvent((ev) => {
      if (ev.type === "fill" && ev.tag !== "entry") {
        if (t) clearTimeout(t);
        t = setTimeout(load, 600);
      }
    });
    return () => { off(); if (t) clearTimeout(t); };
  }, [conn, load]);

  const s = useMemo(() => summarize(trades ?? []), [trades]);
  const set = (k: keyof Filters) => (e: { target: { value: string } }) => setFilters((f) => ({ ...f, [k]: e.target.value }));

  function exportCsv() {
    if (!trades) return;
    const blob = new Blob([toCsv(trades)], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `quantbot-trades-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  return (
    <div className="grid" style={{ gap: 16 }}>
      <form className="card filters" onSubmit={(e) => e.preventDefault()} aria-label="Filtros">
        <label>Símbolo
          <select className="input" value={filters.symbol} onChange={set("symbol")}>
            <option value="">Todos</option>
            {symbols.map((x) => <option key={x} value={x}>{x}</option>)}
          </select>
        </label>
        <label>Lado
          <select className="input" value={filters.side} onChange={set("side")}>
            <option value="">Todos</option><option value="long">Long</option><option value="short">Short</option>
          </select>
        </label>
        <label>Resultado
          <select className="input" value={filters.result} onChange={set("result")}>
            <option value="">Todos</option><option value="win">Ganadores</option><option value="loss">Perdedores</option>
          </select>
        </label>
        <label>Desde<input className="input" type="date" value={filters.from} onChange={set("from")} /></label>
        <label>Hasta<input className="input" type="date" value={filters.to} onChange={set("to")} /></label>
        <div className="filters-actions">
          <button type="button" className="btn" onClick={() => setFilters(EMPTY)}>Limpiar</button>
          <button type="button" className="btn" onClick={exportCsv} disabled={!trades?.length}>Exportar CSV</button>
        </div>
      </form>

      {error && <div className="card error-text" role="alert">{error}</div>}

      <div className="grid kpis">
        <Kpi label="Trades" value={String(s.n)} sub={s.n < 100 && s.n > 0 ? "< 100: muestra anecdótica" : undefined} />
        <Kpi label="Win rate" value={pct(s.winRate, { digits: 1 })} sub={`${s.wins} ganadores`} />
        <Kpi label="PnL neto" value={money(s.netPnl, { signed: true })} cls={dirClass(s.netPnl)} sub={`fees ${money(s.fees)}`} />
        <Kpi label="Ganancia / pérdida promedio" value={`${money(s.avgWin)} / ${money(s.avgLoss)}`} />
        <Kpi label="Slippage real vs esperado" value={s.avgSlippage === null ? "—" : `${num(s.avgSlippage, 1)} bps`}
          sub={s.expectedSlippage === null ? undefined : `esperado ${num(s.expectedSlippage, 1)} bps · ${s.slippageRatio === null ? "—" : `${num(s.slippageRatio, 1)}×`}`}
          cls={s.slippageRatio !== null && s.slippageRatio >= BREAKER_MULT ? "down" : ""} />
      </div>

      <section className="card" aria-labelledby="slip-title">
        <h2 className="card-title" id="slip-title">Slippage real por trade (bps, promedio entrada + salida)</h2>
        <SlippageChart trades={trades ?? []} />
      </section>

      <section className="card" aria-labelledby="hist-title">
        <h2 className="card-title" id="hist-title">Trades cerrados {trades && <span className="muted">({trades.length})</span>}</h2>
        {trades === null ? <p className="muted">Cargando…</p> : trades.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>No hay trades cerrados con estos filtros.</p>
        ) : (
          <table className="rtable">
            <thead>
              <tr>
                <th>Cierre</th><th>Símbolo</th><th>Lado</th><th className="r">Cantidad</th><th className="r">Entrada → Salida</th>
                <th className="r">Duración</th><th className="r">PnL neto</th><th className="r">Fees</th><th>Salida por</th>
                <th className="r">Slippage real</th><th className="r">vs esperado</th>
              </tr>
            </thead>
            <tbody>{trades.map((t) => <TradeRow key={t.id} t={t} />)}</tbody>
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
      <div className={`stat-value ${cls ?? ""}`}>{value}</div>
      {sub && <div className="stat-delta secondary">{sub}</div>}
    </div>
  );
}

export function TradeRow({ t }: { t: Trade }) {
  const diff = t.slippage_bps === null ? null : t.slippage_bps - t.expected_slippage_bps;
  return (
    <tr>
      <td data-label="Cierre" className="num secondary">{dateTime(t.exit_ts)}</td>
      <td data-label="Símbolo" className="mono strong">{t.symbol}</td>
      <td data-label="Lado"><span className={`side side-${t.side}`}>{t.side === "long" ? "▲ LONG" : "▼ SHORT"}</span></td>
      <td data-label="Cantidad" className="r num">{num(t.qty, 5)}</td>
      <td data-label="Entrada → Salida" className="r num">{num(t.entry_price)} → {num(t.exit_price)}</td>
      <td data-label="Duración" className="r num">{duration(Math.max(0, (t.exit_ts - t.entry_ts) / 1000))}</td>
      <td data-label="PnL neto" className={`r num ${dirClass(t.pnl)}`}>
        {money(t.pnl, { signed: true })} <small>({pct(t.return_pct, { signed: true })})</small>
      </td>
      <td data-label="Fees" className="r num secondary">{money(t.fees)}</td>
      <td data-label="Salida por">{REASON[t.exit_reason] ?? t.exit_reason}</td>
      <td data-label="Slippage real" className="r num">{t.slippage_bps === null ? "—" : `${num(t.slippage_bps, 1)} bps`}</td>
      <td data-label="vs esperado" className={`r num ${diff === null ? "" : diff > 0 ? "down" : "up"}`}>
        {diff === null ? "—" : `${diff > 0 ? "+" : diff < 0 ? "−" : ""}${num(Math.abs(diff), 1)} bps`}
      </td>
    </tr>
  );
}

function SlippageChart({ trades }: { trades: Trade[] }) {
  const data = useMemo(
    () => [...trades].filter((t) => t.slippage_bps !== null).sort((a, b) => a.exit_ts - b.exit_ts).slice(-120)
      .map((t, i) => ({ i: i + 1, slip: t.slippage_bps as number, symbol: t.symbol, ts: t.exit_ts, pnl: t.pnl })),
    [trades],
  );
  if (!data.length) return <p className="muted" style={{ margin: 0 }}>Sin trades con slippage medido.</p>;
  const expected = trades[0]?.expected_slippage_bps ?? 5;
  const breaker = expected * BREAKER_MULT;
  const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const bar = css("--accent");
  const over = css("--warning");
  return (
    <>
      <div style={{ width: "100%", height: 240 }}>
        <ResponsiveContainer>
          <BarChart data={data} margin={{ top: 16, right: 12, bottom: 0, left: 0 }} barCategoryGap={2}>
            <CartesianGrid vertical={false} stroke={css("--border")} />
            <XAxis dataKey="i" tick={{ fill: css("--text-muted"), fontSize: 11 }} tickLine={false} axisLine={{ stroke: css("--border") }} />
            <YAxis tick={{ fill: css("--text-muted"), fontSize: 11 }} tickLine={false} axisLine={false} width={40} unit="" />
            <Tooltip cursor={{ fill: css("--surface-3") }} content={<SlipTooltip expected={expected} />} />
            <ReferenceLine y={0} stroke={css("--border")} />
            <ReferenceLine y={expected} stroke={css("--text-secondary")} strokeDasharray="4 4"
              label={{ value: `esperado ${expected} bps`, position: "insideTopRight", fill: css("--text-secondary"), fontSize: 11 }} />
            <ReferenceLine y={breaker} stroke={css("--critical")} strokeDasharray="6 3"
              label={{ value: `umbral breaker ${breaker} bps (promedio)`, position: "insideTopRight", fill: css("--down"), fontSize: 11 }} />
            <Bar dataKey="slip" radius={[3, 3, 0, 0]} isAnimationActive={false}>
              {data.map((d) => <Cell key={d.i} fill={d.slip > breaker ? over : bar} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="muted" style={{ fontSize: 12, margin: "8px 0 0" }}>
        Últimos {data.length} trades en orden de cierre. En <span style={{ color: over }}>■ ámbar</span>, trades por encima
        del umbral; el circuit breaker salta cuando el <strong>promedio</strong> de los últimos 20 fills lo supera.
      </p>
    </>
  );
}

interface TipProps { active?: boolean; payload?: { payload: { slip: number; symbol: string; ts: number; pnl: number } }[]; expected: number }
function SlipTooltip({ active, payload, expected }: TipProps) {
  const p = active && payload?.[0]?.payload;
  if (!p) return null;
  return (
    <div className="chart-tip">
      <div className="mono strong">{p.symbol}</div>
      <div className="secondary">{dateTime(p.ts)}</div>
      <div>Slippage <strong>{num(p.slip, 1)} bps</strong> <span className="muted">(esperado {expected})</span></div>
      <div className={dirClass(p.pnl)}>PnL {money(p.pnl, { signed: true })}</div>
    </div>
  );
}
