"""Montecarlo sobre trades (spec §4.2).

El max DD del backtest es UNA SOLA realización; el futuro casi seguro mostrará uno
peor. El sizing tiene que tolerar el DD del percentil 95, no el del backtest.

Cada simulación:
  1. bootstrap de trades con reposición,
  2. empeora la ejecución (slippage extra aleatorio, proporcional al tamaño del trade:
     los retornos son sobre el equity y una posición del 20% paga el 20% del slippage),
  3. pierde fills al azar (p=5%),
  4. mide max drawdown y retorno final.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from .metrics import max_drawdown


def _percentile(sorted_x: Sequence[float], q: float) -> float:
    if not sorted_x:
        return 0.0
    k = (len(sorted_x) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(sorted_x) - 1)
    return sorted_x[lo] + (sorted_x[hi] - sorted_x[lo]) * (k - lo)


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    n_sims: int
    dd_p50: float
    dd_p95: float
    dd_p99: float
    ret_p5: float
    ret_p50: float
    ret_p95: float
    prob_dd_over_kill: float
    kill_threshold: float
    dd_samples: tuple[float, ...] = ()  # distribución completa (para el histograma)

    def pretty(self) -> str:
        shutdown = 1.5 * self.dd_p95
        if shutdown < self.kill_threshold:
            criterion = f"DD live > {shutdown:.2%} (1.5x p95) => estrategia rota"
        else:
            # Un criterio por encima del kill nunca llega a evaluarse: el kill salta antes.
            criterion = (
                f"1.5x p95 = {shutdown:.2%} supera el kill ({self.kill_threshold:.0%}): "
                f"el kill-switch es el límite efectivo y el sizing es demasiado agresivo"
            )
        return (
            f"  Simulaciones           {self.n_sims}\n"
            f"  Max DD p50 / p95 / p99 "
            f"{self.dd_p50:.2%} / {self.dd_p95:.2%} / {self.dd_p99:.2%}\n"
            f"  Retorno p5 / p50 / p95 "
            f"{self.ret_p5:.2%} / {self.ret_p50:.2%} / {self.ret_p95:.2%}\n"
            f"  P(DD > kill {self.kill_threshold:.0%})       {self.prob_dd_over_kill:.2%}\n"
            f"  Criterio de apagado: {criterion}"
        )


def monte_carlo(
    trade_returns: Sequence[float],
    trade_weights: Sequence[float] | None = None,
    n_sims: int = 10_000,
    extra_slippage: float = 0.0005,  # 5 bps por lado, aleatorio uniforme [0, 2x]
    skip_prob: float = 0.05,
    kill_threshold: float = 0.15,
    seed: int = 1234,
) -> MonteCarloResult:
    if not trade_returns:
        raise ValueError("Sin trades no hay Montecarlo")
    n = len(trade_returns)
    # Sin pesos se asume posición = 100% del equity (lo más pesimista).
    weights = list(trade_weights) if trade_weights is not None else [1.0] * n
    if len(weights) != n:
        raise ValueError("trade_weights debe tener un peso por trade")
    rng = random.Random(seed)
    dds: list[float] = []
    rets: list[float] = []
    for _ in range(n_sims):
        eq = [1.0]
        for _ in range(n):
            if rng.random() < skip_prob:
                continue  # fill perdido
            i = rng.randrange(n)
            # entrada + salida, escalado por el tamaño de la posición vs el equity
            r = trade_returns[i] - weights[i] * rng.uniform(0, 2 * extra_slippage) * 2
            eq.append(eq[-1] * max(1 + r, 0.0))
        dd, _ = max_drawdown(eq)
        dds.append(dd)
        rets.append(eq[-1] - 1)
    dds.sort()
    rets.sort()
    return MonteCarloResult(
        n_sims=n_sims,
        dd_p50=_percentile(dds, 0.50),
        dd_p95=_percentile(dds, 0.95),
        dd_p99=_percentile(dds, 0.99),
        ret_p5=_percentile(rets, 0.05),
        ret_p50=_percentile(rets, 0.50),
        ret_p95=_percentile(rets, 0.95),
        prob_dd_over_kill=sum(1 for d in dds if d > kill_threshold) / n_sims,
        kill_threshold=kill_threshold,
        dd_samples=tuple(dds),
    )
