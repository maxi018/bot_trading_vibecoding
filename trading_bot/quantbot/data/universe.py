"""Universo de una canasta: elegir monedas sin mirar el futuro y empalmar tickers.

- ``rank_by_quote_volume``: ranking por volumen en la moneda de cotización (Σ volumen ×
  cierre) en una ventana del PASADO. Elegir las ganadoras de hoy es sesgo de
  supervivencia; elegir por lo que se veía entonces, no.
- ``splice_bars``: el mismo activo con tickers sucesivos (cambios 1:1 como BCHABC → BCH
  o EOS → A) es UNA serie. Tratarlos como monedas distintas inventa una venta y una
  compra que nunca existieron.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

from ..core.models import Bar


class SpliceError(ValueError):
    """Los tramos no parecen el mismo activo (salto de precio en la unión)."""


def splice_bars(parts: Sequence[Sequence[Bar]], symbol: str, max_jump: float = 0.25) -> list[Bar]:
    """Empalma tramos en orden. Lo que el tramo nuevo repite del anterior se descarta, y en
    la unión el precio no puede saltar más de ``max_jump`` (un cambio 1:1 no salta)."""
    out: list[Bar] = []
    for part in parts:
        bars = sorted(part, key=lambda b: b.ts)
        if out:
            bars = [b for b in bars if b.ts > out[-1].ts]
            if bars:
                jump = abs(bars[0].open / out[-1].close - 1)
                if jump > max_jump:
                    raise SpliceError(
                        f"{symbol}: salto de {jump:.0%} entre {out[-1].symbol} y "
                        f"{bars[0].symbol}: no parece un cambio de ticker 1:1"
                    )
        out += [dataclasses.replace(b, symbol=symbol) for b in bars]
    return out


def rank_by_quote_volume(
    series: Mapping[str, Sequence[Bar]], start_ms: int, end_ms: int
) -> list[tuple[str, float]]:
    """Volumen en la moneda de cotización con apertura en [start_ms, end_ms), de mayor a
    menor. Los símbolos sin velas en la ventana (todavía no listados) quedan con 0."""
    vol = {
        sym: sum(b.volume * b.close for b in bars if start_ms <= b.ts_open < end_ms)
        for sym, bars in series.items()
    }
    return sorted(vol.items(), key=lambda kv: (-kv[1], kv[0]))
