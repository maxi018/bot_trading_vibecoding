"""Generador de datos sintéticos con cambios de régimen (tendencia / lateral / crisis).

Sirve para tests y para verificar el pipeline completo SIN depender de la red.
NO sirve para validar una estrategia: para eso, datos reales point-in-time.
"""

from __future__ import annotations

import math
import random

from ..core.models import Bar

# (drift anual, vol anual, duración media en barras)
_REGIMES = {
    "bull": (0.80, 0.45, 600),
    "bear": (-0.70, 0.55, 400),
    "range": (0.0, 0.35, 500),
    "crisis": (-2.0, 1.40, 60),
}
_TRANSITIONS = {
    "bull": ["range", "range", "bear", "crisis"],
    "bear": ["range", "bull", "crisis"],
    "range": ["bull", "bear", "bull"],
    "crisis": ["range", "bear"],
}


def generate_bars(
    symbol: str = "BTC/USDT",
    n: int = 5000,
    tf_ms: int = 3_600_000,
    start_price: float = 30_000.0,
    start_ts: int = 1_600_000_000_000,
    seed: int = 42,
    periods_per_year: float = 8760.0,
) -> list[Bar]:
    rng = random.Random(seed)
    regime = "range"
    remaining = rng.expovariate(1 / _REGIMES[regime][2])
    price = start_price
    out: list[Bar] = []
    dt = 1 / periods_per_year
    for i in range(n):
        if remaining <= 0:
            regime = rng.choice(_TRANSITIONS[regime])
            remaining = rng.expovariate(1 / _REGIMES[regime][2])
        remaining -= 1
        mu, sigma, _ = _REGIMES[regime]
        # Retornos con colas pesadas (t de Student aproximada con mezcla)
        shock = rng.gauss(0, 1) * (3.0 if rng.random() < 0.02 else 1.0)
        r = (mu - 0.5 * sigma**2) * dt + sigma * math.sqrt(dt) * shock
        o = price
        c = price * math.exp(r)
        wick = abs(rng.gauss(0, sigma * math.sqrt(dt) * 0.6))
        h = max(o, c) * (1 + wick)
        lo = min(o, c) * (1 - abs(rng.gauss(0, sigma * math.sqrt(dt) * 0.6)))
        vol = abs(rng.gauss(100, 30)) * (3 if regime == "crisis" else 1)
        ts_open = start_ts + i * tf_ms
        out.append(Bar(symbol, ts_open, ts_open + tf_ms, o, h, lo, c, vol))
        price = c
    return out
