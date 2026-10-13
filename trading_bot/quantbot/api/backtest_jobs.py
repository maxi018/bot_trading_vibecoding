"""Backtests lanzados desde el dashboard, en un PROCESO aparte.

Un backtest es CPU-bound: correrlo en el event loop congelaría el trading. Se usa un
``ProcessPoolExecutor`` de 1 worker (contexto ``spawn``: no hereda sockets ni el loop
del bot). Los archivos de datos solo pueden venir de ``api.data_dir``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import logging
import multiprocessing
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AppConfig, load_config

log = logging.getLogger(__name__)

MAX_SYNTHETIC_BARS = 50_000
MAX_JOBS_KEPT = 30


class JobError(ValueError):
    pass


MAX_WF_BACKTESTS = 300  # combinaciones x ventanas: más que esto ocupa el worker una eternidad
DATA_SUFFIXES = (".csv", ".parquet")


def check_wf_budget(wf: dict[str, Any], n_bars: int) -> int:
    """Valida el walk-forward y devuelve la cantidad de backtests que implica."""
    combos = 1
    for values in wf["grid"].values():
        combos *= max(1, len(values))
    windows = max(0, (n_bars - int(wf["train"])) // int(wf["test"]))
    if windows < 1:
        raise JobError(
            f"Walk-forward imposible: {n_bars} barras no alcanzan para train={wf['train']} + "
            f"test={wf['test']}"
        )
    total = combos * windows
    if total > MAX_WF_BACKTESTS:
        raise JobError(
            f"Walk-forward demasiado grande: {combos} combinaciones x {windows} ventanas = "
            f"{total} backtests (máximo {MAX_WF_BACKTESTS}). Achicá la grilla o agrandá test."
        )
    return total


def _histogram(samples: list[float], bins: int = 30) -> list[dict[str, float]]:
    if not samples:
        return []
    lo, hi = min(samples), max(samples)
    width = (hi - lo) / bins or 1e-9
    counts = [0] * bins
    for x in samples:
        counts[min(int((x - lo) / width), bins - 1)] += 1
    return [
        {"from": lo + i * width, "to": lo + (i + 1) * width, "count": c}
        for i, c in enumerate(counts)
    ]


def _downsample(xs: list[Any], n: int = 1000) -> list[Any]:
    if len(xs) <= n:
        return xs
    step = -(-len(xs) // n)
    return [*xs[::step], xs[-1]]


def run_job(cfg_dict: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Se ejecuta en el proceso worker. Devuelve solo tipos JSON."""
    from ..backtest.engine import run_backtest
    from ..backtest.montecarlo import monte_carlo
    from ..backtest.walk_forward import walk_forward
    from ..core.clock import timeframe_to_ms
    from ..data.historical import load_csv, load_parquet
    from ..data.synthetic import generate_bars

    cfg = load_config(**cfg_dict)
    tf_ms = timeframe_to_ms(cfg.strategy.timeframe)
    bars = []
    if spec.get("data_files"):
        for path, sym in spec["data_files"]:
            loader = load_parquet if path.endswith(".parquet") else load_csv
            b, report = loader(path, sym, tf_ms)
            if not report.ok:
                raise JobError(f"Datos inválidos: {report.summary()}")
            bars += b
    else:
        for i, sym in enumerate(cfg.strategy.symbols):
            bars += generate_bars(sym, spec["synthetic_bars"], tf_ms, seed=spec["seed"] + i)

    out: dict[str, Any] = {}
    res = run_backtest(cfg, bars, cost_stress=spec["cost_stress"], n_trials=spec["n_trials"])
    out["report"] = res.report.as_dict()
    out["equity"] = _downsample([[t, e] for t, e in zip(res.equity_ts, res.equity, strict=True)])
    out["trades"] = [{"ts": t.ts, "symbol": t.symbol, "pnl": t.pnl} for t in res.trades][-500:]
    out["killed"] = res.killed
    out["kill_reason"] = res.kill_reason
    out["orders_sent"] = res.orders_sent
    out["vetoes"] = res.vetoes
    out["fees_paid"] = res.fees_paid
    out["n_bars"] = len(bars)

    if spec["montecarlo"] and res.trade_returns:
        mc = monte_carlo(
            res.trade_returns, n_sims=spec["mc_sims"], kill_threshold=cfg.risk.total_drawdown_kill
        )
        out["montecarlo"] = {
            k: v for k, v in dataclasses.asdict(mc).items() if k != "dd_samples"
        } | {
            "histogram": _histogram(list(mc.dd_samples)),
            "historical_dd": res.report.max_drawdown,
            "shutdown_criterion_dd": 1.5 * mc.dd_p95,
        }

    if spec.get("walkforward"):
        wf_spec = spec["walkforward"]
        n_ts = len({b.ts for b in bars})
        check_wf_budget(wf_spec, n_ts)
        wf = walk_forward(
            cfg,
            bars,
            wf_spec["grid"],
            wf_spec["train"],
            wf_spec["test"],
            wf_spec.get("warmup", 600),
        )
        out["walkforward"] = {
            "n_trials": wf.n_trials,
            "windows": [
                {
                    "test_start": w.test_start,
                    "test_end": w.test_end,
                    "params": w.best_params,
                    "train_sharpe": w.train_sharpe,
                    "test_sharpe": w.test.report.sharpe,
                    "test_max_dd": w.test.report.max_drawdown,
                }
                for w in wf.windows
            ],
            "param_stability": wf.param_stability(),
            "oos_report": wf.oos_report.as_dict(),
            "oos_equity": _downsample(wf.oos_equity),
        }
    return out


