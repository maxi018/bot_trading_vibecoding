"""Modelo de costos realista (spec §4.3). El backtest miente hasta que se demuestre lo contrario.

Componentes:
1. Latencia: el precio se movió mientras la orden viajaba (fill en la barra SIGUIENTE).
2. Spread: una orden a mercado cruza medio spread.
3. Impacto: slippage adicional (o walk-the-book si hay libro).
4. Comisiones maker/taker.
5. Limit orders: NO se asume fill solo porque el precio tocó el nivel;
   tiene que ATRAVESARLO (había cola delante).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import CostConfig
from ..core.models import Bar, OrderBook, Side


@dataclass(frozen=True, slots=True)
class FillQuote:
    price: float
    qty: float
    is_maker: bool
    fee: float


class CostModel:
    def __init__(self, cfg: CostConfig, stress_multiplier: float = 1.0) -> None:
        self.cfg = cfg
        # >1 para stress tests: "¿qué pasa si los costos son el doble?"
        self.stress = stress_multiplier

    def taker_price(self, side: Side, ref_price: float) -> float:
        bps = (self.cfg.half_spread_bps + self.cfg.slippage_bps) * self.stress
        return ref_price * (1 + side.sign * bps / 1e4)

    def fee(self, notional: float, is_maker: bool) -> float:
        rate = self.cfg.maker_fee if is_maker else self.cfg.taker_fee
        return abs(notional) * rate * (self.stress if rate > 0 else 1.0)

    def market_fill_on_bar(self, side: Side, qty: float, bar: Bar) -> FillQuote:
        """Orden a mercado enviada al cierre anterior -> se ejecuta en la apertura."""
        px = self.taker_price(side, bar.open)
        # Nunca mejor que el rango real de la barra (sanity)
        px = min(max(px, bar.low), bar.high) if self.stress <= 1.0 else px
        return FillQuote(px, qty, False, self.fee(px * qty, False))

    def market_fill_on_book(self, side: Side, qty: float, book: OrderBook) -> FillQuote | None:
        avg, filled = book.walk_book(side, qty)
        if filled <= 0:
            return None
        extra = self.cfg.slippage_bps * (self.stress - 1.0) / 1e4 if self.stress > 1 else 0.0
        px = avg * (1 + side.sign * extra)
        return FillQuote(px, filled, False, self.fee(px * filled, False))

    @staticmethod
    def limit_trades_through(side: Side, limit: float, bar: Bar) -> bool:
        """Conservador: exige que el precio ATRAVIESE el límite, no que lo toque."""
        return bar.low < limit if side is Side.BUY else bar.high > limit

    def stop_fill_on_bar(
        self, side: Side, qty: float, stop: float, bar: Bar, armed_at_open: bool
    ) -> FillQuote | None:
        """Stop-market: se dispara si el rango toca el stop. Gap -> fill en la apertura
        (peor que el stop). Se ejecuta como taker con slippage."""
        if side is Side.SELL:  # stop de una posición larga
            if bar.low > stop:
                return None
            ref = min(bar.open, stop) if armed_at_open else stop
        else:
            if bar.high < stop:
                return None
            ref = max(bar.open, stop) if armed_at_open else stop
        px = self.taker_price(side, ref)
        return FillQuote(px, qty, False, self.fee(px * qty, False))
