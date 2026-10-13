"""Tests del risk engine: sizing, pre-trade checks, circuit breakers y kill-switch."""

from __future__ import annotations

import math

import pytest

from quantbot.config import RiskConfig
from quantbot.core.models import (
    AccountMetrics,
    Direction,
    Fill,
    OrderBook,
    Side,
    Signal,
)
from quantbot.core.portfolio import Portfolio
from quantbot.risk.engine import RiskActionType
from quantbot.risk.sizing import (
    EwmaVolatility,
    PositionSizer,
    TradeStats,
    kelly_continuous,
    kelly_discrete,
)

from .conftest import warm_risk


def sig(clock, price: float, direction=Direction.LONG, stop: float | None = None, sym="BTC/USDT"):
    if stop is None and direction is not Direction.FLAT:
        stop = price * (0.95 if direction is Direction.LONG else 1.05)
    return Signal("t", sym, clock.now_ms(), direction, price, stop_price=stop)


def metrics(**kw) -> AccountMetrics:
    base = dict(
        ts=0,
        equity=10_000,
        cash=10_000,
        peak_equity=10_000,
        day_start_equity=10_000,
        gross_exposure=0,
        net_exposure=0,
        realized_pnl=0,
        unrealized_pnl=0,
        fees_paid=0,
        consecutive_losses=0,
        open_positions=0,
    )
    base.update(kw)
    return AccountMetrics(**base)


# ----------------------------------------------------------------- sizing
class TestSizing:
    def test_kelly_formulas(self):
        assert kelly_discrete(0.5, 2.0) == pytest.approx(0.25)
        assert kelly_discrete(0.3, 1.0) == 0.0  # sin edge -> 0, nunca negativo
        assert kelly_continuous(0.1, 0.5) == pytest.approx(0.4)

    def test_vol_targeting_shrinks_with_volatility(self):
        sizer = PositionSizer(RiskConfig(max_position_pct=1.0, max_risk_per_trade=1.0))
        calm = sizer.size(equity=10_000, price=100, annual_vol=0.15, stop_distance=1)
        wild = sizer.size(equity=10_000, price=100, annual_vol=0.60, stop_distance=1)
        assert calm.binding == "vol_target"
        assert calm.qty == pytest.approx(100)  # lev 1.0 -> 10k/100
        assert wild.qty == pytest.approx(25)  # lev 0.25
        assert wild.qty < calm.qty

    def test_leverage_capped(self):
        sizer = PositionSizer(RiskConfig(max_position_pct=0.5, max_risk_per_trade=0.02))
        r = sizer.size(equity=10_000, price=100, annual_vol=0.01, stop_distance=1)
        assert r.vol_target_qty == pytest.approx(100)  # max_leverage_per_asset=1.0
        assert r.qty <= 50 + 1e-9  # y el cap de posición manda

    def test_risk_per_trade_binds_with_wide_stop(self):
        sizer = PositionSizer(RiskConfig())
        r = sizer.size(equity=10_000, price=100, annual_vol=0.15, stop_distance=20)
        # 1% de 10k = 100 de riesgo / 20 de distancia = 5 unidades
        assert r.qty == pytest.approx(5)
        assert r.binding == "max_risk_per_trade"

    def test_fractional_kelly_is_a_ceiling(self):
        sizer = PositionSizer(RiskConfig(kelly_min_trades=10))
        weak = TradeStats(n=50, win_rate=0.36, payoff=2.0)  # f*=0.04 -> 1/4 = 1%... borde
        weaker = TradeStats(n=50, win_rate=0.34, payoff=2.0)  # f*=0.01 -> 0.25%
        b, src = sizer.risk_budget(weaker)
        assert src == "kelly_cap" and b == pytest.approx(0.25 * 0.01)
        b2, _ = sizer.risk_budget(weak)
        assert b2 <= 0.01
        _b3, src3 = sizer.risk_budget(TradeStats(n=5, win_rate=0.9, payoff=5))
        assert src3 == "max_risk_per_trade"  # pocos trades: Kelly no se usa

    def test_no_vol_or_no_stop_means_no_trade(self):
        sizer = PositionSizer(RiskConfig())
        assert sizer.size(equity=1e4, price=100, annual_vol=None, stop_distance=1).qty == 0
        assert sizer.size(equity=1e4, price=100, annual_vol=0.2, stop_distance=0).qty == 0

    def test_ewma_vol(self):
        v = EwmaVolatility(span=10)
        for i in range(50):
            v.update(100 * (1.01 if i % 2 else 0.99))
        assert v.ready
        assert v.per_period() == pytest.approx(0.02, rel=0.1)


