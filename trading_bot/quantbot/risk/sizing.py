"""Position sizing: volatility targeting con Kelly fraccional como TECHO.

    qty_final = min(
        vol_target_qty,      # equity * min(target_vol / vol_actual, max_lev) / price
        position_cap_qty,    # equity * max_position_pct / price
        risk_cap_qty,        # equity * min(max_risk_per_trade, kelly_cap) / stop_distance
    ) * strength * size_multiplier

Kelly nunca se usa como objetivo: solo puede ACHICAR el riesgo por trade cuando la
muestra de trades indica menos edge del que asumimos.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..config import RiskConfig

_EPS = 1e-12


class EwmaVolatility:
    """Volatilidad EWMA incremental de log-retornos (O(1) por update)."""

    def __init__(self, span: int) -> None:
        if span < 2:
            raise ValueError("span >= 2")
        self._alpha = 2.0 / (span + 1.0)
        self._var: float | None = None
        self._last: float | None = None
        self.n = 0
        self._span = span

    def update(self, price: float) -> None:
        if price <= 0:
            return
        if self._last is not None:
            r = math.log(price / self._last)
            self._var = (
                r * r
                if self._var is None
                else ((1 - self._alpha) * self._var + self._alpha * r * r)
            )
            self.n += 1
        self._last = price

    @property
    def ready(self) -> bool:
        return self.n >= self._span

    def per_period(self) -> float | None:
        return math.sqrt(self._var) if self._var is not None and self.ready else None

    def annualized(self, periods_per_year: float) -> float | None:
        v = self.per_period()
        return v * math.sqrt(periods_per_year) if v is not None else None


def kelly_discrete(win_rate: float, payoff: float) -> float:
    """f* = (p*b - q) / b. Devuelve 0 si no hay edge."""
    if payoff <= 0 or not 0 <= win_rate <= 1:
        return 0.0
    return max(0.0, (win_rate * payoff - (1 - win_rate)) / payoff)


def kelly_continuous(mu: float, sigma: float) -> float:
    """f* = mu / sigma^2 (fracción de apalancamiento óptima, sin techo)."""
    return max(0.0, mu / (sigma * sigma)) if sigma > _EPS else 0.0


@dataclass(frozen=True, slots=True)
class TradeStats:
    n: int
    win_rate: float
    payoff: float  # ganancia promedio / pérdida promedio (en valor absoluto)

    @classmethod
    def from_pnls(cls, pnls: Sequence[float]) -> TradeStats:
        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p < 0]
        n = len(pnls)
        if n == 0:
            return cls(0, 0.0, 0.0)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        payoff = avg_win / avg_loss if avg_loss > _EPS else (10.0 if avg_win > 0 else 0.0)
        return cls(n, len(wins) / n, payoff)


@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: float
    binding: str  # qué límite determinó el tamaño (para logs/auditoría)
    vol_target_qty: float
    position_cap_qty: float
    risk_cap_qty: float
    risk_budget: float  # fracción del equity arriesgada al stop

    @classmethod
    def zero(cls, reason: str) -> SizingResult:
        return cls(0.0, reason, 0.0, 0.0, 0.0, 0.0)


class PositionSizer:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg

    def risk_budget(self, stats: TradeStats | None) -> tuple[float, str]:
        """Fracción del equity a arriesgar por trade: min(max_risk, Kelly fraccional)."""
        base = self.cfg.max_risk_per_trade
        if stats is None or stats.n < self.cfg.kelly_min_trades:
            return base, "max_risk_per_trade"
        f_star = kelly_discrete(stats.win_rate, stats.payoff)
        if f_star <= 0:
            # Sin edge estadístico: modo "probation" (25% del riesgo) en vez de cero,
            # para no bloquear para siempre la estrategia. El kill por drawdown sigue activo.
            return base * 0.25, "kelly_no_edge_probation"
        kelly_cap = self.cfg.kelly_fraction * f_star
        return (kelly_cap, "kelly_cap") if kelly_cap < base else (base, "max_risk_per_trade")

    def size(
        self,
        *,
        equity: float,
        price: float,
        annual_vol: float | None,
        stop_distance: float,
        strength: float = 1.0,
        stats: TradeStats | None = None,
        size_multiplier: float = 1.0,
    ) -> SizingResult:
        c = self.cfg
        if equity <= 0 or price <= 0:
            return SizingResult.zero("equity_or_price_invalid")
        if annual_vol is None or not math.isfinite(annual_vol) or annual_vol <= _EPS:
            return SizingResult.zero("vol_unavailable")  # sin vol no hay sizing: no se opera
        if stop_distance <= 0 or not math.isfinite(stop_distance):
            return SizingResult.zero("no_stop")  # toda posición necesita stop
        strength = min(max(strength, 0.0), 1.0)

        leverage = min(c.target_vol_annual / annual_vol, c.max_leverage_per_asset)
        vol_qty = equity * leverage / price
        pos_cap_qty = equity * c.max_position_pct / price
        budget, budget_src = self.risk_budget(stats)
        risk_qty = equity * budget / stop_distance

        candidates = {
            "vol_target": vol_qty,
            "position_cap": pos_cap_qty,
            budget_src: risk_qty,
        }
        binding = min(candidates, key=lambda k: candidates[k])
        qty = candidates[binding] * strength * max(0.0, min(size_multiplier, 1.0))
        return SizingResult(qty, binding, vol_qty, pos_cap_qty, risk_qty, budget)