@dataclass
class Job:
    id: str
    spec: dict[str, Any]
    status: str = "queued"  # queued | running | done | error
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        rep = (self.result or {}).get("report", {})
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "spec": self.spec,
            "error": self.error,
            "sharpe": rep.get("sharpe"),
            "max_drawdown": rep.get("max_drawdown"),
        }


class BacktestJobs:
    def __init__(self, base_cfg: AppConfig) -> None:
        self.base_cfg = base_cfg
        self.data_dir = Path(base_cfg.api.data_dir).resolve()
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._pool: ProcessPoolExecutor | None = None

    def _executor(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn")
            )
        return self._pool

    def _resolve_data(self, files: list[dict[str, str]]) -> list[tuple[str, str]]:
        out = []
        for f in files:
            p = (self.data_dir / f["path"]).resolve()
            if not p.is_relative_to(self.data_dir):  # anti path traversal
                raise JobError("El archivo debe estar dentro de api.data_dir")
            if not p.is_file():
                raise JobError(f"No existe: {f['path']}")
            out.append((str(p), f["symbol"]))
        return out

    def build_spec(self, req: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Valida el pedido y arma (config, spec) serializables para el worker."""
        cfg = dataclasses.asdict(self.base_cfg)
        cfg["mode"] = "backtest"
        cfg.pop("api"), cfg.pop("demo")
        if req.get("strategy"):
            cfg["strategy"].update(req["strategy"])
        if req.get("risk"):
            cfg["risk"].update(req["risk"])
        if req.get("symbols"):
            cfg["strategy"]["symbols"] = list(req["symbols"])
        try:
            load_config(**cfg)  # valida techos/claves ANTES de encolar
        except Exception as exc:
            raise JobError(str(exc)) from exc
        n = int(req.get("synthetic_bars") or 6000)
        if not 500 <= n <= MAX_SYNTHETIC_BARS:
            raise JobError(f"synthetic_bars debe estar entre 500 y {MAX_SYNTHETIC_BARS}")
        stress = float(req.get("cost_stress") or 1.0)
        if not 0.5 <= stress <= 5:
            raise JobError("cost_stress fuera de [0.5, 5]")
        spec: dict[str, Any] = {
            "synthetic_bars": n,
            "seed": int(req.get("seed") or 42),
            "data_files": self._resolve_data(req.get("data_files") or []),
            "cost_stress": stress,
            "n_trials": max(1, int(req.get("n_trials") or 1)),
            "montecarlo": bool(req.get("montecarlo", True)),
            "mc_sims": max(100, min(int(req.get("mc_sims") or 2000), 20_000)),
            "walkforward": req.get("walkforward"),
        }
        if spec["walkforward"] and not spec["data_files"]:
            spec["wf_backtests"] = check_wf_budget(spec["walkforward"], n)
        return cfg, spec

    def datasets(self) -> list[dict[str, Any]]:
        """Archivos de datos disponibles (solo dentro de api.data_dir)."""
        if not self.data_dir.is_dir():
            return []
        out = []
        for p in sorted(self.data_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in DATA_SUFFIXES:
                out.append({"path": str(p.relative_to(self.data_dir)), "size": p.stat().st_size})
        return out[:500]

    def submit(self, req: dict[str, Any]) -> Job:
        cfg, spec = self.build_spec(req)
        shown = spec | {
            "strategy": cfg["strategy"],
            "risk": {k: cfg["risk"][k] for k in (req.get("risk") or {})},
            "data_files": [
                [str(Path(path).relative_to(self.data_dir)), sym]
                for path, sym in spec["data_files"]
            ],
        }
        job = Job(id=f"bt-{next(self._ids)}", spec=shown)
        self.jobs[job.id] = job
        while len(self.jobs) > MAX_JOBS_KEPT:
            self.jobs.pop(next(iter(self.jobs)))
        asyncio.get_running_loop().create_task(self._run(job, cfg, spec))
        return job

    async def _run(self, job: Job, cfg: dict[str, Any], spec: dict[str, Any]) -> None:
        job.status = "running"
        try:
            loop = asyncio.get_running_loop()
            job.result = await loop.run_in_executor(self._executor(), run_job, cfg, spec)
            job.status = "done"
        except Exception as exc:
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            log.warning("Backtest %s falló: %s", job.id, traceback.format_exc(limit=3))
        finally:
            job.finished_at = time.time()

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
