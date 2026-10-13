"""Reversión a la media dentro de una tendencia alcista (experimentos #5/#6).

Reglas (pre-registradas en research/LOG.md; solo largos):
- Tendencia: el cierre diario está sobre su media de ``trend_filter_days`` días.
- Entrada: RSI(``mr_rsi_period``) < ``mr_entry_rsi`` (caída brusca de corto plazo).
- Salidas: cierre > SMA(``mr_exit_sma``) (rebotó), ``mr_max_bars`` barras sin rebote,
  el filtro de tendencia se apaga, o el cierre perfora el stop (respaldo del nativo).
- Stop nativo fijo: entrada - ``atr_stop_mult`` * ATR. Sin trailing: el trade dura
  pocas barras y se cierra por el rebote, no por el stop.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import StrategyConfig
from ..core.clock import MS_PER_DAY, timeframe_to_ms
from ..core.models import Bar, Direction, Signal
from .base import Strategy, StrategyContext
from .indicators import ATR, RSI, SMA, DailySMA


@dataclass(slots=True)
class _State:
    rsi: RSI
    sma: SMA
    atr: ATR
    daily: DailySMA | None
    in_trade: bool = False
    stop: float = 0.0
    bars_in_trade: int = 0
    pending_stop: float | None = None
    exiting: bool = False  # se emitió FLAT y todavía no se confirmó el cierre


class MeanReversionStrategy(Strategy):
    def __init__(self, cfg: StrategyConfig, strategy_id: str = "meanrev_v1") -> None:
        super().__init__(strategy_id, cfg.symbols)
        self.cfg = cfg
        self._st: dict[str, _State] = {
            s: _State(
                rsi=RSI(cfg.mr_rsi_period),
                sma=SMA(cfg.mr_exit_sma),
                atr=ATR(cfg.atr_period),
                daily=DailySMA(cfg.trend_filter_days) if cfg.trend_filter_days else None,
            )
            for s in cfg.symbols
        }

    def warmup_bars(self) -> int:
        bars = max(self.cfg.mr_rsi_period + 1, self.cfg.mr_exit_sma, self.cfg.atr_period + 1)
        if self.cfg.trend_filter_days:
            per_day = max(1, MS_PER_DAY // timeframe_to_ms(self.cfg.timeframe))
            bars = max(bars, (self.cfg.trend_filter_days + 1) * per_day)
        return bars

    @staticmethod
    def _uptrend(st: _State) -> bool:
        """Sin filtro: siempre. Con filtro y sin historia suficiente: nunca (fail-closed)."""
        if st.daily is None:
            return True
        sma, last = st.daily.value, st.daily.last_close
        return sma is not None and last is not None and last > sma

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]:
        st = self._st.get(bar.symbol)
        if st is None:
            return []
        rsi = st.rsi.update(bar.close)
        sma = st.sma.update(bar.close)
        atr = st.atr.update(bar)
        if st.daily is not None:
            st.daily.update(bar)
        if rsi is None or sma is None or atr is None or atr <= 0:
            return []

        # --- Sincronizar con la posición REAL ---------------------------------
        held = ctx.position_qty(bar.symbol) > 0
        if st.exiting:
            if held:  # la salida no se ejecutó todavía: reintentar
                return [self._flat(bar, "exit_retry")]
            st.exiting = False
        elif st.in_trade and not held:
            self._reset(st)  # lo sacó el stop nativo (o el risk engine)
        elif not st.in_trade and held:
            st.in_trade, st.bars_in_trade = True, 0
            st.stop = st.pending_stop or bar.close - self.cfg.atr_stop_mult * atr
            st.pending_stop = None

        # --- Posición abierta ----------------------------------------------------
        if st.in_trade:
            st.bars_in_trade += 1
            reason = ""
            if bar.close <= st.stop:
                reason = "stop"
            elif not self._uptrend(st):
                reason = "trend_filter"
            elif bar.close > sma:
                reason = "reverted"
            elif st.bars_in_trade >= self.cfg.mr_max_bars:
                reason = "time_stop"
            if reason:
                self._reset(st)
                st.exiting = True
                return [self._flat(bar, reason)]
            return [
                Signal(
                    self.strategy_id,
                    bar.symbol,
                    bar.ts,
                    Direction.LONG,
                    bar.close,
                    stop_price=st.stop,
                    reason="hold",
                )
            ]

        # --- Entrada -------------------------------------------------------------
        if rsi >= self.cfg.mr_entry_rsi or not self._uptrend(st):
            return []
        stop = bar.close - self.cfg.atr_stop_mult * atr
        st.pending_stop = stop
        return [
            Signal(
                self.strategy_id,
                bar.symbol,
                bar.ts,
                Direction.LONG,
                bar.close,
                stop_price=stop,
                reason=f"entry rsi{self.cfg.mr_rsi_period}={rsi:.1f}",
            )
        ]

    def _flat(self, bar: Bar, reason: str) -> Signal:
        return Signal(
            self.strategy_id, bar.symbol, bar.ts, Direction.FLAT, bar.close, reason=reason
        )

    @staticmethod
    def _reset(st: _State) -> None:
        st.in_trade = False
        st.stop = 0.0
        st.bars_in_trade = 0
        st.pending_stop = None
        st.exiting = False
