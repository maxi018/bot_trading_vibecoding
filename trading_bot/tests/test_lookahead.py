"""Look-ahead bias: el motor no puede ver datos con timestamp > now, y las órdenes
decididas con el cierre de una barra se ejecutan en la SIGUIENTE."""

from __future__ import annotations

import pytest

from quantbot.backtest.engine import run_backtest
from quantbot.config import load_config
from quantbot.core.clock import LookaheadError, SimClock, assert_not_future
from quantbot.core.models import Bar, Direction, Signal
from quantbot.strategy.base import Strategy, StrategyContext

from .conftest import T0, H, make_bar


def test_sim_clock_never_goes_back():
    c = SimClock(1000)
    c.advance_to(2000)
    with pytest.raises(LookaheadError):
        c.advance_to(1500)


def test_future_event_raises():
    c = SimClock(1000)
    assert_not_future(1000, c)
    with pytest.raises(LookaheadError):
        assert_not_future(1001, c)


def test_risk_engine_rejects_future_bar(risk_factory, clock):
    risk = risk_factory()
    future = make_bar(10, 100.0)  # clock está en T0, esta barra cierra en T0 + 11h
    with pytest.raises(LookaheadError):
        risk.on_bar(future)


class _Spy(Strategy):
    """Compra en la barra 150 y registra qué barras vio y cuándo."""

    def __init__(self) -> None:
        super().__init__("spy", ("BTC/USDT",))
        self.seen: list[tuple[int, int]] = []

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        self.seen.append((bar.ts, ctx.now_ms()))
        if bar.ts == T0 + 151 * H:
            return [
                Signal(
                    "spy", bar.symbol, bar.ts, Direction.LONG, bar.close, stop_price=bar.close * 0.5
                )
            ]
        return []


def test_strategy_never_sees_future_and_fills_on_next_open(monkeypatch):
    cfg = load_config(mode="backtest")
    # Barras: precio sube 1% por barra; open != close previo para distinguir
    bars = []
    p = 100.0
    for i in range(200):
        o = p * 1.002  # gap de apertura
        c = o * 1.01
        bars.append(Bar("BTC/USDT", T0 + i * H, T0 + (i + 1) * H, o, c * 1.001, o * 0.999, c, 10))
        p = c
    spy = _Spy()
    monkeypatch.setattr("quantbot.backtest.engine.build_strategy", lambda _cfg: spy)
    res = run_backtest(cfg, bars)
    # 1) nunca vio una barra con ts > now
    assert all(bar_ts <= now for bar_ts, now in spy.seen)
    # 2) la señal se convirtió en orden (la verificación del precio de fill está
    #    en test_fill_price_is_next_bar_open)
    assert res.orders_sent >= 1


def test_fill_price_is_next_bar_open(tmp_path):
    """Verificación directa con el exchange simulado."""
    import asyncio

    from quantbot.config import CostConfig
    from quantbot.core.models import Order, OrderType, Side, TimeInForce
    from quantbot.execution.cost_model import CostModel
    from quantbot.execution.paper import PaperExchange

    async def run() -> list:
        clock = SimClock(T0)
        ex = PaperExchange(
            clock, CostModel(CostConfig(half_spread_bps=0, slippage_bps=0)), 10_000, "next_bar"
        )
        fills = []

        async def on_fill(f):
            fills.append(f)

        ex.set_fill_handler(on_fill)
        b0 = Bar("BTC/USDT", T0, T0 + H, 100, 101, 99, 100.5, 1)
        clock.advance_to(b0.ts)
        await ex.process_bar(b0)
        o = Order("c1", "BTC/USDT", Side.BUY, OrderType.MARKET, 1.0, tif=TimeInForce.IOC)
        await ex.create_order(o)  # decidida al cierre de b0
        assert not fills  # nada se ejecuta al mismo cierre
        b1 = Bar("BTC/USDT", T0 + H, T0 + 2 * H, 103, 104, 102, 103.5, 1)
        clock.advance_to(b1.ts_open)
        await ex.process_bar(b1)
        return fills

    fills = asyncio.run(run())
    assert len(fills) == 1 and fills[0].price == pytest.approx(103)  # open de b1
