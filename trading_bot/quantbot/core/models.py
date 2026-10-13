"""Modelo de datos del sistema.

Convenciones:
- Timestamps: enteros en milisegundos UTC (``ts``). Nada de datetimes naive.
- Cantidades: ``qty`` siempre positiva en órdenes/fills; el signo vive en ``Side``.
  En ``Position.qty`` el signo indica dirección (+ largo, - corto).
- Precios y cantidades como ``float``: suficiente para baja frecuencia. El redondeo a
  ``tick_size``/``step_size`` del exchange se hace en el borde (execution).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


class SequenceGapError(Exception):
    """Se perdió un update incremental del libro: el estado local ya no es confiable."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    POST_ONLY = "post_only"


class OrderStatus(StrEnum):
    PENDING_NEW = "pending_new"  # enviada, sin ACK del exchange
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"  # timeout/error de red: hay que consultar por clientOrderId

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)


class Direction(IntEnum):
    SHORT = -1
    FLAT = 0
    LONG = 1


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tick:
    """Un trade ejecutado en el exchange (tape)."""

    symbol: str
    ts: int
    price: float
    size: float
    aggressor: Side | None = None


@dataclass(frozen=True, slots=True)
class Bar:
    """Vela OHLCV. ``ts`` = timestamp de CIERRE: la barra no existe antes de ``ts``."""

    symbol: str
    ts_open: int
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"Barra OHLC inconsistente: {self}")
        if self.low <= 0 or self.volume < 0:
            raise ValueError(f"Barra con precio/volumen inválido: {self}")
        if self.ts <= self.ts_open:
            raise ValueError(f"Barra con ts de cierre <= apertura: {self}")


@dataclass(slots=True)
class OrderBook:
    """Libro L2 local mantenido con snapshot + deltas con número de secuencia.

    Si llega un delta cuyo ``first_seq`` no es contiguo al último aplicado se lanza
    ``SequenceGapError``: el feed debe pedir un snapshot nuevo (resync).
    """

    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_seq: int = -1
    ts: int = 0

    # -- mantenimiento ------------------------------------------------------
    def apply_snapshot(
        self,
        bids: Iterable[tuple[float, float]],
        asks: Iterable[tuple[float, float]],
        seq: int,
        ts: int,
    ) -> None:
        self.bids = {p: q for p, q in bids if q > 0}
        self.asks = {p: q for p, q in asks if q > 0}
        self.last_seq = seq
        self.ts = ts

    def apply_delta(
        self,
        bids: Iterable[tuple[float, float]],
        asks: Iterable[tuple[float, float]],
        first_seq: int,
        last_seq: int,
        ts: int,
    ) -> None:
        if self.last_seq < 0:
            raise SequenceGapError(f"{self.symbol}: delta sin snapshot previo")
        if last_seq <= self.last_seq:
            return  # update viejo/duplicado: se ignora
        if first_seq > self.last_seq + 1:
            raise SequenceGapError(
                f"{self.symbol}: hueco de secuencia {self.last_seq} -> {first_seq}"
            )
        for side_book, updates in ((self.bids, bids), (self.asks, asks)):
            for price, qty in updates:
                if qty <= 0:
                    side_book.pop(price, None)
                else:
                    side_book[price] = qty
        self.last_seq = last_seq
        self.ts = ts
        if self.is_crossed:
            raise SequenceGapError(f"{self.symbol}: libro cruzado tras delta, estado corrupto")

    # -- lecturas -------------------------------------------------------------
    def sorted_bids(self, depth: int | None = None) -> list[tuple[float, float]]:
        levels = sorted(self.bids.items(), key=lambda x: -x[0])
        return levels[:depth] if depth else levels

    def sorted_asks(self, depth: int | None = None) -> list[tuple[float, float]]:
        levels = sorted(self.asks.items(), key=lambda x: x[0])
        return levels[:depth] if depth else levels

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def is_crossed(self) -> bool:
        bb, ba = self.best_bid, self.best_ask
        return bb is not None and ba is not None and bb >= ba

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2 if bb is not None and ba is not None else None

    @property
    def spread_bps(self) -> float | None:
        bb, ba, mid = self.best_bid, self.best_ask, self.mid
        if bb is None or ba is None or not mid:
            return None
        return (ba - bb) / mid * 1e4

    def microprice(self) -> float | None:
        """(best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size)."""
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        bq, aq = self.bids[bb], self.asks[ba]
        return (bb * aq + ba * bq) / (bq + aq)

    def imbalance(self, depth: int = 5) -> float | None:
        """Order Book Imbalance en [-1, 1] sobre los top ``depth`` niveles."""
        b = sum(q for _, q in self.sorted_bids(depth))
        a = sum(q for _, q in self.sorted_asks(depth))
        return (b - a) / (b + a) if (a + b) > 0 else None

    def walk_book(self, side: Side, qty: float) -> tuple[float, float]:
        """Precio promedio de ejecución de una orden a mercado de ``qty``.

        Returns:
            (precio_promedio, cantidad_cubierta). Si el libro no alcanza,
            ``cantidad_cubierta < qty``: señal de liquidez insuficiente.
        """
        levels = self.sorted_asks() if side is Side.BUY else self.sorted_bids()
        remaining, cost = qty, 0.0
        for price, size in levels:
            take = min(remaining, size)
            cost += take * price
            remaining -= take
            if remaining <= 1e-12:
                break
        filled = qty - max(remaining, 0.0)
        return (cost / filled if filled > 0 else math.nan), filled


