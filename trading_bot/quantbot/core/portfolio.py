"""Estado de cuenta local: posiciones, caja, marks y métricas en tiempo real.

Ojo: este es el estado LOCAL. La verdad está en el exchange; el ``OrderManager``
reconcilia periódicamente y ante discrepancia el risk engine ejecuta el kill-switch.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import utc_day
from .models import AccountMetrics, Fill, Position


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    symbol: str
    pnl: float  # neto de fees de salida (aprox.)
    ts: int
    notional: float = 0.0  # tamaño máximo de la posición durante el trade (|qty| x precio)


class Portfolio:
    def __init__(self, initial_cash: float) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash debe ser > 0")
        self.cash = initial_cash
        self.positions: dict[str, Position] = {}
        self.marks: dict[str, float] = {}
        self.fees_paid = 0.0
        self.peak_equity = initial_cash
        self.day_start_equity = initial_cash
        self._day: int | None = None
        self.consecutive_losses = 0
        self.closed_trades: list[ClosedTrade] = []
        self._trade_pnl: dict[str, float] = {}
        self._trade_notional: dict[str, float] = {}
        self._last_ts = 0

    # -- updates --------------------------------------------------------------
    def position(self, symbol: str) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = self.positions[symbol] = Position(symbol)
        return pos

    def apply_fill(self, fill: Fill) -> float:
        """Aplica un fill. Devuelve PnL realizado neto del fee de este fill."""
        pos = self.position(fill.symbol)
        prev_qty = pos.qty
        realized = pos.apply_fill(fill.side, fill.qty, fill.price)
        self.cash -= fill.side.sign * fill.qty * fill.price + fill.fee
        self.fees_paid += fill.fee
        self.marks.setdefault(fill.symbol, fill.price)
        net = realized - fill.fee
        # PnL acumulado del "trade" (entrada -> salida completa), fees incluidos.
        self._trade_pnl[fill.symbol] = self._trade_pnl.get(fill.symbol, 0.0) + net
        closed = prev_qty != 0 and (pos.qty == 0 or (pos.qty > 0) != (prev_qty > 0))
        sym = fill.symbol
        if prev_qty != 0:
            self._trade_notional[sym] = max(
                self._trade_notional.get(sym, 0.0), abs(prev_qty) * fill.price
            )
        if closed:
            trade_pnl = self._trade_pnl.pop(sym)
            notional = self._trade_notional.pop(sym, 0.0)
            self.closed_trades.append(ClosedTrade(sym, trade_pnl, fill.ts, notional))
            self.consecutive_losses = self.consecutive_losses + 1 if trade_pnl < 0 else 0
        if pos.qty != 0:  # apertura, aumento o el tramo nuevo de un flip
            self._trade_notional[sym] = max(
                self._trade_notional.get(sym, 0.0), abs(pos.qty) * fill.price
            )
        self._update_peaks(fill.ts)
        return net

    def mark(self, symbol: str, price: float, ts: int) -> None:
        if price > 0:
            self.marks[symbol] = price
        self._update_peaks(ts)

    def _update_peaks(self, ts: int) -> None:
        self._last_ts = max(self._last_ts, ts)
        eq = self.equity
        day = utc_day(ts)
        if self._day is None or day != self._day:
            self._day = day
            self.day_start_equity = eq
        self.peak_equity = max(self.peak_equity, eq)

    # -- lecturas -------------------------------------------------------------
    def mark_price(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return self.marks.get(symbol, pos.avg_entry if pos else 0.0)

    @property
    def equity(self) -> float:
        return self.cash + sum(p.qty * self.mark_price(s) for s, p in self.positions.items())

    @property
    def gross_exposure(self) -> float:
        return sum(p.notional(self.mark_price(s)) for s, p in self.positions.items())

    @property
    def net_exposure(self) -> float:
        return sum(p.qty * self.mark_price(s) for s, p in self.positions.items())

    def snapshot(self, ts: int | None = None) -> AccountMetrics:
        return AccountMetrics(
            ts=ts if ts is not None else self._last_ts,
            equity=self.equity,
            cash=self.cash,
            peak_equity=self.peak_equity,
            day_start_equity=self.day_start_equity,
            gross_exposure=self.gross_exposure,
            net_exposure=self.net_exposure,
            realized_pnl=sum(p.realized_pnl for p in self.positions.values()),
            unrealized_pnl=sum(p.unrealized(self.mark_price(s)) for s, p in self.positions.items()),
            fees_paid=self.fees_paid,
            consecutive_losses=self.consecutive_losses,
            open_positions=sum(1 for p in self.positions.values() if p.qty != 0),
        )
