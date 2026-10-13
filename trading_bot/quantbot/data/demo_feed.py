"""Feed de DEMO: reproduce barras sintéticas en tiempo acelerado, sin red.

Usa un ``SimClock`` que el consumidor avanza con cada Heartbeat procesado, así el pipeline completo
(risk, estrategia, OMS, paper exchange, journal, API) funciona igual que en paper.
Permite simular cortes de datos (``simulate_outage``) para probar el dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator

from ..core.clock import SimClock, timeframe_to_ms
from ..core.models import Bar
from .synthetic import generate_bars
from .websocket_feed import EventSink, Heartbeat

log = logging.getLogger(__name__)


class DemoFeed:
    def __init__(
        self,
        symbols: tuple[str, ...],
        timeframe: str,
        clock: SimClock,
        sink: EventSink,
        on_stale: Callable[[str, str], None],
        on_fresh: Callable[[str], None],
        bars_per_second: float = 4.0,
        warmup_bars: int = 600,
        seed: int = 7,
        start_price: float = 30_000.0,
        total_bars: int = 50_000,
    ) -> None:
        self.name = "demo"
        self.clock = clock
        self.sink = sink
        self.on_stale = on_stale
        self.on_fresh = on_fresh
        self.interval = 1.0 / bars_per_second
        self.warmup_bars = warmup_bars
        tf_ms = timeframe_to_ms(timeframe)
        start_ts = 1_700_000_000_000 - 1_700_000_000_000 % tf_ms
        # Primera barra "en vivo" (las anteriores son historia: no se opera sobre ellas)
        self.live_from_ts = start_ts + (warmup_bars + 2) * tf_ms
        self._series = [
            iter(
                generate_bars(
                    sym,
                    total_bars,
                    tf_ms,
                    start_price * (1 if i == 0 else 0.06 * (i + 1)),
                    start_ts=start_ts,
                    seed=seed + i,
                )
            )
            for i, sym in enumerate(symbols)
        ]
        self.reconnects = 0
        self.connected = False
        self.last_msg_ms = 0
        self._outage_until = 0.0

    def simulate_outage(self, seconds: float) -> None:
        """Corta los datos ``seconds`` segundos (para probar el banner de stale)."""
        self._outage_until = time.monotonic() + seconds

    def _next_group(self, it: list[Iterator[Bar]]) -> list[Bar]:
        return [next(s) for s in it]

    async def _emit(self, group: list[Bar]) -> None:
        # El reloj NO se mueve acá (eso lo adelantaría respecto del consumidor):
        # lo avanza el consumidor al procesar cada Heartbeat, en orden.
        # Heartbeats primero: todos los símbolos quedan "frescos" antes de las barras.
        for bar in group:
            await self.sink(Heartbeat(bar.symbol, bar.ts))
        for bar in group:
            await self.sink(bar)
        self.last_msg_ms = int(time.time() * 1000)

    async def run(self, stop: asyncio.Event) -> None:
        # Historia previa instantánea: indicadores calientes desde el primer segundo.
        await self._emit(self._next_group(self._series))
        for _ in range(self.warmup_bars):
            await self._emit(self._next_group(self._series))
        self.connected = True
        self.on_fresh(self.name)
        while not stop.is_set():
            if time.monotonic() < self._outage_until:
                if self.connected:
                    self.connected = False
                    self.reconnects += 1
                    self.on_stale(self.name, "corte simulado (demo)")
                await asyncio.sleep(0.2)
                continue
            if not self.connected:
                self.connected = True
                self.on_fresh(self.name)
            try:
                group = self._next_group(self._series)
            except StopIteration:
                log.warning("Demo: fin de la serie sintética")
                return
            await self._emit(group)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass
