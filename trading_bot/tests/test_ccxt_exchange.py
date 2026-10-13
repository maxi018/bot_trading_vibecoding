"""Contrato con Binance spot vía CCXT, SIN red: se intercepta la capa HTTP de CCXT.

Verifica qué requests saldrían al exchange y cómo se interpretan sus respuestas. No
reemplaza probar en testnet, pero agarra los errores de "parámetro equivocado" antes.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("ccxt")

from quantbot.config import ExchangeConfig, Secrets
from quantbot.core.models import Fill, Order, OrderType, Side, TimeInForce
from quantbot.core.portfolio import Portfolio
from quantbot.data.websocket_feed import BinanceSpotAdapter
from quantbot.execution.ccxt_exchange import CcxtExchange


def _market(base: str, min_qty: str = "0.00001") -> dict[str, Any]:
    return {
        "symbol": f"{base}USDT",
        "status": "TRADING",
        "baseAsset": base,
        "baseAssetPrecision": 8,
        "quoteAsset": "USDT",
        "quotePrecision": 8,
        "quoteAssetPrecision": 8,
        "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT"],
        "isSpotTradingAllowed": True,
        "isMarginTradingAllowed": False,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "1e6",
                "tickSize": "0.01",
            },
            {"filterType": "LOT_SIZE", "minQty": min_qty, "maxQty": "9000", "stepSize": min_qty},
        ],
        "permissions": [],
        "permissionSets": [["SPOT"]],
    }


EXCHANGE_INFO = {
    "timezone": "UTC",
    "serverTime": 0,
    "rateLimits": [],
    "exchangeFilters": [],
    "symbols": [_market("BTC"), _market("ETH", "0.0001"), _market("BNB", "0.001")],
}


class FakeBinance:
    """Responde como Binance según el path y registra cada request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.routes: dict[str, Callable[[dict[str, str]], Any]] = {}

    async def fetch(
        self, url: str, method: str = "GET", headers: Any = None, body: Any = None
    ) -> Any:
        path, _, query = url.partition("?")
        params = dict(urllib.parse.parse_qsl(query or (body or "")))
        endpoint = path.split("/api/")[-1]
        self.requests.append((method, endpoint, params))
        if endpoint.endswith("exchangeInfo"):
            return EXCHANGE_INFO
        for suffix, handler in self.routes.items():
            if endpoint.endswith(suffix):
                return handler(params)
        return {}

    def sent(self, suffix: str) -> list[tuple[str, dict[str, str]]]:
        return [(m, p) for m, e, p in self.requests if e.endswith(suffix)]


@pytest.fixture
async def binance():
    ex = CcxtExchange(ExchangeConfig(testnet=True), Secrets(api_key="k", api_secret="s"))
    fake = FakeBinance()
    ex.ex.fetch = fake.fetch
    ex.ex.options["fetchCurrencies"] = False
    yield ex, fake
    await ex.close()


def _order(**kw: Any) -> Order:
    base: dict[str, Any] = dict(client_order_id="cid-1", symbol="BTC/USDT", qty=0.001)
    return Order(**(base | kw))


ORDER_ACK = {
    "symbol": "BTCUSDT",
    "orderId": 123,
    "clientOrderId": "cid-1",
    "transactTime": 0,
    "origQty": "0.001",
    "executedQty": "0",
    "status": "NEW",
    "timeInForce": "GTC",
    "type": "STOP_LOSS",
    "side": "SELL",
}


# ------------------------------------------------------------------ órdenes
async def test_protective_stop_is_a_spot_stop_loss_without_reduce_only(binance):
    ex, fake = binance
    fake.routes["v3/order"] = lambda p: ORDER_ACK
    o = await ex.create_order(
        _order(side=Side.SELL, type=OrderType.STOP_MARKET, stop_price=60000.0, reduce_only=True)
    )
    ((method, sent),) = fake.sent("v3/order")
    assert method == "POST"
    assert sent["type"] == "STOP_LOSS" and sent["stopPrice"] == "60000"
    assert sent["newClientOrderId"] == "cid-1"  # idempotencia: el id lo ponemos nosotros
    assert "reduceOnly" not in sent  # spot no lo acepta; lo garantiza el OrderManager
    assert o.exchange_order_id == "123"  # para mapear fills que solo traen orderId


async def test_ioc_limit_entry(binance):
    ex, fake = binance
    fake.routes["v3/order"] = lambda p: ORDER_ACK | {"type": "LIMIT", "side": "BUY"}
    await ex.create_order(
        _order(side=Side.BUY, type=OrderType.LIMIT, price=61000.0, tif=TimeInForce.IOC)
    )
    ((_, sent),) = fake.sent("v3/order")
    assert sent["type"] == "LIMIT" and sent["timeInForce"] == "IOC" and sent["price"] == "61000"


async def test_spot_has_no_dead_man_switch(binance):
    ex, _ = binance
    assert await ex.set_dead_man_switch(60) is False  # lo cubre el watchdog remoto


