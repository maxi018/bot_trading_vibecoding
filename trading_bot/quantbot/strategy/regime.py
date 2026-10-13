"""Filtro de régimen de mercado (spec §2.3: "probablemente el mejor retorno sobre esfuerzo").

Clasifica cada barra en:
- WARMUP:   no hay historia suficiente -> no operar.
- HIGH_VOL: vol realizada por encima del percentil configurado de su historia
            (crisis / flash crash) -> no abrir posiciones nuevas.
- RANGE:    mercado lateral (efficiency ratio bajo) -> trend following pierde acá.
- TREND:    condiciones aptas para seguir tendencia.

Es deliberadamente simple (2 variables, sin ML). Un HMM o clustering puede
reemplazarlo implementando la misma interfaz ``update(bar) -> Regime``.
"""

from __future__ import annotations

from enum import StrEnum

from ..config import StrategyConfig
from ..core.models import Bar
from .indicators import EfficiencyRatio, RollingPercentile, RollingStd


class Regime(StrEnum):
    WARMUP = "warmup"
    HIGH_VOL = "high_vol"
    RANGE = "range"
    TREND = "trend"

    @property
    def allows_entries(self) -> bool:
        return self is Regime.TREND


class RegimeFilter:
    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg
        self._vol = RollingStd(cfg.vol_lookback)
        self._vol_pct = RollingPercentile(cfg.vol_history)
        self._er = EfficiencyRatio(cfg.er_period)
        self.min_history = max(cfg.vol_lookback * 3, 100)
        self.last: Regime = Regime.WARMUP
        self.last_vol_percentile: float | None = None
        self.last_er: float | None = None

    def update(self, bar: Bar) -> Regime:
        vol = self._vol.update(bar.close)
        er = self._er.update(bar.close)
        self.last_er = er
        if vol is None or er is None:
            self.last = Regime.WARMUP
            return self.last
        pct = self._vol_pct.update(vol)
        self.last_vol_percentile = pct
        if len(self._vol_pct) < self.min_history:
            self.last = Regime.WARMUP
        elif pct >= self.cfg.max_vol_percentile:
            self.last = Regime.HIGH_VOL
        elif er < self.cfg.min_efficiency_ratio:
            self.last = Regime.RANGE
        else:
            self.last = Regime.TREND
        return self.last
