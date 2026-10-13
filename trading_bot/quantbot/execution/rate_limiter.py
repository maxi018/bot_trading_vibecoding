"""Token bucket asíncrono, configurado MÁS conservador que el límite del exchange.

Un ban de IP con posición abierta es grave: preferimos esperar nosotros.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_s <= 0 or burst < 1:
            raise ValueError("rate_per_s > 0 y burst >= 1")
        self.rate = rate_per_s
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._time = time_fn
        self._last = time_fn()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._time()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_acquire(self, cost: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:  # FIFO: nadie se cuela
            while True:
                self._refill()
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await asyncio.sleep((cost - self._tokens) / self.rate)

    def penalize(self, seconds: float) -> None:
        """Ante un 429 del exchange: vaciar el bucket y deber tokens."""
        self._refill()
        self._tokens = -seconds * self.rate


class NullRateLimiter(TokenBucket):
    """Para backtest: sin esperas (el tiempo es simulado)."""

    def __init__(self) -> None:
        super().__init__(1e9, 1_000_000)

    async def acquire(self, cost: float = 1.0) -> None:
        return None
