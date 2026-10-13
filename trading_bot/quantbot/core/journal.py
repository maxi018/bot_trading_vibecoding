"""Journal persistente en SQLite (stdlib, sin servidor).

Registra lo que el dashboard necesita y que sobrevive reinicios:
- ``fills``: cada ejecución, con slippage real vs. esperado.
- ``trades``: trades cerrados (entrada -> salida completa) con precio medio, PnL,
  duración, motivo de salida y slippage promedio.
- ``events``: vetos, alertas, acciones de riesgo y comandos de operador (auditoría).
- ``equity``: curva de equity muestreada.

Es SOLO observabilidad: un error del journal se loguea y jamás afecta al trading.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Fill

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, ts INTEGER, cid TEXT, symbol TEXT, side TEXT, qty REAL,
    price REAL, fee REAL, decision_price REAL, slippage_bps REAL, expected_slippage_bps REAL,
    tag TEXT, is_maker INTEGER
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, qty REAL, entry_ts INTEGER,
    exit_ts INTEGER, entry_price REAL, exit_price REAL, pnl REAL, fees REAL,
    return_pct REAL, exit_reason TEXT, slippage_bps REAL, expected_slippage_bps REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, severity TEXT, title TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER PRIMARY KEY, equity REAL, drawdown REAL);
CREATE INDEX IF NOT EXISTS ix_trades_exit ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_fills_symbol ON fills(symbol, ts);
"""


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: los backtests de la API leen desde otros threads.
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        try:
            with self._lock:
                self._db.execute(sql, params)
        except sqlite3.Error:
            log.exception("journal: fallo escribiendo")

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # --------------------------------------------------------------- writers
    def record_fill(self, f: Fill, tag: str, expected_bps: float) -> None:
        self._exec(
            "INSERT INTO fills (ts,cid,symbol,side,qty,price,fee,decision_price,slippage_bps,"
            "expected_slippage_bps,tag,is_maker) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f.ts,
                f.client_order_id,
                f.symbol,
                str(f.side),
                f.qty,
                f.price,
                f.fee,
                f.decision_price,
                f.slippage_bps if f.decision_price > 0 else None,
                expected_bps,
                tag,
                int(f.is_maker),
            ),
        )

    def record_trade(self, t: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO trades (symbol,side,qty,entry_ts,exit_ts,entry_price,exit_price,pnl,"
            "fees,return_pct,exit_reason,slippage_bps,expected_slippage_bps) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t["symbol"],
                t["side"],
                t["qty"],
                t["entry_ts"],
                t["exit_ts"],
                t["entry_price"],
                t["exit_price"],
                t["pnl"],
                t["fees"],
                t["return_pct"],
                t["exit_reason"],
                t["slippage_bps"],
                t["expected_slippage_bps"],
            ),
        )

    def record_event(self, ts: int, kind: str, severity: str, title: str, body: Any = "") -> None:
        text = body if isinstance(body, str) else json.dumps(body, default=str)
        self._exec(
            "INSERT INTO events (ts,kind,severity,title,body) VALUES (?,?,?,?,?)",
            (ts, kind, severity, title, text),
        )

    def record_equity(self, ts: int, equity: float, drawdown: float) -> None:
        self._exec(
            "INSERT OR REPLACE INTO equity (ts,equity,drawdown) VALUES (?,?,?)",
            (ts, equity, drawdown),
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass(slots=True)
class _OpenTrade:
    side: str
    entry_ts: int
    qty: float = 0.0
    entry_cost: float = 0.0  # sum(qty * price) de las entradas
    exit_qty: float = 0.0
    exit_cost: float = 0.0
    fees: float = 0.0
    slips: list[float] = field(default_factory=list)
    exit_reason: str = ""


class TradeRecorder:
    """Arma trades completos a partir de fills (entrada -> posición cero)."""

    def __init__(self, journal: Journal, expected_slippage_bps: float) -> None:
        self.journal = journal
        self.expected = expected_slippage_bps
        self._open: dict[str, _OpenTrade] = {}
        self._pos: dict[str, float] = {}

    def on_fill(self, f: Fill, tag: str) -> None:
        self.journal.record_fill(f, tag, self.expected)
        prev = self._pos.get(f.symbol, 0.0)
        signed = f.side.sign * f.qty
        new = prev + signed
        self._pos[f.symbol] = 0.0 if abs(new) < 1e-12 else new
        tr = self._open.get(f.symbol)
        if f.decision_price > 0 and tr is not None:
            tr.slips.append(f.slippage_bps)
        opening = prev == 0 or (prev > 0) == (signed > 0)
        if opening:
            if tr is None:
                tr = self._open[f.symbol] = _OpenTrade("long" if signed > 0 else "short", f.ts)
                if f.decision_price > 0:
                    tr.slips.append(f.slippage_bps)
            tr.qty += f.qty
            tr.entry_cost += f.qty * f.price
            tr.fees += f.fee
            return
        if tr is None:  # posición adoptada al arrancar: no hay entrada conocida
            return
        closing = min(f.qty, abs(prev))
        tr.exit_qty += closing
        tr.exit_cost += closing * f.price
        tr.fees += f.fee
        tr.exit_reason = tag or tr.exit_reason
        if abs(new) < 1e-12 or (new > 0) != (prev > 0):
            self._finish(f.symbol, tr, f.ts)
            if abs(new) > 1e-12:  # dio vuelta: arranca un trade nuevo con el remanente
                rest = abs(new)
                self._open[f.symbol] = _OpenTrade(
                    "long" if new > 0 else "short", f.ts, rest, rest * f.price
                )

    def _finish(self, symbol: str, tr: _OpenTrade, ts: int) -> None:
        self._open.pop(symbol, None)
        entry = tr.entry_cost / tr.qty if tr.qty else 0.0
        exit_ = tr.exit_cost / tr.exit_qty if tr.exit_qty else 0.0
        sign = 1 if tr.side == "long" else -1
        gross = sign * (exit_ - entry) * tr.exit_qty
        self.journal.record_trade(
            {
                "symbol": symbol,
                "side": tr.side,
                "qty": tr.exit_qty,
                "entry_ts": tr.entry_ts,
                "exit_ts": ts,
                "entry_price": entry,
                "exit_price": exit_,
                "pnl": gross - tr.fees,
                "fees": tr.fees,
                "return_pct": sign * (exit_ / entry - 1) if entry else 0.0,
                "exit_reason": tr.exit_reason,
                "slippage_bps": sum(tr.slips) / len(tr.slips) if tr.slips else None,
                "expected_slippage_bps": self.expected,
            }
        )

    def open_trade_entry(self, symbol: str) -> tuple[int, float] | None:
        tr = self._open.get(symbol)
        return (tr.entry_ts, tr.entry_cost / tr.qty) if tr and tr.qty else None
