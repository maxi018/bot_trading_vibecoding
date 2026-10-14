"""Integración: backtest completo, kill en crash, métricas, Montecarlo, walk-forward."""

from __future__ import annotations

import dataclasses
import math

import pytest

from quantbot.backtest.engine import run_backtest
from quantbot.backtest.metrics import (
    deflated_sharpe,
    max_drawdown,
    performance_report,
    sharpe,
)
from quantbot.backtest.montecarlo import monte_carlo
from quantbot.backtest.walk_forward import param_grid, walk_forward
from quantbot.config import ConfigError, load_config
from quantbot.core.models import Bar, Fill, Side
from quantbot.core.portfolio import Portfolio
from quantbot.data.historical import load_csv, validate_bars, write_csv
from quantbot.data.synthetic import generate_bars

from .conftest import T0, H


def test_full_backtest_runs_and_respects_limits(cfg):
    bars = generate_bars(n=4000, seed=3)
    res = run_backtest(cfg, bars)
    assert len(res.equity) > 0
    assert res.report.max_drawdown < cfg.risk.total_drawdown_kill or res.killed
    assert all(math.isfinite(x) for x in res.equity)
    # Determinismo: mismo input -> mismo output
    assert run_backtest(cfg, bars).equity == res.equity


def test_cost_stress_hurts(cfg):
    bars = generate_bars(n=4000, seed=5)
    base = run_backtest(cfg, bars)
    stressed = run_backtest(cfg, bars, cost_stress=1.8)  # 9 bps < protección de 10 bps
    assert base.orders_sent > 0
    assert stressed.fees_paid > base.fees_paid * 1.5
    assert stressed.equity[-1] < base.equity[-1]


def test_price_protection_blocks_entries_when_slippage_explodes(cfg):
    """Con costos x3 (15 bps) la limit IOC de entrada (10 bps) no se llena: el bot
    prefiere no entrar antes que pagar cualquier precio."""
    bars = generate_bars(n=4000, seed=5)
    res = run_backtest(cfg, bars, cost_stress=3.0)
    assert res.orders_sent > 0 and res.fees_paid == 0 and not res.trades


def test_crash_triggers_kill_switch_and_flattens():
    cfg = load_config(mode="backtest")
    cfg = dataclasses.replace(
        cfg,
        risk=dataclasses.replace(
            cfg.risk,
            total_drawdown_kill=0.02,
            daily_drawdown_halt=0.10,
            max_position_pct=0.5,
            max_risk_per_trade=0.02,
        ),
        strategy=dataclasses.replace(
            cfg.strategy, atr_stop_mult=50.0, max_vol_percentile=1.0, min_efficiency_ratio=0.0
        ),
    )
    # Tendencia alcista larga y luego crash del 50% (COVID marzo 2020)
    bars, p = [], 100.0
    for i in range(1500):
        o = p
        p *= 1.002 if i < 1400 else 0.97
        bars.append(
            Bar(
                "BTC/USDT",
                T0 + i * H,
                T0 + (i + 1) * H,
                o,
                max(o, p) * 1.001,
                min(o, p) * 0.999,
                p,
                10,
            )
        )
    res = run_backtest(cfg, bars)
    assert res.killed and "DD total" in res.kill_reason
    assert any("KILL" in a for a in res.alerts)
    # Tras el kill no hay más exposición: equity plana hasta el final
    tail = res.equity[-20:]
    assert max(tail) - min(tail) < 1e-6 * tail[0]


def test_metrics_basics():
    eq = [100, 110, 99, 120, 108, 130]
    mdd, dur = max_drawdown(eq)
    assert mdd == pytest.approx(0.1) and dur == 1
    assert sharpe([0.01, -0.01] * 50, 365) == pytest.approx(0.0, abs=1e-9)
    rep = performance_report(eq, [10, -5, 20, -2], 365)
    assert rep.profit_factor == pytest.approx(30 / 7)
    assert rep.n_trades == 4 and any("anecdótico" in w for w in rep.warnings)


def test_deflated_sharpe_penalizes_many_trials():
    import random

    rng = random.Random(0)
    rets = [rng.gauss(0.0005, 0.01) for _ in range(1000)]
    assert deflated_sharpe(rets, n_trials=1) > deflated_sharpe(rets, n_trials=1000)


