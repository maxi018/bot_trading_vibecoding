"""Carga y VALIDACIÓN de datos históricos (spec §4.1: datos sucios).

Formatos:
- CSV con columnas ``ts,open,high,low,close,volume`` (ts = apertura en ms UTC).
- Parquet vía DuckDB (opcional: ``pip install quantbot[data]``).

Ningún backtest debería correr sin pasar por ``validate_bars``.
"""

from __future__ import annotations

import csv
import heapq
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..core.models import Bar

log = logging.getLogger(__name__)


@dataclass(slots=True)
class DataReport:
    symbol: str
    n_bars: int = 0
    gaps: list[tuple[int, int]] = field(default_factory=list)
    duplicates: int = 0
    out_of_order: int = 0
    invalid: int = 0
    suspicious_jumps: list[tuple[int, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.duplicates == 0 and self.out_of_order == 0 and self.invalid == 0

    def summary(self) -> str:
        return (
            f"{self.symbol}: {self.n_bars} barras, {len(self.gaps)} huecos, "
            f"{self.duplicates} duplicadas, {self.out_of_order} desordenadas, "
            f"{self.invalid} inválidas, {len(self.suspicious_jumps)} saltos sospechosos"
        )


def _row_to_bar(symbol: str, row: dict[str, str], tf_ms: int) -> Bar:
    ts_open = int(float(row["ts"]))
    return Bar(
        symbol=symbol,
        ts_open=ts_open,
        ts=ts_open + tf_ms,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


def load_csv(path: str | Path, symbol: str, tf_ms: int) -> tuple[list[Bar], DataReport]:
    raw: list[Bar] = []
    report = DataReport(symbol)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                raw.append(_row_to_bar(symbol, row, tf_ms))
            except (ValueError, KeyError):
                report.invalid += 1
    return validate_bars(raw, tf_ms, report)


def load_parquet(path: str | Path, symbol: str, tf_ms: int) -> tuple[list[Bar], DataReport]:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Instalar extras: pip install 'quantbot[data]'") from exc
    rows = duckdb.sql(
        f"SELECT ts, open, high, low, close, volume FROM read_parquet('{Path(path)}') ORDER BY ts"
    ).fetchall()
    report = DataReport(symbol)
    raw: list[Bar] = []
    for ts, o, h, lo, c, v in rows:
        try:
            raw.append(Bar(symbol, int(ts), int(ts) + tf_ms, o, h, lo, c, v))
        except ValueError:
            report.invalid += 1
    return validate_bars(raw, tf_ms, report)


def validate_bars(
    bars: list[Bar], tf_ms: int, report: DataReport | None = None, max_jump: float = 0.5
) -> tuple[list[Bar], DataReport]:
    """Ordena, deduplica y reporta huecos/saltos. Devuelve barras limpias + reporte."""
    report = report or DataReport(bars[0].symbol if bars else "?")
    # Detectar desorden antes de ordenar (síntoma de un pipeline roto)
    report.out_of_order = sum(1 for a, b in zip(bars, bars[1:], strict=False) if b.ts < a.ts)
    clean: list[Bar] = []
    for b in sorted(bars, key=lambda x: x.ts):
        if clean and b.ts == clean[-1].ts:
            report.duplicates += 1
            continue
        if clean:
            if b.ts - clean[-1].ts > tf_ms:
                report.gaps.append((clean[-1].ts, b.ts))
            jump = abs(b.close / clean[-1].close - 1)
            if jump > max_jump:
                report.suspicious_jumps.append((b.ts, jump))
        clean.append(b)
    report.n_bars = len(clean)
    if not report.ok or report.gaps or report.suspicious_jumps:
        log.warning("Calidad de datos: %s", report.summary())
    return clean, report


def merge_streams(streams: Iterable[list[Bar]]) -> Iterator[Bar]:
    """Mezcla varios símbolos en orden cronológico (por ts de cierre, luego símbolo)."""
    return heapq.merge(*streams, key=lambda b: (b.ts, b.symbol))


def group_by_ts(bars: Iterable[Bar]) -> Iterator[list[Bar]]:
    """Agrupa barras con el mismo ts de cierre (para procesar multi-activo en bloque)."""
    group: list[Bar] = []
    for b in bars:
        if group and b.ts != group[0].ts:
            yield group
            group = []
        group.append(b)
    if group:
        yield group


def write_csv(path: str | Path, bars: list[Bar]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.ts_open, b.open, b.high, b.low, b.close, b.volume])
