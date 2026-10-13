"""Estrategias. Registrar acá las nuevas para poder elegirlas desde la config."""

from ..config import StrategyConfig
from .base import Strategy, StrategyContext
from .trend_following import TrendFollowingStrategy

STRATEGIES: dict[str, type[TrendFollowingStrategy]] = {
    "trend_following": TrendFollowingStrategy,
}


def build_strategy(cfg: StrategyConfig) -> Strategy:
    try:
        return STRATEGIES[cfg.name](cfg)
    except KeyError as exc:
        raise ValueError(f"Estrategia desconocida: {cfg.name}") from exc


__all__ = ["STRATEGIES", "Strategy", "StrategyContext", "TrendFollowingStrategy", "build_strategy"]
