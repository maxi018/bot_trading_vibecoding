import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api, ApiError, auth } from "../api/client";
import { useToast } from "../components/Toasts";
import { buildRequest, parseGrid, wfCost, type BacktestForm } from "../lib/backtest";
import { duration, num, pct } from "../lib/format";

type Report = Record<string, number | string[] | null>;
interface JobSummary {
  id: string; status: "queued" | "running" | "done" | "error"; created_at: number; finished_at: number | null;
  spec: Record<string, unknown>; error: string | null; sharpe: number | null; max_drawdown: number | null;
}
interface McResult {
  n_sims: number; dd_p50: number; dd_p95: number; dd_p99: number; ret_p5: number; ret_p50: number; ret_p95: number;
  prob_dd_over_kill: number; kill_threshold: number; historical_dd: number; shutdown_criterion_dd: number;
  histogram: { from: number; to: number; count: number }[];
}
interface WfResult {
  n_trials: number;
  windows: { test_start: number; test_end: number; params: Record<string, number>; train_sharpe: number; test_sharpe: number; test_max_dd: number }[];
  param_stability: Record<string, number[]>; oos_report: Report; oos_equity: number[];
}
interface JobResult {
  report: Report; equity: [number, number][]; killed: boolean; kill_reason: string; orders_sent: number;
  vetoes: number; fees_paid: number; n_bars: number; n_trials?: number; montecarlo?: McResult; walkforward?: WfResult;
}
type Job = JobSummary & { result: JobResult | null };

const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const STRAT_FIELDS: [string, string][] = [
  ["fast_ema", "EMA rápida"], ["slow_ema", "EMA lenta"], ["atr_stop_mult", "Stop (× ATR)"],
  ["time_stop_bars", "Time stop (barras, 0 = no)"], ["max_vol_percentile", "Vol. máx. (percentil)"],
  ["min_efficiency_ratio", "Efficiency ratio mín."],
];
const RISK_FIELDS: [string, string][] = [
  ["max_risk_per_trade", "Riesgo por trade (%)"], ["target_vol_annual", "Vol. objetivo anual (%)"],
  ["max_position_pct", "Peso máx. por activo (%)"],
];

export function BacktestsPage({ theme }: { theme: string }) {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const loadJobs = useCallback(() => {
    api.get<{ jobs: JobSummary[] }>("/api/backtests").then((r) => setJobs(r.jobs)).catch(() => {});
  }, []);
  useEffect(() => { loadJobs(); }, [loadJobs]);
  const busy = jobs.some((j) => j.status === "queued" || j.status === "running");
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(loadJobs, 1500);
    return () => clearInterval(t);
  }, [busy, loadJobs]);

  return (
    <div className="grid bt-layout">
      <BacktestFormCard onSubmitted={(id) => { setSelected(id); loadJobs(); }} />
      <section className="card" aria-labelledby="jobs-title">
        <h2 className="card-title" id="jobs-title">Backtests de esta sesión</h2>
        {jobs.length === 0 ? <p className="muted" style={{ margin: 0 }}>Todavía no corriste ninguno.</p> : (
          <table className="rtable">
            <thead><tr><th>Job</th><th>Estado</th><th>Datos</th><th className="r">Sharpe</th><th className="r">Max DD</th><th /></tr></thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} className={selected === j.id ? "row-selected" : undefined}>
                  <td data-label="Job" className="mono strong">{j.id}</td>
                  <td data-label="Estado"><JobStatus j={j} /></td>
                  <td data-label="Datos" className="secondary">{dataLabel(j.spec)}</td>
                  <td data-label="Sharpe" className="r num">{num(j.sharpe)}</td>
                  <td data-label="Max DD" className="r num">{pct(j.max_drawdown)}</td>
                  <td data-label=""><button className="btn" onClick={() => setSelected(j.id)} disabled={j.status !== "done" && j.status !== "error"}>Ver</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      {selected && <JobView id={selected} status={jobs.find((j) => j.id === selected)?.status} theme={theme} />}
    </div>
  );
}

function dataLabel(spec: Record<string, unknown>): string {
  const files = spec.data_files as [string, string][] | undefined;
  if (files?.length) return files.map(([p]) => p).join(", ");
  return `${spec.synthetic_bars ?? "?"} barras sintéticas`;
}

