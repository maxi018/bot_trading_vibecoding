"""Clase base de estrategias.

Contrato:
- La estrategia recibe eventos de mercado y devuelve ``Signal``s (intención de
  dirección + stop). NUNCA órdenes, NUNCA tamaños, NUNCA acceso al exchange.
- Solo puede LEER el estado de la cuenta vía ``StrategyContext`` (read-only).
- Debe ser determinista dado el mismo flujo de eventos (reproducibilidad de backtests).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from ..core.models import Bar, Fill, OrderBook, Signal, Tick


class StrategyContext(Protocol):
    """Vista de solo lectura que el núcleo le ofrece a la estrategia."""

    def now_ms(self) -> int: ...

    def position_qty(self, symbol: str) -> float: ...


class Strategy(ABC):
    def __init__(self, strategy_id: str, symbols: tuple[str, ...]) -> None:
        self.strategy_id = strategy_id
        self.symbols = symbols

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        """Procesa una barra CERRADA. La orden resultante se ejecuta después."""

    def on_tick(self, tick: Tick, ctx: StrategyContext) -> list[Signal]:
        return []

    def on_book(self, book: OrderBook, ctx: StrategyContext) -> list[Signal]:
        return []

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:  # noqa: B027 - hook opcional
        """Notificación de fill propio (opcional)."""

    def warmup_bars(self) -> int:
        """Barras necesarias antes de emitir señales (para cargar historia al arrancar)."""
        return 0
