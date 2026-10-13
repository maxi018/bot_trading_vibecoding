"""Reversión a la media (experimentos #5/#6): indicadores y reglas de entrada/salida."""

from __future__ import annotations

import pytest

from quantbot.config import ConfigError, StrategyConfig, load_config
from quantbot.core.models import Bar, Direction
from quantbot.strategy import build_strategy
from quantbot.strategy.indicators import RSI, SMA
from quantbot.strategy.mean_reversion import MeanReversionStrategy

from .conftest import T0, H


class Ctx:
    def __init__(self) -> None:
        self.qty = 0.0

    def now_ms(self) -> int:
        return 0

    def position_qty(self, symbol: str) -> float:
        return self.qty


def bar(i: int, c: float) -> Bar:
    return Bar("BTC/USDT", T0 + i * H, T0 + (i + 1) * H, c, c * 1.002, c * 0.998, c, 1)


def cfg(**kw) -> StrategyConfig:
    base = dict(name="mean_reversion", atr_period=5, mr_rsi_period=2, mr_entry_rsi=10.0)
    base.update(kw)
    return StrategyConfig(**base)


class _Daily:  # filtro de tendencia controlado por el test
    def __init__(self, up: bool) -> None:
        self.value, self.last_close = (50.0, 100.0) if up else (200.0, 100.0)

    def update(self, bar: Bar) -> None:
        pass


def test_rsi_matches_wilder_reference():
    """Ejemplo clásico de Wilder (14 períodos); la tabla publicada redondea los cambios
    intermedios, de ahí la tolerancia."""
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
              45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]  # fmt: skip
    r = RSI(14)
    out = [r.update(c) for c in closes]
    assert out[13] is None
    assert out[14:] == pytest.approx([70.53, 66.32, 66.55, 69.41, 66.36, 57.97], abs=0.1)


def test_sma():
    s = SMA(3)
    assert [s.update(x) for x in (1.0, 2.0, 3.0, 4.0)] == [None, None, 2.0, 3.0]


def test_registered_and_configurable():
    c = load_config("config/experiments/05_meanrev.toml")
    assert isinstance(build_strategy(c.strategy), MeanReversionStrategy)
    assert c.strategy.trend_filter_days == 200 and c.strategy.mr_entry_rsi == 10.0
    with pytest.raises(ConfigError, match="mr_entry_rsi"):
        load_config(strategy={"mr_entry_rsi": 150})


def _strat(up: bool = True) -> MeanReversionStrategy:
    s = MeanReversionStrategy(cfg(trend_filter_days=5))
    s._st["BTC/USDT"].daily = _Daily(up)  # type: ignore[assignment]
    return s


def _feed(s: MeanReversionStrategy, prices: list[float], ctx: Ctx, start: int = 0):
    return [s.on_bar(bar(start + i, p), ctx) for i, p in enumerate(prices)]


CALM = [100.0 + 0.1 * (i % 2) for i in range(30)]  # lateral tranquilo: RSI ~50


def test_buys_a_sharp_dip_with_stop_below():
    s, ctx = _strat(), Ctx()
    _feed(s, CALM, ctx)
    (sig,) = s.on_bar(bar(30, 97.0), ctx)  # caída brusca -> RSI(2) ~0
    assert sig.direction is Direction.LONG and sig.reason.startswith("entry")
    assert sig.stop_price is not None and sig.stop_price < 97.0


def test_no_entry_outside_uptrend_or_without_dip():
    s, ctx = _strat(up=False), Ctx()
    _feed(s, CALM, ctx)
    assert s.on_bar(bar(30, 97.0), ctx) == []  # caída, pero no hay tendencia alcista
    s2 = _strat()
    outs = _feed(s2, CALM, ctx)
    assert not any(outs)  # tendencia sí, pero sin caída: nada


def _enter(s: MeanReversionStrategy, ctx: Ctx) -> None:
    _feed(s, CALM, ctx)
    s.on_bar(bar(30, 97.0), ctx)
    ctx.qty = 1.0  # fill de la entrada


def test_exits_on_reversion_above_short_sma():
    s, ctx = _strat(), Ctx()
    _enter(s, ctx)
    (hold,) = s.on_bar(bar(31, 96.8), ctx)  # sigue abajo: mantiene con el stop fijo
    assert hold.direction is Direction.LONG and hold.reason == "hold"
    (sig,) = s.on_bar(bar(32, 100.5), ctx)  # rebotó sobre la SMA(5)
    assert sig.direction is Direction.FLAT and sig.reason == "reverted"


def test_time_stop_and_trend_filter_exit():
    s, ctx = _strat(), Ctx()
    _enter(s, ctx)
    sigs = [s.on_bar(bar(31 + i, 96.9 - 0.01 * i), ctx) for i in range(10)]
    assert sigs[-1][0].direction is Direction.FLAT and sigs[-1][0].reason == "time_stop"
    assert all(x[0].reason == "hold" for x in sigs[:-1])

    s2, ctx2 = _strat(), Ctx()
    _enter(s2, ctx2)
    s2._st["BTC/USDT"].daily = _Daily(up=False)  # type: ignore[assignment]
    (sig,) = s2.on_bar(bar(31, 96.9), ctx2)
    assert sig.direction is Direction.FLAT and sig.reason == "trend_filter"


def test_detects_native_stop_out_and_can_reenter():
    s, ctx = _strat(), Ctx()
    _enter(s, ctx)
    ctx.qty = 0.0  # el stop nativo la cerró en el exchange
    sigs = s.on_bar(bar(31, 96.0), ctx)
    assert not any(x.direction is Direction.FLAT for x in sigs)  # nada que cerrar: ya cerró
    assert not s._st["BTC/USDT"].exiting
    sigs = s.on_bar(bar(32, 93.0), ctx)  # otra caída brusca: puede volver a entrar
    assert any(x.direction is Direction.LONG for x in sigs)


def test_warmup_covers_trend_filter():
    s = MeanReversionStrategy(cfg(trend_filter_days=200, timeframe="1h"))
    assert s.warmup_bars() >= 201 * 24