# ---------------------------------------------------------- pre-trade checks
class TestPreTrade:
    def test_approves_and_respects_position_cap(self, risk_factory, clock, portfolio):
        risk = risk_factory()
        px = warm_risk(risk, clock)
        d = risk.evaluate(sig(clock, px), portfolio)
        assert d.approved and d.order is not None
        assert d.order.side is Side.BUY
        assert d.order.qty * px <= 0.20 * 10_000 + 1e-6
        assert d.order.price > px  # market protegido: limit agresiva por encima
        assert d.stop_price is not None

    def test_veto_without_market_data(self, risk_factory, clock, portfolio):
        risk = risk_factory()
        d = risk.evaluate(sig(clock, 100), portfolio)
        assert not d.approved and d.reason == "no_market_data"

    def test_veto_on_stale_data(self, risk_factory, clock, portfolio):
        risk = risk_factory()
        px = warm_risk(risk, clock)
        clock.advance_to(clock.now_ms() + 10_000)  # 10s sin datos > 5s
        d = risk.evaluate(sig(clock, px), portfolio)
        assert not d.approved and d.reason == "data_stale"

    def test_veto_when_feed_marked_stale(self, risk_factory, clock, portfolio):
        risk = risk_factory()
        px = warm_risk(risk, clock)
        risk.mark_data_stale("ws", "desconectado")
        assert not risk.evaluate(sig(clock, px), portfolio).approved
        risk.mark_data_fresh("ws")
        assert risk.evaluate(sig(clock, px), portfolio).approved

    def test_fat_finger_price(self, risk_factory, clock, portfolio):
        risk = risk_factory()
        px = warm_risk(risk, clock)
        d = risk.evaluate(sig(clock, px * 1.2), portfolio)
        assert not d.approved and d.reason.startswith("fat_finger_price")

    def test_order_rate_limit(self, risk_factory, clock, portfolio):
        risk = risk_factory(max_orders_per_minute=3, max_order_size_vs_avg=100)
        px = warm_risk(risk, clock)
        results = [risk.evaluate(sig(clock, px), portfolio).approved for _ in range(5)]
        assert results == [True, True, True, False, False]

    def test_slippage_estimate_from_thin_book_vetoes(self, risk_factory, clock, portfolio):
        risk = risk_factory()
        px = warm_risk(risk, clock)
        book = OrderBook("BTC/USDT")
        # Libro finito: 0.1 unidades en el best ask, el resto 5% arriba
        book.apply_snapshot(
            [(px * 0.999, 50)], [(px * 1.001, 0.1), (px * 1.05, 100)], 1, clock.now_ms()
        )
        risk.on_book(book)
        d = risk.evaluate(sig(clock, px), portfolio)
        # Debe achicar hasta caber en max_slippage_bps o vetar
        if d.approved:
            assert d.order is not None
            est = risk.estimate_slippage_bps("BTC/USDT", Side.BUY, d.order.qty, px)
            assert est <= risk.cfg.max_slippage_bps
            assert any("slippage" in a for a in d.adjustments)
        else:
            assert "slippage" in d.reason

    def test_gross_leverage_limit_reduces(self, risk_factory, clock):
        risk = risk_factory(max_gross_leverage=0.25, max_correlated_exposure=0.25)
        px = warm_risk(risk, clock)
        pf = Portfolio(10_000)
        pf.apply_fill(Fill("x", "ETH/USDT", Side.BUY, 10, 200.0, 0, clock.now_ms()))  # 2k
        pf.mark("BTC/USDT", px, clock.now_ms())
        d = risk.evaluate(sig(clock, px), pf)
        assert d.approved and d.order is not None
        assert d.order.qty * px <= 0.25 * pf.equity - 2_000 + 1e-6
        assert any("max_gross_leverage" in a or "correlated" in a for a in d.adjustments)

    def test_flip_is_split_into_close_first(self, risk_factory, clock):
        risk = risk_factory()
        px = warm_risk(risk, clock)
        pf = Portfolio(10_000)
        pf.apply_fill(Fill("x", "BTC/USDT", Side.BUY, 5, px, 0, clock.now_ms()))
        d = risk.evaluate(sig(clock, px, Direction.SHORT), pf)
        assert d.approved and d.order is not None
        assert d.order.side is Side.SELL and d.order.qty == pytest.approx(5)
        assert d.order.reduce_only

    def test_exit_allowed_while_halted(self, risk_factory, clock):
        risk = risk_factory()
        px = warm_risk(risk, clock)
        pf = Portfolio(10_000)
        pf.apply_fill(Fill("x", "BTC/USDT", Side.BUY, 5, px, 0, clock.now_ms()))
        risk.check_circuit_breakers(metrics(equity=9_600, day_start_equity=10_000))
        assert risk.halted
        assert not risk.evaluate(sig(clock, px), Portfolio(10_000)).approved  # entrada nueva: no
        exit_d = risk.evaluate(sig(clock, px, Direction.FLAT), pf)
        assert exit_d.approved and exit_d.order is not None and exit_d.order.reduce_only

    def test_internal_error_fails_closed(self, risk_factory, clock, portfolio, monkeypatch):
        risk = risk_factory()
        px = warm_risk(risk, clock)

        def boom(*a, **k):
            raise RuntimeError("bug")

        monkeypatch.setattr(risk.sizer, "size", boom)
        d = risk.evaluate(sig(clock, px), portfolio)
        assert not d.approved and "risk_internal_error" in d.reason


