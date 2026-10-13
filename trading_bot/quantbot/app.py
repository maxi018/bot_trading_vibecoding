"""Aplicación de trading en vivo / paper: el Main Event Loop asíncrono.

Arquitectura de concurrencia (clave para no quedar en estado indeterminado):

    [WS feed] ----\
    [fills]  ------>  asyncio.Queue  -->  CONSUMIDOR ÚNICO  -->  TradingCore
    [watchdog 1s] -/                      (single writer: el estado de cuenta/risk
    [reconcile Ns]/                        solo se muta desde acá, sin carreras)

    Tareas en paralelo que NO mutan estado: alertas, dead man's switch, métricas.

Supervisión: todas las tareas corren en un ``asyncio.TaskGroup``. Si UNA falla con
una excepción no manejada, se cancelan todas y se ejecuta ``safe_shutdown``:
    - trading deshabilitado,
    - se cancelan órdenes de entrada pendientes,
    - los STOPS NATIVOS QUEDAN VIVOS (posiciones protegidas aunque el bot esté caído),
    - alerta CRITICAL.
Un SIGTERM (systemd stop) hace lo mismo de forma ordenada.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass

from .config import AppConfig, Secrets
from .core.alerting import AlertChannel, Alerter, DiscordChannel, Severity, TelegramChannel
from .core.clock import LiveClock, periods_per_year, timeframe_to_ms
from .core.kill_switch import KillSwitch
from .core.models import Bar, Fill, OrderBook, Tick
from .core.portfolio import Portfolio
from .data.websocket_feed import BinanceSpotAdapter, FeedEvent, Heartbeat, ResilientWebSocketFeed
from .execution.base import AuthError, ExchangeClient
from .execution.cost_model import CostModel
from .execution.order_manager import OrderManager
from .execution.paper import PaperExchange
from .execution.rate_limiter import TokenBucket
from .risk.engine import RiskEngine
from .strategy import build_strategy
from .trading_core import TradingCore

log = logging.getLogger(__name__)

LIVE_CONFIRM_ENV = "QB_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_UNDERSTAND_THE_RISK"


# Eventos internos que viajan por la cola junto con los de mercado
@dataclass(frozen=True, slots=True)
class _FillEvent:
    fill: Fill


@dataclass(frozen=True, slots=True)
class _WatchdogTick:
    pass


@dataclass(frozen=True, slots=True)
class _ReconcileTick:
    pass


QueueItem = FeedEvent | _FillEvent | _WatchdogTick | _ReconcileTick


def build_alert_channels(secrets: Secrets) -> list[AlertChannel]:
    channels: list[AlertChannel] = []
    if secrets.telegram_token and secrets.telegram_chat_id:
        channels.append(TelegramChannel(secrets.telegram_token, secrets.telegram_chat_id))
    if secrets.discord_webhook:
        channels.append(DiscordChannel(secrets.discord_webhook))
    return channels


class TradingApp:
    def __init__(
        self,
        cfg: AppConfig,
        secrets: Secrets,
        exchange: ExchangeClient | None = None,
        feed: ResilientWebSocketFeed | None = None,
    ) -> None:
        if cfg.mode == "backtest":
            raise ValueError("Para backtest usar quantbot.backtest")
        self.cfg = cfg
        self.secrets = secrets
        self.clock = LiveClock()
        self.stop = asyncio.Event()
        self.queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=50_000)
        self.alerter = Alerter(build_alert_channels(secrets), cfg.alerts.min_interval_s)
        self.kill = KillSwitch(cfg.state_dir)
        self.risk = RiskEngine(
            cfg.risk,
            cfg.costs,
            self.kill,
            self.clock,
            periods_per_year(cfg.strategy.timeframe),
            self.alerter,
        )
        self.portfolio = Portfolio(cfg.initial_cash)
        self.exchange = exchange or self._build_exchange()
        self.limiter = TokenBucket(cfg.exchange.rate_limit_per_s, cfg.exchange.rate_limit_burst)
        self.orders = OrderManager(
            self.exchange,
            self.risk,
            self.portfolio,
            self.clock,
            self.limiter,
            self.alerter,
            max_retries=cfg.exchange.max_retries,
        )
        # Los fills entran por la cola (single writer), no directo al OrderManager.
        self.exchange.set_fill_handler(self._enqueue_fill)
        self.strategy = build_strategy(cfg.strategy)
        self.core = TradingCore(self.strategy, self.risk, self.orders, self.portfolio, self.clock)
        self.feed = feed or self._build_feed()
        self.shutdown_reason = ""

    # ----------------------------------------------------------- builders
    def _build_exchange(self) -> ExchangeClient:
        if self.cfg.mode == "paper":
            return PaperExchange(
                self.clock, CostModel(self.cfg.costs), self.cfg.initial_cash, "immediate"
            )
        from .execution.ccxt_exchange import CcxtExchange

        return CcxtExchange(self.cfg.exchange, self.secrets)

    def _build_feed(self) -> ResilientWebSocketFeed:
        adapter = BinanceSpotAdapter(
            self.cfg.strategy.symbols, self.cfg.strategy.timeframe, self.cfg.exchange.ws_url
        )
        return ResilientWebSocketFeed(
            "binance_ws",
            adapter,
            sink=self._enqueue,
            on_stale=self.risk.mark_data_stale,
            on_fresh=self.risk.mark_data_fresh,
            heartbeat_timeout_s=self.cfg.exchange.heartbeat_timeout_s,
        )

    # ------------------------------------------------------------- colas
    async def _enqueue(self, ev: QueueItem) -> None:
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            # Backpressure: el consumidor no da abasto -> datos viejos -> no operar.
            self.risk.mark_data_stale("queue", "cola de eventos llena")
            await self.queue.put(ev)

    async def _enqueue_fill(self, fill: Fill) -> None:
        await self.queue.put(_FillEvent(fill))  # los fills NUNCA se descartan

    # ---------------------------------------------------------- arranque
    async def preflight(self) -> bool:
        """Chequeos antes de operar. False = no arrancar."""
        if self.kill.engaged:
            rec = self.kill.record
            self.alerter.alert(
                Severity.CRITICAL,
                "Bot NO arranca: kill-switch activo",
                f"Motivo: {rec.reason if rec else '?'}. "
                "Rearmar con: quantbot reset-kill-switch --operator <nombre> --confirm",
            )
            return False
        if self.cfg.mode == "live":
            if os.environ.get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_VALUE:
                log.critical("Modo LIVE requiere %s=%s", LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE)
                return False
            if not (self.secrets.api_key and self.secrets.api_secret):
                log.critical("Modo LIVE sin API keys en el entorno")
                return False
        # Estado inicial desde el exchange: NUNCA confiar en el estado local.
        try:
            positions = await self.exchange.fetch_positions()
            equity = await self.exchange.fetch_equity()
        except AuthError:
            log.critical("Credenciales inválidas")
            return False
        if equity > 0 and self.cfg.mode == "live":
            self.portfolio.cash = equity
            self.portfolio.peak_equity = self.portfolio.day_start_equity = equity
        for sym, qty in positions.items():
            # Se adoptan sin tocar la caja (ya están pagadas). La estrategia les
            # asigna stop nativo en la próxima barra.
            self.portfolio.position(sym).qty = qty
        if positions:
            self.alerter.alert(
                Severity.WARNING, "Posiciones existentes adoptadas al arrancar", str(positions)
            )
        await self._warmup()
        for sym in positions:
            pos = self.portfolio.position(sym)
            pos.avg_entry = self.portfolio.marks.get(sym, 0.0)  # mejor estimación disponible
        return True

    async def _warmup(self) -> None:
        """Carga historia reciente para calentar indicadores SIN operar."""
        n = self.strategy.warmup_bars()
        try:
            import ccxt.async_support as ccxt_async
        except ImportError:
            log.warning("ccxt no instalado: sin warmup, la estrategia tardará %d barras", n)
            return
        ex = getattr(ccxt_async, self.cfg.exchange.name)({"enableRateLimit": True})
        tf_ms = timeframe_to_ms(self.cfg.strategy.timeframe)
        self.core.trading_start_ts = self.clock.now_ms()
        try:
            for sym in self.cfg.strategy.symbols:
                rows = await ex.fetch_ohlcv(
                    sym, self.cfg.strategy.timeframe, limit=min(n + 5, 1000)
                )
                now = self.clock.now_ms()
                for ts, o, h, lo, c, v in rows:
                    if ts + tf_ms > now:
                        continue  # barra en curso: todavía no existe
                    await self.core.on_bar(Bar(sym, ts, ts + tf_ms, o, h, lo, c, v))
        except Exception:
            log.exception("Warmup falló; se continúa sin historia")
        finally:
            await ex.close()

    # -------------------------------------------------------------- loops
    async def _consumer(self) -> None:
        while not (self.stop.is_set() and self.queue.empty()):
            try:
                ev = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            await self._dispatch(ev)

    async def _dispatch(self, ev: QueueItem) -> None:
        paper = self.exchange if isinstance(self.exchange, PaperExchange) else None
        match ev:
            case Heartbeat(symbol=sym, ts=ts):
                self.risk.touch(sym, ts)
            case OrderBook():
                if paper is not None:
                    paper.on_book(ev)
                    if ev.mid:
                        await paper.on_market_price(ev.symbol, ev.mid)
                await self.core.on_book(ev)
            case Bar():
                if paper is not None:
                    await paper.on_market_price(ev.symbol, ev.close)
                await self.core.on_bar(ev)
            case Tick():
                await self.core.on_tick(ev)
            case _FillEvent(fill=fill):
                await self.orders.on_fill(fill)
            case _WatchdogTick():
                await self.core.run_circuit_breakers()
            case _ReconcileTick():
                details = await self.orders.reconcile()
                if details:
                    action = self.risk.report_position_mismatch(details)
                    await self.core.handle_risk_action(action)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Duerme, pero despierta apenas se pide parar (shutdown rápido)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)

    async def _ticker(self, interval_s: float, item: QueueItem) -> None:
        while not self.stop.is_set():
            await self._sleep_or_stop(interval_s)
            if not self.stop.is_set():
                await self._enqueue(item)

    async def _dead_man_switch(self) -> None:
        """Refresca el 'cancel all after X s' del exchange. Si el bot muere, el
        exchange cancela las órdenes pendientes solo (los stops se re-colocan al volver)."""
        timeout = self.cfg.exchange.dead_man_switch_s
        supported = await self.exchange.set_dead_man_switch(timeout)
        if not supported:
            log.warning("Exchange sin dead man's switch nativo: usar watchdog externo")
            return
        while not self.stop.is_set():
            await self._sleep_or_stop(timeout / 3)
            with contextlib.suppress(Exception):
                await self.exchange.set_dead_man_switch(timeout)

    async def _metrics_logger(self) -> None:
        while not self.stop.is_set():
            await self._sleep_or_stop(60)
            m = self.portfolio.snapshot(self.clock.now_ms())
            log.info(
                "account",
                extra={
                    "equity": round(m.equity, 2),
                    "daily_pnl_pct": round(m.daily_pnl_pct, 5),
                    "drawdown": round(m.drawdown_from_peak, 5),
                    "gross_leverage": round(m.gross_leverage, 3),
                    "positions": m.open_positions,
                    "halts": self.risk.halt_reasons(),
                    "killed": self.risk.killed,
                    "feed_reconnects": self.feed.reconnects,
                    "queue": self.queue.qsize(),
                },
            )

    async def _heartbeat_pinger(self) -> None:
        """Ping a un servicio externo (healthchecks.io). Si deja de llegar, ESE servicio
        alerta y el watchdog remoto (deploy/remote_watchdog.py) actúa."""
        url = self.secrets.heartbeat_url
        if not url:
            return
        import urllib.request

        while not self.stop.is_set():
            # Solo se reporta vivo si el consumidor procesa eventos (no solo el proceso).
            if self.queue.qsize() < 1000 and not self.risk.killed:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(urllib.request.urlopen, url, None, 10)
            await self._sleep_or_stop(30)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._request_stop, sig.name)

    def _request_stop(self, why: str) -> None:
        log.warning("Parada solicitada (%s)", why)
        self.shutdown_reason = self.shutdown_reason or why
        self.stop.set()

    # --------------------------------------------------------------- run
    async def run(self) -> int:
        self._install_signal_handlers()
        if not await self.preflight():
            await self.alerter.flush()
            await self.exchange.close()
            return 2
        self.alerter.alert(
            Severity.INFO,
            f"quantbot arrancó en modo {self.cfg.mode.upper()}",
            f"símbolos={self.cfg.strategy.symbols} tf={self.cfg.strategy.timeframe}",
        )
        exit_code = 0
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.feed.run(self.stop), name="feed")
                tg.create_task(self._consumer(), name="consumer")
                tg.create_task(self._ticker(1.0, _WatchdogTick()), name="watchdog")
                tg.create_task(
                    self._ticker(self.cfg.exchange.reconcile_interval_s, _ReconcileTick()),
                    name="reconcile",
                )
                tg.create_task(self._dead_man_switch(), name="dead_man")
                tg.create_task(self._metrics_logger(), name="metrics")
                tg.create_task(self._heartbeat_pinger(), name="heartbeat")
                tg.create_task(self.alerter.run(self.stop), name="alerts")
                poll = getattr(self.exchange, "poll_fills", None)
                if poll is not None:
                    tg.create_task(poll(self.stop), name="fills")
        except* AuthError:
            self.shutdown_reason = "credenciales revocadas"
            self.risk.manual_kill("AuthError: credenciales inválidas/revocadas", flatten=False)
            exit_code = 3
        except* Exception as eg:
            for exc in eg.exceptions:
                log.critical("Tarea crítica falló", exc_info=exc)
            self.shutdown_reason = f"error no manejado: {eg.exceptions[0]!r}"
            exit_code = 1
        finally:
            await self.safe_shutdown()
        return exit_code

    async def safe_shutdown(self) -> None:
        """Llevar el sistema a un estado CONOCIDO y SEGURO antes de salir."""
        self.stop.set()
        log.warning("Safe shutdown: %s", self.shutdown_reason or "normal")
        try:
            # Entradas pendientes fuera; stops nativos se mantienen.
            await asyncio.wait_for(self.orders.cancel_all(keep_stops=True), timeout=15)
            await asyncio.wait_for(self.orders.reconcile(), timeout=15)
        except Exception:
            log.exception("Error durante safe shutdown")
        m = self.portfolio.snapshot(self.clock.now_ms())
        clean = self.shutdown_reason in ("", "SIGTERM", "SIGINT")
        sev = Severity.WARNING if clean else Severity.CRITICAL
        self.alerter.alert(
            sev,
            "quantbot detenido",
            f"motivo={self.shutdown_reason or 'normal'} equity={m.equity:.2f} "
            f"posiciones={m.open_positions} (stops nativos activos)",
        )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.alerter.flush(), timeout=10)
        with contextlib.suppress(Exception):
            await self.exchange.close()
