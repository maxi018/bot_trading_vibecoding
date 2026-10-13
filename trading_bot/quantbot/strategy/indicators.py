"""Indicadores INCREMENTALES (O(1) por barra).

Al ser incrementales es imposible que miren el futuro: solo conocen las barras que
ya recibieron. Mismo código en backtest y en vivo.
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections import deque

from ..core.models import Bar


class EMA:
    def __init__(self, period: int) -> None:
        self.period = period
        self._alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self.n = 0

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.value + self._alpha * (x - self.value)
        self.n += 1
        return self.value

    @property
    def ready(self) -> bool:
        return self.n >= self.period


class ATR:
    """Average True Range con suavizado de Wilder."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list[float] = []

    def update(self, bar: Bar) -> float | None:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None


class RollingStd:
    """Desvío estándar móvil de log-retornos (Welford sobre ventana)."""

    def __init__(self, window: int) -> None:
        self.window = window
        self._buf: deque[float] = deque(maxlen=window)
        self._prev: float | None = None

    def update(self, price: float) -> float | None:
        if self._prev is not None and price > 0 and self._prev > 0:
            self._buf.append(math.log(price / self._prev))
        self._prev = price
        return self.value

    @property
    def value(self) -> float | None:
        n = len(self._buf)
        if n < self.window:
            return None
        m = sum(self._buf) / n
        return math.sqrt(sum((r - m) ** 2 for r in self._buf) / (n - 1))


class EfficiencyRatio:
    """Kaufman Efficiency Ratio: |cambio neto| / suma de |cambios|.

    ~1 = tendencia limpia, ~0 = ruido/lateral. Sirve como filtro de régimen simple.
    """

    def __init__(self, period: int) -> None:
        self._prices: deque[float] = deque(maxlen=period + 1)

    def update(self, price: float) -> float | None:
        self._prices.append(price)
        if len(self._prices) < self._prices.maxlen:  # type: ignore[operator]
            return None
        p = list(self._prices)
        path = sum(abs(b - a) for a, b in itertools.pairwise(p))
        return abs(p[-1] - p[0]) / path if path > 0 else 0.0


class RollingPercentile:
    """Percentil del valor actual dentro de su propia historia reciente."""

    def __init__(self, history: int) -> None:
        self._hist: deque[float] = deque(maxlen=history)
        self._sorted: list[float] = []

    def update(self, x: float) -> float:
        """Devuelve el percentil de ``x`` contra la historia PREVIA, luego lo agrega."""
        pct = bisect.bisect_left(self._sorted, x) / len(self._sorted) if self._sorted else 0.5
        if len(self._hist) == self._hist.maxlen:
            old = self._hist.popleft()
            del self._sorted[bisect.bisect_left(self._sorted, old)]
        self._hist.append(x)
        bisect.insort(self._sorted, x)
        return pct

    def __len__(self) -> int:
        return len(self._hist)