def test_montecarlo_is_worse_than_single_path():
    trades = [0.03, -0.01, -0.01, 0.02, -0.01, -0.01, 0.04, -0.01] * 10
    mc = monte_carlo(trades, n_sims=500)
    eq = [1.0]
    for r in trades:
        eq.append(eq[-1] * (1 + r))
    hist_dd, _ = max_drawdown(eq)
    assert mc.dd_p95 > hist_dd  # el futuro casi seguro es peor que la historia
    assert mc.dd_p50 <= mc.dd_p95 <= mc.dd_p99


def test_walk_forward_uses_only_test_segments(cfg):
    bars = generate_bars(n=3000, seed=11)
    wf = walk_forward(
        cfg,
        bars,
        {"fast_ema": [10, 20], "slow_ema": [60, 100]},
        train_bars=1200,
        test_bars=600,
        warmup_bars=400,
    )
    assert len(wf.windows) == 3
    assert wf.n_trials == 3 * 4
    for w in wf.windows:
        assert w.test.equity_ts[0] >= w.test_start  # equity solo del tramo de test
        assert all(t.ts >= w.test_start for t in w.test.trades)
    assert param_grid({"a": [1, 2], "b": [3]}) == [{"a": 1, "b": 3}, {"a": 2, "b": 3}]


def test_walk_forward_without_enough_train_trades_uses_toml_params_and_says_so(cfg):
    """Regresión: si ninguna combinación llega a 5 trades en train, antes se elegía en
    silencio la PRIMERA de la grilla (en 1d pasaba en casi todas las ventanas)."""
    bars = generate_bars(n=1500, seed=11)
    grid = {"fast_ema": [10, 30], "slow_ema": [60, 200]}
    wf = walk_forward(cfg, bars, grid, train_bars=150, test_bars=300, warmup_bars=400)
    toml = {"fast_ema": cfg.strategy.fast_ema, "slow_ema": cfg.strategy.slow_ema}
    assert toml not in param_grid(grid)  # si cayera en la grilla, el test no distinguiría
    assert wf.windows and wf.n_fallback == len(wf.windows)
    for w in wf.windows:
        assert w.fallback and w.train_sharpe == -math.inf
        assert w.best_params == toml

    ok = walk_forward(cfg, bars, grid, train_bars=900, test_bars=300, warmup_bars=200)
    assert not any(w.fallback for w in ok.windows if w.train_sharpe > -math.inf)


def test_delisted_symbol_is_closed_at_its_last_price_and_does_not_halt_the_rest(cfg):
    """Una moneda que deja de cotizar (BSV en 2019): antes su posición quedaba congelada
    a su último precio y el breaker de datos viejos frenaba a TODA la canasta."""
    cfg = dataclasses.replace(
        cfg, strategy=dataclasses.replace(cfg.strategy, symbols=("BTC/USDT", "ETH/USDT"))
    )
    btc = generate_bars("BTC/USDT", 3000, H, seed=1)
    eth = generate_bars("ETH/USDT", 3000, H, seed=101)[:1800]  # se deslista con posición
    res = run_backtest(cfg, btc + eth)
    assert res.delisted == ["ETH/USDT"]
    assert any(t.symbol == "ETH/USDT" and t.ts == eth[-1].ts for t in res.trades)
    after = [t for t in res.trades if t.symbol == "BTC/USDT" and t.ts > eth[-1].ts]
    alone = [t for t in run_backtest(cfg, btc).trades if t.ts > eth[-1].ts]
    assert after and len(after) == len(alone)  # BTC sigue operando como si nada


