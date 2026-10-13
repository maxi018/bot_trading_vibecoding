"""Order Manager: idempotencia por clientOrderId, stops nativos, reconciliación, flatten."""

from __future__ import annotations

import pytest

from quantbot.config import CostConfig
from quantbot.core.models import (
    Fill,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from quantbot.core.portfolio import Portfolio
from quantbot.execution.cost_model import CostModel
from quantbot.execution.order_manager import OrderManager
from quantbot.execution.paper import PaperExchange
from quantbot.execution.rate_limiter import NullRateLimiter, TokenBucket

from .conftest import warm_risk


@pytest.fixture
def setup(risk_factory, clock):
    risk = risk_factory()
    px = warm_risk(risk, clock)
    pf = Portfolio(10_000)
    ex = PaperExchange(clock, CostModel(CostConfig()), 10_000, "immediate")
    ex.on_price("BTC/USDT", px)
    om = OrderManager(ex, risk, pf, clock, NullRateLimiter(), retry_base_delay_s=0)
    return om, ex, pf, risk, px


def buy(px, qty=1.0):
    return OrderRequest(
        "BTC/USDT",
        Side.BUY,
        qty,
        OrderType.LIMIT,
        price=px * 1.01,
        tif=TimeInForce.IOC,
        decision_price=px,
        tag="entry",
    )


async def test_fill_updates_portfolio_and_places_native_stop(setup):
    om, ex, pf, _, px = setup
    om.set_desired_stop("BTC/USDT", px * 0.95)
    o = await om.submit(buy(px))
    assert o.status is OrderStatus.FILLED
    assert pf.position("BTC/USDT").qty == pytest.approx(1.0)
    stops = [x for x in ex.orders.values() if x.type is OrderType.STOP_MARKET and not x.is_terminal]
    assert len(stops) == 1 and stops[0].reduce_only and stops[0].qty == pytest.approx(1.0)


async def test_trailing_stop_replaces_new_first_then_cancels_old(setup):
    om, ex, _, _, px = setup
    om.set_desired_stop("BTC/USDT", px * 0.95)
    await om.submit(buy(px))
    old = om.stop_orders["BTC/USDT"]
    om.set_desired_stop("BTC/USDT", px * 0.97)
    await om.sync_protective_stop("BTC/USDT")
    new = om.stop_orders["BTC/USDT"]
    assert new != old
    assert ex.orders[old].status is OrderStatus.CANCELED
    assert ex.orders[new].stop_price == pytest.approx(px * 0.97)
    live = [x for x in ex.orders.values() if x.type is OrderType.STOP_MARKET and not x.is_terminal]
    assert len(live) == 1  # nunca dos stops vivos al final, nunca cero en el medio


async def test_timeout_but_order_arrived_is_not_duplicated(setup):
    om, ex, pf, _, px = setup
    ex.fail_after_accept = True  # la orden llega, la respuesta se pierde
    o = await om.submit(buy(px))
    assert o.status is OrderStatus.FILLED
    entries = [x for x in ex.orders.values() if x.type is OrderType.LIMIT]
    assert len(entries) == 1  # consultó por clientOrderId en vez de duplicar
    assert pf.position("BTC/USDT").qty == pytest.approx(1.0)


async def test_timeout_order_lost_is_retried_with_same_id(setup):
    om, ex, _, _, px = setup
    ex.fail_next_creates = 2
    o = await om.submit(buy(px))
    assert o.status is OrderStatus.FILLED
    assert list(ex.orders) == [o.client_order_id]  # mismo id, una sola orden


async def test_resending_same_client_id_is_idempotent(setup):
    _, ex, _, _, px = setup
    from quantbot.core.models import Order

    o = Order(
        "same-id", "BTC/USDT", Side.BUY, OrderType.LIMIT, 1.0, price=px * 2, tif=TimeInForce.GTC
    )
    await ex.create_order(o)
    await ex.create_order(o)
    assert len(ex.orders) == 1


async def test_rejection_is_recorded_for_reject_rate(setup):
    om, ex, _, risk, px = setup
    ex.reject_predicate = lambda _o: "insufficient balance"
    o = await om.submit(buy(px))
    assert o.status is OrderStatus.REJECTED and "insufficient" in o.reject_reason
    assert list(risk._order_outcomes) == [True]


async def test_duplicate_fill_events_are_ignored(setup):
    om, _, pf, _, px = setup
    f = Fill("x", "BTC/USDT", Side.BUY, 1.0, px, 0.1, 1, trade_id="T1")
    await om.on_fill(f)
    await om.on_fill(f)  # reenvío tras reconexión del WS
    assert pf.position("BTC/USDT").qty == pytest.approx(1.0)


async def test_reconcile_requires_two_strikes(setup):
    om, ex, _pf, _, _px = setup
    ex.positions["BTC/USDT"] = 3.0  # el exchange dice otra cosa (p.ej. orden manual)
    assert await om.reconcile() is None  # primer strike: puede ser un fill en vuelo
    details = await om.reconcile()
    assert details and "BTC/USDT" in details


async def test_reconcile_resolves_unknown_and_cancels_orphans(setup):
    om, ex, _, _, px = setup
    from quantbot.core.models import Order

    orphan = Order("qb-orphan-1", "BTC/USDT", Side.BUY, OrderType.LIMIT, 1, price=px * 0.5)
    await ex.create_order(orphan)
    assert await om.reconcile() is None
    assert ex.orders["qb-orphan-1"].status is OrderStatus.CANCELED


async def test_flatten_uses_exchange_positions_and_keeps_stop_until_flat(setup):
    om, ex, pf, _, px = setup
    om.set_desired_stop("BTC/USDT", px * 0.95)
    await om.submit(buy(px, 2.0))
    await om.flatten_all()
    assert pf.position("BTC/USDT").qty == pytest.approx(0.0)
    assert ex.positions["BTC/USDT"] == pytest.approx(0.0)
    assert not [o for o in ex.orders.values() if not o.is_terminal]  # stop cancelado al final


async def test_token_bucket_limits_rate():
    t = [0.0]
    tb = TokenBucket(rate_per_s=2, burst=2, time_fn=lambda: t[0])
    assert tb.try_acquire() and tb.try_acquire()
    assert not tb.try_acquire()
    t[0] += 0.5
    assert tb.try_acquire()
    tb.penalize(5)
    t[0] += 1
    assert not tb.try_acquire()


async def test_failed_exit_restores_last_stop(setup):
    """Spot: se libera el stop para salir; si la salida IOC no se llena, el stop vuelve."""
    om, ex, pf, _, px = setup
    om.set_desired_stop("BTC/USDT", px * 0.95)
    await om.submit(buy(px))
    om.set_desired_stop("BTC/USDT", None)  # señal FLAT
    await om.release_stop("BTC/USDT")
    exit_req = OrderRequest(
        "BTC/USDT",
        Side.SELL,
        1.0,
        OrderType.LIMIT,
        price=px * 2,
        reduce_only=True,
        tif=TimeInForce.IOC,
        decision_price=px,
    )
    o = await om.submit(exit_req)  # límite absurdo: no se llena
    assert o.status is OrderStatus.EXPIRED
    await om.sync_protective_stop("BTC/USDT")
    live = [x for x in ex.orders.values() if x.type is OrderType.STOP_MARKET and not x.is_terminal]
    assert len(live) == 1 and live[0].stop_price == pytest.approx(px * 0.95)
    assert pf.position("BTC/USDT").qty == pytest.approx(1.0)


async def test_stop_sync_waits_for_fills_in_flight(setup):
    """Bug real hallado en modo demo: con fills encolados (single writer), el portfolio
    local va atrasado; re-colocar el stop en ese momento lo hace rechazar."""
    om, ex, pf, risk, px = setup
    queued = []

    async def enqueue(f):  # como en TradingApp: los fills van a una cola
        queued.append(f)

    om.set_desired_stop("BTC/USDT", px * 0.95)
    await om.submit(buy(px))
    ex.set_fill_handler(enqueue)
    om.set_desired_stop("BTC/USDT", None)
    await om.release_stop("BTC/USDT")
    exit_req = OrderRequest(
        "BTC/USDT",
        Side.SELL,
        1.0,
        OrderType.LIMIT,
        price=px * 0.99,
        reduce_only=True,
        tif=TimeInForce.IOC,
        decision_price=px,
    )
    await om.submit(exit_req)  # se llena en el exchange; el fill queda en cola
    assert queued and pf.position("BTC/USDT").qty == pytest.approx(1.0)  # local atrasado
    await om.sync_protective_stop("BTC/USDT")
    assert not [o for o in ex.orders.values() if o.status is OrderStatus.REJECTED]
    for f in queued:
        await om.on_fill(f)
    assert pf.position("BTC/USDT").qty == pytest.approx(0.0)
    assert not [o for o in ex.orders.values() if not o.is_terminal]
    assert list(risk._order_outcomes).count(True) == 0


async def test_reduce_only_is_sized_against_exchange_truth(setup):
    """El stop se ejecutó en el exchange pero el fill todavía no llegó: la salida
    se omite en vez de ser rechazada (y no infla la tasa de rechazos)."""
    om, ex, _pf, risk, px = setup
    await om.submit(buy(px))
    ex.positions["BTC/USDT"] = 0.0  # el exchange ya está plano
    o = await om.submit(
        OrderRequest(
            "BTC/USDT",
            Side.SELL,
            1.0,
            OrderType.LIMIT,
            price=px * 0.99,
            reduce_only=True,
            tif=TimeInForce.IOC,
            decision_price=px,
            tag="exit",
        )
    )
    assert o.status is OrderStatus.CANCELED and "nada que reducir" in o.reject_reason
    assert True not in risk._order_outcomes
    ex.positions["BTC/USDT"] = 0.4  # parcialmente cerrado: se recorta la cantidad
    o2 = await om.submit(
        OrderRequest(
            "BTC/USDT",
            Side.SELL,
            1.0,
            OrderType.LIMIT,
            price=px * 0.99,
            reduce_only=True,
            tif=TimeInForce.IOC,
            decision_price=px,
            tag="exit",
        )
    )
    assert o2.qty == pytest.approx(0.4)
