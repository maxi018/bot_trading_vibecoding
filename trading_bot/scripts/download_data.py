"""Descarga OHLCV histórico vía CCXT a Parquet (o CSV) para backtests.

    python scripts/download_data.py --symbol BTC/USDT --timeframe 1h --since 2019-01-01 \
        --out data/BTCUSDT_1h.parquet

Requiere: pip install 'quantbot[live,data]'
Consultas posteriores con DuckDB:
    duckdb -c "SELECT count(*), min(ts), max(ts) FROM 'data/BTCUSDT_1h.parquet'"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import UTC, datetime


async def download(exchange: str, symbol: str, tf: str, since_ms: int) -> list[list[float]]:
    import ccxt.async_support as ccxt_async

    ex = getattr(ccxt_async, exchange)({"enableRateLimit": True})
    rows: list[list[float]] = []
    try:
        cursor = since_ms
        while True:
            batch = await ex.fetch_ohlcv(symbol, tf, since=cursor, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            nxt = int(batch[-1][0]) + 1
            if nxt <= cursor:
                break
            cursor = nxt
            print(f"\r{len(rows)} velas hasta {datetime.fromtimestamp(nxt / 1000, UTC)}", end="")
    finally:
        await ex.close()
    print()
    # La última vela puede estar en curso: se descarta (no existe todavía).
    return rows[:-1]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--exchange", default="binance")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--since", default="2019-01-01")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    since = int(datetime.fromisoformat(a.since).replace(tzinfo=UTC).timestamp() * 1000)
    rows = asyncio.run(download(a.exchange, a.symbol, a.timeframe, since))
    cols = ["ts", "open", "high", "low", "close", "volume"]
    if a.out.endswith(".parquet"):
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.table({c: [r[i] for r in rows] for i, c in enumerate(cols)})
        pq.write_table(table, a.out)
    else:
        with open(a.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows(rows)
    print(f"Guardadas {len(rows)} velas en {a.out}")


if __name__ == "__main__":
    main()
