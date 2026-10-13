"""Risk Engine: el componente con PODER DE VETO sobre toda orden (spec §3).

    Estrategia --señal--> [RISK ENGINE] --orden aprobada--> Ejecución
                               |--veto/reducción--> log + alerta
                               |--KILL--> cancelar todo, cerrar posiciones

Principios de diseño:
- Es PURO respecto de I/O: no habla con el exchange. Decide y devuelve ``RiskAction``s
  que el núcleo ejecuta. Esto lo hace 100% testeable y determinista en backtest.
- Fail-closed: ante cualquier duda (sin datos, datos viejos, vol desconocida,
  excepción interna) la respuesta es VETO.
- El kill-switch se activa acá, pero este módulo no tiene forma de desactivarlo.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from ..config import CostConfig, RiskConfig
from ..core.alerting import Alerter, Severity
from ..core.clock import MS_PER_DAY, Clock, LookaheadError, assert_not_future, utc_day
from ..core.kill_switch import KillSwitch
from ..core.models import (
    AccountMetrics,
    Bar,
    Direction,
    Fill,
    OrderBook,
    OrderRequest,
    OrderType,
    Side,
    Signal,
    Tick,
    TimeInForce,
)
from ..core.portfolio import Portfolio
from .sizing import EwmaVolatility, PositionSizer, SizingResult, TradeStats

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tipos de salida
# ---------------------------------------------------------------------------


class RiskActionType(StrEnum):
    HALT = "halt"  # no se abren posiciones nuevas; salidas permitidas
    KILL = "kill"  # cancelar todo (+ flatten opcional). Solo un humano reactiva.
    REDUCE_SIZE = "reduce_size"
    RESTORE_SIZE = "restore_size"


@dataclass(frozen=True, slots=True)
class RiskAction:
    type: RiskActionType
    reason: str
    flatten: bool = False
    until_ms: int | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str
    order: OrderRequest | None = None
    stop_price: float | None = None  # stop protectivo vigente para la posición
    sizing: SizingResult | None = None
    adjustments: tuple[str, ...] = ()

    @classmethod
    def veto(cls, reason: str) -> RiskDecision:
        return cls(False, reason)


@dataclass(slots=True)
class _Halt:
    reason: str
    until_ms: int | None  # None = hasta que la condición se limpie


@dataclass(slots=True)
class _SymbolState:
    vol: EwmaVolatility
    last_price: float | None = None
    last_data_ts: int = 0  # último mensaje de CUALQUIER tipo (liveness)
    book: OrderBook | None = None
    returns: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    _prev_close: float | None = None


class CorrelationTracker:
    """Correlación de retornos entre símbolos para el límite de exposición correlacionada."""

    def __init__(self, min_obs: int = 30) -> None:
        self.min_obs = min_obs

    @staticmethod
    def pearson(a: list[float], b: list[float]) -> float | None:
        n = min(len(a), len(b))
        if n < 3:
            return None
        a, b = a[-n:], b[-n:]
        ma, mb = sum(a) / n, sum(b) / n
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
        va = sum((x - ma) ** 2 for x in a)
        vb = sum((y - mb) ** 2 for y in b)
        if va <= 0 or vb <= 0:
            return None
        return cov / math.sqrt(va * vb)

    def cluster(self, symbol: str, states: dict[str, _SymbolState], threshold: float) -> set[str]:
        """Símbolos con corr > threshold con ``symbol`` (incluye a ``symbol``).

        Sin datos suficientes se asume correlación 1 (pesimista): en una crisis
        todo cripto se mueve como BTC.
        """
        out = {symbol}
        ra = list(states[symbol].returns)
        for other, st in states.items():
            if other == symbol:
                continue
            rb = list(st.returns)
            if min(len(ra), len(rb)) < self.min_obs:
                out.add(other)
                continue
            c = self.pearson(ra, rb)
            if c is None or c > threshold:
                out.add(other)
        return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class RiskEngine:
    def __init__(
        self,
        cfg: RiskConfig,
        costs: CostConfig,
        kill_switch: KillSwitch,
        clock: Clock,
        periods_per_year: float,
        alerter: Alerter | None = None,
    ) -> None:
        self.cfg = cfg
        self.costs = costs
        self.kill_switch = kill_switch
        self.clock = clock
        self.periods_per_year = periods_per_year
        self.alerter = alerter
        self.sizer = PositionSizer(cfg)
        self.correlation = CorrelationTracker()

        self._symbols: dict[str, _SymbolState] = {}
        self._halts: dict[str, _Halt] = {}
        self._stale_sources: set[str] = set()
        self.size_multiplier = 1.0
        self._slippage_bps: deque[float] = deque(maxlen=cfg.slippage_window)
        self._order_outcomes: deque[bool] = deque(maxlen=cfg.reject_window)  # True=rechazo
        self._order_times: deque[int] = deque()
        self._order_notionals: deque[float] = deque(maxlen=50)
        self.veto_count = 0
        # Observadores (dashboard / journal). Nunca pueden afectar la decisión.
        self.veto_listeners: list[Callable[[Signal, str], None]] = []

    # ------------------------------------------------------------------ estado
    @property
    def killed(self) -> bool:
        return self.kill_switch.engaged

    @property
    def halted(self) -> bool:
        return bool(self._halts) or bool(self._stale_sources)

    @property
    def trading_enabled(self) -> bool:
        return not self.killed and not self.halted

    @property
    def paused(self) -> bool:
        return "manual_pause" in self._halts

    def pause(self, reason: str = "pausa manual") -> RiskAction:
        """Pausa manual: como un halt (sin riesgo nuevo, salidas permitidas, stops vivos),
        pero solo se levanta con ``resume()``."""
        return self._halt("manual_pause", f"PAUSA: {reason}", None)

    def resume(self) -> bool:
        """Levanta SOLO la pausa manual. Los demás halts y el kill siguen mandando."""
        if self.killed:
            return False
        if self._halts.pop("manual_pause", None) is not None:
            self._alert(Severity.INFO, "Bot reanudado (RESUME)")
        return True

    def update_config(self, cfg: RiskConfig) -> None:
        """Aplica un RiskConfig ya validado (ver ``risk.params.apply_changes``)."""
        self.cfg = cfg
        self.sizer.cfg = cfg

    def halt_reasons(self) -> list[str]:
        return [h.reason for h in self._halts.values()] + [
            f"data_stale:{s}" for s in sorted(self._stale_sources)
        ]

    def _state(self, symbol: str) -> _SymbolState:
        st = self._symbols.get(symbol)
        if st is None:
            st = self._symbols[symbol] = _SymbolState(vol=EwmaVolatility(self.cfg.vol_ewma_span))
        return st

    # ------------------------------------------------------------ market inputs
    def on_bar(self, bar: Bar) -> None:
        assert_not_future(bar.ts, self.clock, "barra")
        st = self._state(bar.symbol)
        st.vol.update(bar.close)
        if st._prev_close:
            st.returns.append(math.log(bar.close / st._prev_close))
        st._prev_close = bar.close
        st.last_price = bar.close
        st.last_data_ts = max(st.last_data_ts, bar.ts)

    def on_tick(self, tick: Tick) -> None:
        st = self._state(tick.symbol)
        st.last_price = tick.price
        st.last_data_ts = max(st.last_data_ts, tick.ts)

    def on_book(self, book: OrderBook) -> None:
        st = self._state(book.symbol)
        st.book = book
        st.last_data_ts = max(st.last_data_ts, book.ts)

    def touch(self, symbol: str, ts: int) -> None:
        """Heartbeat de datos: cualquier mensaje del feed prueba que está vivo."""
        st = self._state(symbol)
        st.last_data_ts = max(st.last_data_ts, ts)

    def mark_data_stale(self, source: str, reason: str = "") -> None:
        """Llamado por el feed al perder la conexión: no operar a ciegas."""
        if source not in self._stale_sources:
            self._stale_sources.add(source)
            self._alert(Severity.WARNING, f"Datos stale ({source})", reason)

    def mark_data_fresh(self, source: str) -> None:
        if source in self._stale_sources:
            self._stale_sources.discard(source)
            self._alert(Severity.INFO, f"Datos recuperados ({source})")

    # -------------------------------------------------------- execution inputs
    def on_fill(self, fill: Fill) -> None:
        if fill.decision_price > 0:
            self._slippage_bps.append(fill.slippage_bps)

    def on_order_result(self, rejected: bool, reason: str = "") -> None:
        self._order_outcomes.append(rejected)
        if rejected:
            log.warning("Orden rechazada por el exchange: %s", reason)

    def report_position_mismatch(self, details: str) -> RiskAction:
        """Desync local vs exchange: kill + flatten usando la posición del EXCHANGE."""
        return self._kill(f"Desync con exchange: {details}", flatten=True)

    def manual_kill(self, reason: str, flatten: bool = True) -> RiskAction:
        return self._kill(f"Manual: {reason}", flatten=flatten)

    # ------------------------------------------------------------- evaluación
    def evaluate(self, signal: Signal, portfolio: Portfolio) -> RiskDecision:
        """Convierte una señal en una orden aprobada, reducida o vetada.

        Fail-closed: cualquier excepción interna se convierte en veto.
        """
        try:
            decision = self._evaluate(signal, portfolio)
        except LookaheadError:
            raise  # bug de backtest: tiene que explotar, no esconderse
        except Exception as exc:  # pragma: no cover - defensivo
            log.exception("Error interno del risk engine: veto por seguridad")
            decision = RiskDecision.veto(f"risk_internal_error:{exc!r}")
        if not decision.approved:
            self.veto_count += 1
            for listener in self.veto_listeners:
                try:
                    listener(signal, decision.reason)
                except Exception:  # un observador roto no puede afectar al riesgo
                    log.exception("veto listener falló")
            log.info(
                "VETO %s %s: %s",
                signal.symbol,
                signal.direction.name,
                decision.reason,
                extra={"strategy": signal.strategy_id},
            )
        return decision

    def _evaluate(self, signal: Signal, portfolio: Portfolio) -> RiskDecision:
        c = self.cfg
        now = self.clock.now_ms()
        if self.killed:
            return RiskDecision.veto("kill_switch_engaged")
        assert_not_future(signal.ts, self.clock, "señal")

        st = self._symbols.get(signal.symbol)
        if st is None or st.last_price is None:
            return RiskDecision.veto("no_market_data")
        if self._stale_sources or (now - st.last_data_ts) > c.max_data_age_s * 1000:
            return RiskDecision.veto("data_stale")
        price = st.last_price
        if abs(signal.price / price - 1) > c.max_price_deviation_pct:
            return RiskDecision.veto(f"fat_finger_price: señal {signal.price} vs último {price}")

        metrics = portfolio.snapshot(now)
        equity = metrics.equity
        if equity <= 0:
            return RiskDecision.veto("equity_non_positive")
        cur_qty = portfolio.position(signal.symbol).qty

        # 1) Tamaño objetivo -------------------------------------------------
        sizing: SizingResult | None = None
        if signal.direction is Direction.FLAT:
            target = 0.0
        else:
            assert signal.stop_price is not None  # garantizado por Signal
            sizing = self.sizer.size(
                equity=equity,
                price=price,
                annual_vol=st.vol.annualized(self.periods_per_year),
                stop_distance=abs(price - signal.stop_price),
                strength=signal.strength,
                stats=self._trade_stats(portfolio),
                size_multiplier=self.size_multiplier,
            )
            target = int(signal.direction) * sizing.qty

        adjustments: list[str] = []
        # Dar vuelta la posición se hace en dos pasos: primero cerrar (reduce-only).
        if cur_qty != 0 and target != 0 and (target > 0) != (cur_qty > 0):
            target = 0.0
            adjustments.append("flip_split_close_first")

        reducing = cur_qty != 0 and target * cur_qty >= 0 and abs(target) < abs(cur_qty)
        if self.halted and not reducing and target != 0:
            # En halt: mantener lo que hay (sin agrandar), salidas permitidas.
            target = cur_qty if cur_qty * target > 0 else 0.0
            adjustments.append("halted_no_new_risk")
            if cur_qty == 0:
                return RiskDecision.veto("halted:" + ",".join(self.halt_reasons()))

        delta = target - cur_qty
        # Banda de no-rebalanceo: evita churn (y fees) por pequeñas variaciones de vol.
        if cur_qty != 0 and target != 0 and abs(delta) < c.rebalance_threshold * abs(target):
            delta = 0.0
        stop_for_position = signal.stop_price if target != 0 else None
        if abs(delta) * price < c.min_order_notional:
            return RiskDecision(
                True, "no_change", None, stop_for_position, sizing, tuple(adjustments)
            )

        side = Side.BUY if delta > 0 else Side.SELL
        qty = abs(delta)

        # 2) Límites de exposición (solo si agrega riesgo) --------------------
        if not reducing:
            qty = self._apply_exposure_limits(
                signal.symbol, qty, price, portfolio, equity, adjustments
            )
            if qty * price < c.min_order_notional:
                return RiskDecision.veto("exposure_limits_leave_no_room")
            if qty * price > c.max_order_notional_pct * equity:
                return RiskDecision.veto(
                    f"fat_finger_notional: {qty * price:.2f} > "
                    f"{c.max_order_notional_pct:.0%} del equity"
                )
            if len(self._order_notionals) >= 5:
                avg = sum(self._order_notionals) / len(self._order_notionals)
                if avg > 0 and qty * price > c.max_order_size_vs_avg * avg:
                    return RiskDecision.veto(f"fat_finger_size: {qty * price:.2f} vs avg {avg:.2f}")

        # 3) Rate de órdenes (anti Knight Capital) ----------------------------
        while self._order_times and now - self._order_times[0] > 60_000:
            self._order_times.popleft()
        if len(self._order_times) >= c.max_orders_per_minute:
            return RiskDecision.veto("order_rate_limit")

        # 4) Slippage estimado pre-trade --------------------------------------
        est = self.estimate_slippage_bps(signal.symbol, side, qty, price)
        if est > c.max_slippage_bps:
            if reducing:
                adjustments.append(f"exit_despite_slippage_{est:.1f}bps")
            else:
                for _ in range(4):
                    qty /= 2
                    est = self.estimate_slippage_bps(signal.symbol, side, qty, price)
                    if est <= c.max_slippage_bps:
                        adjustments.append(f"slippage_reduced_qty_{est:.1f}bps")
                        break
                else:
                    return RiskDecision.veto(f"slippage_estimate_{est:.1f}bps")
                if qty * price < c.min_order_notional:
                    return RiskDecision.veto("slippage_leaves_no_size")

        # 5) Orden "market con protección": limit IOC agresiva -----------------
        prot = (c.exit_protection_bps if reducing else c.market_protection_bps) / 1e4
        limit_px = price * (1 + prot) if side is Side.BUY else price * (1 - prot)
        order = OrderRequest(
            symbol=signal.symbol,
            side=side,
            qty=qty,
            type=OrderType.LIMIT,
            price=limit_px,
            reduce_only=reducing or target == 0,
            tif=TimeInForce.IOC,
            decision_price=price,
            tag="exit" if target == 0 else ("reduce" if reducing else "entry"),
        )
        self._order_times.append(now)
        self._order_notionals.append(qty * price)
        return RiskDecision(True, "approved", order, stop_for_position, sizing, tuple(adjustments))

    def _apply_exposure_limits(
        self,
        symbol: str,
        qty: float,
        price: float,
        portfolio: Portfolio,
        equity: float,
        adjustments: list[str],
    ) -> float:
        c = self.cfg
        pos = portfolio.position(symbol)
        cur_notional = abs(pos.qty) * price
        limits = {
            "max_position_pct": c.max_position_pct * equity - cur_notional,
            "max_gross_leverage": c.max_gross_leverage * equity - portfolio.gross_exposure,
        }
        cluster = self.correlation.cluster(symbol, self._symbols, c.correlation_threshold)
        cluster_exposure = sum(
            portfolio.position(s).notional(portfolio.mark_price(s)) for s in cluster
        )
        limits["max_correlated_exposure"] = c.max_correlated_exposure * equity - cluster_exposure
        out = qty
        for name, room in limits.items():
            allowed = max(room, 0.0) / price
            if allowed < out:
                out = allowed
                adjustments.append(f"reduced_by_{name}")
        return out

    def estimate_slippage_bps(self, symbol: str, side: Side, qty: float, price: float) -> float:
        """Costo de ejecución estimado (vs mid) en bps. Usa el libro si está fresco."""
        st = self._symbols.get(symbol)
        now = self.clock.now_ms()
        book = st.book if st else None
        if book is not None and book.mid and (now - book.ts) <= self.cfg.max_data_age_s * 1000:
            avg, filled = book.walk_book(side, qty)
            if filled < qty * 0.999:
                return math.inf  # el libro no alcanza: liquidez insuficiente
            return max(0.0, side.sign * (avg - book.mid) / book.mid * 1e4)
        return self.costs.half_spread_bps + self.costs.slippage_bps

    def _trade_stats(self, portfolio: Portfolio) -> TradeStats:
        return TradeStats.from_pnls([t.pnl for t in portfolio.closed_trades[-100:]])

    # ---------------------------------------------------------- circuit breakers
    def check_circuit_breakers(self, metrics: AccountMetrics) -> list[RiskAction]:
        """Se llama tras cada evento de mercado/fill y periódicamente por un watchdog."""
        c = self.cfg
        now = self.clock.now_ms()
        actions: list[RiskAction] = []
        if self.killed:
            return actions

        # Expirar halts con fecha de vencimiento
        for key in [k for k, h in self._halts.items() if h.until_ms and h.until_ms <= now]:
            log.warning("Halt expirado: %s", self._halts[key].reason)
            del self._halts[key]

        # 1. Drawdown total desde el máximo -> KILL (requiere revisión humana)
        if metrics.drawdown_from_peak >= c.total_drawdown_kill:
            actions.append(
                self._kill(
                    f"DD total {metrics.drawdown_from_peak:.2%} >= {c.total_drawdown_kill:.0%}",
                    flatten=True,
                )
            )
            return actions

        # 2. Drawdown diario -> halt hasta el próximo día UTC (sin flatten)
        if metrics.daily_pnl_pct <= -c.daily_drawdown_halt and "daily_dd" not in self._halts:
            next_day = (utc_day(now) + 1) * MS_PER_DAY
            actions.append(
                self._halt(
                    "daily_dd",
                    f"DD diario {metrics.daily_pnl_pct:.2%} <= -{c.daily_drawdown_halt:.0%}",
                    next_day,
                )
            )

        # 3. Slippage anómalo -> liquidez rota o modelo de costos malo
        if len(self._slippage_bps) >= c.slippage_window:
            mean_slip = sum(self._slippage_bps) / len(self._slippage_bps)
            if mean_slip > c.slippage_anomaly_multiplier * c.expected_slippage_bps:
                self._slippage_bps.clear()
                actions.append(
                    self._halt(
                        "slippage",
                        f"Slippage medio {mean_slip:.1f}bps > "
                        f"{c.slippage_anomaly_multiplier}x esperado",
                        now + 3_600_000,
                    )
                )

        # 4. Rachas perdedoras -> reducir tamaño
        target_mult = (
            c.size_reduction_factor
            if metrics.consecutive_losses >= c.consecutive_losses_reduce
            else 1.0
        )
        if target_mult != self.size_multiplier:
            kind = RiskActionType.REDUCE_SIZE if target_mult < 1 else RiskActionType.RESTORE_SIZE
            self.size_multiplier = target_mult
            reason = f"{metrics.consecutive_losses} pérdidas seguidas -> size x{target_mult}"
            self._alert(Severity.WARNING, reason)
            actions.append(RiskAction(kind, reason))

        # 5. Salud técnica: datos viejos y tasa de rechazos
        for sym, st in self._symbols.items():
            age = (now - st.last_data_ts) / 1000
            key = f"stale:{sym}"
            if st.last_data_ts and age > c.max_data_age_s:
                if key not in self._halts:
                    actions.append(self._halt(key, f"Market data stale {sym} ({age:.1f}s)", None))
            elif key in self._halts:
                del self._halts[key]  # condición limpia -> se levanta sola

        n = len(self._order_outcomes)
        if n >= c.min_orders_for_reject_rate:
            rate = sum(self._order_outcomes) / n
            if rate > c.max_reject_rate:
                self._order_outcomes.clear()
                actions.append(
                    self._halt("rejects", f"Rechazos excesivos {rate:.0%}", now + 900_000)
                )
        return actions

    # ----------------------------------------------------------- observabilidad
    def breaker_readings(
        self, metrics: AccountMetrics, mismatch_strikes: int = 0
    ) -> list[dict[str, object]]:
        """Estado de cada circuit breaker para el dashboard (solo lectura).

        ``level``: fracción del umbral consumida (1.0 = disparado).
        ``status``: green < 0.5 <= yellow < 0.8 <= red (o disparado).
        """
        c = self.cfg
        now = self.clock.now_ms()
        slip = sum(self._slippage_bps) / len(self._slippage_bps) if self._slippage_bps else 0.0
        n_out = len(self._order_outcomes)
        reject_rate = sum(self._order_outcomes) / n_out if n_out else 0.0
        ages = [(now - st.last_data_ts) / 1000 for st in self._symbols.values() if st.last_data_ts]
        rows: list[tuple[str, str, float, float, str, bool]] = [
            (
                "total_drawdown",
                "DD total desde el pico",
                metrics.drawdown_from_peak,
                c.total_drawdown_kill,
                "KILL + flatten",
                self.killed,
            ),
            (
                "daily_drawdown",
                "PnL del día",
                max(0.0, -metrics.daily_pnl_pct),
                c.daily_drawdown_halt,
                "HALT hasta mañana",
                "daily_dd" in self._halts,
            ),
            (
                "slippage",
                "Slippage medio (bps)",
                slip,
                c.slippage_anomaly_multiplier * c.expected_slippage_bps,
                "HALT 1h",
                "slippage" in self._halts,
            ),
            (
                "losing_streak",
                "Pérdidas seguidas",
                float(metrics.consecutive_losses),
                float(c.consecutive_losses_reduce),
                "size x0.5",
                self.size_multiplier < 1,
            ),
            (
                "data_age",
                "Antigüedad de datos (s)",
                max(ages, default=0.0),
                c.max_data_age_s,
                "HALT hasta que vuelvan",
                bool(self._stale_sources) or any(k.startswith("stale:") for k in self._halts),
            ),
            (
                "reject_rate",
                "Tasa de rechazos",
                reject_rate,
                c.max_reject_rate,
                "HALT 15 min",
                "rejects" in self._halts,
            ),
            (
                "desync",
                "Desync con exchange (strikes)",
                float(mismatch_strikes),
                2.0,
                "KILL + flatten",
                False,
            ),
        ]
        out: list[dict[str, object]] = []
        for key, label, value, threshold, action, tripped in rows:
            level = value / threshold if threshold > 0 else 0.0
            status = "red" if tripped or level >= 0.8 else "yellow" if level >= 0.5 else "green"
            out.append(
                {
                    "key": key,
                    "label": label,
                    "value": value,
                    "threshold": threshold,
                    "level": level,
                    "status": status,
                    "tripped": tripped,
                    "action": action,
                }
            )
        return out

    # ---------------------------------------------------------------- helpers
    def _halt(self, key: str, reason: str, until_ms: int | None) -> RiskAction:
        self._halts[key] = _Halt(reason, until_ms)
        self._alert(Severity.WARNING, f"HALT: {reason}")
        return RiskAction(RiskActionType.HALT, reason, until_ms=until_ms)

    def _kill(self, reason: str, flatten: bool) -> RiskAction:
        self.kill_switch.engage(reason, flatten, self.clock.now_ms())
        self._alert(Severity.CRITICAL, f"KILL SWITCH: {reason}", "Requiere reactivación humana.")
        return RiskAction(RiskActionType.KILL, reason, flatten=flatten)

    def _alert(self, sev: Severity, title: str, body: str = "") -> None:
        if self.alerter is not None:
            self.alerter.alert(sev, title, body)
        else:
            log.warning("%s %s", title, body)

    def flatten_orders(self, positions: dict[str, float]) -> list[OrderRequest]:
        """Órdenes de cierre para el kill-switch. Camino privilegiado: sin checks.

        ``positions`` debe venir del EXCHANGE (la verdad), no del estado local.
        Usa limit agresiva (precio muy desfavorable) en vez de market pura.
        """
        out: list[OrderRequest] = []
        for sym, qty in positions.items():
            if abs(qty) < 1e-12:
                continue
            st = self._symbols.get(sym)
            px = st.last_price if st and st.last_price else None
            side = Side.SELL if qty > 0 else Side.BUY
            limit = None
            if px:
                limit = px * (0.98 if side is Side.SELL else 1.02)
            out.append(
                OrderRequest(
                    symbol=sym,
                    side=side,
                    qty=abs(qty),
                    type=OrderType.LIMIT if limit else OrderType.MARKET,
                    price=limit,
                    reduce_only=True,
                    tif=TimeInForce.IOC,
                    decision_price=px or 0.0,
                    tag="flatten",
                )
            )
        return out
