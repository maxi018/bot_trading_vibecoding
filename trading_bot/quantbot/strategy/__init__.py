"""Estrategias. Registrar acá las nuevas para poder elegirlas desde la config."""

from ..config import StrategyConfig
from .base import Strategy, StrategyContext
from .mean_reversion import MeanReversionStrategy
from .trend_following import TrendFollowingStrategy

STRATEGIES: dict[str, type[TrendFollowingStrategy] | type[MeanReversionStrategy]] = {
    "trend_following": TrendFollowingStrategy,
    "mean_reversion": MeanReversionStrategy,
}


def build_strategy(cfg: StrategyConfig) -> Strategy:
    try:
        return STRATEGIES[cfg.name](cfg)
    except KeyError as exc:
        raise ValueError(f"Estrategia desconocida: {cfg.name}") from exc


__all__ = [
    "STRATEGIES",
    "MeanReversionStrategy",
    "Strategy",
    "StrategyContext",
    "TrendFollowingStrategy",
    "build_strategy",
]
