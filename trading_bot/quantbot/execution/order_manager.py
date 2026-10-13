"""Order Manager: ciclo de vida de órdenes, idempotencia, stops nativos y reconciliación.

Garantías:
- Cada orden lleva un ``clientOrderId`` único. Ante timeout, se CONSULTA por ese id
  antes de reintentar; si se reintenta, se reusa el mismo id (el exchange deduplica).
- Toda posición abierta tiene un stop NATIVO en el exchange (si el bot muere, el stop
  sigue vivo). Al mover el stop: primero se coloca el nuevo, después se cancela el viejo.
- Fills deduplicados por ``trade_id`` (los WS reenvían eventos al reconectar).
- Reconciliación periódica local vs exchange. Discrepancia confirmada en dos chequeos
  seguidos -> se reporta al risk engine (kill + flatten con posiciones del exchange).
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import logging
import secrets
from collections.abc import Awaitable, Callable

from ..core.alerting import Alerter, Severity
from ..core.clock import Clock
from ..core.models import Fill, Order, OrderRequest, OrderStatus, OrderType, Side, TimeInForce
from ..core.portfolio import Portfolio
from ..risk.engine import RiskEngine
from .base import AuthError, ExchangeClient, ExchangeError, OrderRejectedError
from .rate_limiter import TokenBucket

log = logging.getLogger(__name__)

FillListener = Callable[[Fill], Awaitable[None]]


class OrderManager:
    def __init__(
        self,
        exchange: ExchangeClient,
        risk: RiskEngine,
        portfolio: Portfolio,
        clock: Clock,
        rate_limiter: TokenBucket,
        alerter: Alerter | None = None,
        max_retries: int = 4,
        retry_base_delay_s: float = 0.5,
        id_prefix: str = "qb",
    ) -> None:
        self.exchange = exchange
        self.risk = risk
        self.portfolio = portfolio
        self.clock = clock
        self.limiter = rate_limiter
        self.alerter = alerter
        self.max_retries = max_retries
        self.retry_base_delay_s = retry_base_delay_s
        self.id_prefix = id_prefix
        self.orders: dict[str, Order] = {}
        self.desired_stop: dict[str, float | None] = {}
        self.stop_orders: dict[str, str] = {}  # symbol -> cid del stop vigente
        self._last_stop: dict[str, float] = {}  # último stop colocado (red de seguridad)
        self._seen_trades: set[str] = set()
        self._counter = itertools.count(1)
        self._listeners: list[FillListener] = []
        self._mismatch_strikes = 0
        self._lock = asyncio.Lock()  # serializa gestión de stops/flatten
        exchange.set_fill_handler(self.on_fill)

    def add_fill_listener(self, listener: FillListener) -> None:
        self._listeners.append(listener)

    def new_client_order_id(self) -> str:
        # <= 36 chars (límite de Binance). Prefijo para reconocer órdenes propias.
        return (
            f"{self.id_prefix}-{self.clock.now_ms()}-{next(self._counter)}-{secrets.token_hex(3)}"
        )

    # -------------------------------------------------------------- envío
    async def submit(self, req: OrderRequest) -> Order:
        order = Order.from_request(req, self.new_client_order_id(), self.clock.now_ms())
        self.orders[order.client_order_id] = order
        return await self._send_with_retries(order)

    async def _send_with_retries(self, order: Order) -> Order:
        cid = order.client_order_id
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire()
            try:
                ack = await self.exchange.create_order(order)
                self._merge(order, ack)
                self.risk.on_order_result(rejected=False)
                return order
            except OrderRejectedError as exc:
                order.status = OrderStatus.REJECTED
                order.reject_reason = str(exc)
                self.risk.on_order_result(rejected=True, reason=str(exc))
                return order
            except AuthError:
                order.status = OrderStatus.UNKNOWN
                raise  # fatal: lo maneja el supervisor (kill)
            except ExchangeError as exc:
                if not exc.retryable:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = str(exc)
                    self.risk.on_order_result(rejected=True, reason=str(exc))
                    return order
                order.status = OrderStatus.UNKNOWN
                log.warning("Error de red en %s (intento %d): %s", cid, attempt + 1, exc)
                # IDEMPOTENCIA: antes de reintentar, ¿la orden llegó?
                remote = await self._safe_fetch(cid, order.symbol)
                if remote is not None:
                    self._merge(order, remote)
                    return order
                await asyncio.sleep(self.retry_base_delay_s * (2**attempt))
        self._alert(
            Severity.CRITICAL,
            f"Orden {cid} en estado DESCONOCIDO tras {self.max_retries + 1} intentos",
            "Se resolverá en la próxima reconciliación.",
        )
        return order

    async def _safe_fetch(self, cid: str, symbol: str) -> Order | None:
        try:
            await self.limiter.acquire()
            return await self.exchange.fetch_order(cid, symbol)
        except ExchangeError as exc:
            log.warning("No se pudo consultar %s: %s", cid, exc)
            return None

    @staticmethod
    def _merge(local: Order, remote: Order) -> None:
        # Los fills se aplican por on_fill; acá solo sincronizamos estado/ids.
        local.exchange_order_id = remote.exchange_order_id or local.exchange_order_id
        if remote.status.is_terminal or local.status in (
            OrderStatus.PENDING_NEW,
            OrderStatus.UNKNOWN,
        ):
            if not (local.status is OrderStatus.FILLED and remote.status is not OrderStatus.FILLED):
                local.status = remote.status
        local.updated_ts = remote.updated_ts or local.updated_ts

    # ---------------------------------------------------------------- fills
    async def on_fill(self, fill: Fill) -> None:
        key = fill.trade_id or f"{fill.client_order_id}:{fill.ts}:{fill.qty}:{fill.price}"
        if key in self._seen_trades:
            return  # duplicado (reenvío tras reconexión)
        self._seen_trades.add(key)
        order = self.orders.get(fill.client_order_id)
        if order is None:  # algunos exchanges reportan el id del exchange, no el nuestro
            order = next(
                (o for o in self.orders.values() if o.exchange_order_id == fill.client_order_id),
                None,
            )
            if order is not None:
                fill = dataclasses.replace(fill, client_order_id=order.client_order_id)
        if order is not None and fill.decision_price <= 0 and order.decision_price > 0:
            fill = dataclasses.replace(fill, decision_price=order.decision_price)
        if order is not None:
            try:
                order.apply_fill(fill.qty, fill.price, fill.ts)
            except ValueError:
                log.exception("Fill inconsistente con la orden local %s", fill.client_order_id)
        else:
            log.warning("Fill de orden desconocida %s (¿orden manual?)", fill.client_order_id)
        self.portfolio.apply_fill(fill)
        self.risk.on_fill(fill)
        log.info(
            "FILL %s %s %.6f @ %.4f fee=%.4f slip=%.1fbps",
            fill.symbol,
            fill.side,
            fill.qty,
            fill.price,
            fill.fee,
            fill.slippage_bps,
            extra={"cid": fill.client_order_id},
        )
        if self.stop_orders.get(fill.symbol) == fill.client_order_id:
            self.stop_orders.pop(fill.symbol, None)
            self.desired_stop[fill.symbol] = None
            self._last_stop.pop(fill.symbol, None)
            self._alert(Severity.INFO, f"Stop ejecutado en {fill.symbol} @ {fill.price:.4f}")
        for listener in self._listeners:
            await listener(fill)
        await self.sync_protective_stop(fill.symbol)

    # ---------------------------------------------------------- stops nativos
    def set_desired_stop(self, symbol: str, stop: float | None) -> None:
        self.desired_stop[symbol] = stop

    async def sync_protective_stop(self, symbol: str) -> None:
        """Asegura exactamente UN stop nativo reduce-only, alineado con la posición."""
        async with self._lock:
            pos = self.portfolio.position(symbol).qty
            stop = self.desired_stop.get(symbol)
            cur_cid = self.stop_orders.get(symbol)
            cur = self.orders.get(cur_cid) if cur_cid else None
            if cur is not None and cur.is_terminal:
                self.stop_orders.pop(symbol, None)
                cur = None

            if abs(pos) < 1e-12:
                self._last_stop.pop(symbol, None)
                if cur is not None:
                    await self._cancel(cur)
                    self.stop_orders.pop(symbol, None)
                return
            if stop is None:
                if cur is not None:
                    return  # se mantiene el stop vigente
                stop = self._last_stop.get(symbol)
                if stop is None:
                    # Posición sin stop conocido (p.ej. adoptada): avisar fuerte.
                    self._alert(Severity.WARNING, f"Posición en {symbol} SIN stop protectivo")
                    return
                # Una salida no se llenó: NUNCA dejar la posición desnuda.
                log.warning("Re-colocando último stop conocido en %s @ %s", symbol, stop)
            side = Side.SELL if pos > 0 else Side.BUY
            if (
                cur is not None
                and cur.stop_price is not None
                and abs(cur.stop_price - stop) / stop < 1e-6
                and abs(cur.remaining - abs(pos)) / abs(pos) < 1e-6
                and cur.side is side
            ):
                return  # ya está bien
            req = OrderRequest(
                symbol=symbol,
                side=side,
                qty=abs(pos),
                type=OrderType.STOP_MARKET,
                stop_price=stop,
                reduce_only=True,
                tif=TimeInForce.GTC,
                decision_price=stop,
                tag="stop",
            )
            new = await self.submit(req)
            if new.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                self.stop_orders[symbol] = new.client_order_id
                self._last_stop[symbol] = stop
                if cur is not None:
                    await self._cancel(cur)  # el viejo se cancela DESPUÉS
            else:
                self._alert(
                    Severity.CRITICAL,
                    f"No se pudo colocar stop en {symbol}",
                    f"status={new.status} {new.reject_reason}",
                )

    async def release_stop(self, symbol: str) -> None:
        """Cancela el stop antes de una salida: en SPOT el stop bloquea el saldo y la
        orden de salida sería rechazada. Si la salida no se llena, sync_protective_stop
        re-coloca el último stop conocido."""
        async with self._lock:
            cid = self.stop_orders.pop(symbol, None)
            order = self.orders.get(cid) if cid else None
            if order is not None and not order.is_terminal:
                await self._cancel(order)

    async def _cancel(self, order: Order) -> None:
        try:
            await self.limiter.acquire()
            await self.exchange.cancel_order(order.client_order_id, order.symbol)
            order.status = OrderStatus.CANCELED
        except ExchangeError as exc:
            log.warning("Fallo cancelando %s: %s", order.client_order_id, exc)

    # -------------------------------------------------------- kill / flatten
    async def cancel_all(self, keep_stops: bool) -> None:
        """Cancela órdenes. Con ``keep_stops`` las posiciones siguen protegidas."""
        stop_ids = set(self.stop_orders.values())
        if not keep_stops:
            try:
                await self.exchange.cancel_all_orders()
            except ExchangeError:
                log.exception("cancel_all_orders falló; cancelando una por una")
            else:
                for o in self.orders.values():
                    if not o.is_terminal:
                        o.status = OrderStatus.CANCELED
                self.stop_orders.clear()
                return
        for o in list(self.orders.values()):
            if not o.is_terminal and (not keep_stops or o.client_order_id not in stop_ids):
                await self._cancel(o)
        if not keep_stops:
            self.stop_orders.clear()

    async def flatten_all(self) -> None:
        """Cierra TODO usando las posiciones del exchange (la verdad), no las locales."""
        # Orden de operaciones:
        # 1) cancelar todo lo que NO es stop (entradas pendientes, órdenes viejas);
        # 2) mandar los cierres reduce-only;
        # 3) los stops quedan vivos hasta que la posición llegue a cero (si el cierre
        #    falla o es parcial, el remanente sigue protegido). sync_protective_stop
        #    los cancela solo cuando ya no hay posición.
        # Sin self._lock: los fills de flatten re-entran en sync_protective_stop.
        for sym in list(self.desired_stop):
            self.desired_stop[sym] = None
        await self.cancel_all(keep_stops=True)
        try:
            positions = await self.exchange.fetch_positions()
        except ExchangeError:
            log.exception("No se pudieron leer posiciones del exchange; uso estado local")
            positions = {s: p.qty for s, p in self.portfolio.positions.items() if p.qty}
        for req in self.risk.flatten_orders(positions):
            await self.submit(req)  # camino privilegiado: sin pasar por evaluate()

    # ---------------------------------------------------------- reconciliación
    async def reconcile(self, tolerance: float = 1e-6) -> str | None:
        """Compara estado local con el exchange. Devuelve detalle si hay desync CONFIRMADO.

        Una sola discrepancia puede ser un fill en vuelo; dos seguidas, no.
        """
        try:
            remote_pos = await self.exchange.fetch_positions()
            remote_open = await self.exchange.fetch_open_orders()
        except ExchangeError as exc:
            log.warning("Reconciliación imposible: %s", exc)
            return None

        # 1) Resolver órdenes locales en estado desconocido / abiertas que ya no están
        remote_ids = {o.client_order_id for o in remote_open}
        for o in list(self.orders.values()):
            if o.is_terminal or o.client_order_id in remote_ids:
                continue
            remote = await self._safe_fetch(o.client_order_id, o.symbol)
            if remote is not None:
                self._merge(o, remote)
            elif o.status is OrderStatus.UNKNOWN:
                o.status = OrderStatus.REJECTED  # nunca llegó
                o.reject_reason = "no existe en el exchange"

        # 2) Órdenes propias en el exchange que no conocemos -> cancelar (huérfanas)
        for ro in remote_open:
            if ro.client_order_id not in self.orders and ro.client_order_id.startswith(
                self.id_prefix
            ):
                log.warning("Orden huérfana %s: cancelando", ro.client_order_id)
                await self._cancel(ro)

        # 3) Posiciones
        diffs = []
        symbols = set(remote_pos) | {s for s, p in self.portfolio.positions.items() if p.qty}
        for s in symbols:
            local = self.portfolio.position(s).qty
            remote_q = remote_pos.get(s, 0.0)
            scale = max(abs(local), abs(remote_q), 1e-9)
            if abs(local - remote_q) / scale > tolerance and abs(local - remote_q) > 1e-9:
                diffs.append(f"{s}: local={local} exchange={remote_q}")
        if not diffs:
            self._mismatch_strikes = 0
            return None
        self._mismatch_strikes += 1
        log.warning("Discrepancia de posiciones (strike %d): %s", self._mismatch_strikes, diffs)
        return "; ".join(diffs) if self._mismatch_strikes >= 2 else None

    def _alert(self, sev: Severity, title: str, body: str = "") -> None:
        if self.alerter:
            self.alerter.alert(sev, title, body)
        else:
            log.warning("%s %s", title, body)
