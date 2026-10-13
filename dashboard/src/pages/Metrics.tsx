import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Area, AreaChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api, ApiError, auth } from "../api/client";
import type { LiveConnection } from "../api/live";
import { Meter } from "../components/Meter";
import { useToast } from "../components/Toasts";
import { alignCurves, guide, type Point } from "../lib/metrics";
import { duration, money, num, pct } from "../lib/format";

type Report = Record<string, number | null | string[]>;
interface Reference {
  job_id: string;
  created_at: number | null;
  n_bars: number | null;
  report: Report;
  equity: Point[];
  montecarlo: { dd_p95: number; dd_p50: number; n_sims: number } | null;
}
interface MetricsResponse {
  report: Report;
  equity: Point[];
  reference: Reference | null;
  timeframe_ms: number;
  drawdown_kill: number;
}

const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const n = (r: Report | undefined, k: string): number | null => {
  const v = r?.[k];
  return typeof v === "number" ? v : null;
};

export function MetricsPage({ conn, theme }: { conn: LiveConnection; theme: string }) {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    api.get<MetricsResponse>("/api/metrics")
      .then((d) => { setData(d); setError(""); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "No se pudieron cargar las métricas"));
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 15_000);
    let deb: ReturnType<typeof setTimeout> | null = null;
    const off = conn.onEvent((ev) => {
      if (ev.type === "fill" && ev.tag !== "entry") { if (deb) clearTimeout(deb); deb = setTimeout(load, 800); }
    });
    return () => { clearInterval(t); off(); if (deb) clearTimeout(deb); };
  }, [conn, load]);

  if (error) return <div className="card error-text" role="alert">{error}</div>;
  if (!data) return <div className="placeholder">Cargando métricas…</div>;

  const live = data.report;
  const ref = data.reference;
  const nTrades = n(live, "n_trades") ?? 0;
  const tfH = data.timeframe_ms / 3_600_000;
  const recovery = (r: Report | undefined) => {
    const p = n(r, "max_dd_duration_periods");
    return p === null ? null : p * tfH * 3600;
  };

  const tiles: { key: string; label: string; fmt: (v: number | null) => string; live: number | null; bt: number | null }[] = [
    { key: "sharpe", label: "Sharpe", fmt: (v) => num(v), live: n(live, "sharpe"), bt: n(ref?.report, "sharpe") },
    { key: "sortino", label: "Sortino", fmt: (v) => num(v), live: n(live, "sortino"), bt: n(ref?.report, "sortino") },
    { key: "max_drawdown", label: "Max drawdown", fmt: (v) => pct(v), live: n(live, "max_drawdown"), bt: n(ref?.report, "max_drawdown") },
    { key: "calmar", label: "Calmar", fmt: (v) => num(v), live: n(live, "calmar"), bt: n(ref?.report, "calmar") },
    { key: "profit_factor", label: "Profit factor", fmt: (v) => num(v), live: n(live, "profit_factor"), bt: n(ref?.report, "profit_factor") },
    { key: "expectancy", label: "Expectancy por trade", fmt: (v) => money(v, { signed: true }), live: n(live, "expectancy"), bt: n(ref?.report, "expectancy") },
    { key: "win_rate", label: "Win rate", fmt: (v) => pct(v, { digits: 1 }), live: n(live, "win_rate"), bt: n(ref?.report, "win_rate") },
    { key: "n_trades", label: "Trades", fmt: (v) => (v === null ? "—" : String(v)), live: n(live, "n_trades"), bt: n(ref?.report, "n_trades") },
    { key: "recovery", label: "Mayor tiempo bajo el agua", fmt: (v) => (v === null ? "—" : duration(v)), live: recovery(live), bt: recovery(ref?.report) },
    { key: "deflated_sharpe", label: "Deflated Sharpe", fmt: (v) => pct(v, { digits: 0 }), live: n(live, "deflated_sharpe"), bt: n(ref?.report, "deflated_sharpe") },
  ];

  const liveMaxDd = n(live, "max_drawdown");
  const shutdownDd = ref?.montecarlo ? 1.5 * ref.montecarlo.dd_p95 : null;

  return (
    <div className="grid" style={{ gap: 16 }}>
      <ReferenceCard reference={ref} onChange={load} />

      <div className="grid metric-tiles">
        {tiles.map((t) => {
          const g = guide(t.key, t.live, nTrades);
          return (
            <div className="card" key={t.key}>
              <div className="stat-label">{t.label}</div>
              <div className="stat-value">{t.fmt(t.live)}</div>
              <div className="stat-delta secondary">
                backtest {ref ? <span className="num">{t.fmt(t.bt)}</span> : <span className="muted">sin referencia</span>}
              </div>
              {g.warn ? (
                <div className="metric-note warn"><span aria-hidden>⚠ </span>{g.warn}</div>
              ) : (
                <div className="metric-note">{g.note}</div>
              )}
            </div>
          );
        })}
      </div>

      <section className="card" aria-labelledby="eqc-title">
        <h2 className="card-title" id="eqc-title">Equity live vs. backtest de referencia (retorno acumulado)</h2>
        <EquityCompare data={data} theme={theme} />
      </section>

      <div className="grid two-col">
        <section className="card" aria-labelledby="dd-title">
          <h2 className="card-title" id="dd-title">Drawdown live</h2>
          <DrawdownChart data={data} shutdownDd={shutdownDd} theme={theme} />
        </section>
        <section className="card" aria-labelledby="sd-title">
          <h2 className="card-title" id="sd-title">Criterio de apagado (definido ANTES de prender)</h2>
          {shutdownDd !== null ? (
            <>
              <Meter label="Max DD live vs. 1.5 × DD p95 de Montecarlo" value={liveMaxDd} limit={shutdownDd}
                format={(v) => pct(v)} hint="al límite: la estrategia se considera rota" />
              <p className="secondary" style={{ fontSize: 13 }}>
                El max DD del backtest es una sola realización. Montecarlo ({ref?.montecarlo?.n_sims} simulaciones) dice que el
                95% de los futuros posibles tiene un DD ≤ {pct(ref?.montecarlo?.dd_p95)}. Si en vivo se supera 1.5× eso, lo
                que se ve no es mala suerte: el edge se rompió. Kill-switch duro del risk engine: {pct(data.drawdown_kill, { digits: 0 })}.
              </p>
            </>
          ) : (
            <p className="secondary">Fijá un backtest de referencia (con Montecarlo) para calcular el criterio de apagado.</p>
          )}
        </section>
      </div>

      {Array.isArray(live.warnings) && live.warnings.length > 0 && (
        <section className="card" aria-labelledby="warn-title">
          <h2 className="card-title" id="warn-title">Banderas rojas del reporte live</h2>
          <ul className="secondary" style={{ margin: 0, paddingLeft: 18 }}>
            {live.warnings.map((w) => <li key={w}><span aria-hidden>⚠ </span>{w}</li>)}
          </ul>
        </section>
      )}
    </div>
  );
}

