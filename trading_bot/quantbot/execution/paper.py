"""Exchange simulado: se usa en BACKTEST y en PAPER TRADING (mismo código que producción).

Mantiene su propio libro de órdenes, posiciones y caja: es la "verdad del exchange"
contra la que el ``OrderManager`` reconcilia, igual que con un exchange real.

Modos de fill para órdenes agresivas (market / limit IOC):
- ``next_bar`` (backtest): se ejecutan en la APERTURA de la barra siguiente a la
  decisión. Imposible ejecutar al mismo cierre que generó la señal (anti look-ahead).
- ``immediate`` (paper en vivo): se ejecutan contra el último libro/precio conocido.

Incluye inyección de fallas para tests (timeouts, rechazos).
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
from collections.abc import Callable

from ..core.clock import Clock
from ..core.models import (
    Bar,
    Fill,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from .base import FillHandler, NetworkError, OrderRejectedError
from .cost_model import CostModel, FillQuote

log = logging.getLogger(__name__)


class PaperExchange:
    name = "paper"

    def __init__(
        self,
        clock: Clock,
        cost_model: CostModel,
        initial_cash: float,
        fill_mode: str = "next_bar",
    ) -> None:
        if fill_mode not in ("next_bar", "immediate"):
            raise ValueError(fill_mode)
        self.clock = clock
        self.costs = cost_model
        self.fill_mode = fill_mode
        self.cash = initial_cash
        self.positions: dict[str, float] = {}
        self.orders: dict[str, Order] = {}
        self.last_price: dict[str, float] = {}
        self.books: dict[str, OrderBook] = {}
        self._fill_handler: FillHandler | None = None
        self._trade_ids = itertools.count(1)
        # --- inyección de fallas (tests) ---
        self.fail_next_creates = 0  # NetworkError en los próximos N create_order
        self.fail_after_accept = False  # acepta la orden pero "pierde" la respuesta
        self.reject_predicate: Callable[[Order], str | None] | None = None
        self.dead_man_timeout_s: int | None = None

    # ----------------------------------------------------------------- API
    def set_fill_handler(self, handler: FillHandler) -> None:
        self._fill_handler = handler

    async def create_order(self, order: Order) -> Order:
        existing = self.orders.get(order.client_order_id)
        if existing is not None:
            return self._copy(existing)  # idempotencia por clientOrderId
        if self.fail_next_creates > 0:
            self.fail_next_creates -= 1
            raise NetworkError("timeout simulado (la orden NO llegó)")
        reason = self.reject_predicate(order) if self.reject_predicate else None
        if reason is None:
            reason = self._validate(order)
        if reason:
            raise OrderRejectedError(reason)

        stored = self._copy(order)
        stored.status = OrderStatus.OPEN
        stored.exchange_order_id = f"paper-{len(self.orders) + 1}"
        stored.updated_ts = self.clock.now_ms()
        self.orders[stored.client_order_id] = stored

        if self.fill_mode == "immediate" and self._is_aggressive(stored):
            await self._fill_immediately(stored)

        if self.fail_after_accept:
            self.fail_after_accept = False
            raise NetworkError("timeout simulado (la orden SÍ llegó)")
        return self._copy(stored)

    async def cancel_order(self, client_order_id: str, symbol: str) -> None:
        o = self.orders.get(client_order_id)
        if o is not None and not o.is_terminal:
            o.status = OrderStatus.CANCELED
            o.updated_ts = self.clock.now_ms()

    async def cancel_all_orders(self, symbol: str | None = None) -> None:
        for o in self.orders.values():
            if not o.is_terminal and (symbol is None or o.symbol == symbol):
                o.status = OrderStatus.CANCELED

    async def fetch_order(self, client_order_id: str, symbol: str) -> Order | None:
        o = self.orders.get(client_order_id)
        return self._copy(o) if o else None

    async def fetch_open_orders(self) -> list[Order]:
        return [self._copy(o) for o in self.orders.values() if not o.is_terminal]

    async def fetch_positions(self) -> dict[str, float]:
        return {s: q for s, q in self.positions.items() if abs(q) > 1e-12}

    async def fetch_equity(self) -> float:
        return self.cash + sum(q * self.last_price.get(s, 0.0) for s, q in self.positions.items())

    async def set_dead_man_switch(self, timeout_s: int) -> bool:
        self.dead_man_timeout_s = timeout_s
        return True

    async def close(self) -> None:
        return None

    # ------------------------------------------------------- market events
    async def process_bar(self, bar: Bar) -> None:
        """Ejecuta órdenes pendientes contra la barra recién llegada.

        Las órdenes existentes ANTES de esta barra se procesan contra su OHLC.
        Los stops creados DURANTE la barra (tras un fill de entrada en la apertura) se
        evalúan pesimistamente contra el mismo rango: si el low tocó el stop, asumimos
        que se disparó.
        """
        self.last_price[bar.symbol] = bar.close
        pending = [o for o in self.orders.values() if o.symbol == bar.symbol and not o.is_terminal]
        pending_ids = {o.client_order_id for o in pending}
        for o in pending:
            if o.is_terminal:
                continue
            await self._process_order_on_bar(o, bar, armed_at_open=True)
        # Segunda pasada: stops nacidos durante esta barra
        fresh = [
            o
            for o in self.orders.values()
            if o.symbol == bar.symbol
            and not o.is_terminal
            and o.type is OrderType.STOP_MARKET
            and o.client_order_id not in pending_ids
        ]
        for o in fresh:
            await self._process_order_on_bar(o, bar, armed_at_open=False)

    def on_book(self, book: OrderBook) -> None:
        self.books[book.symbol] = book
        if book.mid:
            self.last_price[book.symbol] = book.mid

    def on_price(self, symbol: str, price: float) -> None:
        self.last_price[symbol] = price

    async def on_market_price(self, symbol: str, price: float) -> None:
        """Paper en vivo: dispara stops y limits pasivas con cada precio nuevo."""
        self.last_price[symbol] = price
        for o in [o for o in self.orders.values() if o.symbol == symbol and not o.is_terminal]:
            if o.type is OrderType.STOP_MARKET and o.stop_price is not None:
                hit = price <= o.stop_price if o.side is Side.SELL else price >= o.stop_price
                if hit:
                    px = self.costs.taker_price(o.side, price)
                    q = o.remaining
                    await self._execute(
                        o,
                        FillQuote(px, q, False, self.costs.fee(px * q, False)),
                        self.clock.now_ms(),
                    )
            elif o.type is OrderType.LIMIT and o.tif is not TimeInForce.IOC and o.price:
                through = price < o.price if o.side is Side.BUY else price > o.price
                if through:
                    q = o.remaining
                    await self._execute(
                        o,
                        FillQuote(o.price, q, True, self.costs.fee(o.price * q, True)),
                        self.clock.now_ms(),
                    )

    # ---------------------------------------------------------- internals
    def _validate(self, order: Order) -> str | None:
        if order.qty <= 0:
            return "qty <= 0"
        if order.type is OrderType.LIMIT and not order.price:
            return "limit sin precio"
        if order.type is OrderType.STOP_MARKET and not order.stop_price:
            return "stop sin stop_price"
        if order.reduce_only:
            pos = self.positions.get(order.symbol, 0.0)
            if pos == 0 or (pos > 0) == (order.side is Side.BUY):
                return "reduce_only rechazada: no reduce posición"
        return None

    @staticmethod
    def _is_aggressive(o: Order) -> bool:
        return o.type is OrderType.MARKET or (
            o.type is OrderType.LIMIT and o.tif is TimeInForce.IOC
        )

    async def _process_order_on_bar(self, o: Order, bar: Bar, armed_at_open: bool) -> None:
        quote: FillQuote | None = None
        if self._is_aggressive(o):
            quote = self.costs.market_fill_on_bar(o.side, o.remaining, bar)
            if o.type is OrderType.LIMIT and o.price is not None:
                worse = quote.price > o.price if o.side is Side.BUY else quote.price < o.price
                if worse:
                    # Protección de precio: la IOC no se llena más allá del límite.
                    o.status = OrderStatus.EXPIRED
                    return
            await self._execute(o, quote, self.clock.now_ms())
            if not o.is_terminal and o.tif is TimeInForce.IOC:
                o.status = OrderStatus.EXPIRED
        elif o.type is OrderType.LIMIT and o.price is not None:
            if self.costs.limit_trades_through(o.side, o.price, bar):
                q = o.remaining
                quote = FillQuote(o.price, q, True, self.costs.fee(o.price * q, True))
                await self._execute(o, quote, self.clock.now_ms())
        elif o.type is OrderType.STOP_MARKET and o.stop_price is not None:
            quote = self.costs.stop_fill_on_bar(
                o.side, o.remaining, o.stop_price, bar, armed_at_open
            )
            if quote is not None:
                await self._execute(o, quote, self.clock.now_ms())

    async def _fill_immediately(self, o: Order) -> None:
        book = self.books.get(o.symbol)
        quote: FillQuote | None = None
        if book is not None and book.mid:
            quote = self.costs.market_fill_on_book(o.side, o.remaining, book)
        elif o.symbol in self.last_price:
            px = self.costs.taker_price(o.side, self.last_price[o.symbol])
            quote = FillQuote(px, o.remaining, False, self.costs.fee(px * o.remaining, False))
        if quote is not None:
            if o.type is OrderType.LIMIT and o.price is not None:
                worse = quote.price > o.price if o.side is Side.BUY else quote.price < o.price
                if worse:
                    o.status = OrderStatus.EXPIRED
                    return
            await self._execute(o, quote, self.clock.now_ms())
        if not o.is_terminal and o.tif is TimeInForce.IOC:
            o.status = OrderStatus.EXPIRED

    async def _execute(self, o: Order, q: FillQuote, ts: int) -> None:
        qty = min(q.qty, o.remaining)
        if o.reduce_only:
            pos = self.positions.get(o.symbol, 0.0)
            reducible = abs(pos) if (pos > 0) != (o.side is Side.BUY) else 0.0
            qty = min(qty, reducible)
            if qty <= 1e-12:
                o.status = OrderStatus.CANCELED  # reduce-only sin nada que reducir
                return
        fee = q.fee * (qty / q.qty) if q.qty > 0 else 0.0
        o.apply_fill(qty, q.price, ts)
        self.positions[o.symbol] = self.positions.get(o.symbol, 0.0) + o.side.sign * qty
        self.cash -= o.side.sign * qty * q.price + fee
        fill = Fill(
            client_order_id=o.client_order_id,
            symbol=o.symbol,
            side=o.side,
            qty=qty,
            price=q.price,
            fee=fee,
            ts=ts,
            is_maker=q.is_maker,
            trade_id=f"t{next(self._trade_ids)}",
            decision_price=o.decision_price,
        )
        if self._fill_handler is not None:
            await self._fill_handler(fill)

    @staticmethod
    def _copy(o: Order) -> Order:
        return dataclasses.replace(o)
