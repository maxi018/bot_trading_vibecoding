from __future__ import annotations

import math

import pytest

from quantbot.config import StrategyConfig
from quantbot.core.models import Bar, Direction, Signal
from quantbot.strategy.indicators import ATR, EMA, EfficiencyRatio, RollingPercentile
from quantbot.strategy.regime import Regime, RegimeFilter
from quantbot.strategy.trend_following import TrendFollowingStrategy

from .conftest import T0, H


class Ctx:
    def __init__(self) -> None:
        self.qty = 0.0

    def now_ms(self) -> int:
        return 0

    def position_qty(self, symbol: str) -> float:
        return self.qty


def bar(i: int, c: float, wick: float = 0.003) -> Bar:
    return Bar("BTC/USDT", T0 + i * H, T0 + (i + 1) * H, c, c * (1 + wick), c * (1 - wick), c, 1)


def small_cfg(**kw) -> StrategyConfig:
    base = dict(
        fast_ema=5,
        slow_ema=20,
        atr_period=5,
        vol_lookback=10,
        vol_history=200,
        er_period=10,
        min_efficiency_ratio=0.2,
        max_vol_percentile=0.95,
    )
    base.update(kw)
    return StrategyConfig(**base)


def test_indicators():
    e = EMA(3)
    for x in (1, 2, 3, 4):
        e.update(x)
    assert e.ready and 3 < e.value < 4
    a = ATR(2)
    a.update(bar(0, 100))
    a.update(bar(1, 100))
    assert a.ready and a.value == pytest.approx(0.6, rel=1e-6)
    er = EfficiencyRatio(3)
    for x in (1, 2, 3, 4):
        r = er.update(x)
    assert r == pytest.approx(1.0)
    rp = RollingPercentile(10)
    for x in range(10):
        rp.update(float(x))
    assert rp.update(100.0) == 1.0 and rp.update(-1.0) == 0.0


def test_signal_requires_stop_on_correct_side():
    with pytest.raises(ValueError):
        Signal("s", "X", 0, Direction.LONG, 100, stop_price=None)
    with pytest.raises(ValueError):
        Signal("s", "X", 0, Direction.LONG, 100, stop_price=101)
    Signal("s", "X", 0, Direction.FLAT, 100)  # FLAT no necesita stop


def test_regime_filter_flags_volatility_spike():
    cfg = small_cfg(vol_history=300)
    rf = RegimeFilter(cfg)
    p = 100.0
    last = Regime.WARMUP
    for i in range(400):
        p *= 1.002 if i % 3 else 0.999  # tendencia tranquila
        last = rf.update(bar(i, p))
    assert last is Regime.TREND
    for i in range(400, 410):
        p *= 1.08 if i % 2 else 0.92  # crash/whipsaw violento
        last = rf.update(bar(i, p, wick=0.05))
    assert last is Regime.HIGH_VOL
    assert not last.allows_entries


def test_regime_filter_flags_range():
    rf = RegimeFilter(small_cfg())
    last = Regime.WARMUP
    for i in range(400):
        last = rf.update(bar(i, 100 * (1 + 0.01 * math.sin(i))))
    assert last is Regime.RANGE


def run_series(strat, prices, ctx):
    out = []
    for i, p in enumerate(prices):
        out.append(strat.on_bar(bar(i, p), ctx))
    return out


def test_trend_strategy_enters_long_with_atr_stop_and_trails():
    strat = TrendFollowingStrategy(small_cfg())
    ctx = Ctx()
    prices = [100 * (1.003**i) for i in range(300)]
    outs = run_series(strat, prices, ctx)
    entries = [s for o in outs for s in o if s.direction is Direction.LONG]
    assert entries, "debería entrar en una tendencia limpia"
    first = entries[0]
    assert first.stop_price < first.price
    # Simular fill y seguir: el stop solo sube
    ctx.qty = 1.0
    stops = []
    p = prices[-1]
    for i in range(300, 340):
        p *= 1.003
        sigs = strat.on_bar(bar(i, p), ctx)
        stops += [s.stop_price for s in sigs if s.direction is Direction.LONG]
    assert stops == sorted(stops) and len(stops) > 10


def test_no_entries_when_short_disabled():
    strat = TrendFollowingStrategy(small_cfg(allow_short=False))
    outs = run_series(strat, [100 * (0.997**i) for i in range(300)], Ctx())
    assert not [s for o in outs for s in o if s.direction is Direction.SHORT]


def test_exit_on_stop_and_wait_for_new_cross():
    strat = TrendFollowingStrategy(small_cfg())
    ctx = Ctx()
    prices = [100 * (1.003**i) for i in range(300)]
    run_series(strat, prices, ctx)
    ctx.qty = 1.0
    p = prices[-1]
    strat.on_bar(bar(300, p * 1.003), ctx)  # adopta posición
    sigs = strat.on_bar(bar(301, p * 0.90), ctx)  # caída fuerte: cierra bajo el stop
    assert sigs and sigs[0].direction is Direction.FLAT and sigs[0].reason == "stop"
    # Mientras la posición siga abierta, reintenta la salida
    again = strat.on_bar(bar(302, p * 0.90), ctx)
    assert again and again[0].reason == "exit_retry"
    ctx.qty = 0.0
    # Ya plano: no reentra sin un cruce nuevo
    assert not strat.on_bar(bar(303, p * 0.91), ctx)


