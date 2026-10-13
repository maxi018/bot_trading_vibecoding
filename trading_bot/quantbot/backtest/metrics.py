"""Métricas de performance (spec §6.2) + Deflated Sharpe Ratio (Bailey & López de Prado).

Todas las funciones son puras y trabajan con listas de floats (stdlib).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from statistics import NormalDist

_N = NormalDist()
EULER_GAMMA = 0.5772156649


def simple_returns(equity: Sequence[float]) -> list[float]:
    return [b / a - 1.0 for a, b in itertools.pairwise(equity) if a > 0]


def _mean(x: Sequence[float]) -> float:
    return sum(x) / len(x) if x else 0.0


def _std(x: Sequence[float]) -> float:
    n = len(x)
    if n < 2:
        return 0.0
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))


def sharpe(returns: Sequence[float], periods_per_year: float, rf_per_period: float = 0.0) -> float:
    ex = [r - rf_per_period for r in returns]
    s = _std(ex)
    return _mean(ex) / s * math.sqrt(periods_per_year) if s > 0 else 0.0


def sortino(returns: Sequence[float], periods_per_year: float) -> float:
    downside = [min(r, 0.0) for r in returns]
    dd = math.sqrt(sum(d * d for d in downside) / len(downside)) if downside else 0.0
    return _mean(returns) / dd * math.sqrt(periods_per_year) if dd > 0 else 0.0


def max_drawdown(equity: Sequence[float]) -> tuple[float, int]:
    """(max DD como fracción positiva, duración máxima bajo el agua en períodos)."""
    peak = -math.inf
    mdd, dur, max_dur = 0.0, 0, 0
    for v in equity:
        if v >= peak:
            peak, dur = v, 0
        else:
            dur += 1
            mdd = max(mdd, 1 - v / peak)
        max_dur = max(max_dur, dur)
    return mdd, max_dur


def cagr(equity: Sequence[float], periods_per_year: float) -> float:
    if len(equity) < 2 or equity[0] <= 0 or equity[-1] <= 0:
        return 0.0
    years = (len(equity) - 1) / periods_per_year
    return (equity[-1] / equity[0]) ** (1 / years) - 1 if years > 0 else 0.0


def skew_kurt(x: Sequence[float]) -> tuple[float, float]:
    n = len(x)
    s = _std(x)
    if n < 4 or s == 0:
        return 0.0, 3.0
    m = _mean(x)
    sk = sum(((v - m) / s) ** 3 for v in x) / n
    ku = sum(((v - m) / s) ** 4 for v in x) / n
    return sk, ku


def probabilistic_sharpe(sr: float, sr_benchmark: float, n: int, skew: float, kurt: float) -> float:
    """PSR: probabilidad de que el SR verdadero supere ``sr_benchmark`` (SR por período)."""
    if n < 2:
        return 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        return 0.0
    return _N.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom))


def deflated_sharpe(
    returns: Sequence[float], n_trials: int, trials_sr_variance: float | None = None
) -> float:
    """Deflated Sharpe Ratio: corrige el SR por la cantidad de pruebas hechas (data snooping).

    Args:
        returns: retornos por período de la estrategia elegida.
        n_trials: cuántas configuraciones/estrategias se probaron EN TOTAL (sé honesto).
        trials_sr_variance: varianza de los SR (por período) entre pruebas. Si no se
            conoce, se usa la varianza asintótica del estimador, 1/T (aprox).
    """
    n = len(returns)
    s = _std(returns)
    if n < 10 or s == 0:
        return 0.0
    sr = _mean(returns) / s
    var = trials_sr_variance if trials_sr_variance is not None else 1.0 / n
    if n_trials <= 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(var) * (
            (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
            + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e))
        )
    sk, ku = skew_kurt(returns)
    return probabilistic_sharpe(sr, sr0, n, sk, ku)


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    max_dd_duration_periods: int
    calmar: float
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    avg_win: float
    avg_loss: float
    deflated_sharpe: float
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def pretty(self) -> str:
        pct = {"total_return", "cagr", "max_drawdown", "win_rate"}
        lines = []
        for k, v in self.as_dict().items():
            if k == "warnings":
                continue
            if isinstance(v, float):
                v = f"{v:.2%}" if k in pct else f"{v:.3f}"
            lines.append(f"  {k:<26} {v}")
        for w in self.warnings:
            lines.append(f"  ⚠️  {w}")
        return "\n".join(lines)


def performance_report(
    equity: Sequence[float],
    trade_pnls: Sequence[float],
    periods_per_year: float,
    n_trials: int = 1,
) -> PerformanceReport:
    rets = simple_returns(equity)
    sr = sharpe(rets, periods_per_year)
    mdd, dd_dur = max_drawdown(equity)
    g = cagr(equity, periods_per_year)
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    n = len(trade_pnls)
    win_rate = len(wins) / n if n else 0.0
    avg_win = _mean(wins)
    avg_loss = -_mean(losses) if losses else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)

    warnings: list[str] = []
    if sr > 3:
        warnings.append("Sharpe > 3: test del pesimista -> asumir que hay un bug")
    if n < 100:
        warnings.append(f"Solo {n} trades: < 100 es anecdótico")
    if pf > 3 and n > 50:
        warnings.append("Profit factor > 3 con muchos trades: sospechar")
    return PerformanceReport(
        total_return=(equity[-1] / equity[0] - 1) if len(equity) > 1 else 0.0,
        cagr=g,
        sharpe=sr,
        sortino=sortino(rets, periods_per_year),
        max_drawdown=mdd,
        max_dd_duration_periods=dd_dur,
        calmar=g / mdd if mdd > 0 else 0.0,
        n_trades=n,
        win_rate=win_rate,
        profit_factor=pf,
        expectancy=win_rate * avg_win - (1 - win_rate) * avg_loss,
        avg_win=avg_win,
        avg_loss=avg_loss,
        deflated_sharpe=deflated_sharpe(rets, n_trials),
        warnings=tuple(warnings),
    )
