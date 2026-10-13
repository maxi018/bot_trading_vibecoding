"""Feed de market data por WebSocket, resiliente (spec §1.4: "asumir que todo se rompe").

Bucle de vida:
    conectar -> suscribir -> RESYNC (snapshot REST) -> marcar datos frescos -> leer
    ante error/zombie/hueco -> marcar datos STALE (risk no opera a ciegas)
                             -> backoff exponencial con jitter -> reconectar

Detecciones:
- Zombie socket: si no llega NINGÚN mensaje en ``heartbeat_timeout_s`` el feed se
  da por muerto aunque TCP parezca vivo.
- Hueco de secuencia en el libro: ``SequenceGapError`` -> reconexión + snapshot.
- Errores del consumidor (bugs en el núcleo) NO se tratan como problemas de red:
  se propagan al supervisor para que el bot vaya a estado seguro.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import urllib.request
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..core.clock import Clock, LiveClock
from ..core.models import Bar, OrderBook, SequenceGapError, Tick

if TYPE_CHECKING:
    from ..config import ExchangeConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Cualquier mensaje válido: prueba de vida del feed para ese símbolo."""

    symbol: str
    ts: int


FeedEvent = Bar | Tick | OrderBook | Heartbeat
EventSink = Callable[[FeedEvent], Awaitable[None]]


class WsConnection(Protocol):
    async def send(self, msg: str) -> None: ...

    async def recv(self) -> str | bytes: ...


ConnectFn = Callable[[str], AbstractAsyncContextManager[WsConnection]]


class FeedAdapter(Protocol):
    """Traduce el protocolo de un exchange concreto."""

    @property
    def url(self) -> str: ...

    def subscribe_messages(self) -> list[str]: ...

    async def resync(self) -> list[FeedEvent]:
        """Pide snapshots por REST y devuelve eventos de estado inicial."""
        ...

    def parse(self, raw: str | bytes) -> list[FeedEvent]:
        """Puede lanzar ``SequenceGapError``."""
        ...


class SinkError(Exception):
    """Error del consumidor: no es culpa de la red, no se reintenta."""


def _default_connect(url: str) -> AbstractAsyncContextManager[WsConnection]:
    import websockets  # dependencia opcional (extra "live")

    return websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=4096)


