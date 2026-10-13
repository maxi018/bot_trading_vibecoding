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


class SMA:
    """Media simple de las últimas N barras."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._xs: deque[float] = deque(maxlen=period)
        self._sum = 0.0

    def update(self, x: float) -> float | None:
        if len(self._xs) == self.period:
            self._sum -= self._xs[0]
        self._xs.append(x)
        self._sum += x
        return self.value

    @property
    def ready(self) -> bool:
        return len(self._xs) == self.period

    @property
    def value(self) -> float | None:
        return self._sum / self.period if self.ready else None


class RSI:
    """RSI de Wilder. Con período 2 reacciona a 2-3 barras: mide sobrecompra/sobreventa
    de muy corto plazo, no tendencia."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._prev: float | None = None
        self._gain = self._loss = 0.0
        self._n = 0
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        self._n += 1
        if self._n <= self.period:  # semilla: promedio simple de los primeros cambios
            self._gain += gain / self.period
            self._loss += loss / self.period
            if self._n < self.period:
                return None
        else:
            self._gain = (self._gain * (self.period - 1) + gain) / self.period
            self._loss = (self._loss * (self.period - 1) + loss) / self.period
        if self._loss == 0:
            self.value = 100.0 if self._gain > 0 else 50.0
        else:
            self.value = 100.0 - 100.0 / (1.0 + self._gain / self._loss)
        return self.value


class DailySMA:
    """Media simple de los últimos N cierres DIARIOS (días UTC completos).

    El cierre de un día es el de su última barra. Se registra cuando el día TERMINÓ: en
    la barra que cierra justo a las 00:00 UTC (datos alineados, como los de Binance) o, si
    las barras no están alineadas, al llegar la primera barra del día siguiente. Un día
    en curso nunca cuenta: sin look-ahead, y el filtro cambia a lo sumo una vez por día.
    """

    DAY_MS = 86_400_000

    def __init__(self, days: int) -> None:
        self.days = days
        self._closes: deque[float] = deque(maxlen=days)
        self._sum = 0.0
        self._pending: tuple[int, float] | None = None  # (día UTC, último cierre visto)

    def _push(self, close: float) -> None:
        if len(self._closes) == self.days:
            self._sum -= self._closes[0]
        self._closes.append(close)
        self._sum += close

    def update(self, bar: Bar) -> None:
        day = (bar.ts - 1) // self.DAY_MS  # una barra que cierra a las 00:00 es del día previo
        if self._pending is not None and day > self._pending[0]:
            self._push(self._pending[1])  # barras desalineadas: el día anterior terminó
            self._pending = None
        if bar.ts % self.DAY_MS == 0:
            self._push(bar.close)  # cierra justo a las 00:00: el día terminó ahora
            self._pending = None
        else:
            self._pending = (day, bar.close)

    @property
    def ready(self) -> bool:
        return len(self._closes) == self.days

    @property
    def last_close(self) -> float | None:
        return self._closes[-1] if self._closes else None

    @property
    def value(self) -> float | None:
        return self._sum / self.days if self.ready else None


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