def test_walk_forward_reports_oos_pnl_by_symbol_and_concentration(cfg):
    cfg = dataclasses.replace(
        cfg, strategy=dataclasses.replace(cfg.strategy, symbols=("BTC/USDT", "ETH/USDT"))
    )
    bars = generate_bars("BTC/USDT", 3000, H, seed=11) + generate_bars("ETH/USDT", 3000, H, seed=12)
    wf = walk_forward(cfg, bars, {"fast_ema": [10, 20]}, 1200, 600, 400)
    by_sym = wf.oos_by_symbol()
    trades = [t for w in wf.windows for t in w.test.trades]
    assert set(by_sym) == {t.symbol for t in trades}
    assert sum(n for n, _ in by_sym.values()) == len(trades)
    total = sum(p for _, p in by_sym.values())
    assert total == pytest.approx(sum(t.pnl for t in trades))
    pnls = [p for _, p in by_sym.values()]
    assert pnls == sorted(pnls, reverse=True)
    share = wf.top_symbol_share()
    assert share is None if total <= 0 else share == pytest.approx(pnls[0] / total)


def test_data_validation_and_csv_roundtrip(tmp_path):
    bars = generate_bars(n=50, seed=1)
    dirty = bars[:10] + bars[12:] + [bars[5]]  # hueco + duplicado + desorden
    clean, rep = validate_bars(dirty, H)
    assert rep.duplicates == 1 and len(rep.gaps) == 1 and rep.out_of_order >= 1
    assert [b.ts for b in clean] == sorted({b.ts for b in clean})
    path = tmp_path / "x.csv"
    write_csv(path, bars)
    loaded, rep2 = load_csv(path, "BTC/USDT", H)
    assert rep2.ok and loaded == bars


def test_portfolio_trade_accounting():
    pf = Portfolio(1000)
    pf.apply_fill(Fill("a", "X", Side.BUY, 2, 100, 0.2, 1))
    pf.apply_fill(Fill("b", "X", Side.SELL, 1, 110, 0.1, 2))
    assert pf.closed_trades == []  # cierre parcial: el trade sigue abierto
    pf.apply_fill(Fill("c", "X", Side.SELL, 1, 90, 0.1, 3))
    assert len(pf.closed_trades) == 1
    assert pf.closed_trades[0].pnl == pytest.approx(10 - 10 - 0.4)
    assert pf.consecutive_losses == 1
    assert pf.equity == pytest.approx(1000 - 0.4)


def test_config_safety_ceilings(tmp_path):
    with pytest.raises(ConfigError):
        load_config(risk={"max_gross_leverage": 20.0})
    with pytest.raises(ConfigError):
        load_config(risk={"max_drawdwon": 0.1})  # typo -> falla fuerte
    with pytest.raises(ConfigError):
        load_config(strategy={"fast_ema": 200, "slow_ema": 100})
    p = tmp_path / "c.toml"
    p.write_text('[strategy]\nsymbols = ["ETH/USDT"]\n')
    assert load_config(p).strategy.symbols == ("ETH/USDT",)


def test_env_overrides_for_docker():
    from quantbot.config import apply_env_overrides

    cfg = load_config()
    assert cfg.api.host == "127.0.0.1"  # default seguro
    out = apply_env_overrides(cfg, {"QB_API_HOST": "0.0.0.0", "QB_API_PORT": "9000"})
    assert (out.api.host, out.api.port) == ("0.0.0.0", 9000)
    assert apply_env_overrides(cfg, {}).api == cfg.api
    with pytest.raises(ConfigError):
        apply_env_overrides(cfg, {"QB_API_PORT": "abc"})
    with pytest.raises(ConfigError):
        apply_env_overrides(cfg, {"QB_API_PORT": "70000"})


def test_montecarlo_shutdown_criterion_never_above_kill() -> None:
    from quantbot.backtest.montecarlo import MonteCarloResult

    def mc(dd_p95: float) -> str:
        return MonteCarloResult(
            n_sims=10,
            dd_p50=dd_p95 / 2,
            dd_p95=dd_p95,
            dd_p99=dd_p95,
            ret_p5=0.0,
            ret_p50=0.0,
            ret_p95=0.0,
            prob_dd_over_kill=0.0,
            kill_threshold=0.15,
        ).pretty()

    assert "DD live > 12.00%" in mc(0.08)
    aggressive = mc(0.30)
    assert "límite efectivo" in aggressive and "DD live >" not in aggressive


