"""Wrapper de exchange real vía CCXT (async).

IMPORTANTE: cada exchange tiene particularidades (nombres de params para stops,
reduce-only, clientOrderId, cancel-all-after). Este wrapper usa la API unificada de
CCXT y está pensado para validarse PRIMERO en testnet. No pasar a dinero real sin
haber probado: entrada, stop nativo, cancelación, reconexión y reconciliación.

Responsabilidades:
- Traducir excepciones de CCXT a la jerarquía de ``execution.base``.
- Timeout duro por request (un request colgado no puede colgar el bot).
- Fills por polling de ``fetch_my_trades`` con dedupe (o WS si se integra ccxt.pro).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import ExchangeConfig, Secrets
from ..core.models import Fill, Order, OrderStatus, OrderType, Side, TimeInForce
from .base import (
    AuthError,
    ExchangeError,
    FillHandler,
    NetworkError,
    OrderRejectedError,
    RateLimitError,
)

log = logging.getLogger(__name__)

_STATUS = {
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


class CcxtExchange:
    def __init__(self, cfg: ExchangeConfig, secrets: Secrets) -> None:
        try:
            import ccxt.async_support as ccxt_async
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Instalar extras: pip install 'quantbot[live]'") from exc
        import ccxt

        self._ccxt = ccxt
        klass = getattr(ccxt_async, cfg.name)
        self.ex: Any = klass(
            {
                "apiKey": secrets.api_key,
                "secret": secrets.api_secret,
                "enableRateLimit": True,  # limitador interno de CCXT + el nuestro
                "timeout": int(cfg.request_timeout_s * 1000),
            }
        )
        if cfg.testnet:
            self.ex.set_sandbox_mode(True)
        self.name = cfg.name
        self.cfg = cfg
        self._fill_handler: FillHandler | None = None
        self._seen_trades: set[str] = set()
        self._symbols: set[str] = set()

    def set_fill_handler(self, handler: FillHandler) -> None:
        self._fill_handler = handler

    # ------------------------------------------------------------ helpers
    async def _call(self, fn: str, *args: Any, **kwargs: Any) -> Any:
        """Llama a CCXT con timeout duro y traducción de errores."""
        c = self._ccxt
        try:
            return await asyncio.wait_for(
                getattr(self.ex, fn)(*args, **kwargs), timeout=self.cfg.request_timeout_s + 2
            )
        except TimeoutError as exc:
            raise NetworkError(f"{fn}: timeout") from exc
        except (c.RateLimitExceeded, c.DDoSProtection) as exc:
            raise RateLimitError(f"{fn}: {exc}") from exc
        except c.AuthenticationError as exc:
            raise AuthError(f"{fn}: {exc}") from exc
        except (c.InvalidOrder, c.InsufficientFunds, c.BadRequest) as exc:
            raise OrderRejectedError(f"{fn}: {exc}") from exc
        except c.OrderNotFound:
            return None
        except (c.NetworkError, c.ExchangeNotAvailable) as exc:
            raise NetworkError(f"{fn}: {exc}") from exc
        except c.ExchangeError as exc:
            raise ExchangeError(f"{fn}: {exc}") from exc

    def _to_order(self, o: dict[str, Any], fallback: Order | None = None) -> Order:
        cid = o.get("clientOrderId") or (fallback.client_order_id if fallback else "")
        typ = str(o.get("type") or "").lower()
        return Order(
            client_order_id=cid,
            symbol=o.get("symbol") or (fallback.symbol if fallback else ""),
            side=Side(o.get("side") or (fallback.side if fallback else "buy")),
            type=OrderType.STOP_MARKET
            if "stop" in typ
            else OrderType.LIMIT
            if typ == "limit"
            else OrderType.MARKET,
            qty=float(o.get("amount") or (fallback.qty if fallback else 0)),
            price=o.get("price"),
            stop_price=o.get("triggerPrice") or o.get("stopPrice"),
            status=_STATUS.get(str(o.get("status")), OrderStatus.UNKNOWN),
            filled_qty=float(o.get("filled") or 0),
            avg_fill_price=float(o.get("average") or 0),
            exchange_order_id=str(o.get("id")) if o.get("id") else None,
            updated_ts=int(o.get("lastUpdateTimestamp") or o.get("timestamp") or 0),
        )

    async def _is_spot(self, symbol: str) -> bool:
        if not getattr(self.ex, "markets", None):
            await self._call("load_markets")
        market = (self.ex.markets or {}).get(symbol, {})
        return bool(market.get("spot", True))

    # ---------------------------------------------------------------- API
    async def create_order(self, order: Order) -> Order:
        self._symbols.add(order.symbol)
        params: dict[str, Any] = {"clientOrderId": order.client_order_id}
        # reduceOnly solo existe en derivados; en spot el exchange lo rechaza.
        # En spot, "reduce-only" lo garantiza el OrderManager (qty <= posición).
        if order.reduce_only and not await self._is_spot(order.symbol):
            params["reduceOnly"] = True
        if order.tif is TimeInForce.IOC:
            params["timeInForce"] = "IOC"
        elif order.tif is TimeInForce.POST_ONLY:
            params["postOnly"] = True
        if order.type is OrderType.STOP_MARKET:
            params["triggerPrice"] = order.stop_price
            raw = await self._call(
                "create_order", order.symbol, "market", order.side.value, order.qty, None, params
            )
        else:
            raw = await self._call(
                "create_order",
                order.symbol,
                "limit" if order.type is OrderType.LIMIT else "market",
                order.side.value,
                order.qty,
                order.price,
                params,
            )
        return self._to_order(raw or {}, fallback=order)

    async def cancel_order(self, client_order_id: str, symbol: str) -> None:
        await self._call("cancel_order", None, symbol, {"clientOrderId": client_order_id})

    async def cancel_all_orders(self, symbol: str | None = None) -> None:
        symbols = [symbol] if symbol else sorted(self._symbols)
        for s in symbols:
            await self._call("cancel_all_orders", s)

    async def fetch_order(self, client_order_id: str, symbol: str) -> Order | None:
        # Binance acepta origClientOrderId; CCXT unifica 'clientOrderId' en varios exchanges.
        raw = await self._call(
            "fetch_order",
            None,
            symbol,
            {"clientOrderId": client_order_id, "origClientOrderId": client_order_id},
        )
        return self._to_order(raw) if raw else None

    async def fetch_open_orders(self) -> list[Order]:
        out: list[Order] = []
        for s in sorted(self._symbols):
            for raw in await self._call("fetch_open_orders", s) or []:
                out.append(self._to_order(raw))
        return out

    async def fetch_positions(self) -> dict[str, float]:
        """Derivados: fetch_positions. Spot: balance del activo base."""
        if self.ex.has.get("fetchPositions"):
            raw = await self._call("fetch_positions", sorted(self._symbols) or None)
            out: dict[str, float] = {}
            for p in raw or []:
                qty = float(p.get("contracts") or 0) * (1 if p.get("side") == "long" else -1)
                if qty:
                    out[p["symbol"]] = qty
            return out
        bal = await self._call("fetch_balance")
        out = {}
        for s in self._symbols:
            base = s.split("/")[0]
            qty = float((bal or {}).get("total", {}).get(base) or 0)
            if qty:
                out[s] = qty
        return out

    async def fetch_equity(self) -> float:
        bal = await self._call("fetch_balance")
        return float((bal or {}).get("total", {}).get("USDT") or 0)

    async def set_dead_man_switch(self, timeout_s: int) -> bool:
        if not self.ex.has.get("cancelAllOrdersAfter"):
            return False
        await self._call("cancel_all_orders_after", timeout_s * 1000)
        return True

    async def poll_fills(self, stop: asyncio.Event, interval_s: float = 2.0) -> None:
        """Loop de fills por polling (respaldo si no hay WS privado)."""
        since: int | None = None
        while not stop.is_set():
            for s in sorted(self._symbols):
                try:
                    trades = await self._call("fetch_my_trades", s, since) or []
                except ExchangeError as exc:
                    log.warning("poll_fills %s: %s", s, exc)
                    continue
                for t in trades:
                    tid = str(t.get("id"))
                    if tid in self._seen_trades:
                        continue
                    self._seen_trades.add(tid)
                    since = max(since or 0, int(t["timestamp"]))
                    fee = float((t.get("fee") or {}).get("cost") or 0)
                    fill = Fill(
                        client_order_id=str(t.get("clientOrderId") or t.get("order") or ""),
                        symbol=s,
                        side=Side(t["side"]),
                        qty=float(t["amount"]),
                        price=float(t["price"]),
                        fee=fee,
                        ts=int(t["timestamp"]),
                        is_maker=t.get("takerOrMaker") == "maker",
                        trade_id=tid,
                    )
                    if self._fill_handler:
                        await self._fill_handler(fill)
            await asyncio.sleep(interval_s)

    async def close(self) -> None:
        await self.ex.close()
