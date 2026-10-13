"""Interfaz de exchange y jerarquía de errores.

Toda implementación (paper, CCXT, Nautilus...) debe:
- aceptar ``client_order_id`` y ser idempotente respecto de él;
- traducir sus excepciones a esta jerarquía para que el ``OrderManager`` sepa
  qué es reintentable y qué no.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from ..core.models import Fill, Order


class ExchangeError(Exception):
    retryable = False


class NetworkError(ExchangeError):
    """Timeout / desconexión: el resultado de la operación es DESCONOCIDO."""

    retryable = True


class RateLimitError(ExchangeError):
    retryable = True


class OrderRejectedError(ExchangeError):
    """El exchange rechazó la orden (fondos, filtros, reduce-only...). No reintentar."""


class AuthError(ExchangeError):
    """Credenciales inválidas/revocadas: fatal."""


FillHandler = Callable[[Fill], Awaitable[None]]


class ExchangeClient(Protocol):
    name: str

    def set_fill_handler(self, handler: FillHandler) -> None: ...

    async def create_order(self, order: Order) -> Order:
        """Envía la orden. Devuelve el estado tras el ACK. Debe ser idempotente por
        ``client_order_id``: reenviar el mismo id no puede duplicar la orden."""
        ...

    async def cancel_order(self, client_order_id: str, symbol: str) -> None: ...

    async def cancel_all_orders(self, symbol: str | None = None) -> None: ...

    async def fetch_order(self, client_order_id: str, symbol: str) -> Order | None: ...

    async def fetch_open_orders(self) -> list[Order]: ...

    async def fetch_positions(self) -> dict[str, float]:
        """Posiciones netas con signo por símbolo (fuente de verdad)."""
        ...

    async def fetch_equity(self) -> float: ...

    async def set_dead_man_switch(self, timeout_s: int) -> bool:
        """ "Cancel all after X s sin heartbeat" nativo. False si no está soportado."""
        ...

    async def close(self) -> None: ...