class ResilientWebSocketFeed:
    def __init__(
        self,
        name: str,
        adapter: FeedAdapter,
        sink: EventSink,
        on_stale: Callable[[str, str], None],
        on_fresh: Callable[[str], None],
        heartbeat_timeout_s: float = 15.0,
        max_backoff_s: float = 60.0,
        connect: ConnectFn = _default_connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.name = name
        self.adapter = adapter
        self.sink = sink
        self.on_stale = on_stale
        self.on_fresh = on_fresh
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.max_backoff_s = max_backoff_s
        self._connect = connect
        self._sleep = sleep
        self.clock = clock or LiveClock()
        self._rng = rng or random.Random()
        self.reconnects = 0
        self.last_msg_ms = 0
        self.connected = False

    async def _emit(self, ev: FeedEvent) -> None:
        try:
            await self.sink(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise SinkError(f"consumidor falló procesando {type(ev).__name__}") from exc

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with self._connect(self.adapter.url) as ws:
                    for msg in self.adapter.subscribe_messages():
                        await ws.send(msg)
                    for ev in await self.adapter.resync():  # CRÍTICO: nunca confiar en estado local
                        await self._emit(ev)
                    self.connected = True
                    self.on_fresh(self.name)
                    backoff = 1.0
                    log.info("Feed %s conectado y resincronizado", self.name)
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.heartbeat_timeout_s)
                        self.last_msg_ms = self.clock.now_ms()
                        try:
                            events = self.adapter.parse(raw)
                        except SequenceGapError:
                            raise
                        except (ValueError, KeyError, TypeError) as exc:
                            log.warning("Mensaje malformado descartado: %s", exc)
                            continue
                        for ev in events:
                            await self._emit(ev)
            except (asyncio.CancelledError, SinkError):
                self.connected = False
                self.on_stale(self.name, "feed detenido")
                raise
            except TimeoutError:
                self._on_disconnect(f"sin mensajes en {self.heartbeat_timeout_s}s (zombie)")
            except SequenceGapError as exc:
                self._on_disconnect(f"hueco de secuencia: {exc}")
            except Exception as exc:  # red, handshake, protocolo...
                self._on_disconnect(f"{type(exc).__name__}: {exc}")
            if stop.is_set():
                break
            delay = min(backoff + self._rng.uniform(0, backoff), self.max_backoff_s)
            log.warning("Feed %s: reconectando en %.1fs", self.name, delay)
            await self._sleep(delay)
            backoff = min(backoff * 2, self.max_backoff_s)
        self.connected = False

    def _on_disconnect(self, reason: str) -> None:
        self.connected = False
        self.reconnects += 1
        log.warning("Feed %s caído: %s", self.name, reason)
        self.on_stale(self.name, reason)  # no operar a ciegas


# ---------------------------------------------------------------------------
# Adapter Binance (spot): klines + depth diff con secuencia
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


class BinanceSpotAdapter:
    REST = "https://api.binance.com"
    WS = "wss://stream.binance.com:9443/stream"
    # Testnet: libro y precios PROPIOS (no espejan mainnet). Operar en testnet mirando
    # datos de mainnet hace que el fat-finger vete o que las limit nunca se llenen.
    TESTNET_REST = "https://testnet.binance.vision"
    TESTNET_WS = "wss://stream.testnet.binance.vision/stream"

    def __init__(
        self,
        symbols: tuple[str, ...],
        timeframe: str,
        ws_base: str = WS,
        with_depth: bool = True,
        snapshot_fetcher: Callable[[str], Awaitable[Any]] | None = None,
        rest_base: str = REST,
    ) -> None:
        self.symbols = symbols
        self._rest = rest_base
        self.timeframe = timeframe
        self.with_depth = with_depth
        self._ws_base = ws_base
        self._by_raw = {s.replace("/", "").lower(): s for s in symbols}
        self.books: dict[str, OrderBook] = {s: OrderBook(s) for s in symbols}
        self._fetch = snapshot_fetcher or self._default_fetch

    @property
    def url(self) -> str:
        streams = []
        for raw in self._by_raw:
            streams.append(f"{raw}@kline_{self.timeframe}")
            if self.with_depth:
                streams.append(f"{raw}@depth@100ms")
        return f"{self._ws_base}?streams={'/'.join(streams)}"

    @classmethod
    def endpoints(cls, mode: str, exchange: ExchangeConfig) -> tuple[str, str]:
        """(ws, rest) de market data. Solo live+testnet usa los de testnet: paper simula
        contra el mercado REAL aunque ``testnet`` sea true por defecto."""
        if mode == "live" and exchange.testnet and exchange.ws_url == cls.WS:
            return cls.TESTNET_WS, cls.TESTNET_REST
        return exchange.ws_url, cls.REST

    def subscribe_messages(self) -> list[str]:
        return []  # suscripción embebida en la URL (combined streams)

    async def _default_fetch(self, raw_symbol: str) -> Any:
        url = f"{self._rest}/api/v3/depth?symbol={raw_symbol.upper()}&limit=1000"
        return await asyncio.to_thread(_http_get_json, url)

    async def resync(self) -> list[FeedEvent]:
        if not self.with_depth:
            return []
        out: list[FeedEvent] = []
        for raw, sym in self._by_raw.items():
            snap = await self._fetch(raw)
            book = self.books[sym]
            book.apply_snapshot(
                ((float(p), float(q)) for p, q in snap["bids"]),
                ((float(p), float(q)) for p, q in snap["asks"]),
                seq=int(snap["lastUpdateId"]),
                ts=0,
            )
            out.append(book)
        return out

    def parse(self, raw: str | bytes) -> list[FeedEvent]:
        msg = json.loads(raw)
        data = msg.get("data", msg)
        etype = data.get("e")
        sym = self._by_raw.get(str(data.get("s", "")).lower())
        if sym is None:
            return []
        ts = int(data["E"])
        if etype == "kline":
            k = data["k"]
            events: list[FeedEvent] = [Heartbeat(sym, ts)]
            if k["x"]:  # solo barras CERRADAS llegan a la estrategia
                events.append(
                    Bar(
                        sym,
                        int(k["t"]),
                        int(k["T"]) + 1,
                        float(k["o"]),
                        float(k["h"]),
                        float(k["l"]),
                        float(k["c"]),
                        float(k["v"]),
                    )
                )
            return events
        if etype == "depthUpdate":
            book = self.books[sym]
            book.apply_delta(
                ((float(p), float(q)) for p, q in data["b"]),
                ((float(p), float(q)) for p, q in data["a"]),
                first_seq=int(data["U"]),
                last_seq=int(data["u"]),
                ts=ts,
            )
            return [book]
        return [Heartbeat(sym, ts)]
