"""Descarga de velas históricas REALES desde data.binance.vision (archivo público de Binance).

Por qué este origen y no la API REST:
- Archivos mensuales oficiales con **SHA-256 publicado**: si un byte llega mal, se rechaza.
- Sin rate limits ni API keys; no depende de que la API esté accesible desde tu país.

Detalles que muerden si no se manejan:
- Desde 2025 los klines **spot** vienen con timestamps en **microsegundos** (antes ms).
- Algunos archivos traen fila de encabezado y otros no.
- Meses anteriores al listado del par dan 404: se informan, no se inventan.
- El mes en curso no existe como archivo mensual: solo se bajan meses completos.

Solo stdlib (urllib, zipfile, hashlib): el núcleo del bot no suma dependencias.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..core.models import Bar
from .historical import DataReport, validate_bars, write_csv

log = logging.getLogger(__name__)

BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"
# Todo timestamp en ms posterior a ~año 5138 es en realidad µs.
_US_THRESHOLD = 100_000_000_000_000


class DownloadError(RuntimeError):
    pass


class NotFound(DownloadError):
    """El archivo no existe (p.ej. mes anterior al listado del par)."""


Fetcher = Callable[[str], bytes]


def _default_fetch(url: str, timeout: float = 30.0, retries: int = 4) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "quantbot-data/1.0"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data: bytes = resp.read()
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NotFound(url) from exc
            if exc.code < 500 or attempt == retries:
                raise DownloadError(f"{url}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise DownloadError(f"{url}: {exc}") from exc
        time.sleep(2**attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def raw_symbol(symbol: str) -> str:
    return symbol.replace("/", "").upper()


def parse_month(value: str) -> tuple[int, int]:
    try:
        dt = datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError(f"Mes inválido {value!r}: usar AAAA-MM") from exc
    return dt.year, dt.month


def months(start: tuple[int, int], end: tuple[int, int]) -> Iterator[tuple[int, int]]:
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def last_complete_month(now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(UTC)
    return (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)


def _verify_checksum(data: bytes, checksum_file: bytes, name: str) -> None:
    expected = checksum_file.decode().split()[0].strip().lower()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise DownloadError(f"{name}: SHA-256 no coincide (esperado {expected}, real {actual})")


def parse_klines_zip(data: bytes, symbol: str, tf_ms: int) -> tuple[list[Bar], int]:
    """Parsea un zip mensual de klines. Devuelve (barras, filas inválidas)."""
    bars: list[Bar] = []
    invalid = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            with zf.open(name) as fh:
                for row in csv.reader(io.TextIOWrapper(fh, encoding="utf-8")):
                    if not row or not row[0].strip().isdigit():
                        continue  # encabezado o línea vacía
                    try:
                        ts_open = int(row[0])
                        if ts_open >= _US_THRESHOLD:
                            ts_open //= 1000
                        bars.append(
                            Bar(
                                symbol=symbol,
                                ts_open=ts_open,
                                ts=ts_open + tf_ms,
                                open=float(row[1]),
                                high=float(row[2]),
                                low=float(row[3]),
                                close=float(row[4]),
                                volume=float(row[5]),
                            )
                        )
                    except (ValueError, IndexError):
                        invalid += 1
    return bars, invalid


@dataclass(slots=True)
class DownloadResult:
    symbol: str
    path: Path
    report: DataReport
    months_ok: list[str] = field(default_factory=list)
    months_missing: list[str] = field(default_factory=list)
    from_cache: int = 0

    def summary(self) -> str:
        first = self.months_ok[0] if self.months_ok else "-"
        last = self.months_ok[-1] if self.months_ok else "-"
        missing = f", {len(self.months_missing)} meses sin archivo" if self.months_missing else ""
        return (
            f"{self.report.summary()} | {first}..{last} "
            f"({len(self.months_ok)} meses, {self.from_cache} de caché{missing}) -> {self.path}"
        )


def download_klines(
    symbol: str,
    timeframe: str,
    tf_ms: int,
    start: tuple[int, int],
    end: tuple[int, int],
    out_dir: str | Path,
    cache_dir: str | Path | None = None,
    fetch: Fetcher = _default_fetch,
) -> DownloadResult:
    """Baja, verifica y une los meses ``start..end`` en ``out_dir/<SYM>_<tf>.csv``."""
    if end > last_complete_month():
        end = last_complete_month()
    if start > end:
        raise ValueError(f"Rango vacío: {start} > {end}")
    raw = raw_symbol(symbol)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(cache_dir) if cache_dir else out_dir / ".cache" / "binance"
    cache.mkdir(parents=True, exist_ok=True)

    bars: list[Bar] = []
    report = DataReport(symbol)
    result = DownloadResult(symbol, out_dir / f"{raw}_{timeframe}.csv", report)
    for y, m in months(start, end):
        name = f"{raw}-{timeframe}-{y:04d}-{m:02d}.zip"
        url = f"{BASE_URL}/{raw}/{timeframe}/{name}"
        cached = cache / name
        try:
            checksum = fetch(url + ".CHECKSUM")
        except NotFound:
            result.months_missing.append(f"{y:04d}-{m:02d}")
            continue
        if cached.exists():
            data = cached.read_bytes()
            try:
                _verify_checksum(data, checksum, name)
                result.from_cache += 1
            except DownloadError:
                log.warning("Caché corrupta para %s: se vuelve a bajar", name)
                data = b""
        else:
            data = b""
        if not data:
            data = fetch(url)
            _verify_checksum(data, checksum, name)  # falla fuerte: nunca datos sin verificar
            tmp = cached.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(cached)
        month_bars, invalid = parse_klines_zip(data, symbol, tf_ms)
        report.invalid += invalid
        bars += month_bars
        result.months_ok.append(f"{y:04d}-{m:02d}")

    if not bars:
        raise DownloadError(f"{symbol} {timeframe}: no hay datos en el rango pedido")
    clean, report = validate_bars(bars, tf_ms, report)
    result.report = report
    write_csv(result.path, clean)
    return result
