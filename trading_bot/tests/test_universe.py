"""Canasta: empalme de tickers 1:1, ranking por volumen pasado y el comando universe."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.__main__ import main
from quantbot.core.models import Bar
from quantbot.data.historical import load_csv, write_csv
from quantbot.data.universe import SpliceError, rank_by_quote_volume, splice_bars

D = 86_400_000
DEC_2018 = int(datetime(2018, 12, 1, tzinfo=UTC).timestamp() * 1000)
JAN_2019 = DEC_2018 + 31 * D


def daily(symbol: str, start: int, n: int, px: float = 100.0, vol: float = 1.0) -> list[Bar]:
    return [
        Bar(symbol, start + i * D, start + (i + 1) * D, px, px * 1.01, px * 0.99, px, vol)
        for i in range(n)
    ]


def test_splice_relabels_drops_overlap_and_rejects_price_jumps() -> None:
    old = daily("BCHABC/USDT", DEC_2018, 40)
    new = daily("BCH/USDT", DEC_2018 + 38 * D, 10)  # se solapan 2 días
    out = splice_bars([old, new], "BCHABC/USDT")
    assert len(out) == 48 and {b.symbol for b in out} == {"BCHABC/USDT"}
    assert [b.ts for b in out] == sorted({b.ts for b in out})
    with pytest.raises(SpliceError, match="salto"):
        splice_bars([old, daily("BCH/USDT", DEC_2018 + 40 * D, 5, px=200.0)], "X")


def test_rank_uses_only_the_past_window() -> None:
    series = {
        "AAA/USDT": daily("AAA/USDT", DEC_2018, 31, px=10, vol=100)  # 31 000 en diciembre
        + daily("AAA/USDT", JAN_2019, 31, px=10, vol=1),
        "BBB/USDT": daily("BBB/USDT", DEC_2018, 62, px=10, vol=50),  # 15 500 en diciembre
        "NEW/USDT": daily("NEW/USDT", JAN_2019, 31, px=10, vol=10**6),  # listada después
    }
    ranking = rank_by_quote_volume(series, DEC_2018, JAN_2019)
    assert ranking == [("AAA/USDT", 31_000.0), ("BBB/USDT", 15_500.0), ("NEW/USDT", 0.0)]


def test_universe_command_builds_the_basket(tmp_path: Path) -> None:
    src, out = tmp_path / "raw", tmp_path / "basket"
    src.mkdir()
    write_csv(src / "AAAUSDT_1d.csv", daily("AAA/USDT", DEC_2018, 62, vol=100))
    write_csv(src / "OLDUSDT_1d.csv", daily("OLD/USDT", DEC_2018, 40, vol=80))
    write_csv(src / "NEWUSDT_1d.csv", daily("NEW/USDT", DEC_2018 + 40 * D, 22, vol=80))
    write_csv(src / "CCCUSDT_1d.csv", daily("CCC/USDT", DEC_2018, 62, vol=1))
    write_csv(src / "LATEUSDT_1d.csv", daily("LATE/USDT", JAN_2019, 31, vol=10**6))
    gh = tmp_path / "gh_output"
    rc = main(
        [
            "universe",
            "--candidates",
            "AAA/USDT,OLD/USDT>NEW/USDT,CCC/USDT,LATE/USDT,MISSING/USDT",
            "--data-dir",
            str(src),
            "--out-dir",
            str(out),
            "--rank-from",
            "2018-12",
            "--rank-to",
            "2019-01",
            "--trade-from",
            "2019-01",
            "--top",
            "2",
            "--github-output",
            str(gh),
        ]
    )
    assert rc == 0
    lines = gh.read_text().splitlines()
    assert lines[0] == "symbols=AAA/USDT,OLD/USDT"  # LATE no existía en diciembre
    assert lines[1] == f"data={out / 'AAAUSDT_1d.csv'},{out / 'OLDUSDT_1d.csv'}"
    bars, report = load_csv(out / "OLDUSDT_1d.csv", "OLD/USDT", D)
    assert report.ok and len(bars) == 31  # desde enero, con el tramo NEW empalmado
    assert bars[0].ts_open == JAN_2019 and bars[-1].ts_open == DEC_2018 + 61 * D
