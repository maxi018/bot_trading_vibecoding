"""Backtester event-driven que reutiliza el pipeline de producción (TradingCore).

Secuencia por grupo de barras con el mismo ts (multi-activo):

    1. reloj -> ts_open de la barra
    2. exchange simulado ejecuta órdenes PENDIENTES contra el OHLC de esta barra
       (las órdenes se decidieron con el cierre ANTERIOR -> sin look-ahead)
    3. reloj -> ts de cierre
    4. marks + circuit breakers + estrategia -> nuevas órdenes (se ejecutan en la
       barra siguiente)

El backtest usa un kill-switch en un directorio temporal: un kill en backtest
detiene ESE backtest, nunca afecta al bot real.
"""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig
from ..core.alerting import Alerter
from ..core.clock import SimClock, periods_per_year
from ..core.kill_switch import KillSwitch
from ..core.models import Bar
from ..core.portfolio import ClosedTrade, Portfolio
from ..data.historical import group_by_ts
from ..execution.cost_model import CostModel
from ..execution.order_manager import OrderManager
from ..execution.paper import PaperExchange
from ..execution.rate_limiter import NullRateLimiter
from ..risk.engine import RiskEngine
from ..strategy import build_strategy
from ..trading_core import TradingCore
from .metrics import PerformanceReport, performance_report


@dataclass(slots=True)
class BacktestResult:
    equity_ts: list[int]
    equity: list[float]
    trades: list[ClosedTrade]
    trade_returns: list[float]
    report: PerformanceReport
    killed: bool
    kill_reason: str
    vetoes: int
    orders_sent: int
    fees_paid: float
    alerts: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)


class Backtester:
    def __init__(
        self,
        cfg: AppConfig,
        bars: Sequence[Bar],
        strategy_params: dict[str, Any] | None = None,
        cost_stress: float = 1.0,
        trading_start_ts: int = 0,
        n_trials: int = 1,
    ) -> None:
        if strategy_params:
            cfg = dataclasses.replace(
                cfg, strategy=dataclasses.replace(cfg.strategy, **strategy_params)
            )
        self.cfg = cfg
        self.bars = sorted(bars, key=lambda b: (b.ts, b.symbol))
        self.params = strategy_params or {}
        self.cost_stress = cost_stress
        self.trading_start_ts = trading_start_ts
        self.n_trials = n_trials

    async def run(self) -> BacktestResult:
        cfg = self.cfg
        ppy = periods_per_year(cfg.strategy.timeframe)
        start = self.bars[0].ts_open if self.bars else 0
        clock = SimClock(start)
        alerter = Alerter(channels=[], min_interval_s=0)
        with tempfile.TemporaryDirectory(prefix="qb-bt-") as tmp:
            kill = KillSwitch(tmp)
            risk = RiskEngine(cfg.risk, cfg.costs, kill, clock, ppy, alerter)
            portfolio = Portfolio(cfg.initial_cash)
            exchange = PaperExchange(
                clock, CostModel(cfg.costs, self.cost_stress), cfg.initial_cash, "next_bar"
            )
            om = OrderManager(
                exchange, risk, portfolio, clock, NullRateLimiter(), alerter, retry_base_delay_s=0
            )
            core = TradingCore(build_strategy(cfg.strategy), risk, om, portfolio, clock)
            core.trading_start_ts = self.trading_start_ts

            eq_ts: list[int] = []
            eq: list[float] = []
            for group in group_by_ts(self.bars):
                ts_open = max(b.ts_open for b in group)
                clock.advance_to(max(clock.now_ms(), ts_open))
                for bar in group:
                    await exchange.process_bar(bar)
                clock.advance_to(group[0].ts)
                await core.on_bars(group)
                if group[0].ts >= self.trading_start_ts:
                    eq_ts.append(group[0].ts)
                    eq.append(portfolio.equity)

            killed = kill.engaged
            kill_reason = kill.record.reason if kill.record else ""

        trades = [t for t in portfolio.closed_trades if t.ts >= self.trading_start_ts]
        equity_at = dict(zip(eq_ts, eq, strict=True))
        trade_returns = []
        for t in trades:
            base = equity_at.get(t.ts, cfg.initial_cash) - t.pnl
            trade_returns.append(t.pnl / base if base > 0 else 0.0)
        report = performance_report(
            eq or [cfg.initial_cash], [t.pnl for t in trades], ppy, self.n_trials
        )
        return BacktestResult(
            equity_ts=eq_ts,
            equity=eq,
            trades=trades,
            trade_returns=trade_returns,
            report=report,
            killed=killed,
            kill_reason=kill_reason,
            vetoes=risk.veto_count,
            orders_sent=core.orders_sent,
            fees_paid=portfolio.fees_paid,
            alerts=[a.title for a in alerter.sent],
            params=self.params,
        )


def run_backtest(cfg: AppConfig, bars: Sequence[Bar], **kwargs: Any) -> BacktestResult:
    return asyncio.run(Backtester(cfg, bars, **kwargs).run())