# ---------------------------------------------------------------- posiciones
async def test_spot_positions_come_from_balance_not_futures(binance):
    """Bug real: Binance declara fetchPositions (por futuros) y el adapter lo usaba en
    spot: "sin posiciones" con BTC en la cuenta -> kill falso y flatten que no cierra."""
    ex, fake = binance
    fake.routes["v3/account"] = lambda p: {
        "balances": [
            {"asset": "BTC", "free": "0.4", "locked": "0.1"},
            {"asset": "ETH", "free": "0.00002", "locked": "0"},  # polvo < mínimo (0.0001)
            {"asset": "USDT", "free": "1000", "locked": "0"},
        ],
        "accountType": "SPOT",
    }
    ex._symbols |= {"BTC/USDT", "ETH/USDT"}
    assert await ex.fetch_positions() == {"BTC/USDT": pytest.approx(0.5)}
    assert not [r for r in fake.requests if "fapi" in r[1]]  # ni posiciones ni mercados


# --------------------------------------------------------------------- fills
def _trade(tid: int, ts: int, commission: str, asset: str, symbol: str = "BTCUSDT") -> dict:
    return {
        "symbol": symbol,
        "id": tid,
        "orderId": 123,
        "price": "60000",
        "qty": "0.01",
        "quoteQty": "600",
        "commission": commission,
        "commissionAsset": asset,
        "time": ts,
        "isBuyer": True,
        "isMaker": False,
        "isBestMatch": True,
    }


async def _poll_once(ex: CcxtExchange) -> list[Fill]:
    got: list[Fill] = []

    async def handler(f: Fill) -> None:
        got.append(f)

    ex.set_fill_handler(handler)
    stop = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(ex.poll_fills(stop, interval_s=0.01), stopper())
    return got


async def test_fee_in_base_asset_is_reported_as_base_fee(binance):
    ex, fake = binance
    ex._symbols.add("BTC/USDT")
    fake.routes["v3/myTrades"] = lambda p: [_trade(1, 1000, "0.00001", "BTC")]
    (fill,) = await _poll_once(ex)
    assert fill.base_fee == pytest.approx(0.00001)
    assert fill.fee == pytest.approx(0.00001 * 60000)  # valuado en USDT
    assert fill.client_order_id == "123"  # orderId: el OrderManager lo mapea al nuestro


async def test_fee_in_quote_and_in_bnb(binance):
    ex, fake = binance
    ex._symbols.add("BTC/USDT")
    fake.routes["v3/myTrades"] = lambda p: [
        _trade(1, 1000, "0.6", "USDT"),
        _trade(2, 2000, "0.002", "BNB"),
    ]
    fake.routes["v3/ticker/24hr"] = lambda p: {"symbol": "BNBUSDT", "lastPrice": "500"}
    usdt, bnb = await _poll_once(ex)
    assert usdt.fee == pytest.approx(0.6) and usdt.base_fee == 0
    assert bnb.fee == pytest.approx(0.002 * 500) and bnb.base_fee == 0


async def test_trade_cursor_is_per_symbol(binance):
    """Bug real: un cursor compartido usaba el último trade de BTC como `since` de ETH."""
    ex, fake = binance
    ex._symbols |= {"BTC/USDT", "ETH/USDT"}
    fake.routes["v3/myTrades"] = lambda p: (
        [_trade(1, 5000, "0.6", "USDT")] if p.get("symbol") == "BTCUSDT" else []
    )
    await _poll_once(ex)
    await _poll_once(ex)  # segunda vuelta: ya hay cursor para BTC, no para ETH

    def since(symbol: str) -> list[str | None]:
        return [p.get("startTime") for _, p in fake.sent("v3/myTrades") if p["symbol"] == symbol]

    assert since("ETHUSDT") and all(s is None for s in since("ETHUSDT"))
    assert since("BTCUSDT")[-1] == "5000"


# ------------------------------------------------- portfolio con fee en base
def test_portfolio_position_matches_exchange_balance_with_base_fee():
    p = Portfolio(10_000)
    p.apply_fill(Fill("c", "BTC/USDT", Side.BUY, 0.01, 60_000, 0.6, 1, base_fee=0.00001))
    assert p.position("BTC/USDT").qty == pytest.approx(0.00999)  # lo que el exchange tiene
    assert p.cash == pytest.approx(10_000 - 600)  # el fee no salió de la caja
    assert p.equity == pytest.approx(10_000 - 0.6)  # pero se descontó una sola vez
    p.apply_fill(Fill("d", "BTC/USDT", Side.SELL, 0.00999, 60_000, 0.6, 2))
    assert p.position("BTC/USDT").qty == 0
    assert p.closed_trades[-1].pnl == pytest.approx(-1.2)  # dos fees, nada más


# --------------------------------------------------------- market data testnet
@pytest.mark.parametrize(
    ("mode", "testnet", "expected"),
    [
        ("live", True, BinanceSpotAdapter.TESTNET_REST),
        ("live", False, BinanceSpotAdapter.REST),
        ("paper", True, BinanceSpotAdapter.REST),  # paper simula contra el mercado real
    ],
)
def test_market_data_endpoints(mode, testnet, expected):
    ws, rest = BinanceSpotAdapter.endpoints(mode, ExchangeConfig(testnet=testnet))
    assert rest == expected
    assert ("testnet" in ws) == (expected == BinanceSpotAdapter.TESTNET_REST)