# --------------------------------------------------------- circuit breakers
class TestCircuitBreakers:
    def test_daily_drawdown_halts_until_next_day(self, risk_factory, clock):
        risk = risk_factory()
        acts = risk.check_circuit_breakers(metrics(equity=9_650, day_start_equity=10_000))
        assert [a.type for a in acts] == [RiskActionType.HALT]
        assert not risk.killed and risk.halted
        assert acts[0].until_ms is not None and acts[0].until_ms > clock.now_ms()
        clock.advance_to(acts[0].until_ms)
        risk.check_circuit_breakers(metrics())
        assert not risk.halted  # vence solo al día siguiente

    def test_total_drawdown_kills_with_flatten(self, risk_factory):
        risk = risk_factory()
        acts = risk.check_circuit_breakers(metrics(equity=8_400, peak_equity=10_000))
        assert acts[0].type is RiskActionType.KILL and acts[0].flatten
        assert risk.killed and not risk.trading_enabled

    def test_slippage_anomaly_halts(self, risk_factory, clock):
        risk = risk_factory()
        for i in range(20):
            risk.on_fill(
                Fill(
                    f"c{i}", "BTC/USDT", Side.BUY, 1, 100.2, 0, clock.now_ms(), decision_price=100.0
                )
            )  # 20 bps >> 3x5
        acts = risk.check_circuit_breakers(metrics())
        assert any(a.type is RiskActionType.HALT and "Slippage" in a.reason for a in acts)

    def test_losing_streak_reduces_size(self, risk_factory):
        risk = risk_factory()
        acts = risk.check_circuit_breakers(metrics(consecutive_losses=8))
        assert acts[0].type is RiskActionType.REDUCE_SIZE and risk.size_multiplier == 0.5
        acts = risk.check_circuit_breakers(metrics(consecutive_losses=0))
        assert acts[0].type is RiskActionType.RESTORE_SIZE and risk.size_multiplier == 1.0

    def test_reject_rate_halts(self, risk_factory):
        risk = risk_factory()
        for i in range(10):
            risk.on_order_result(rejected=i < 3)
        acts = risk.check_circuit_breakers(metrics())
        assert any("Rechazos" in a.reason for a in acts)

    def test_position_mismatch_kills(self, risk_factory):
        risk = risk_factory()
        a = risk.report_position_mismatch("BTC local=1 exchange=0")
        assert a.type is RiskActionType.KILL and a.flatten and risk.killed

    def test_stale_market_data_halts_and_clears(self, risk_factory, clock):
        risk = risk_factory()
        warm_risk(risk, clock)
        clock.advance_to(clock.now_ms() + 6_000)
        risk.check_circuit_breakers(metrics())
        assert risk.halted
        risk.touch("BTC/USDT", clock.now_ms())
        risk.check_circuit_breakers(metrics())
        assert not risk.halted


def test_flatten_orders_use_exchange_positions(risk_factory, clock):
    risk = risk_factory()
    px = warm_risk(risk, clock)
    orders = risk.flatten_orders({"BTC/USDT": 2.0, "ETH/USDT": 0.0})
    assert len(orders) == 1
    o = orders[0]
    assert o.side is Side.SELL and o.qty == 2.0 and o.reduce_only and o.tag == "flatten"
    assert o.price is not None and math.isclose(o.price, px * 0.98)