def test_closed_trade_records_peak_notional():
    p = Portfolio(10_000)
    p.apply_fill(Fill("a", "BTC/USDT", Side.BUY, 0.01, 100_000, 0.0, T0))
    p.apply_fill(Fill("b", "BTC/USDT", Side.BUY, 0.01, 100_000, 0.0, T0 + H))  # sube a 2000
    p.apply_fill(Fill("c", "BTC/USDT", Side.SELL, 0.02, 101_000, 0.0, T0 + 2 * H))
    (trade,) = p.closed_trades
    assert trade.notional == pytest.approx(0.02 * 101_000)
    # flip: cierra el largo y abre un corto que es un trade nuevo
    p.apply_fill(Fill("d", "BTC/USDT", Side.BUY, 0.01, 100_000, 0.0, T0 + 3 * H))
    p.apply_fill(Fill("e", "BTC/USDT", Side.SELL, 0.03, 100_000, 0.0, T0 + 4 * H))
    p.apply_fill(Fill("f", "BTC/USDT", Side.BUY, 0.02, 99_000, 0.0, T0 + 5 * H))
    assert [round(t.notional) for t in p.closed_trades] == [2020, 1000, 2000]


def test_montecarlo_slippage_scales_with_position_size():
    flat = [0.0] * 400  # trades sin resultado: todo el daño es el slippage extra
    full = monte_carlo(flat, [1.0] * 400, n_sims=300)
    small = monte_carlo(flat, [0.1] * 400, n_sims=300)
    assert full.ret_p50 < -0.25  # 400 trades x ~10 bps sobre todo el equity
    assert -0.06 < small.ret_p50 < -0.02  # misma cantidad de trades al 10%: ~10x menos
    assert monte_carlo(flat, n_sims=50).ret_p50 == pytest.approx(
        monte_carlo(flat, [1.0] * 400, n_sims=50).ret_p50
    )  # sin pesos = 100% del equity (pesimista)
    with pytest.raises(ValueError):
        monte_carlo([0.01, 0.02], [1.0], n_sims=10)


def test_montecarlo_median_is_close_to_the_backtest_it_resamples(cfg):
    """Regresión: el slippage sin escalar daba medianas de -38% para un backtest de -5%."""
    res = run_backtest(cfg, generate_bars(n=6000, seed=5))
    assert res.trade_returns and len(res.trade_weights) == len(res.trade_returns)
    assert all(0 < w <= 1.0 for w in res.trade_weights)  # sizing de la config: <= 100%
    mc = monte_carlo(res.trade_returns, res.trade_weights, n_sims=500)
    hist = res.equity[-1] / res.equity[0] - 1
    # el Montecarlo puede ser algo peor (slippage extra + fills perdidos), no otro planeta.
    # Semillas fijas: con este backtest (27 trades, ~20% del equity c/u) la versión sin
    # escalar se iba 2.5 puntos; la correcta, 0.5.
    assert abs(mc.ret_p50 - hist) < 0.015


def test_backtest_reports_veto_reasons(cfg):
    tight = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, total_drawdown_kill=0.002))
    res = run_backtest(tight, generate_bars(n=4000, seed=5))
    assert res.killed and res.vetoes > 0
    assert sum(res.veto_reasons.values()) == res.vetoes
    assert "kill_switch_engaged" in res.veto_reasons
    assert all(":" not in why for why in res.veto_reasons)  # agrupado por motivo, sin detalle


def test_reserved_balances_config():
    cfg = load_config(exchange={"reserved": {"BTC": 1.0}})
    assert cfg.exchange.reserved == {"BTC": 1.0}
    with pytest.raises(ConfigError, match="reserved"):
        load_config(exchange={"reserved": {"BTC": -1.0}})


def test_walk_forward_warms_the_trend_filter_out_of_sample(cfg):
    """Con el warmup fijo de 600 barras (25 días en 1h), un filtro de 100 días no se
    encendía nunca en ventanas de test de 41 días: walk-forward con 0 trades sin avisar."""
    filt = dataclasses.replace(
        cfg, strategy=dataclasses.replace(cfg.strategy, trend_filter_days=100)
    )
    bars = generate_bars(n=9000, seed=3)
    wf = walk_forward(filt, bars, {"fast_ema": [20], "slow_ema": [100]}, 3000, 1000, 600)
    assert sum(w.test.report.n_trades for w in wf.windows) > 0
