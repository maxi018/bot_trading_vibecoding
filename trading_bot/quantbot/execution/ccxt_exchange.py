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
import time
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
        if cfg.name == "binance":
            # El bot opera SPOT (feed, stops sin reduce-only). Cargar también los mercados
            # de futuros es un request extra que en testnet CCXT ya no soporta.
            self.ex.options.setdefault("fetchMarkets", {})["types"] = ["spot"]
        if cfg.testnet:
            self.ex.set_sandbox_mode(True)
        self.name = cfg.name
        self.cfg = cfg
        self._fill_handler: FillHandler | None = None
        self._seen_trades: set[str] = set()
        self._symbols: set[str] = set()
        self._since: dict[str, int] = {}  # cursor de fetch_my_trades POR símbolo
        self._fee_px: dict[str, tuple[float, float]] = {}  # "BNB/USDT" -> (precio, monotonic)

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
        """Spot: saldo del activo base. Derivados: fetch_positions.

        Se decide POR MERCADO, no por ``has["fetchPositions"]``: Binance lo declara True
        (por sus futuros) y usarlo para spot devolvía "sin posiciones" con BTC en la
        cuenta. Con eso la reconciliación disparaba el kill y ``flatten_all``, que usa
        estas posiciones como la verdad, no cerraba nada.
        """
        spot: list[str] = []
        deriv: list[str] = []
        for s in sorted(self._symbols):
            (spot if await self._is_spot(s) else deriv).append(s)
        out: dict[str, float] = {}
        if deriv:
            for p in await self._call("fetch_positions", deriv) or []:
                qty = float(p.get("contracts") or 0) * (1 if p.get("side") == "long" else -1)
                if qty:
                    out[p["symbol"]] = qty
        if spot:
            bal = await self._call("fetch_balance")
            for s in spot:
                qty = float((bal or {}).get("total", {}).get(s.split("/")[0]) or 0)
                # Polvo que quedó de fees: no se puede operar (menor al mínimo del
                # mercado) y no es una posición; contarlo daría discrepancias eternas.
                min_qty = (
                    ((self.ex.markets or {}).get(s, {}).get("limits") or {}).get("amount") or {}
                ).get("min") or 0
                if qty > 0 and qty >= float(min_qty):
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

    async def _fee_rate(self, currency: str, quote: str) -> float:
        """Precio de un activo de fee ajeno al par (p.ej. BNB) en la moneda de cotización."""
        pair = f"{currency}/{quote}"
        cached = self._fee_px.get(pair)
        if cached and time.monotonic() - cached[1] < 300:
            return cached[0]
        try:
            ticker = await self._call("fetch_ticker", pair)
            px = float((ticker or {}).get("last") or 0)
        except ExchangeError as exc:
            log.warning("Sin precio para valuar fee en %s: %s (se registra 0)", currency, exc)
            px = 0.0
        self._fee_px[pair] = (px, time.monotonic())
        return px

    async def _parse_fees(self, t: dict[str, Any], symbol: str) -> tuple[float, float]:
        """Devuelve (fee en moneda de cotización, parte cobrada en el activo base)."""
        base, quote = symbol.split("/")[0], symbol.split("/")[1].split(":")[0]
        fees = t.get("fees") or ([t["fee"]] if t.get("fee") else [])
        fee_quote = base_fee = 0.0
        for f in fees:
            cost = float((f or {}).get("cost") or 0)
            cur = (f or {}).get("currency")
            if not cost:
                continue
            if cur is None or cur == quote:
                fee_quote += cost
            elif cur == base:
                base_fee += cost
                fee_quote += cost * float(t["price"])
            else:
                fee_quote += cost * await self._fee_rate(cur, quote)
        return fee_quote, base_fee

    async def poll_fills(self, stop: asyncio.Event, interval_s: float = 2.0) -> None:
        """Loop de fills por polling (respaldo si no hay WS privado)."""
        while not stop.is_set():
            for s in sorted(self._symbols):
                try:
                    trades = await self._call("fetch_my_trades", s, self._since.get(s)) or []
                except ExchangeError as exc:
                    log.warning("poll_fills %s: %s", s, exc)
                    continue
                for t in trades:
                    tid = str(t.get("id"))
                    if tid in self._seen_trades:
                        continue
                    self._seen_trades.add(tid)
                    # Cursor por símbolo: uno compartido salteaba trades de un símbolo
                    # más viejos que el último trade visto en OTRO símbolo.
                    self._since[s] = max(self._since.get(s, 0), int(t["timestamp"]))
                    fee, base_fee = await self._parse_fees(t, s)
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
                        base_fee=base_fee,
                    )
                    if self._fill_handler:
                        await self._fill_handler(fill)
            await asyncio.sleep(interval_s)

    async def close(self) -> None:
        await self.ex.close()
