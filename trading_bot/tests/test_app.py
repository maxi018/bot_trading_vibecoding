"""Main event loop: preflight, consumo de eventos y safe shutdown ante fallas."""

from __future__ import annotations

import asyncio

import pytest

from quantbot.app import TradingApp
from quantbot.config import Secrets, load_config
from quantbot.core.alerting import Severity
from quantbot.core.kill_switch import KillSwitch
from quantbot.core.models import OrderStatus, OrderType, Side, TimeInForce
from quantbot.data.websocket_feed import Heartbeat


class FakeFeed:
    """Feed controlado por el test."""

    def __init__(self, app_ref: list, events: list, crash: Exception | None = None) -> None:
        self._app_ref = app_ref
        self.events = events
        self.crash = crash
        self.reconnects = 0

    async def run(self, stop: asyncio.Event) -> None:
        app = self._app_ref[0]
        for ev in self.events:
            await app._enqueue(ev)
        await asyncio.sleep(0.05)
        if self.crash:
            raise self.crash
        stop.set()


def make_app(tmp_path, events=(), crash=None, mode="paper"):
    cfg = load_config(mode=mode, state_dir=str(tmp_path / "state"))
    ref: list = []
    app = TradingApp(cfg, Secrets(), feed=FakeFeed(ref, list(events), crash))  # type: ignore[arg-type]
    ref.append(app)
    return app


async def test_refuses_to_start_with_kill_switch(tmp_path):
    KillSwitch(tmp_path / "state").engage("DD total", flatten=True)
    app = make_app(tmp_path)
    assert await app.run() == 2
    assert any(
        a.severity is Severity.CRITICAL and "kill-switch" in a.title for a in app.alerter.sent
    )


async def test_live_requires_explicit_confirmation(tmp_path, monkeypatch):
    monkeypatch.delenv("QB_LIVE_CONFIRM", raising=False)
    cfg = load_config(mode="live", state_dir=str(tmp_path / "s"))
    from quantbot.core.clock import LiveClock
    from quantbot.execution.cost_model import CostModel
    from quantbot.execution.paper import PaperExchange  # exchange inyectado (sin red)

    ex = PaperExchange(LiveClock(), CostModel(cfg.costs), 1000, "immediate")
    app = TradingApp(
        cfg, Secrets(api_key="k", api_secret="s"), exchange=ex, feed=FakeFeed([None], [])
    )  # type: ignore[arg-type]
    assert not await app.preflight()


async def test_clean_run_processes_events_and_stops(tmp_path):
    app = make_app(tmp_path, events=[Heartbeat("BTC/USDT", 0)])
    code = await asyncio.wait_for(app.run(), timeout=10)
    assert code == 0
    assert app.queue.empty()


async def test_crash_triggers_safe_shutdown_keeping_native_stops(tmp_path):
    app = make_app(tmp_path, crash=RuntimeError("bug inesperado"))
    ex = app.exchange
    ex.on_price("BTC/USDT", 100.0)  # type: ignore[attr-defined]
    # Estado previo: posición con stop nativo + una entrada límite pendiente
    app.orders.set_desired_stop("BTC/USDT", 95.0)
    from quantbot.core.models import OrderRequest

    await app.orders.submit(
        OrderRequest(
            "BTC/USDT",
            Side.BUY,
            1.0,
            OrderType.LIMIT,
            price=101,
            tif=TimeInForce.IOC,
            decision_price=100,
        )
    )
    # El fill llegó por la cola (single writer): procesarlo
    while not app.queue.empty():
        await app._dispatch(app.queue.get_nowait())
    pending_entry = await app.orders.submit(
        OrderRequest("BTC/USDT", Side.BUY, 1.0, OrderType.LIMIT, price=90, tif=TimeInForce.GTC)
    )
    assert pending_entry.status is OrderStatus.OPEN

    code = await asyncio.wait_for(app.run(), timeout=10)

    assert code == 1
    stops = [o for o in ex.orders.values() if o.type is OrderType.STOP_MARKET]  # type: ignore[attr-defined]
    assert stops and all(not o.is_terminal for o in stops)  # posición sigue protegida
    assert ex.orders[pending_entry.client_order_id].status is OrderStatus.CANCELED  # type: ignore[attr-defined]
    assert any(a.severity is Severity.CRITICAL and "detenido" in a.title for a in app.alerter.sent)


async def test_reconcile_mismatch_kills_via_event_loop(tmp_path):
    app = make_app(tmp_path)
    app.exchange.positions["BTC/USDT"] = 1.0  # type: ignore[attr-defined]
    from quantbot.app import _ReconcileTick

    await app._dispatch(_ReconcileTick())
    assert not app.risk.killed  # 1er strike
    await app._dispatch(_ReconcileTick())
    assert app.risk.killed and "Desync" in app.kill.record.reason  # type: ignore[union-attr]


def test_secrets_are_not_in_repr():
    s = Secrets(api_key="SUPERSECRET", api_secret="X", telegram_token="T")
    assert "SUPERSECRET" not in repr(s)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # Garantiza que ningún test haga warmup por red.
    async def no_warmup(self):
        return None

    monkeypatch.setattr(TradingApp, "_warmup", no_warmup)