# ------------------------------------------------- testnet-check de punta a punta
class StatefulBinance(FakeBinance):
    """Binance spot mínimo con estado: ejecuta IOC al precio actual (fee 0.1 %: en BTC al
    comprar, en USDT al vender), deja stops abiertos y mantiene saldos y trades."""

    PX = 60_000.0

    def __init__(self, usdt: float = 1000.0) -> None:
        super().__init__()
        self.bal = {"BTC": 0.0, "USDT": usdt}
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.routes.update(
            {
                "v3/ticker/24hr": lambda p: {"symbol": "BTCUSDT", "lastPrice": str(self.PX)},
                "v3/account": lambda p: {
                    "balances": [
                        {"asset": a, "free": f"{v:.8f}", "locked": "0"} for a, v in self.bal.items()
                    ]
                },
                "v3/myTrades": lambda p: [
                    t for t in self.trades if t["time"] >= int(p.get("startTime", 0))
                ],
                "v3/openOrders": self._open_orders,
            }
        )

    async def fetch(
        self, url: str, method: str = "GET", headers: Any = None, body: Any = None
    ) -> Any:
        if url.split("?")[0].endswith("v3/openOrders") and method == "DELETE":
            self.requests.append((method, "v3/openOrders", {}))
            live = [o for o in self.orders.values() if o["status"] == "NEW"]
            for o in live:
                o["status"] = "CANCELED"
            return live
        if url.split("?")[0].endswith("v3/order"):
            _, _, query = url.partition("?")
            params = dict(urllib.parse.parse_qsl(query or (body or "")))
            self.requests.append((method, "v3/order", params))
            if method == "POST":
                return self._new_order(params)
            o = self.orders[params["origClientOrderId"]]
            if method == "DELETE":
                o["status"] = "CANCELED"
            return o
        return await super().fetch(url, method, headers, body)

    def _open_orders(self, p: dict[str, str]) -> list[dict[str, Any]]:
        return [o for o in self.orders.values() if o["status"] == "NEW"]

    def _new_order(self, p: dict[str, str]) -> dict[str, Any]:
        qty = float(p["quantity"])
        o = {
            "symbol": "BTCUSDT",
            "orderId": 1000 + len(self.orders),
            "clientOrderId": p["newClientOrderId"],
            "price": p.get("price", "0"),
            "origQty": p["quantity"],
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "status": "NEW",
            "timeInForce": p.get("timeInForce", "GTC"),
            "type": p["type"],
            "side": p["side"],
            "stopPrice": p.get("stopPrice", "0"),
            "time": 1,
            "updateTime": 1,
        }
        if p["type"] == "LIMIT":
            buy = p["side"] == "BUY"
            fee, fee_asset = (qty * 0.001, "BTC") if buy else (qty * self.PX * 0.001, "USDT")
            self.bal["BTC"] += qty - (fee if buy else 0) if buy else -qty
            self.bal["USDT"] += -qty * self.PX if buy else qty * self.PX - fee
            self.trades.append(
                {
                    "symbol": "BTCUSDT",
                    "id": len(self.trades) + 1,
                    "orderId": o["orderId"],
                    "price": str(self.PX),
                    "qty": p["quantity"],
                    "quoteQty": str(qty * self.PX),
                    "commission": f"{fee:.8f}",
                    "commissionAsset": fee_asset,
                    "time": 10 + len(self.trades),
                    "isBuyer": buy,
                    "isMaker": False,
                    "isBestMatch": True,
                }
            )
            o |= {"executedQty": p["quantity"], "status": "FILLED"}
        self.orders[o["clientOrderId"]] = o
        return o


async def test_testnet_check_full_cycle_against_stateful_exchange():
    from quantbot.execution.testnet_check import run_checks

    ex = CcxtExchange(ExchangeConfig(testnet=True), Secrets(api_key="k", api_secret="s"))
    fake = StatefulBinance()
    ex.ex.fetch = fake.fetch
    ex.ex.options["fetchCurrencies"] = False
    try:
        results = await run_checks(ex, notional=15.0, wait_s=2.0, poll_s=0.05)
    finally:
        await ex.close()
    report = "\n".join(r.line() for r in results)
    assert all(r.ok for r in results), report
    assert len(results) == 9, report
    assert fake.bal["BTC"] < 0.00001  # volvió a la posición inicial (solo polvo)
    assert [o["status"] for o in fake.orders.values()] == ["FILLED", "CANCELED", "FILLED"]


async def test_testnet_check_refuses_mainnet():
    from quantbot.execution.testnet_check import run_checks

    ex = CcxtExchange(ExchangeConfig(testnet=False), Secrets(api_key="k", api_secret="s"))
    try:
        with pytest.raises(RuntimeError, match="testnet"):
            await run_checks(ex)
    finally:
        await ex.close()