function JobStatus({ j }: { j: JobSummary }) {
  const now = Date.now() / 1000;
  const map = {
    queued: ["◷", "en cola", "secondary"], running: ["◐", `corriendo ${duration(now - j.created_at)}`, "warn-text"],
    done: ["✓", `listo (${duration((j.finished_at ?? now) - j.created_at)})`, "up"], error: ["✕", "error", "down"],
  } as const;
  const [icon, text, cls] = map[j.status];
  return <span className={cls}><span aria-hidden>{icon}</span> {text}</span>;
}

// ------------------------------------------------------------------ formulario
interface ConfigResp { strategy: Record<string, number | boolean | string[]>; risk: Record<string, number> }
interface DatasetsResp { data_dir: string; datasets: { path: string; size: number }[]; symbols: string[] }

function BacktestFormCard({ onSubmitted }: { onSubmitted: (id: string) => void }) {
  const [form, setForm] = useState<BacktestForm | null>(null);
  const [baseRisk, setBaseRisk] = useState<Record<string, number>>({});
  const [ds, setDs] = useState<DatasetsResp | null>(null);
  const [sending, setSending] = useState(false);
  const toast = useToast();

  useEffect(() => {
    Promise.all([api.get<ConfigResp>("/api/config"), api.get<DatasetsResp>("/api/backtests/datasets")]).then(([cfg, d]) => {
      setDs(d);
      setBaseRisk(cfg.risk);
      const strategy: Record<string, number | boolean> = { allow_short: Boolean(cfg.strategy.allow_short) };
      for (const [k] of STRAT_FIELDS) strategy[k] = Number(cfg.strategy[k]);
      setForm({
        source: "synthetic", bars: 6000, seed: 42, files: Object.fromEntries(d.symbols.map((s) => [s, ""])),
        strategy, riskPct: Object.fromEntries(RISK_FIELDS.map(([k]) => [k, Math.round((cfg.risk[k] ?? 0) * 1e6) / 1e4])),
        stress: 1, montecarlo: true, mcSims: 2000, walkforward: false,
        grid: { fast_ema: "10, 20, 30", slow_ema: "50, 100", atr_stop_mult: "2.5, 3" },
        train: 3000, test: 1000, warmup: 600, trials: 1,
      });
    }).catch(() => toast("error", "No se pudo leer la configuración del bot"));
  }, [toast]);

  const built = useMemo(() => (form ? buildRequest(form, baseRisk) : null), [form, baseRisk]);
  if (!form || !built) return <section className="card"><p className="muted">Cargando…</p></section>;
  const set = <K extends keyof BacktestForm>(k: K, v: BacktestForm[K]) => setForm((f) => (f ? { ...f, [k]: v } : f));
  const numIn = (v: string) => (v.trim() === "" ? NaN : Number(v.replace(",", ".")));
  const wf = form.walkforward && form.source === "synthetic"
    ? (() => { const g = parseGrid(form.grid); return g.error ? null : wfCost(g.grid, form.bars, form.train, form.test); })()
    : null;

  async function submit() {
    if (!built?.body) return;
    setSending(true);
    try {
      const job = await api.post<{ id: string }>("/api/backtests", built.body);
      toast("ok", `Backtest ${job.id} en marcha (proceso aparte: no frena al bot)`);
      onSubmitted(job.id);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "No se pudo lanzar");
    } finally {
      setSending(false);
    }
  }

  return (
    <section className="card bt-form" aria-labelledby="btf-title">
      <h2 className="card-title" id="btf-title">Nuevo backtest</h2>

      <fieldset>
        <legend>Datos</legend>
        <div className="seg" role="radiogroup">
          <button role="radio" aria-checked={form.source === "synthetic"} aria-selected={form.source === "synthetic"} onClick={() => set("source", "synthetic")}>Sintéticos</button>
          <button role="radio" aria-checked={form.source === "files"} aria-selected={form.source === "files"} onClick={() => set("source", "files")}>Históricos</button>
        </div>
        {form.source === "synthetic" ? (
          <div className="form-row">
            <label>Barras por símbolo<input className="input" inputMode="numeric" value={form.bars} onChange={(e) => set("bars", numIn(e.target.value))} /></label>
            <label>Semilla<input className="input" inputMode="numeric" value={form.seed} onChange={(e) => set("seed", numIn(e.target.value))} /></label>
            <p className="muted form-hint">Sintéticos sirven para probar el pipeline, NO para validar la estrategia.</p>
          </div>
        ) : (
          <div className="form-row">
            {ds?.datasets.length ? Object.keys(form.files).map((sym) => (
              <label key={sym}>{sym}
                <select className="input" value={form.files[sym]} onChange={(e) => set("files", { ...form.files, [sym]: e.target.value })}>
                  <option value="">— no usar —</option>
                  {ds.datasets.map((d) => <option key={d.path} value={d.path}>{d.path}</option>)}
                </select>
              </label>
            )) : (
              <p className="secondary form-hint">
                No hay archivos en <span className="mono">{ds?.data_dir}</span>. Descargalos con{" "}
                <span className="mono">scripts/download_data.py</span>.
              </p>
            )}
          </div>
        )}
      </fieldset>

      <fieldset>
        <legend>Estrategia</legend>
        <div className="form-row">
          {STRAT_FIELDS.map(([k, label]) => (
            <label key={k}>{label}
              <input className="input" inputMode="decimal" value={Number.isNaN(form.strategy[k]) ? "" : String(form.strategy[k])}
                onChange={(e) => set("strategy", { ...form.strategy, [k]: numIn(e.target.value) })} />
            </label>
          ))}
          <label className="check"><input type="checkbox" checked={Boolean(form.strategy.allow_short)}
            onChange={(e) => set("strategy", { ...form.strategy, allow_short: e.target.checked })} /> Permitir cortos</label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Riesgo y costos</legend>
        <div className="form-row">
          {RISK_FIELDS.map(([k, label]) => (
            <label key={k}>{label}
              <input className="input" inputMode="decimal" value={Number.isNaN(form.riskPct[k]) ? "" : String(form.riskPct[k])}
                onChange={(e) => set("riskPct", { ...form.riskPct, [k]: numIn(e.target.value) })} />
            </label>
          ))}
          <label>Costos (stress)
            <select className="input" value={form.stress} onChange={(e) => set("stress", Number(e.target.value))}>
              {[1, 1.5, 2, 3].map((x) => <option key={x} value={x}>{x}× {x === 1 ? "(modelo base)" : ""}</option>)}
            </select>
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Robustez</legend>
        <div className="form-row">
          <label className="check"><input type="checkbox" checked={form.montecarlo} onChange={(e) => set("montecarlo", e.target.checked)} /> Montecarlo</label>
          {form.montecarlo && <label>Simulaciones<input className="input" inputMode="numeric" value={form.mcSims} onChange={(e) => set("mcSims", numIn(e.target.value))} /></label>}
          <label className="check"><input type="checkbox" checked={form.walkforward} onChange={(e) => set("walkforward", e.target.checked)} /> Walk-forward</label>
          {!form.walkforward && <label>Configuraciones probadas (Deflated SR)<input className="input" inputMode="numeric" value={form.trials} onChange={(e) => set("trials", numIn(e.target.value))} /></label>}
        </div>
        {form.walkforward && (
          <div className="form-row">
            {Object.keys(form.grid).map((k) => (
              <label key={k}>Grilla {k}<input className="input mono" value={form.grid[k]} onChange={(e) => set("grid", { ...form.grid, [k]: e.target.value })} /></label>
            ))}
            <label>Train (barras)<input className="input" inputMode="numeric" value={form.train} onChange={(e) => set("train", numIn(e.target.value))} /></label>
            <label>Test (barras)<input className="input" inputMode="numeric" value={form.test} onChange={(e) => set("test", numIn(e.target.value))} /></label>
            {wf && <p className={`form-hint ${wf.total > 300 ? "down" : "secondary"}`}>
              {wf.combos} combinaciones × {wf.windows} ventanas = <strong>{wf.total} backtests</strong> (máx. 300)
            </p>}
          </div>
        )}
      </fieldset>

      {built.errors.length > 0 && (
        <ul className="error-text" style={{ paddingLeft: 18 }}>{built.errors.map((e) => <li key={e}>{e}</li>)}</ul>
      )}
      <div className="dialog-actions">
        <span className="muted" style={{ marginRight: "auto", fontSize: 12 }}>
          Corre en un proceso aparte: no frena al bot.{" "}
          {form.walkforward && form.source === "files"
            ? "Las configuraciones del walk-forward cuentan como pruebas para el Deflated Sharpe."
            : `Cuenta ${built.trialsUsed} prueba(s) para el Deflated Sharpe.`}
        </span>
        <button className="btn primary" disabled={!built.body || sending} onClick={submit}>{sending ? "Enviando…" : "Lanzar backtest"}</button>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ resultados
function JobView({ id, status, theme }: { id: string; status?: string; theme: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const toast = useToast();
  useEffect(() => {
    let alive = true;
    api.get<Job>(`/api/backtests/${id}`).then((j) => alive && setJob(j)).catch(() => {});
    return () => { alive = false; };
  }, [id, status]);
  if (!job) return null;
  if (job.status === "error") return <section className="card error-text">Backtest {id} falló: {job.error}</section>;
  if (job.status !== "done" || !job.result) {
    return <section className="card secondary">Backtest {id}: {job.status === "queued" ? "en cola" : "corriendo"}…</section>;
  }
  const r = job.result;
  const rep = r.report;
  const n = (k: string) => (typeof rep[k] === "number" ? (rep[k] as number) : null);

  async function setReference() {
    try {
      await api.put("/api/metrics/reference", { operator: auth.operator || "dashboard", job_id: id });
      toast("ok", `${id} fijado como referencia: comparalo en Métricas`);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "No se pudo fijar");
    }
  }

  return (
    <section className="grid" style={{ gap: 12 }} aria-label={`Resultado de ${id}`}>
      <div className="card ref-card">
        <div>
          <div className="stat-label">Resultado</div>
          <div><span className="mono strong">{id}</span> · {dataLabel(job.spec)} · {r.n_bars} barras · {r.orders_sent} órdenes · {r.vetoes} vetos
            {r.n_trials ? <> · {r.n_trials} prueba(s) en el Deflated Sharpe</> : null}</div>
          <div className="muted mono" style={{ fontSize: 12 }}>{specLine(job.spec)}</div>
        </div>
        <button className="btn" onClick={setReference}>Fijar como referencia</button>
      </div>
      {r.killed && <div className="kill-banner">■ El kill-switch saltó durante el backtest: {r.kill_reason}</div>}
      <div className="grid metric-tiles">
        {[
          ["Retorno total", pct(n("total_return"), { signed: true })], ["CAGR", pct(n("cagr"), { signed: true })],
          ["Sharpe", num(n("sharpe"))], ["Sortino", num(n("sortino"))], ["Max drawdown", pct(n("max_drawdown"))],
          ["Profit factor", num(n("profit_factor"))], ["Trades", String(n("n_trades") ?? "—")], ["Win rate", pct(n("win_rate"), { digits: 1 })],
          ["Deflated Sharpe", pct(n("deflated_sharpe"), { digits: 0 })], ["Fees", `$${num(r.fees_paid)}`],
        ].map(([label, value]) => (
          <div className="card" key={label}><div className="stat-label">{label}</div><div className="stat-value">{value}</div></div>
        ))}
      </div>
      {Array.isArray(rep.warnings) && rep.warnings.length > 0 && (
        <div className="card sizing-warning">{rep.warnings.map((w) => <div key={w}><span aria-hidden>⚠ </span>{w}</div>)}</div>
      )}
      <section className="card"><h2 className="card-title">Equity del backtest</h2><EquityLine points={r.equity.map(([t, e]) => ({ t, e }))} theme={theme} /></section>
      {r.montecarlo && <MonteCarloCard mc={r.montecarlo} theme={theme} />}
      {r.walkforward && <WalkForwardCard wf={r.walkforward} theme={theme} />}
    </section>
  );
}

function specLine(spec: Record<string, unknown>): string {
  const s = (spec.strategy ?? {}) as Record<string, unknown>;
  const risk = (spec.risk ?? {}) as Record<string, number>;
  const parts = [`EMA ${s.fast_ema}/${s.slow_ema}`, `stop ${s.atr_stop_mult}×ATR`, `costos ${spec.cost_stress}×`];
  for (const [k, v] of Object.entries(risk)) parts.push(`${k}=${v}`);
  return parts.join(" · ");
}

function EquityLine({ points, theme }: { points: { t: number; e: number }[]; theme: string }) {
  const c = useMemo(() => ({ line: theme === "light" ? "#2a78d6" : "#3987e5", grid: css("--border"), text: css("--text-muted") }), [theme]);
  if (points.length < 2) return <p className="muted">Sin datos.</p>;
  const e0 = points[0]!.e;
  const data = points.map((p) => ({ t: p.t, r: p.e / e0 - 1 }));
  return (
    <div style={{ width: "100%", height: 240 }}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke={c.grid} />
          <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tick={{ fill: c.text, fontSize: 11 }} tickLine={false}
            tickFormatter={(t: number) => new Date(t).toLocaleDateString("es-AR", { month: "short", year: "2-digit" })} />
          <YAxis tick={{ fill: c.text, fontSize: 11 }} tickLine={false} axisLine={false} width={56} tickFormatter={(v: number) => pct(v, { digits: 0 })} />
          <ReferenceLine y={0} stroke={c.grid} />
          <Tooltip formatter={(v) => [pct(Number(v), { signed: true }), "retorno"]} labelFormatter={(t) => new Date(Number(t)).toLocaleString("es-AR")}
            contentStyle={{ background: css("--surface-3"), border: `1px solid ${c.grid}`, borderRadius: 8, fontSize: 12 }} />
          <Line dataKey="r" stroke={c.line} strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function MonteCarloCard({ mc, theme }: { mc: McResult; theme: string }) {
  const c = useMemo(() => ({
    bar: theme === "light" ? "#2a78d6" : "#3987e5", tail: css("--warning"), grid: css("--border"), text: css("--text-muted"),
    sec: css("--text-secondary"), crit: css("--critical"), down: css("--down"), cursor: css("--surface-3"),
  }), [theme]);
  const data = mc.histogram.map((b) => ({ mid: (b.from + b.to) / 2, count: b.count, width: b.to - b.from }));
  const maxX = Math.max(...mc.histogram.map((b) => b.to), mc.kill_threshold * 1.05);
  const pKill = mc.prob_dd_over_kill;
  return (
    <section className="card" aria-labelledby="mc-title">
      <h2 className="card-title" id="mc-title">Montecarlo: distribución de max drawdown ({mc.n_sims} simulaciones)</h2>
      <div style={{ width: "100%", height: 260 }}>
        <ResponsiveContainer>
          <BarChart data={data} margin={{ top: 24, right: 16, bottom: 0, left: 0 }} barCategoryGap={1}>
            <CartesianGrid vertical={false} stroke={c.grid} />
            <XAxis dataKey="mid" type="number" domain={[0, maxX]} tick={{ fill: c.text, fontSize: 11 }} tickLine={false}
              tickFormatter={(v: number) => pct(v, { digits: 0 })} />
            <YAxis tick={{ fill: c.text, fontSize: 11 }} tickLine={false} axisLine={false} width={40} />
            <Tooltip cursor={{ fill: c.cursor }} formatter={(v) => [String(v), "simulaciones"]} labelFormatter={(x) => `DD ≈ ${pct(Number(x), { digits: 1 })}`}
              contentStyle={{ background: css("--surface-3"), border: `1px solid ${c.grid}`, borderRadius: 8, fontSize: 12 }} />
            <Bar dataKey="count" radius={[3, 3, 0, 0]} isAnimationActive={false}>
              {data.map((d) => <Cell key={d.mid} fill={d.mid > mc.dd_p95 ? c.tail : c.bar} />)}
            </Bar>
            <ReferenceLine x={mc.historical_dd} stroke={c.sec} strokeDasharray="3 3" label={{ value: "backtest", position: "top", fill: c.sec, fontSize: 11 }} />
            <ReferenceLine x={mc.dd_p95} stroke={c.tail} strokeWidth={2} label={{ value: `p95 ${pct(mc.dd_p95, { digits: 1 })}`, position: "top", fill: c.tail, fontSize: 12 }} />
            <ReferenceLine x={mc.kill_threshold} stroke={c.crit} strokeDasharray="6 3" label={{ value: "kill", position: "insideTopRight", fill: c.down, fontSize: 11 }} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="grid kpis" style={{ marginTop: 12 }}>
        <div><div className="stat-label">DD p50 / p95 / p99</div><div className="num">{pct(mc.dd_p50)} / <strong>{pct(mc.dd_p95)}</strong> / {pct(mc.dd_p99)}</div></div>
        <div><div className="stat-label">DD del backtest (una sola realización)</div><div className="num">{pct(mc.historical_dd)}</div></div>
        <div><div className="stat-label">Retorno p5 / p50 / p95</div><div className="num">{pct(mc.ret_p5, { signed: true })} / {pct(mc.ret_p50, { signed: true })} / {pct(mc.ret_p95, { signed: true })}</div></div>
        <div><div className="stat-label">P(DD &gt; kill {pct(mc.kill_threshold, { digits: 0 })})</div>
          <div className={`num ${pKill > 0.05 ? "down" : "up"}`}>{pKill > 0.05 && <span aria-hidden>⚠ </span>}{pct(pKill, { digits: 1 })}</div></div>
        <div><div className="stat-label">Criterio de apagado (1.5 × p95)</div><div className="num">{pct(mc.shutdown_criterion_dd)}</div></div>
      </div>
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
        En <span style={{ color: c.tail }}>■ ámbar</span>, el 5% de peores escenarios. El sizing tiene que tolerar el DD del p95,
        no el del backtest.
      </p>
    </section>
  );
}

function WalkForwardCard({ wf, theme }: { wf: WfResult; theme: string }) {
  const c = useMemo(() => ({ line: theme === "light" ? "#2a78d6" : "#3987e5", grid: css("--border"), text: css("--text-muted") }), [theme]);
  const oos = wf.oos_equity.map((e, i) => ({ i, r: e / wf.oos_equity[0]! - 1 }));
  const rep = wf.oos_report;
  const n = (k: string) => (typeof rep[k] === "number" ? (rep[k] as number) : null);
  const unstable = Object.values(wf.param_stability).some((vals) => new Set(vals).size > 1);
  const degraded = wf.windows.filter((w) => w.test_sharpe < w.train_sharpe - 1).length;
  return (
    <section className="card" aria-labelledby="wf-title">
      <h2 className="card-title" id="wf-title">Walk-forward ({wf.windows.length} ventanas · {wf.n_trials} pruebas)</h2>
      <div className="grid metric-tiles" style={{ marginBottom: 12 }}>
        <div className="card"><div className="stat-label">Sharpe out-of-sample</div><div className="stat-value">{num(n("sharpe"))}</div></div>
        <div className="card"><div className="stat-label">Max DD out-of-sample</div><div className="stat-value">{pct(n("max_drawdown"))}</div></div>
        <div className="card"><div className="stat-label">Retorno out-of-sample</div><div className="stat-value">{pct(n("total_return"), { signed: true })}</div></div>
        <div className="card"><div className="stat-label">Deflated Sharpe OOS</div><div className="stat-value">{pct(n("deflated_sharpe"), { digits: 0 })}</div></div>
      </div>
      {(unstable || degraded > 0) && (
        <div className="sizing-warning" style={{ marginBottom: 12 }}>
          {unstable && <div><span aria-hidden>⚠ </span>Los parámetros óptimos cambian entre ventanas: puede ser ruido optimizado, no un edge estable.</div>}
          {degraded > 0 && <div><span aria-hidden>⚠ </span>{degraded} de {wf.windows.length} ventanas rinden mucho peor en test que en train (señal de overfitting).</div>}
        </div>
      )}
      <table className="rtable">
        <thead><tr><th>Test desde</th><th>Parámetros elegidos (train)</th><th className="r">Sharpe train</th><th className="r">Sharpe test</th><th className="r">Max DD test</th></tr></thead>
        <tbody>
          {wf.windows.map((w) => (
            <tr key={w.test_start}>
              <td data-label="Test desde" className="num secondary">{new Date(w.test_start).toLocaleDateString("es-AR")}</td>
              <td data-label="Parámetros" className="mono">{Object.entries(w.params).map(([k, v]) => `${k}=${v}`).join(" · ")}</td>
              <td data-label="Sharpe train" className="r num">{num(w.train_sharpe)}</td>
              <td data-label="Sharpe test" className={`r num ${w.test_sharpe < w.train_sharpe - 1 ? "down" : ""}`}>{num(w.test_sharpe)}</td>
              <td data-label="Max DD test" className="r num">{pct(w.test_max_dd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3 className="stat-label" style={{ margin: "16px 0 4px" }}>Equity out-of-sample concatenada (lo único que cuenta)</h3>
      <div style={{ width: "100%", height: 200 }}>
        <ResponsiveContainer>
          <LineChart data={oos} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke={c.grid} />
            <XAxis dataKey="i" tick={false} axisLine={{ stroke: c.grid }} />
            <YAxis tick={{ fill: c.text, fontSize: 11 }} tickLine={false} axisLine={false} width={56} tickFormatter={(v: number) => pct(v, { digits: 0 })} />
            <ReferenceLine y={0} stroke={c.grid} />
            <Tooltip formatter={(v) => [pct(Number(v), { signed: true }), "retorno OOS"]} labelFormatter={() => ""}
              contentStyle={{ background: css("--surface-3"), border: `1px solid ${c.grid}`, borderRadius: 8, fontSize: 12 }} />
            <Line dataKey="r" stroke={c.line} strokeWidth={2} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}
