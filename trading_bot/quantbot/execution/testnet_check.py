"""Chequeo guiado contra la TESTNET de Binance: el ciclo de vida real de una orden.

    python -m quantbot testnet-check            (QB_API_KEY / QB_API_SECRET de testnet)

Con un monto chico (default 15 USDT) recorre lo que el bot hace en live y que ningún
simulador puede garantizar: entrada IOC, fill por polling con su fee, posición contra
el saldo real, stop nativo, consulta y cancelación por NUESTRO clientOrderId, y vuelta
a la posición inicial. Siempre intenta dejar la cuenta como estaba, aunque algo falle.

Fuerza ``testnet = true``: no hay forma de correrlo contra dinero real.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..core.models import Fill, Order, OrderStatus, OrderType, Side, TimeInForce
from .base import ExchangeError
from .ccxt_exchange import CcxtExchange


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return f"{'✔' if self.ok else '✖'} {self.name}" + (
            f": {self.detail}" if self.detail else ""
        )


async def _wait(cond: Callable[[], bool], timeout_s: float, step_s: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(step_s)
    return cond()


async def run_checks(
    ex: CcxtExchange,
    symbol: str = "BTC/USDT",
    notional: float = 15.0,
    wait_s: float = 15.0,
    poll_s: float = 1.0,
) -> list[Check]:
    if not ex.cfg.testnet:
        raise RuntimeError("testnet-check solo corre con testnet = true")
    results: list[Check] = []

    def record(name: str, ok: bool, detail: str = "") -> bool:
        results.append(Check(name, ok, detail))
        return ok

    await ex._call("load_markets")
    px = float((await ex._call("fetch_ticker", symbol) or {}).get("last") or 0)
    equity = await ex.fetch_equity()
    if not record(
        "Conexión, credenciales y saldo",
        px > 0 and equity >= 2 * notional,
        f"USDT={equity:.2f}, {symbol}={px}",
    ):
        return results

    ex._symbols.add(symbol)
    min_step = float(
        ((ex.ex.markets[symbol].get("limits") or {}).get("amount") or {}).get("min") or 0
    )
    pos_before = (await ex.fetch_positions()).get(symbol, 0.0)
    fills: list[Fill] = []

    async def collect(f: Fill) -> None:
        fills.append(f)

    ex.set_fill_handler(collect)
    stop_polling = asyncio.Event()
    poller = asyncio.create_task(ex.poll_fills(stop_polling, interval_s=poll_s))
    tag = f"qbchk{int(time.time())}"
    try:
        # 1) Entrada: limit IOC agresiva (como la "market con protección" del bot)
        qty = float(ex.ex.amount_to_precision(symbol, notional / px))
        entry = Order(
            f"{tag}in",
            symbol,
            Side.BUY,
            OrderType.LIMIT,
            qty,
            price=float(ex.ex.price_to_precision(symbol, px * 1.01)),
            tif=TimeInForce.IOC,
        )
        ack = await ex.create_order(entry)
        if not record(
            "Entrada limit IOC ejecutada",
            ack.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) or ack.filled_qty > 0,
            f"status={ack.status} ejecutado={ack.filled_qty}",
        ):
            return results
        mine = {entry.client_order_id, ack.exchange_order_id}

        # 2) El fill llega por polling, mapeable a nuestra orden, con su fee
        got = await _wait(lambda: any(f.client_order_id in mine for f in fills), wait_s)
        entry_fills = [f for f in fills if f.client_order_id in mine]
        bought = sum(f.qty for f in entry_fills)
        base_fee = sum(f.base_fee for f in entry_fills)
        record(
            "Fill recibido por polling (orderId -> nuestra orden)",
            got and bought > 0,
            f"{len(entry_fills)} fill(s), {bought} @ fee {sum(f.fee for f in entry_fills):.6f} "
            f"USDT (en base: {base_fee})",
        )

        # 3) La posición que ve el bot = saldo real (fee en BTC incluido)
        pos = (await ex.fetch_positions()).get(symbol, 0.0)
        expected = pos_before + bought - base_fee
        record(
            "Posición = saldo real del exchange",
            abs(pos - expected) <= max(min_step, 1e-9),
            f"exchange={pos} esperado={expected}",
        )

        # 4) Stop nativo por lo que REALMENTE se tiene (no por lo comprado)
        held = float(ex.ex.amount_to_precision(symbol, pos - pos_before))
        stop = Order(
            f"{tag}stop",
            symbol,
            Side.SELL,
            OrderType.STOP_MARKET,
            held,
            stop_price=float(ex.ex.price_to_precision(symbol, px * 0.9)),
            reduce_only=True,
        )
        sack = await ex.create_order(stop)
        record("Stop nativo aceptado", sack.status is OrderStatus.OPEN, f"status={sack.status}")

        # 5) Se encuentra por NUESTRO id (idempotencia y reconciliación dependen de esto)
        opens = await ex.fetch_open_orders()
        found = await ex.fetch_order(stop.client_order_id, symbol)
        record(
            "Stop visible por clientOrderId",
            any(o.client_order_id == stop.client_order_id for o in opens)
            and found is not None
            and found.status is OrderStatus.OPEN,
            f"en open orders: {len(opens)}, fetch_order: {found.status if found else None}",
        )

        # 6) Cancelación por clientOrderId
        await ex.cancel_order(stop.client_order_id, symbol)
        after = await ex.fetch_order(stop.client_order_id, symbol)
        record(
            "Stop cancelado por clientOrderId",
            after is not None and after.status is OrderStatus.CANCELED,
            f"status={after.status if after else None}",
        )

        # Informativo: el dead-man switch nativo no existe en spot
        dms = await ex.set_dead_man_switch(60)
        record(
            "Dead-man switch nativo",
            True,
            "soportado" if dms else "no soportado en spot: usar deploy/remote_watchdog.py",
        )
    except ExchangeError as exc:
        record("Error del exchange", False, str(exc))
    finally:
        # 7) Limpieza: cancelar lo nuestro y vender lo comprado, pase lo que pase
        try:
            await ex.cancel_all_orders(symbol)
            extra = (await ex.fetch_positions()).get(symbol, 0.0) - pos_before
            sell = float(ex.ex.amount_to_precision(symbol, max(extra, 0.0)))
            if sell >= max(min_step, 1e-12):
                await ex.create_order(
                    Order(
                        f"{tag}out",
                        symbol,
                        Side.SELL,
                        OrderType.LIMIT,
                        sell,
                        price=float(ex.ex.price_to_precision(symbol, px * 0.99)),
                        tif=TimeInForce.IOC,
                        reduce_only=True,
                    )
                )
            left = (await ex.fetch_positions()).get(symbol, 0.0) - pos_before
            record(
                "Limpieza: posición como al empezar",
                abs(left) <= max(min_step, 1e-9),
                f"sobrante={left}",
            )
        except ExchangeError as exc:
            record("Limpieza", False, f"{exc} (revisar la cuenta de testnet a mano)")
        stop_polling.set()
        await poller
    return results