# ---------------------------------------------------------------------------
# Órdenes, fills y posiciones
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Orden APROBADA por el risk engine, todavía no enviada.

    Las estrategias jamás construyen esto: solo el ``RiskEngine``.
    """

    symbol: str
    side: Side
    qty: float
    type: OrderType = OrderType.MARKET
    price: float | None = None  # límite (o precio de protección del "market protegido")
    stop_price: float | None = None
    reduce_only: bool = False
    tif: TimeInForce = TimeInForce.GTC
    decision_price: float = 0.0  # arrival price para medir slippage real
    tag: str = ""  # "entry", "exit", "stop", "flatten"...


@dataclass(slots=True)
class Order:
    client_order_id: str
    symbol: str
    side: Side
    type: OrderType
    qty: float
    price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    tif: TimeInForce = TimeInForce.GTC
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    exchange_order_id: str | None = None
    created_ts: int = 0
    updated_ts: int = 0
    decision_price: float = 0.0
    tag: str = ""
    reject_reason: str = ""
    # Lo que el EXCHANGE reporta como ejecutado (ACK/consulta). Si supera a
    # ``filled_qty`` hay fills en vuelo que todavía no se aplicaron localmente.
    exchange_filled_qty: float = 0.0

    @classmethod
    def from_request(cls, req: OrderRequest, client_order_id: str, ts: int) -> Order:
        return cls(
            client_order_id=client_order_id,
            symbol=req.symbol,
            side=req.side,
            type=req.type,
            qty=req.qty,
            price=req.price,
            stop_price=req.stop_price,
            reduce_only=req.reduce_only,
            tif=req.tif,
            created_ts=ts,
            updated_ts=ts,
            decision_price=req.decision_price,
            tag=req.tag,
        )

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def apply_fill(self, qty: float, price: float, ts: int) -> None:
        if qty <= 0:
            raise ValueError("fill con qty <= 0")
        if qty > self.remaining + 1e-9:
            raise ValueError(
                f"overfill en {self.client_order_id}: {qty} > remaining {self.remaining}"
            )
        notional = self.avg_fill_price * self.filled_qty + price * qty
        self.filled_qty += qty
        self.avg_fill_price = notional / self.filled_qty
        self.status = OrderStatus.FILLED if self.remaining <= 1e-9 else OrderStatus.PARTIALLY_FILLED
        self.updated_ts = ts


@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float  # en moneda de cotización, siempre >= 0 (rebates como fee negativo)
    ts: int
    is_maker: bool = False
    trade_id: str = ""
    decision_price: float = 0.0

    @property
    def slippage_bps(self) -> float:
        """Slippage contra el arrival price. Positivo = peor para nosotros."""
        if self.decision_price <= 0:
            return 0.0
        return self.side.sign * (self.price - self.decision_price) / self.decision_price * 1e4


@dataclass(slots=True)
class Position:
    symbol: str
    qty: float = 0.0  # con signo
    avg_entry: float = 0.0
    realized_pnl: float = 0.0

    @property
    def direction(self) -> Direction:
        if self.qty > 1e-12:
            return Direction.LONG
        if self.qty < -1e-12:
            return Direction.SHORT
        return Direction.FLAT

    def apply_fill(self, side: Side, qty: float, price: float) -> float:
        """Aplica un fill y devuelve el PnL realizado (sin fees) por esta operación."""
        signed = side.sign * qty
        realized = 0.0
        if self.qty == 0 or (self.qty > 0) == (signed > 0):
            # Abre o aumenta: promedio ponderado
            new_qty = self.qty + signed
            self.avg_entry = (abs(self.qty) * self.avg_entry + qty * price) / abs(new_qty)
            self.qty = new_qty
        else:
            # Reduce, cierra o da vuelta
            closing = min(qty, abs(self.qty))
            realized = closing * (price - self.avg_entry) * (1 if self.qty > 0 else -1)
            new_qty = self.qty + signed
            if abs(new_qty) < 1e-12:
                self.qty, self.avg_entry = 0.0, 0.0
            elif (new_qty > 0) != (self.qty > 0):
                self.qty, self.avg_entry = new_qty, price  # dio vuelta
            else:
                self.qty = new_qty
        self.realized_pnl += realized
        return realized

    def unrealized(self, mark: float) -> float:
        return self.qty * (mark - self.avg_entry) if self.qty else 0.0

    def notional(self, mark: float) -> float:
        return abs(self.qty) * mark


@dataclass(frozen=True, slots=True)
class AccountMetrics:
    """Snapshot inmutable del estado de cuenta: lo que leen risk, logs y alertas."""

    ts: int
    equity: float
    cash: float
    peak_equity: float
    day_start_equity: float
    gross_exposure: float
    net_exposure: float
    realized_pnl: float
    unrealized_pnl: float
    fees_paid: float
    consecutive_losses: int
    open_positions: int

    @property
    def gross_leverage(self) -> float:
        return self.gross_exposure / self.equity if self.equity > 0 else math.inf

    @property
    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return self.equity / self.day_start_equity - 1.0

    @property
    def drawdown_from_peak(self) -> float:
        """Drawdown como fracción positiva (0.15 = 15% debajo del máximo)."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, 1.0 - self.equity / self.peak_equity)


# ---------------------------------------------------------------------------
# Señales (salida de la estrategia, entrada del risk engine)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signal:
    """Intención de la estrategia: dirección objetivo, NO tamaño ni orden.

    El tamaño lo decide exclusivamente el risk engine. ``stop_price`` es obligatorio
    para cualquier dirección distinta de FLAT: sin stop no hay trade.
    """

    strategy_id: str
    symbol: str
    ts: int
    direction: Direction
    price: float  # precio de referencia al momento de la decisión (arrival)
    stop_price: float | None = None
    strength: float = 1.0  # 0..1, escala el tamaño
    reason: str = ""

    def __post_init__(self) -> None:
        if self.direction is not Direction.FLAT:
            if self.stop_price is None or self.stop_price <= 0:
                raise ValueError(f"Señal {self.direction.name} sin stop: {self}")
            wrong_side = (
                self.stop_price >= self.price
                if self.direction is Direction.LONG
                else self.stop_price <= self.price
            )
            if wrong_side:
                raise ValueError(f"Stop del lado equivocado del precio: {self}")
