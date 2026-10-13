"""Vistas de SOLO LECTURA del estado del bot para el dashboard.

Nada de acá muta estado ni habla con el exchange. Todo se serializa a tipos JSON
simples. Las API keys no viven en ``AppConfig`` (están en ``Secrets``), así que
``config_view`` no puede filtrarlas por construcción.
"""

from __future__ import annotations

import dataclasses
import json
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..backtest.metrics import performance_report
from ..core.clock import periods_per_year, timeframe_to_ms
from ..risk.params import CONSERVATIVE_DIRECTION

if TYPE_CHECKING:
    from ..app import TradingApp


QUEUE_BACKLOG_STALE = 200


def _f(x: float | None) -> float | None:
    """NaN/inf no son JSON válido."""
    if x is None or not math.isfinite(x):
        return None
    return float(x)


def _downsample(points: list[Any], max_points: int = 1500) -> list[Any]:
    if len(points) <= max_points:
        return points
    step = math.ceil(len(points) / max_points)
    out = points[::step]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


# ---------------------------------------------------------------- overview
def overview(app: TradingApp) -> dict[str, Any]:
    now = app.clock.now_ms()
    m = app.portfolio.snapshot(now)
    initial = app.cfg.initial_cash
    rec = app.kill.record
    return {
        "mode": app.cfg.mode,
        "testnet": app.cfg.exchange.testnet if app.cfg.mode == "live" else None,
        "status": app.status,
        "halt_reasons": app.risk.halt_reasons(),
        "kill": {"reason": rec.reason, "at": rec.engaged_at_ms, "flatten": rec.flatten}
        if rec
        else None,
        "equity": _f(m.equity),
        "cash": _f(m.cash),
        "pnl_day": _f(m.equity - m.day_start_equity),
        "pnl_day_pct": _f(m.daily_pnl_pct),
        "pnl_total": _f(m.equity - initial),
        "pnl_total_pct": _f(m.equity / initial - 1) if initial else None,
        "realized_pnl": _f(m.realized_pnl),
        "unrealized_pnl": _f(m.unrealized_pnl),
        "fees_paid": _f(m.fees_paid),
        "drawdown": _f(m.drawdown_from_peak),
        "drawdown_limit": app.cfg.risk.total_drawdown_kill,
        "daily_loss_limit": app.cfg.risk.daily_drawdown_halt,
        "peak_equity": _f(m.peak_equity),
        "gross_leverage": _f(m.gross_leverage),
        "max_gross_leverage": app.cfg.risk.max_gross_leverage,
        "open_positions": m.open_positions,
        "consecutive_losses": m.consecutive_losses,
        "size_multiplier": app.risk.size_multiplier,
        "symbols": list(app.cfg.strategy.symbols),
        "timeframe": app.cfg.strategy.timeframe,
        "sim_time": now,
        "uptime_s": time.time() - app.started_at,
    }


def health(app: TradingApp) -> dict[str, Any]:
    age = time.monotonic() - app.last_market_wall if app.last_market_wall else None
    threshold = max(app.cfg.risk.max_data_age_s, 3.0)
    stale_sources = sorted(app.risk._stale_sources)
    backlog = app.queue.qsize() > QUEUE_BACKLOG_STALE  # el consumidor no da abasto
    stale = (
        age is None or age > threshold or bool(stale_sources) or not app.feed.connected or backlog
    )
    return {
        "feed_connected": app.feed.connected,
        "reconnects": app.feed.reconnects,
        "last_data_age_s": _f(age),
        "stale_threshold_s": threshold,
        "stale": stale,
        "stale_sources": stale_sources,
        "feed_lag_ms": _f(app.feed_lag_ms),
        "exchange_rtt_ms": _f(app.exchange_rtt_ms),
        "queue_size": app.queue.qsize(),
        "queue_backlog": backlog,
        "server_time": int(time.time() * 1000),
    }


# --------------------------------------------------------- posiciones / órdenes
def _current_stop(app: TradingApp, symbol: str) -> float | None:
    cid = app.orders.stop_orders.get(symbol)
    order = app.orders.orders.get(cid) if cid else None
    if order is not None and not order.is_terminal:
        return order.stop_price
    return None


def positions(app: TradingApp) -> list[dict[str, Any]]:
    out = []
    for sym, p in app.portfolio.positions.items():
        if abs(p.qty) < 1e-12:
            continue
        mark = app.portfolio.mark_price(sym)
        stop = _current_stop(app, sym)
        trailing = app.orders.desired_stop.get(sym)
        dist = abs(mark - stop) / mark if stop and mark else None
        entry = app.recorder.open_trade_entry(sym)
        out.append(
            {
                "symbol": sym,
                "side": "long" if p.qty > 0 else "short",
                "qty": abs(p.qty),
                "avg_entry": _f(p.avg_entry),
                "mark": _f(mark),
                "notional": _f(abs(p.qty) * mark),
                "unrealized_pnl": _f(p.unrealized(mark)),
                "unrealized_pct": _f((mark / p.avg_entry - 1) * (1 if p.qty > 0 else -1))
                if p.avg_entry
                else None,
                "stop": _f(stop),
                "trailing_stop": _f(trailing),
                "distance_to_stop_pct": _f(dist),
                "risk_to_stop": _f(abs(p.qty) * abs(mark - stop)) if stop else None,
                "protected": stop is not None,
                "entry_ts": entry[0] if entry else None,
            }
        )
    return out


