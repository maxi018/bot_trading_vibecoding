"""Agregadores de barras a partir de ticks: tiempo, volumen y dólar (López de Prado).

Las barras por dólar/volumen muestrean más cuando hay actividad y menos en calma:
propiedades estadísticas más cercanas a la normalidad que las barras por tiempo.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Bar, Tick


@dataclass(slots=True)
class _Acc:
    ts_open: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    dollar: float = 0.0

    def add(self, t: Tick) -> None:
        self.high = max(self.high, t.price)
        self.low = min(self.low, t.price)
        self.close = t.price
        self.volume += t.size
        self.dollar += t.size * t.price


class TimeBarAggregator:
    """Emite una barra cuando llega un tick de la ventana siguiente.

    Importante: la barra se emite con ``ts`` = fin de la ventana; nunca antes.
    """

    def __init__(self, symbol: str, tf_ms: int) -> None:
        self.symbol = symbol
        self.tf_ms = tf_ms
        self._acc: _Acc | None = None

    def update(self, t: Tick) -> Bar | None:
        window = t.ts - t.ts % self.tf_ms
        out = None
        if self._acc is not None and window != self._acc.ts_open:
            out = self._emit(self._acc.ts_open + self.tf_ms)
        if self._acc is None:
            self._acc = _Acc(window, t.price, t.price, t.price, t.price)
        self._acc.add(t)
        return out

    def _emit(self, ts_close: int) -> Bar:
        a = self._acc
        assert a is not None
        self._acc = None
        return Bar(self.symbol, a.ts_open, ts_close, a.open, a.high, a.low, a.close, a.volume)


class DollarBarAggregator:
    """Una barra cada ``threshold`` unidades de moneda de cotización operadas."""

    def __init__(self, symbol: str, threshold: float, use_volume: bool = False) -> None:
        self.symbol = symbol
        self.threshold = threshold
        self.use_volume = use_volume  # True -> volume bars
        self._acc: _Acc | None = None

    def update(self, t: Tick) -> Bar | None:
        if self._acc is None:
            self._acc = _Acc(t.ts, t.price, t.price, t.price, t.price)
        self._acc.add(t)
        measure = self._acc.volume if self.use_volume else self._acc.dollar
        if measure >= self.threshold:
            a = self._acc
            self._acc = None
            end = max(t.ts, a.ts_open + 1)
            return Bar(self.symbol, a.ts_open, end, a.open, a.high, a.low, a.close, a.volume)
        return None
