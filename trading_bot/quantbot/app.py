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
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import AppConfig, Secrets
from .core.alerting import (
    Alert,
    AlertChannel,
    Alerter,
    DiscordChannel,
    Severity,
    TelegramChannel,
)
from .core.clock import Clock, LiveClock, SimClock, periods_per_year, timeframe_to_ms
from .core.journal import Journal, TradeRecorder
from .core.kill_switch import KillSwitch, human_reset
from .core.models import Bar, Fill, OrderBook, Signal, Tick
from .core.portfolio import Portfolio
from .data.demo_feed import DemoFeed
from .data.websocket_feed import (
    BinanceSpotAdapter,
    FeedEvent,
    Heartbeat,
    ResilientWebSocketFeed,
)
from .execution.base import AuthError, ExchangeClient
from .execution.cost_model import CostModel
from .execution.order_manager import OrderManager
from .execution.paper import PaperExchange
from .execution.rate_limiter import NullRateLimiter, TokenBucket
from .risk.engine import RiskEngine
from .risk.params import ParamChange, RiskParamError, apply_changes
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


@dataclass(frozen=True, slots=True)
class _KillExecute:
    """Ejecución del kill pedido por un operador (cancelar + flatten) dentro del
    consumidor único. El FLAG de kill ya se activó en el momento del pedido."""

    flatten: bool


QueueItem = FeedEvent | _FillEvent | _WatchdogTick | _ReconcileTick | _KillExecute

UiEvent = dict[str, Any]


class Feed(Protocol):
    reconnects: int
    connected: bool
    last_msg_ms: int

    async def run(self, stop: asyncio.Event) -> None: ...


