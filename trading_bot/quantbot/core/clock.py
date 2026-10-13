"""Relojes: la única fuente de "ahora" del sistema.

Ningún módulo llama a ``time.time()`` directamente: así el backtest usa exactamente el
mismo código que producción y podemos detectar look-ahead bias (un evento con
``ts > clock.now_ms()`` es un bug, no una feature).
"""

from __future__ import annotations

import time
from typing import Protocol


class LookaheadError(RuntimeError):
    """Se intentó procesar un dato del futuro."""


class Clock(Protocol):
    def now_ms(self) -> int: ...


class LiveClock:
    # Tolerancia a desfase de reloj vs el exchange (NTP/chrony debe estar activo).
    tolerance_ms = 2_000

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class SimClock:
    """Reloj del backtest: solo avanza, nunca retrocede."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def advance_to(self, ts: int) -> None:
        if ts < self._now:
            raise LookaheadError(f"El reloj no puede retroceder: {ts} < {self._now}")
        self._now = ts


def assert_not_future(event_ts: int, clock: Clock, what: str = "evento") -> None:
    now = clock.now_ms() + getattr(clock, "tolerance_ms", 0)
    if event_ts > now:
        raise LookaheadError(f"{what} con ts={event_ts} > now={now}: look-ahead bias")


MS_PER_DAY = 86_400_000


def utc_day(ts_ms: int) -> int:
    """Número de día UTC (para resets diarios del risk engine)."""
    return ts_ms // MS_PER_DAY


_TF_UNITS = {"m": 60_000, "h": 3_600_000, "d": MS_PER_DAY, "w": 7 * MS_PER_DAY}


def timeframe_to_ms(tf: str) -> int:
    """'1m' -> 60000, '4h' -> 14400000, '1d' -> 86400000."""
    try:
        return int(tf[:-1]) * _TF_UNITS[tf[-1]]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"timeframe inválido: {tf!r}") from exc


def periods_per_year(tf: str, trading_days: int = 365) -> float:
    """Cripto opera 365 días: un año de barras de 1h = 8760 períodos."""
    return trading_days * MS_PER_DAY / timeframe_to_ms(tf)