function ReferenceCard({ reference, onChange }: { reference: Reference | null; onChange: () => void }) {
  const [running, setRunning] = useState<string | null>(null);
  const toast = useToast();
  async function runReference() {
    try {
      const job = await api.post<{ id: string }>("/api/backtests", { synthetic_bars: 6000, montecarlo: true, mc_sims: 2000 });
      setRunning(job.id);
      for (;;) {
        await new Promise((r) => setTimeout(r, 1000));
        const j = await api.get<{ status: string; error: string | null }>(`/api/backtests/${job.id}`);
        if (j.status === "error") throw new ApiError(500, j.error ?? "El backtest falló");
        if (j.status === "done") break;
      }
      await api.put("/api/metrics/reference", { operator: auth.operator || "dashboard", job_id: job.id });
      toast("ok", `Backtest ${job.id} fijado como referencia`);
      onChange();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "No se pudo generar la referencia");
    } finally {
      setRunning(null);
    }
  }
  return (
    <section className="card ref-card">
      <div>
        <div className="stat-label">Backtest de referencia</div>
        {reference ? (
          <div>
            <span className="mono">{reference.job_id}</span> · {reference.n_bars} barras
            {reference.created_at && <> · {new Date(reference.created_at * 1000).toLocaleString("es-AR")}</>}
            <div className="muted" style={{ fontSize: 12 }}>
              Las métricas live se comparan contra este backtest con la misma configuración.
            </div>
          </div>
        ) : (
          <div className="secondary">Sin referencia: no hay contra qué comparar el desempeño live.</div>
        )}
      </div>
      <button className="btn" disabled={running !== null} onClick={runReference}>
        {running ? `Corriendo ${running}…` : reference ? "Regenerar con la config actual" : "Correr backtest de referencia"}
      </button>
      <p className="muted" style={{ fontSize: 12, margin: 0, flexBasis: "100%" }}>
        Este botón usa datos sintéticos. Para una referencia real, corré el backtest con datos históricos en la pantalla
        Backtests y fijalo desde ahí.
      </p>
    </section>
  );
}