def open_orders(app: TradingApp) -> list[dict[str, Any]]:
    return [
        {
            "client_order_id": o.client_order_id,
            "symbol": o.symbol,
            "side": str(o.side),
            "type": str(o.type),
            "qty": o.qty,
            "filled_qty": o.filled_qty,
            "price": _f(o.price),
            "stop_price": _f(o.stop_price),
            "status": str(o.status),
            "tag": o.tag,
            "reduce_only": o.reduce_only,
            "created_ts": o.created_ts,
        }
        for o in app.orders.orders.values()
        if not o.is_terminal
    ]


# ------------------------------------------------------------------ riesgo
def risk_view(app: TradingApp) -> dict[str, Any]:
    m = app.portfolio.snapshot(app.clock.now_ms())
    limits = []
    for f in dataclasses.fields(app.cfg.risk):
        limits.append(
            {
                "name": f.name,
                "value": getattr(app.cfg.risk, f.name),
                "editable": f.name in CONSERVATIVE_DIRECTION,
                "conservative_direction": CONSERVATIVE_DIRECTION.get(f.name),
            }
        )
    return {
        "status": app.status,
        "paused": app.risk.paused,
        "killed": app.risk.killed,
        "halt_reasons": app.risk.halt_reasons(),
        "size_multiplier": app.risk.size_multiplier,
        "veto_count": app.risk.veto_count,
        "breakers": [
            {k: (_f(v) if isinstance(v, float) else v) for k, v in row.items()}
            for row in app.risk.breaker_readings(m, app.orders.mismatch_strikes)
        ],
        "limits": limits,
    }


# ------------------------------------------------------------- gráfico
def candles(app: TradingApp, symbol: str, limit: int = 500) -> list[dict[str, Any]]:
    bars = list(app.bars.get(symbol, ()))[-limit:]
    return [
        {
            "time": b.ts_open // 1000,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]


def markers(app: TradingApp, symbol: str, since_ms: int = 0) -> list[dict[str, Any]]:
    rows = app.journal.query(
        "SELECT ts, side, qty, price, tag FROM fills WHERE symbol=? AND ts>=? ORDER BY ts",
        (symbol, since_ms),
    )
    return rows


def stop_lines(app: TradingApp, symbol: str) -> dict[str, Any]:
    return {
        "stop": _f(_current_stop(app, symbol)),
        "trailing": _f(app.orders.desired_stop.get(symbol)),
    }


# ------------------------------------------------------------- historial
def trades(
    app: TradingApp,
    symbol: str | None = None,
    side: str | None = None,
    result: str | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if side in ("long", "short"):
        where.append("side = ?")
        params.append(side)
    if result == "win":
        where.append("pnl > 0")
    elif result == "loss":
        where.append("pnl <= 0")
    if since_ms is not None:
        where.append("exit_ts >= ?")
        params.append(since_ms)
    if until_ms is not None:
        where.append("exit_ts <= ?")
        params.append(until_ms)
    sql = "SELECT * FROM trades"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY exit_ts DESC LIMIT ?"
    params.append(max(1, min(limit, 5000)))
    return app.journal.query(sql, tuple(params))


def events(app: TradingApp, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 2000))
    if kind:
        return app.journal.query(
            "SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)
        )
    return app.journal.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------- métricas
def metrics(app: TradingApp) -> dict[str, Any]:
    eq_rows = app.journal.query("SELECT ts, equity, drawdown FROM equity ORDER BY ts")
    pnls = [r["pnl"] for r in app.journal.query("SELECT pnl FROM trades ORDER BY exit_ts")]
    equity = [r["equity"] for r in eq_rows]
    ppy = periods_per_year(app.cfg.strategy.timeframe)
    report = performance_report(equity or [app.cfg.initial_cash], pnls, ppy).as_dict()
    clean = {k: (_f(v) if isinstance(v, float) else v) for k, v in report.items()}
    reference = None
    ref_path = Path(app.cfg.state_dir) / "reference_backtest.json"
    if ref_path.exists():
        try:
            reference = json.loads(ref_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reference = None
    return {
        "report": clean,
        "equity": _downsample([[r["ts"], r["equity"], r["drawdown"]] for r in eq_rows]),
        "reference": reference,
        "timeframe_ms": timeframe_to_ms(app.cfg.strategy.timeframe),
        "drawdown_kill": app.cfg.risk.total_drawdown_kill,
    }


def reference_from_job(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Lo que se guarda como referencia: reporte, curva y Montecarlo (sin el histograma)."""
    result = job["result"]
    mc = result.get("montecarlo")
    return {
        "job_id": job_id,
        "created_at": job.get("finished_at"),
        "spec": job.get("spec"),
        "n_bars": result.get("n_bars"),
        "report": result["report"],
        "equity": result["equity"],
        "montecarlo": {k: v for k, v in mc.items() if k != "histogram"} if mc else None,
    }


def config_view(app: TradingApp) -> dict[str, Any]:
    cfg = dataclasses.asdict(app.cfg)
    return cfg
