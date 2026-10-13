"""Descarga de datos reales: checksum, µs vs ms, encabezados, huecos y caché (sin red)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.__main__ import _backtest_config, main
from quantbot.data.binance_vision import (
    BASE_URL,
    DownloadError,
    NotFound,
    download_klines,
    last_complete_month,
    months,
    parse_month,
)
from quantbot.data.historical import load_csv

H = 3_600_000


def _month_start_ms(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)


def _zip(y: int, m: int, n: int = 24, micro: bool = False, header: bool = False) -> bytes:
    rows = []
    if header:
        rows.append("open_time,open,high,low,close,volume,close_time,qav,n,tbb,tbq,ignore")
    t0 = _month_start_ms(y, m)
    for i in range(n):
        ts = t0 + i * H
        px = 100.0 + i
        stamp = ts * 1000 if micro else ts
        rows.append(f"{stamp},{px},{px + 2},{px - 1},{px + 1},5.0,{stamp + H - 1},0,1,0,0,0")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"BTCUSDT-1h-{y}-{m:02d}.csv", "\n".join(rows) + "\n")
    return buf.getvalue()


class FakeArchive:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []

    def add(self, y: int, m: int, data: bytes, checksum: bytes | None = None) -> None:
        name = f"BTCUSDT-1h-{y}-{m:02d}.zip"
        url = f"{BASE_URL}/BTCUSDT/1h/{name}"
        self.files[url] = data
        digest = checksum or hashlib.sha256(data).hexdigest().encode()
        self.files[url + ".CHECKSUM"] = digest + b"  " + name.encode()

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.files:
            raise NotFound(url)
        return self.files[url]


def _dl(fake: FakeArchive, tmp_path: Path, start: str, end: str):  # type: ignore[no-untyped-def]
    return download_klines(
        "BTC/USDT", "1h", H, parse_month(start), parse_month(end), tmp_path, fetch=fake
    )


def test_months_and_parse() -> None:
    assert list(months((2024, 11), (2025, 2))) == [(2024, 11), (2024, 12), (2025, 1), (2025, 2)]
    assert last_complete_month(datetime(2026, 1, 15, tzinfo=UTC)) == (2025, 12)
    assert last_complete_month(datetime(2026, 10, 1, tzinfo=UTC)) == (2026, 9)
    with pytest.raises(ValueError):
        parse_month("2024/01")


def test_downloads_verifies_and_normalizes_microseconds(tmp_path: Path) -> None:
    fake = FakeArchive()
    fake.add(2024, 12, _zip(2024, 12, n=24 * 31))  # ms, mes completo
    fake.add(2025, 1, _zip(2025, 1, n=24, micro=True, header=True))  # µs + encabezado
    res = _dl(fake, tmp_path, "2024-12", "2025-01")
    assert res.report.ok and res.report.invalid == 0
    assert res.months_ok == ["2024-12", "2025-01"]
    assert res.report.gaps == []  # µs bien convertidos: dic -> ene sin hueco
    bars, report = load_csv(res.path, "BTC/USDT", H)
    assert report.ok and len(bars) == 24 * 31 + 24
    assert bars[-1].ts_open == _month_start_ms(2025, 1) + 23 * H
    assert bars[-1].ts == bars[-1].ts_open + H  # ts = cierre (anti look-ahead)


def test_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    fake = FakeArchive()
    fake.add(2024, 1, _zip(2024, 1), checksum=b"0" * 64)
    with pytest.raises(DownloadError, match="SHA-256"):
        _dl(fake, tmp_path, "2024-01", "2024-01")
    assert not (tmp_path / "BTCUSDT_1h.csv").exists()
    assert not list((tmp_path / ".cache").rglob("*.zip"))  # nada sin verificar en caché


def test_months_before_listing_are_reported_not_invented(tmp_path: Path) -> None:
    fake = FakeArchive()
    fake.add(2024, 3, _zip(2024, 3))
    res = _dl(fake, tmp_path, "2024-01", "2024-03")
    assert res.months_missing == ["2024-01", "2024-02"]
    assert res.months_ok == ["2024-03"]


def test_no_data_at_all_fails(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match="no hay datos"):
        _dl(FakeArchive(), tmp_path, "2024-01", "2024-02")


def test_cache_avoids_redownload_and_heals_corruption(tmp_path: Path) -> None:
    fake = FakeArchive()
    fake.add(2024, 5, _zip(2024, 5))
    _dl(fake, tmp_path, "2024-05", "2024-05")
    zip_url = f"{BASE_URL}/BTCUSDT/1h/BTCUSDT-1h-2024-05.zip"
    assert fake.calls.count(zip_url) == 1

    res = _dl(fake, tmp_path, "2024-05", "2024-05")
    assert res.from_cache == 1 and fake.calls.count(zip_url) == 1

    cached = next((tmp_path / ".cache").rglob("*.zip"))
    cached.write_bytes(b"basura")
    res = _dl(fake, tmp_path, "2024-05", "2024-05")
    assert res.from_cache == 0 and fake.calls.count(zip_url) == 2
    assert res.report.ok


def test_invalid_rows_are_counted(tmp_path: Path) -> None:
    buf = io.BytesIO()
    t0 = _month_start_ms(2024, 2)
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "x.csv",
            f"{t0},100,102,99,101,5,0,0,0,0,0,0\n{t0 + H},100,90,99,101,5,0,0,0,0,0,0\n",
        )  # segunda fila: high < low
    fake = FakeArchive()
    fake.add(2024, 2, buf.getvalue())
    res = _dl(fake, tmp_path, "2024-02", "2024-02")
    assert res.report.invalid == 1 and not res.report.ok


def test_symbol_flag_also_drives_the_strategy(tmp_path: Path) -> None:
    import argparse

    args = argparse.Namespace(config="config/default.toml", symbol="ETH/USDT, SOL/USDT")
    cfg = _backtest_config(args)
    assert cfg.strategy.symbols == ("ETH/USDT", "SOL/USDT")
    cfg = _backtest_config(argparse.Namespace(config="config/default.toml", symbol=None))
    assert cfg.strategy.symbols == ("BTC/USDT",)


def test_backtest_cli_trades_the_symbol_it_loaded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regresión: --symbol ETH/USDT con un TOML de BTC tiene que operar ETH."""
    from quantbot.data.historical import write_csv
    from quantbot.data.synthetic import generate_bars

    path = tmp_path / "eth.csv"
    write_csv(path, generate_bars("ETH/USDT", 3000, H, seed=3))
    rc = main(
        ["backtest", "--config", "config/default.toml", "--data", str(path), "--symbol", "ETH/USDT"]
    )
    out = capsys.readouterr().out
    assert rc == 0 and "3000 barras" in out
    n_trades = int(next(ln for ln in out.splitlines() if "n_trades" in ln).split()[-1])
    assert n_trades > 0


def test_timeframe_flag_and_mismatch_guard(tmp_path: Path) -> None:
    import argparse

    from quantbot.data.historical import write_csv
    from quantbot.data.synthetic import generate_bars

    ns = argparse.Namespace(config="config/default.toml", symbol=None, timeframe="4h")
    assert _backtest_config(ns).strategy.timeframe == "4h"
    with pytest.raises(ValueError):
        _backtest_config(argparse.Namespace(config=None, symbol=None, timeframe="4x"))

    h1, h4 = tmp_path / "h1.csv", tmp_path / "h4.csv"
    write_csv(h1, generate_bars("BTC/USDT", 2000, H, seed=1))
    write_csv(h4, generate_bars("BTC/USDT", 2000, 4 * H, seed=1))
    base = ["validate-data", "--config", "config/default.toml", "--symbol", "BTC/USDT"]
    assert main([*base, "--data", str(h4), "--timeframe", "4h"]) == 0
    assert main([*base, "--data", str(h1)]) == 0
    for path, tf in ((h4, "1h"), (h1, "4h")):  # archivo y config no coinciden
        with pytest.raises(SystemExit, match="no parece de"):
            main([*base, "--data", str(path), "--timeframe", tf])