function EquityCompare({ data, theme }: { data: MetricsResponse; theme: string }) {
  const points = useMemo(
    () => alignCurves(data.equity, data.reference?.equity ?? null, data.timeframe_ms),
    [data],
  );
  // "theme" en las deps: al cambiar de tema se releen los colores
  const colors = useMemo(() => ({
    live: theme === "light" ? "#2a78d6" : "#3987e5", // categórico 1
    bt: theme === "light" ? "#eb6834" : "#d95926", // categórico 2
    grid: css("--border"), text: css("--text-muted"),
  }), [theme]);
  if (points.length < 2) return <p className="muted" style={{ margin: 0 }}>Todavía no hay suficiente historia live.</p>;
  const tfH = data.timeframe_ms / 3_600_000;
  return (
    <div style={{ width: "100%", height: 300 }}>
      <ResponsiveContainer>
        <LineChart data={points} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke={colors.grid} />
          <XAxis dataKey="x" type="number" domain={["dataMin", "dataMax"]} tick={{ fill: colors.text, fontSize: 11 }}
            tickLine={false} axisLine={{ stroke: colors.grid }} tickFormatter={(x: number) => `${Math.round((x * tfH) / 24)} d`} />
          <YAxis tick={{ fill: colors.text, fontSize: 11 }} tickLine={false} axisLine={false} width={52}
            tickFormatter={(v: number) => pct(v, { digits: 1 })} />
          <ReferenceLine y={0} stroke={colors.grid} />
          <Tooltip content={<CompareTip tfH={tfH} />} />
          <Legend verticalAlign="top" height={28} iconType="plainline" wrapperStyle={{ fontSize: 12 }} />
          <Line name="Live" dataKey="live" stroke={colors.live} strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line name="Backtest (mismo horizonte)" dataKey="bt" stroke={colors.bt} strokeWidth={2} strokeDasharray="6 4"
            dot={false} isAnimationActive={false} connectNulls={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

interface CompareTipProps { active?: boolean; payload?: { payload: { x: number; live: number; bt: number | null } }[]; tfH: number }
function CompareTip({ active, payload, tfH }: CompareTipProps) {
  const p = active && payload?.[0]?.payload;
  if (!p) return null;
  return (
    <div className="chart-tip">
      <div className="secondary">{duration(p.x * tfH * 3600)} desde el inicio</div>
      <div>Live <strong>{pct(p.live, { signed: true })}</strong></div>
      <div>Backtest <strong>{p.bt === null ? "—" : pct(p.bt, { signed: true })}</strong></div>
      {p.bt !== null && <div className="muted">diferencia {pct(p.live - p.bt, { signed: true })}</div>}
    </div>
  );
}

function DrawdownChart({ data, shutdownDd, theme }: { data: MetricsResponse; shutdownDd: number | null; theme: string }) {
  const points = useMemo(() => alignCurves(data.equity, null, data.timeframe_ms).map((p) => ({ x: p.x, dd: -p.dd })), [data]);
  const c = useMemo(() => ({ dd: css("--down"), grid: css("--border"), text: css("--text-muted"), crit: css("--critical"), warn: css("--warning") }), [theme]);
  if (points.length < 2) return <p className="muted" style={{ margin: 0 }}>Todavía no hay suficiente historia live.</p>;
  const tfH = data.timeframe_ms / 3_600_000;
  const floor = Math.min(...points.map((p) => p.dd), -(shutdownDd ?? 0), -data.drawdown_kill) * 1.1;
  return (
    <div style={{ width: "100%", height: 220 }}>
      <ResponsiveContainer>
        <AreaChart data={points} margin={{ top: 8, right: 88, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke={c.grid} />
          <XAxis dataKey="x" type="number" domain={["dataMin", "dataMax"]} tick={{ fill: c.text, fontSize: 11 }} tickLine={false}
            axisLine={{ stroke: c.grid }} tickFormatter={(x: number) => `${Math.round((x * tfH) / 24)} d`} />
          <YAxis domain={[floor, 0]} tick={{ fill: c.text, fontSize: 11 }} tickLine={false} axisLine={false} width={52}
            tickFormatter={(v: number) => pct(v, { digits: 0 })} />
          <Tooltip formatter={(v) => [pct(Number(v)), "drawdown"]} labelFormatter={(x) => `${duration(Number(x) * tfH * 3600)} desde el inicio`}
            contentStyle={{ background: css("--surface-3"), border: `1px solid ${c.grid}`, borderRadius: 8, fontSize: 12 }} />
          <Area dataKey="dd" stroke={c.dd} fill={c.dd} fillOpacity={0.25} strokeWidth={2} isAnimationActive={false} />
          {shutdownDd !== null && (
            <ReferenceLine y={-shutdownDd} stroke={c.warn} strokeDasharray="6 3"
              label={{ value: "apagado", position: "right", fill: c.warn, fontSize: 11 }} />
          )}
          <ReferenceLine y={-data.drawdown_kill} stroke={c.crit}
            label={{ value: "kill", position: "right", fill: css("--down"), fontSize: 11 }} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
