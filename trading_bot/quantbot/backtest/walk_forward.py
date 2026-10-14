"""Walk-forward analysis y sensibilidad de parámetros (spec §4.2).

    |---- Train 1 ----|- Test 1 -|
         |---- Train 2 ----|- Test 2 -|
              |---- Train 3 ----|- Test 3 -|
    Resultado real = concatenación SOLO de los tramos de Test.

Si los parámetros óptimos saltan mucho entre ventanas, no hay edge estable:
es ruido optimizado.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..core.clock import periods_per_year
from ..core.models import Bar
from ..strategy import build_strategy
from .engine import BacktestResult, run_backtest
from .metrics import PerformanceReport, performance_report, simple_returns

# Con menos trades en train el Sharpe es ruido: esa combinación no compite.
MIN_TRAIN_TRADES = 5


def param_grid(grid: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*grid.values())]


def _valid(params: dict[str, Any], cfg: AppConfig) -> bool:
    fast = params.get("fast_ema", cfg.strategy.fast_ema)
    slow = params.get("slow_ema", cfg.strategy.slow_ema)
    return bool(fast < slow)


@dataclass(frozen=True, slots=True)
class WindowResult:
    train_start: int
    test_start: int
    test_end: int
    best_params: dict[str, Any]
    train_sharpe: float
    test: BacktestResult
    # Ninguna combinación llegó a MIN_TRAIN_TRADES en train: no hubo optimización y se
    # usaron los parámetros del TOML (fijados de antemano), no los de la grilla.
    fallback: bool = False


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    windows: list[WindowResult]
    oos_equity: list[float]
    oos_report: PerformanceReport
    n_trials: int

    @property
    def n_fallback(self) -> int:
        return sum(w.fallback for w in self.windows)

    def oos_by_symbol(self) -> dict[str, tuple[int, float]]:
        """Trades y PnL fuera de muestra por símbolo, de mayor a menor PnL."""
        out: dict[str, tuple[int, float]] = {}
        for t in (t for w in self.windows for t in w.test.trades):
            n, pnl = out.get(t.symbol, (0, 0.0))
            out[t.symbol] = (n + 1, pnl + t.pnl)
        return dict(sorted(out.items(), key=lambda kv: -kv[1][1]))

    def top_symbol_share(self) -> float | None:
        """Fracción de la ganancia fuera de muestra que explica el mejor símbolo (None si
        no hubo ganancia). Cerca de 1: el "edge" es de esa moneda, no de la estrategia."""
        by_sym = self.oos_by_symbol()
        total = sum(pnl for _, pnl in by_sym.values())
        if total <= 0 or not by_sym:
            return None
        return max(pnl for _, pnl in by_sym.values()) / total

    def param_stability(self) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for w in self.windows:
            for k, v in w.best_params.items():
                out.setdefault(k, []).append(v)
        return out


def walk_forward(
    cfg: AppConfig,
    bars: Sequence[Bar],
    grid: Mapping[str, Sequence[Any]],
    train_bars: int,
    test_bars: int,
    warmup_bars: int = 600,
) -> WalkForwardResult:
    """Walk-forward con ventanas deslizantes (por cantidad de timestamps únicos)."""
    # Nunca menos warmup del que la estrategia declara: con un filtro de 200 días y el
    # default de 600 barras (25 días en 1h), el filtro jamás se encendía fuera de muestra.
    warmup_bars = max(warmup_bars, build_strategy(cfg.strategy).warmup_bars())
    ts_sorted = sorted({b.ts for b in bars})
    combos = [p for p in param_grid(grid) if _valid(p, cfg)]
    if not combos:
        raise ValueError("Grilla vacía tras filtrar combinaciones inválidas")
    toml_params = {k: getattr(cfg.strategy, k) for k in grid}
    ppy = periods_per_year(cfg.strategy.timeframe)
    windows: list[WindowResult] = []
    oos_returns: list[float] = []
    n_trials = 0

    start = 0
    while start + train_bars + test_bars <= len(ts_sorted):
        tr0, tr1 = ts_sorted[start], ts_sorted[start + train_bars - 1]
        te0 = ts_sorted[start + train_bars]
        te1 = ts_sorted[start + train_bars + test_bars - 1]
        # Train también con warmup: antes arrancaba en frío y sus primeras barras operaban
        # con indicadores sin calibrar.
        train_from = ts_sorted[max(0, start - warmup_bars)]
        train = [b for b in bars if train_from <= b.ts <= tr1]
        # 1) Optimizar SOLO en train
        # Si NINGUNA combinación opera lo suficiente en train, no hay nada que optimizar.
        # Antes quedaba elegida en silencio la primera de la grilla (con 1d pasaba en casi
        # todas las ventanas): ahora se usan los parámetros del TOML y la ventana se marca.
        best: tuple[float, dict[str, Any]] = (-math.inf, toml_params)
        for params in combos:
            n_trials += 1
            res = run_backtest(cfg, train, strategy_params=params, trading_start_ts=tr0)
            score = res.report.sharpe if res.report.n_trades >= MIN_TRAIN_TRADES else -math.inf
            if score > best[0]:
                best = (score, params)
        fallback = best[0] == -math.inf
        # 2) Evaluar en test con warmup (indicadores calientes, sin operar)
        warm_from = ts_sorted[max(0, start + train_bars - warmup_bars)]
        test_slice = [b for b in bars if warm_from <= b.ts <= te1]
        test_res = run_backtest(cfg, test_slice, strategy_params=best[1], trading_start_ts=te0)
        windows.append(WindowResult(tr0, te0, te1, best[1], best[0], test_res, fallback))
        oos_returns.extend(simple_returns(test_res.equity))
        start += test_bars

    oos_equity = [cfg.initial_cash]
    for r in oos_returns:
        oos_equity.append(oos_equity[-1] * (1 + r))
    pnls = [t.pnl for w in windows for t in w.test.trades]
    report = performance_report(oos_equity, pnls, ppy, n_trials=max(n_trials, 1))
    return WalkForwardResult(windows, oos_equity, report, n_trials)


def parameter_sensitivity(
    cfg: AppConfig, bars: Sequence[Bar], grid: Mapping[str, Sequence[Any]]
) -> list[tuple[dict[str, Any], float, float, int]]:
    """Grilla completa: (params, sharpe, max_dd, n_trades).

    Buscar una MESETA ancha de buen desempeño, no un pico aislado.
    Si MA 50 funciona y MA 48 o 52 pierden, es un fantasma.
    """
    out = []
    for params in param_grid(grid):
        if not _valid(params, cfg):
            continue
        r = run_backtest(cfg, bars, strategy_params=params)
        out.append((params, r.report.sharpe, r.report.max_drawdown, r.report.n_trades))
    return out