class CommandError(ValueError):
    """Comando de operador inválido o no permitido en el estado actual."""


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
        feed: Feed | None = None,
        clock: Clock | None = None,
    ) -> None:
        if cfg.mode == "backtest":
            raise ValueError("Para backtest usar quantbot.backtest")
        self.cfg = cfg
        self.secrets = secrets
        # En demo el tiempo lo marca el feed sintético (SimClock); si no, reloj real.
        self.clock: Clock = clock or (SimClock(0) if cfg.mode == "demo" else LiveClock())
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
        # Demo: el "exchange" es local, no hay límite que proteger.
        self.limiter = (
            NullRateLimiter()
            if cfg.mode == "demo"
            else TokenBucket(cfg.exchange.rate_limit_per_s, cfg.exchange.rate_limit_burst)
        )
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
        self.feed: Feed = feed or self._build_feed()
        self.shutdown_reason = ""
        self._consumer_task: asyncio.Task[None] | None = None

        # ---- Observabilidad para el dashboard (nunca afecta decisiones) ----
        self.started_at = time.time()
        self.journal = Journal(Path(cfg.state_dir) / "journal.sqlite")
        expected = cfg.costs.half_spread_bps + cfg.costs.slippage_bps
        self.recorder = TradeRecorder(self.journal, expected)
        self.bars: dict[str, deque[Bar]] = {s: deque(maxlen=2000) for s in cfg.strategy.symbols}
        self.ui_listeners: list[Callable[[UiEvent], None]] = []
        self.recent_events: deque[UiEvent] = deque(maxlen=300)
        self.last_market_wall = 0.0  # time.monotonic() del último dato de mercado
        self.feed_lag_ms: float | None = None  # reloj local - timestamp del exchange
        self.exchange_rtt_ms: float | None = None
        self.orders.add_fill_listener(self._on_fill_observed)
        self.risk.veto_listeners.append(self._on_veto_observed)
        self.alerter.listeners.append(self._on_alert_observed)

    # ----------------------------------------------------------- builders
    def _build_exchange(self) -> ExchangeClient:
        if self.cfg.mode in ("paper", "demo"):
            return PaperExchange(
                self.clock, CostModel(self.cfg.costs), self.cfg.initial_cash, "immediate"
            )
        from .execution.ccxt_exchange import CcxtExchange

        return CcxtExchange(self.cfg.exchange, self.secrets)

    def _build_feed(self) -> Feed:
        if self.cfg.mode == "demo":
            assert isinstance(self.clock, SimClock)
            d = self.cfg.demo
            return DemoFeed(
                self.cfg.strategy.symbols,
                self.cfg.strategy.timeframe,
                self.clock,
                sink=self._enqueue,
                on_stale=self.risk.mark_data_stale,
                on_fresh=self.risk.mark_data_fresh,
                bars_per_second=d.bars_per_second,
                warmup_bars=d.warmup_bars,
                seed=d.seed,
                start_price=d.start_price,
            )
        ws_url, rest = BinanceSpotAdapter.endpoints(self.cfg.mode, self.cfg.exchange)
        if rest == BinanceSpotAdapter.TESTNET_REST:
            log.warning("Live en TESTNET: market data de %s", ws_url)
        adapter = BinanceSpotAdapter(
            self.cfg.strategy.symbols, self.cfg.strategy.timeframe, ws_url, rest_base=rest
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
        if self._consumer_task is not None and asyncio.current_task() is self._consumer_task:
            # El fill nació DENTRO del consumidor (paper/demo ejecuta en create_order o al
            # disparar un stop): se aplica ya. Sigue siendo single-writer (misma tarea) y
            # evita decidir con un portfolio atrasado.
            await self._dispatch(_FillEvent(fill))
        else:
            await self.queue.put(_FillEvent(fill))  # los fills NUNCA se descartan

    # ---------------------------------------------------------- arranque
    async def preflight(self) -> bool:
        """Chequeos antes de operar. False = no arrancar."""
        if self.kill.engaged:
            rec = self.kill.record
            if not self.cfg.api.enabled:
                self.alerter.alert(
                    Severity.CRITICAL,
                    "Bot NO arranca: kill-switch activo",
                    f"Motivo: {rec.reason if rec else '?'}. "
                    "Rearmar con: quantbot reset-kill-switch --operator <nombre> --confirm",
                )
                return False
            # Con dashboard: arranca en estado KILLED (el risk engine veta TODO) para
            # que el operador pueda inspeccionar y, si corresponde, rearmar.
            self.alerter.alert(
                Severity.CRITICAL,
                "Bot arrancó en estado KILLED: no opera",
                f"Motivo: {rec.reason if rec else '?'}. Revisar y rearmar desde el dashboard.",
            )
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
        if isinstance(self.feed, DemoFeed):
            self.core.trading_start_ts = self.feed.live_from_ts  # el demo trae su historia
            return
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
        self._consumer_task = asyncio.current_task()
        while not (self.stop.is_set() and self.queue.empty()):
            try:
                ev = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if self.stop.is_set() and not isinstance(ev, (_FillEvent, _KillExecute)):
                # Parando: los fills y kills pendientes se aplican SIEMPRE; los datos de
                # mercado viejos se descartan (no se opera después de pedir la parada).
                continue
            await self._dispatch(ev)

    async def _dispatch(self, ev: QueueItem) -> None:
        paper = self.exchange if isinstance(self.exchange, PaperExchange) else None
        if isinstance(ev, Heartbeat) and isinstance(self.clock, SimClock):
            self.clock.advance_to(max(self.clock.now_ms(), ev.ts))  # demo: tiempo en orden
        if isinstance(ev, (Heartbeat, OrderBook, Bar, Tick)):
            self._observe_market(ev)
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
                self._after_bar(ev)
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
            case _KillExecute(flatten=flatten):
                await self.core.execute_kill(flatten)

    # ------------------------------------------------------ observabilidad
    def _publish(self, ev: UiEvent) -> None:
        self.recent_events.append(ev)
        for listener in list(self.ui_listeners):
            try:
                listener(ev)
            except Exception:
                log.exception("ui listener falló")

    def _observe_market(self, ev: Heartbeat | OrderBook | Bar | Tick) -> None:
        self.last_market_wall = time.monotonic()
        if isinstance(self.clock, LiveClock):
            ts = ev.ts if not isinstance(ev, OrderBook) or ev.ts else 0
            if ts and not isinstance(ev, Bar):
                self.feed_lag_ms = max(0.0, time.time() * 1000 - ts)
        else:
            self.feed_lag_ms = 0.0

    def _after_bar(self, bar: Bar) -> None:
        buf = self.bars.get(bar.symbol)
        if buf is None:
            buf = self.bars[bar.symbol] = deque(maxlen=2000)
        buf.append(bar)
        # La curva de equity arranca cuando el bot OPERA: la historia del warmup
        # (retornos en cero) diluiría la volatilidad y falsearía Sharpe/Sortino.
        if bar.ts >= self.core.trading_start_ts:
            m = self.portfolio.snapshot(self.clock.now_ms())
            self.journal.record_equity(bar.ts, m.equity, m.drawdown_from_peak)
        self._publish(
            {
                "type": "bar",
                "symbol": bar.symbol,
                "bar": {
                    "time": bar.ts_open // 1000,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                },
            }
        )

    async def _on_fill_observed(self, fill: Fill) -> None:
        order = self.orders.orders.get(fill.client_order_id)
        tag = order.tag if order else ""
        self.recorder.on_fill(fill, tag)
        self._publish(
            {
                "type": "fill",
                "ts": fill.ts,
                "symbol": fill.symbol,
                "side": str(fill.side),
                "qty": fill.qty,
                "price": fill.price,
                "tag": tag,
                "slippage_bps": fill.slippage_bps if fill.decision_price > 0 else None,
            }
        )

    def _on_veto_observed(self, sig: Signal, reason: str) -> None:
        self.journal.record_event(
            sig.ts, "veto", "INFO", f"VETO {sig.symbol} {sig.direction.name}", reason
        )
        self._publish(
            {
                "type": "veto",
                "ts": sig.ts,
                "symbol": sig.symbol,
                "direction": sig.direction.name,
                "reason": reason,
            }
        )

    def _on_alert_observed(self, a: Alert) -> None:
        ts = self.clock.now_ms()
        self.journal.record_event(ts, "alert", str(a.severity), a.title, a.body)
        self._publish(
            {
                "type": "alert",
                "ts": ts,
                "severity": str(a.severity),
                "title": a.title,
                "body": a.body,
            }
        )

    def _audit(self, operator: str, action: str, details: Any = "") -> None:
        ts = self.clock.now_ms()
        self.journal.record_event(ts, "command", "WARNING", f"{action} por {operator}", details)
        self._publish(
            {
                "type": "command",
                "ts": ts,
                "operator": operator,
                "action": action,
                "details": details,
            }
        )
        log.warning("COMANDO %s por %s: %s", action, operator, details)

    async def _latency_probe(self) -> None:
        """RTT al exchange cada 15 s (a través del rate limiter)."""
        while not self.stop.is_set():
            t0 = time.perf_counter()
            try:
                await self.limiter.acquire()
                await asyncio.wait_for(self.exchange.fetch_equity(), timeout=10)
                self.exchange_rtt_ms = (time.perf_counter() - t0) * 1000
            except Exception:
                self.exchange_rtt_ms = None
            await self._sleep_or_stop(15)

    # ------------------------------------------------- comandos de operador
    # Todos pasan por el risk engine. Ninguno manda órdenes al exchange directo:
    # el kill usa el mismo camino privilegiado que un circuit breaker.

    @property
    def status(self) -> str:
        if self.risk.killed:
            return "KILLED"
        if self.risk.paused:
            return "PAUSED"
        if self.risk.halted:
            return "HALTED"
        return "RUNNING"

    async def cmd_kill(self, operator: str, reason: str, flatten: bool = True) -> None:
        if self.risk.killed:
            raise CommandError("El kill-switch ya está activo")
        # 1) El flag se activa YA (persistido en disco): desde este instante el risk
        #    engine veta todo. 2) La cancelación/flatten se ejecuta en el consumidor.
        self.risk.manual_kill(f"{reason} (operador: {operator})", flatten=flatten)
        self._audit(operator, "KILL", {"reason": reason, "flatten": flatten})
        await self.queue.put(_KillExecute(flatten))

    def cmd_pause(self, operator: str, reason: str = "") -> None:
        if self.risk.killed:
            raise CommandError("Bot en KILL: no aplica pausa")
        if not self.risk.paused:
            self.risk.pause(f"{reason or 'manual'} (operador: {operator})")
            self._audit(operator, "PAUSE", reason)

    def cmd_resume(self, operator: str) -> None:
        if self.risk.killed:
            raise CommandError("Bot en KILL: primero rearmar (REACTIVAR)")
        if self.risk.paused:
            self.risk.resume()
            self._audit(operator, "RESUME")

    def cmd_rearm(self, operator: str, confirm_text: str) -> None:
        """Rearme humano tras un kill. Queda PAUSADO: operar exige un RESUME aparte."""
        if confirm_text != "REACTIVAR":
            raise CommandError("Confirmación inválida: escribir exactamente REACTIVAR")
        if not operator.strip():
            raise CommandError("Se requiere nombre de operador")
        if not self.risk.killed:
            raise CommandError("El kill-switch no está activo")
        human_reset(self.cfg.state_dir, operator, source="dashboard")
        self.kill.reload()
        self.core.reset_after_rearm()
        self.risk.pause(f"rearmado por {operator}: revisar posiciones y hacer RESUME")
        self._audit(operator, "REARM")

    def cmd_set_reference(self, operator: str, reference: dict[str, Any]) -> None:
        """Guarda un backtest terminado como referencia para comparar contra live."""
        import json

        path = Path(self.cfg.state_dir) / "reference_backtest.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reference, default=str), encoding="utf-8")
        tmp.replace(path)
        self._audit(operator, "SET_REFERENCE", {"job_id": reference.get("job_id")})

    def cmd_update_risk(
        self, operator: str, changes: dict[str, float], confirm_relax: bool
    ) -> list[ParamChange]:
        try:
            new_risk, diff = apply_changes(
                self.cfg, changes, paused=self.risk.paused, confirm_relax=confirm_relax
            )
        except RiskParamError as exc:
            raise CommandError(str(exc)) from exc
        if diff:
            import dataclasses

            self.cfg = dataclasses.replace(self.cfg, risk=new_risk)
            self.risk.update_config(new_risk)
            self._audit(
                operator,
                "RISK_PARAMS",
                {c.name: {"old": c.old, "new": c.new, "relaxes": c.relaxes} for c in diff},
            )
        return diff

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
                tg.create_task(self._latency_probe(), name="latency")
                if self.cfg.api.enabled:
                    tg.create_task(self._run_api(), name="api")
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

    async def _run_api(self) -> None:
        """API del dashboard, AISLADA: si falla, se loguea y el trading sigue."""
        try:
            from .api.server import serve_api

            await serve_api(self, self.stop)
        except asyncio.CancelledError:
            raise
        except (Exception, SystemExit):  # uvicorn hace sys.exit si el puerto está ocupado
            log.exception("La API del dashboard falló; el bot sigue operando sin ella")
            self.alerter.alert(Severity.WARNING, "Dashboard API caída (el bot sigue)")

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
        with contextlib.suppress(Exception):
            self.journal.close()
