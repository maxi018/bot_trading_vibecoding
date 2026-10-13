"""Núcleo de trading: el MISMO pipeline en backtest, paper y live.

    evento de mercado -> risk.on_bar/marks -> circuit breakers -> estrategia -> señales
    señal -> risk.evaluate (VETO/REDUCE/APPROVE) -> OrderManager -> exchange
    fill -> OrderManager -> portfolio + risk + estrategia -> stops nativos

Lo único que cambia entre modos es: el reloj, la fuente de datos y el ``ExchangeClient``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .core.clock import Clock
from .core.models import Bar, Fill, OrderBook, Signal, Tick
from .core.portfolio import Portfolio
from .execution.order_manager import OrderManager
from .risk.engine import RiskAction, RiskActionType, RiskEngine
from .strategy.base import Strategy

log = logging.getLogger(__name__)


class _Ctx:
    """Vista read-only para la estrategia."""

    def __init__(self, clock: Clock, portfolio: Portfolio) -> None:
        self._clock = clock
        self._portfolio = portfolio

    def now_ms(self) -> int:
        return self._clock.now_ms()

    def position_qty(self, symbol: str) -> float:
        pos = self._portfolio.positions.get(symbol)
        return pos.qty if pos else 0.0


class TradingCore:
    def __init__(
        self,
        strategy: Strategy,
        risk: RiskEngine,
        orders: OrderManager,
        portfolio: Portfolio,
        clock: Clock,
    ) -> None:
        self.strategy = strategy
        self.risk = risk
        self.orders = orders
        self.portfolio = portfolio
        self.clock = clock
        self.ctx = _Ctx(clock, portfolio)
        self.signals_seen = 0
        self.orders_sent = 0
        self._kill_executed = False
        # Antes de este ts la estrategia solo "calienta" indicadores (walk-forward).
        self.trading_start_ts = 0
        orders.add_fill_listener(self._on_fill)

    # ------------------------------------------------------------ eventos
    async def on_bars(self, bars: Sequence[Bar]) -> None:
        """Procesa un grupo de barras con el MISMO ts de cierre (multi-activo)."""
        for bar in bars:
            self.risk.on_bar(bar)
            self.portfolio.mark(bar.symbol, bar.close, bar.ts)
        await self.run_circuit_breakers()
        for bar in bars:
            for sig in self.strategy.on_bar(bar, self.ctx):
                await self.handle_signal(sig)

    async def on_bar(self, bar: Bar) -> None:
        await self.on_bars([bar])

    async def on_tick(self, tick: Tick) -> None:
        self.risk.on_tick(tick)
        self.portfolio.mark(tick.symbol, tick.price, tick.ts)
        for sig in self.strategy.on_tick(tick, self.ctx):
            await self.handle_signal(sig)

    async def on_book(self, book: OrderBook) -> None:
        self.risk.on_book(book)
        for sig in self.strategy.on_book(book, self.ctx):
            await self.handle_signal(sig)

    async def _on_fill(self, fill: Fill) -> None:
        self.strategy.on_fill(fill, self.ctx)

    # ------------------------------------------------------------ señales
    async def handle_signal(self, sig: Signal) -> None:
        if sig.ts < self.trading_start_ts:
            return
        self.signals_seen += 1
        decision = self.risk.evaluate(sig, self.portfolio)
        if not decision.approved:
            return
        if decision.adjustments:
            log.info("Orden ajustada por riesgo: %s", decision.adjustments)
        # El stop deseado se registra ANTES de enviar: si el fill llega en el
        # mismo await (paper inmediato), el stop nativo se coloca al instante.
        self.orders.set_desired_stop(sig.symbol, decision.stop_price)
        if decision.order is not None:
            self.orders_sent += 1
            if decision.order.reduce_only:
                await self.orders.release_stop(sig.symbol)
            await self.orders.submit(decision.order)
        await self.orders.sync_protective_stop(sig.symbol)

    # --------------------------------------------------- circuit breakers
    async def run_circuit_breakers(self) -> list[RiskAction]:
        actions = self.risk.check_circuit_breakers(self.portfolio.snapshot(self.clock.now_ms()))
        for a in actions:
            if a.type is RiskActionType.KILL:
                await self.execute_kill(a.flatten)
        # Kill con flatten pendiente (p.ej. IOC que no se llenó): reintentar.
        rec = self.risk.kill_switch.record
        if rec is not None and rec.flatten and self._kill_executed:
            open_pos = any(abs(p.qty) > 1e-12 for p in self.portfolio.positions.values())
            pending = any(
                o.tag == "flatten" and not o.is_terminal for o in self.orders.orders.values()
            )
            if open_pos and not pending:
                await self.orders.flatten_all()
        return actions

    async def execute_kill(self, flatten: bool) -> None:
        log.critical("Ejecutando KILL (flatten=%s)", flatten)
        self._kill_executed = True
        if flatten:
            await self.orders.flatten_all()
        else:
            await self.orders.cancel_all(keep_stops=True)

    def reset_after_rearm(self) -> None:
        """Llamado SOLO tras un rearme humano auditado."""
        self._kill_executed = False

    async def handle_risk_action(self, action: RiskAction) -> None:
        if action.type is RiskActionType.KILL:
            await self.execute_kill(action.flatten)
