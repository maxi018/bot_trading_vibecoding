"""Estrategia de referencia: trend following con cruce de EMAs + stops ATR (spec §0.6).

Reglas:
- Entrada larga: EMA rápida > EMA lenta Y régimen = TREND (vol normal, mercado direccional).
- Entrada corta: espejo, solo si ``allow_short``.
- Stop inicial: entrada -/+ k * ATR. Trailing tipo chandelier: máximo desde la
  entrada - k * ATR; el stop SOLO se mueve a favor.
- Salidas: cruce en contra, cierre más allá del stop (respaldo del stop nativo en el
  exchange) o time stop (el trade no avanzó 1 ATR en N barras).
- Sin take profit fijo: unas pocas tendencias grandes pagan las muchas pérdidas chicas.
- Tras un stop-out se exige un cruce NUEVO para reentrar (evita whipsaw).

Mientras hay posición, emite en cada barra la señal vigente (dirección + stop
actualizado): el risk engine decide si hay que operar o solo mover el stop.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import StrategyConfig
from ..core.models import Bar, Direction, Signal
from .base import Strategy, StrategyContext
from .indicators import ATR, EMA
from .regime import Regime, RegimeFilter


@dataclass(slots=True)
class _SymbolState:
    fast: EMA
    slow: EMA
    atr: ATR
    regime: RegimeFilter
    prev_trend: Direction = Direction.FLAT
    direction: Direction = Direction.FLAT  # dirección del trade VIGENTE (según posición real)
    entry: float = 0.0
    extreme: float = 0.0
    stop: float = 0.0
    bars_in_trade: int = 0
    wait_for_cross: bool = False
    pending_stop: float | None = None
    exiting: bool = False  # se emitió FLAT y todavía no se confirmó el cierre


class TrendFollowingStrategy(Strategy):
    def __init__(self, cfg: StrategyConfig, strategy_id: str = "trend_v1") -> None:
        super().__init__(strategy_id, cfg.symbols)
        self.cfg = cfg
        self._st: dict[str, _SymbolState] = {
            s: _SymbolState(
                fast=EMA(cfg.fast_ema),
                slow=EMA(cfg.slow_ema),
                atr=ATR(cfg.atr_period),
                regime=RegimeFilter(cfg),
            )
            for s in cfg.symbols
        }
        self.last_regime: dict[str, Regime] = {}

    def warmup_bars(self) -> int:
        any_state = next(iter(self._st.values()))
        return max(self.cfg.slow_ema, any_state.regime.min_history + self.cfg.vol_lookback + 1)

    # ------------------------------------------------------------------------
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        st = self._st.get(bar.symbol)
        if st is None:
            return []
        fast = st.fast.update(bar.close)
        slow = st.slow.update(bar.close)
        atr = st.atr.update(bar)
        regime = st.regime.update(bar)
        self.last_regime[bar.symbol] = regime
        if not (st.slow.ready and st.atr.ready) or atr is None or atr <= 0:
            return []

        trend = Direction.LONG if fast > slow else Direction.SHORT
        crossed = st.prev_trend is not Direction.FLAT and trend is not st.prev_trend
        st.prev_trend = trend
        k = self.cfg.atr_stop_mult

        # --- Sincronizar con la posición REAL (la verdad la tiene la cuenta) ---
        qty = ctx.position_qty(bar.symbol)
        actual = Direction.LONG if qty > 0 else Direction.SHORT if qty < 0 else Direction.FLAT
        if st.exiting:
            if actual is not Direction.FLAT:
                # La salida todavía no se ejecutó (IOC sin fill, halt...): reintentar.
                return [
                    Signal(
                        self.strategy_id,
                        bar.symbol,
                        bar.ts,
                        Direction.FLAT,
                        bar.close,
                        reason="exit_retry",
                    )
                ]
            st.exiting = False
        elif st.direction is not Direction.FLAT and actual is Direction.FLAT:
            # Nos sacó el stop del exchange (o el risk engine): esperar cruce nuevo.
            self._reset(st, wait=True)
        elif st.direction is Direction.FLAT and actual is not Direction.FLAT:
            # Posición nueva (fill de nuestra entrada o adoptada al reiniciar).
            st.direction, st.entry, st.bars_in_trade = actual, bar.close, 0
            st.extreme = bar.high if actual is Direction.LONG else bar.low
            default_stop = bar.close - k * atr if actual is Direction.LONG else bar.close + k * atr
            st.stop = st.pending_stop or default_stop
            st.pending_stop = None

        if crossed:
            st.wait_for_cross = False

        # --- Gestión de la posición abierta ---------------------------------
        if st.direction is not Direction.FLAT:
            return self._manage_open(bar, st, trend, atr)

        # --- Entradas ----------------------------------------------------------
        if st.wait_for_cross or not regime.allows_entries:
            return []
        if trend is Direction.SHORT and not self.cfg.allow_short:
            return []
        stop = bar.close - k * atr if trend is Direction.LONG else bar.close + k * atr
        st.pending_stop = stop
        return [
            Signal(
                self.strategy_id,
                bar.symbol,
                bar.ts,
                trend,
                bar.close,
                stop_price=stop,
                reason=f"entry ema{self.cfg.fast_ema}/{self.cfg.slow_ema} regime={regime}",
            )
        ]

    def _manage_open(
        self, bar: Bar, st: _SymbolState, trend: Direction, atr: float
    ) -> list[Signal]:
        k = self.cfg.atr_stop_mult
        st.bars_in_trade += 1
        long = st.direction is Direction.LONG
        # Chandelier: solo se mueve a favor
        if long:
            st.extreme = max(st.extreme, bar.high)
            st.stop = max(st.stop, st.extreme - k * atr)
        else:
            st.extreme = min(st.extreme, bar.low)
            st.stop = min(st.stop, st.extreme + k * atr)

        exit_reason = ""
        if (long and bar.close <= st.stop) or (not long and bar.close >= st.stop):
            exit_reason = "stop"
        elif trend is not st.direction:
            exit_reason = "trend_reversal"
        elif self.cfg.time_stop_bars and st.bars_in_trade >= self.cfg.time_stop_bars:
            progress = (bar.close - st.entry) * (1 if long else -1)
            if progress < atr:
                exit_reason = "time_stop"

        if exit_reason:
            sig = Signal(
                self.strategy_id, bar.symbol, bar.ts, Direction.FLAT, bar.close, reason=exit_reason
            )
            self._reset(st, wait=exit_reason != "trend_reversal")
            st.exiting = True
            return [sig]
        # Stop por debajo/encima del precio (garantizado por la lógica de arriba).
        return [
            Signal(
                self.strategy_id,
                bar.symbol,
                bar.ts,
                st.direction,
                bar.close,
                stop_price=st.stop,
                reason="hold_trailing",
            )
        ]

    @staticmethod
    def _reset(st: _SymbolState, wait: bool) -> None:
        st.direction = Direction.FLAT
        st.entry = st.extreme = st.stop = 0.0
        st.bars_in_trade = 0
        st.pending_stop = None
        st.wait_for_cross = wait
        st.exiting = False
