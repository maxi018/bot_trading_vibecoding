"""Cambios de parámetros de riesgo EN CALIENTE con regla de "solo más conservador".

Cada parámetro editable declara qué dirección reduce el riesgo:
- ``lower``: bajar el valor es más conservador (p.ej. ``max_risk_per_trade``).
- ``higher``: subir el valor es más conservador.

Endurecer se acepta siempre. Relajar exige el bot PAUSADO y confirmación explícita.
Los parámetros que no figuran acá no se pueden editar desde afuera (requieren
reinicio con un archivo de config revisado). Los techos duros de ``config.validate``
siguen aplicando en todos los casos.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Literal

from ..config import AppConfig, ConfigError, RiskConfig, validate

Direction = Literal["lower", "higher"]

CONSERVATIVE_DIRECTION: dict[str, Direction] = {
    # sizing
    "target_vol_annual": "lower",
    "kelly_fraction": "lower",
    "max_leverage_per_asset": "lower",
    # límites duros
    "max_risk_per_trade": "lower",
    "max_position_pct": "lower",
    "max_gross_leverage": "lower",
    "max_correlated_exposure": "lower",
    "correlation_threshold": "lower",  # umbral más bajo => más activos cuentan como uno
    # circuit breakers
    "daily_drawdown_halt": "lower",
    "total_drawdown_kill": "lower",
    "max_slippage_bps": "lower",
    "slippage_anomaly_multiplier": "lower",
    "consecutive_losses_reduce": "lower",
    "size_reduction_factor": "lower",
    "max_data_age_s": "lower",
    "max_reject_rate": "lower",
    # fat finger
    "max_price_deviation_pct": "lower",
    "max_order_notional_pct": "lower",
    "max_orders_per_minute": "lower",
    "max_order_size_vs_avg": "lower",
}


class RiskParamError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParamChange:
    name: str
    old: float
    new: float
    relaxes: bool


def classify(cfg: RiskConfig, changes: dict[str, float]) -> list[ParamChange]:
    out: list[ParamChange] = []
    for name, raw in changes.items():
        if name not in CONSERVATIVE_DIRECTION:
            raise RiskParamError(f"'{name}' no es editable en caliente")
        old = getattr(cfg, name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise RiskParamError(f"'{name}': valor inválido {raw!r}")
        new = type(old)(raw)
        if new == old:
            continue
        lower = CONSERVATIVE_DIRECTION[name] == "lower"
        out.append(ParamChange(name, old, new, relaxes=(new > old) if lower else (new < old)))
    return out


def apply_changes(
    app_cfg: AppConfig,
    changes: dict[str, float],
    *,
    paused: bool,
    confirm_relax: bool,
) -> tuple[RiskConfig, list[ParamChange]]:
    """Valida y devuelve el nuevo ``RiskConfig``. No muta nada (lo aplica el llamador)."""
    diff = classify(app_cfg.risk, changes)
    relaxing = [c.name for c in diff if c.relaxes]
    if relaxing and not paused:
        raise RiskParamError(f"Relajar {relaxing} requiere el bot PAUSADO")
    if relaxing and not confirm_relax:
        raise RiskParamError(f"Relajar {relaxing} requiere confirmación explícita")
    updates: dict[str, Any] = {c.name: c.new for c in diff}
    new_risk = dataclasses.replace(app_cfg.risk, **updates)
    try:
        validate(dataclasses.replace(app_cfg, risk=new_risk))  # techos duros
    except ConfigError as exc:
        raise RiskParamError(str(exc)) from exc
    return new_risk, diff