def test_external_stop_out_is_detected():
    strat = TrendFollowingStrategy(small_cfg())
    ctx = Ctx()
    prices = [100 * (1.003**i) for i in range(300)]
    run_series(strat, prices, ctx)
    ctx.qty = 1.0
    strat.on_bar(bar(300, prices[-1] * 1.003), ctx)
    ctx.qty = 0.0  # el stop nativo del exchange nos sacó
    sigs = strat.on_bar(bar(301, prices[-1] * 1.006), ctx)
    assert not sigs  # espera cruce nuevo, no reentra de inmediato


# ------------------------------------------- filtro de 200 días (experimentos #3/#4)
DAY = 86_400_000


def test_daily_sma_uses_only_completed_days():
    from quantbot.strategy.indicators import DailySMA

    f = DailySMA(3)
    t0 = 1_700_006_400_000  # 00:00 UTC
    assert t0 % DAY == 0

    def b(ts_close: int, c: float) -> Bar:
        return Bar("BTC/USDT", ts_close - H, ts_close, c, c, c, c, 1)

    for d, c in enumerate((100.0, 110.0, 120.0), start=1):
        f.update(b(t0 + d * DAY - H, 999.0))  # barra intradiaria: no cuenta
        f.update(b(t0 + d * DAY, c))  # la que cierra a las 00:00: cierre del día
    assert f.ready and f.value == pytest.approx(110.0) and f.last_close == 120.0
    f.update(b(t0 + 4 * DAY - H, 1.0))  # día en curso: no mueve nada (sin look-ahead)
    assert f.value == pytest.approx(110.0) and f.last_close == 120.0


def test_daily_sma_with_unaligned_bars_closes_day_on_next_day_first_bar():
    """Bug encontrado al escribir los tests: con barras que no cierran a las 00:00 (datos
    sintéticos, otros exchanges) el filtro no registraba ningún día y nunca operaba."""
    from quantbot.strategy.indicators import DailySMA

    f = DailySMA(2)
    t0 = 1_700_006_400_000 + 30 * 60_000  # barras que cierran a las hh:30

    def b(i: int, c: float) -> Bar:
        ts = t0 + i * H
        return Bar("BTC/USDT", ts - H, ts, c, c, c, c, 1)

    for i in range(24):  # día 1: barras que cierran de 00:30 a 23:30
        f.update(b(i, 100.0 + i))
    assert f.last_close is None  # el día 1 todavía puede seguir: no cuenta
    f.update(b(24, 500.0))  # primera barra del día 2 -> el día 1 terminó
    assert f.last_close == 123.0 and not f.ready  # cierre del día 1 = su última barra
    for i in range(25, 49):  # resto del día 2 + primera barra del día 3
        f.update(b(i, 200.0))
    assert f.ready and f.value == pytest.approx((123.0 + 200.0) / 2)


def test_filter_blocks_entries_until_it_has_history():
    prices = [100 * (1.003**i) for i in range(400)]
    plain = run_series(TrendFollowingStrategy(small_cfg()), prices, Ctx())
    filt = run_series(TrendFollowingStrategy(small_cfg(trend_filter_days=10)), prices, Ctx())

    def first_entry(outs):
        return next(i for i, o in enumerate(outs) if any(s.direction is Direction.LONG for s in o))

    midnights = [i for i in range(400) if bar(i, 1).ts % DAY == 0]
    assert first_entry(plain) < midnights[9] <= first_entry(filt)  # recién con 10 días


class _FakeDaily:
    def __init__(self, sma: float, last: float) -> None:
        self.value, self.last_close = sma, last

    def update(self, bar: Bar) -> None:
        pass


def test_open_long_exits_when_filter_turns_off_and_reenters_without_new_cross():
    strat = TrendFollowingStrategy(small_cfg(trend_filter_days=2))
    st = strat._st["BTC/USDT"]
    ctx = Ctx()
    st.daily = _FakeDaily(sma=50.0, last=100.0)  # filtro encendido
    prices = [100 * (1.003**i) for i in range(304)]  # serie exacta: sin saltos de vol
    run_series(strat, prices[:300], ctx)
    ctx.qty = 1.0  # entró
    assert any(s.direction is Direction.LONG for s in strat.on_bar(bar(300, prices[300]), ctx))

    st.daily = _FakeDaily(sma=200.0, last=100.0)  # se apaga: cierre diario bajo la media
    (sig,) = strat.on_bar(bar(301, prices[301]), ctx)
    assert sig.direction is Direction.FLAT and sig.reason == "trend_filter"

    ctx.qty = 0.0  # salida ejecutada
    assert not strat.on_bar(bar(302, prices[302]), ctx)  # filtro apagado: no reentra
    st.daily = _FakeDaily(sma=50.0, last=100.0)  # vuelve a encenderse, tendencia sigue
    sigs = strat.on_bar(bar(303, prices[303]), ctx)
    assert any(s.direction is Direction.LONG for s in sigs)  # sin esperar un cruce nuevo


def test_warmup_covers_the_filter():
    cfg = small_cfg(trend_filter_days=200, timeframe="1h")
    assert TrendFollowingStrategy(cfg).warmup_bars() >= 201 * 24
    assert (
        TrendFollowingStrategy(small_cfg(trend_filter_days=200, timeframe="4h")).warmup_bars()
        >= 201 * 6
    )
