"""API del dashboard: REST + WebSocket sobre el bot en ejecución.

Reglas:
- Solo LEE estado y envía COMANDOS a ``TradingApp`` (que los pasa por el risk engine).
  No existe ningún endpoint que cree órdenes.
- Escucha en 127.0.0.1 por defecto. Autenticación por token (``QB_DASHBOARD_TOKEN``),
  comparado en tiempo constante y con freno a la fuerza bruta.
- Corre dentro del mismo event loop que el bot pero AISLADA: si falla, el bot sigue.
- No se loguean requests (el access log de uvicorn está apagado): nada sensible a logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from ..core.clock import timeframe_to_ms
from . import state
from .backtest_jobs import BacktestJobs, JobError

if TYPE_CHECKING:
    from ..app import TradingApp

log = logging.getLogger(__name__)

MIN_TOKEN_LEN = 16
# Build del dashboard: junto al repo (instalación editable) o donde diga QB_DASHBOARD_DIST
# (imagen Docker, donde el paquete vive en site-packages).
DASHBOARD_DIST = Path(
    os.environ.get("QB_DASHBOARD_DIST")
    or Path(__file__).resolve().parents[3] / "dashboard" / "dist"
)


# ------------------------------------------------------------------- auth
class _AuthGuard:
    """Token bearer + bloqueo temporal por IP tras intentos fallidos."""

    def __init__(self, token: str, max_failures: int = 10, window_s: float = 60.0) -> None:
        if len(token) < MIN_TOKEN_LEN:
            raise RuntimeError(
                f"QB_DASHBOARD_TOKEN ausente o corto (mínimo {MIN_TOKEN_LEN} caracteres)"
            )
        self._token = token.encode()
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self.max_failures = max_failures
        self.window_s = window_s

    def _blocked(self, ip: str) -> bool:
        q = self._failures[ip]
        now = time.monotonic()
        while q and now - q[0] > self.window_s:
            q.popleft()
        return len(q) >= self.max_failures

    def check(self, ip: str, presented: str | None) -> None:
        if self._blocked(ip):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demasiados intentos")
        if not presented or not hmac.compare_digest(presented.encode(), self._token):
            self._failures[ip].append(time.monotonic())
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:] if h.lower().startswith("bearer ") else None


# ---------------------------------------------------------------- bodies
class Operator(BaseModel):
    operator: str = Field(min_length=1, max_length=64)


class KillBody(Operator):
    confirm: bool
    reason: str = Field(default="kill manual desde el dashboard", max_length=300)
    flatten: bool = True


class PauseBody(Operator):
    reason: str = Field(default="", max_length=300)


class RearmBody(Operator):
    confirm_text: str


class RiskParamsBody(Operator):
    changes: dict[str, float]
    confirm_relax: bool = False


class ReferenceBody(Operator):
    job_id: str = Field(max_length=40)


class WalkForwardSpec(BaseModel):
    grid: dict[str, list[float]]
    train: int = Field(ge=200, le=50_000)
    test: int = Field(ge=100, le=50_000)
    warmup: int = Field(default=600, ge=0, le=5000)


class DataFile(BaseModel):
    path: str = Field(max_length=300)
    symbol: str = Field(max_length=30)


class BacktestBody(BaseModel):
    strategy: dict[str, Any] = {}
    risk: dict[str, float] = {}
    symbols: list[str] | None = None
    synthetic_bars: int = 6000
    seed: int = 42
    data_files: list[DataFile] = []
    cost_stress: float = 1.0
    n_trials: int = 1
    montecarlo: bool = True
    mc_sims: int = 2000
    walkforward: WalkForwardSpec | None = None


class OutageBody(BaseModel):
    seconds: float = Field(default=10, gt=0, le=120)


# --------------------------------------------------------------- factory
def create_api(app: TradingApp, token: str) -> FastAPI:
    from ..app import CommandError

    guard = _AuthGuard(token)
    jobs = BacktestJobs(app.cfg)
    api = FastAPI(title="quantbot dashboard API", docs_url=None, redoc_url=None, openapi_url=None)
    api.state.jobs = jobs
    api.add_middleware(
        CORSMiddleware,
        allow_origins=list(app.cfg.api.cors_origins),
        allow_methods=["GET", "POST", "PATCH", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )

    async def auth(request: Request) -> None:
        guard.check(request.client.host if request.client else "?", _bearer(request))

    # TODO lo que cuelga de este router exige token: imposible olvidarse en un endpoint.
    r = APIRouter(dependencies=[Depends(auth)])

    @contextlib.contextmanager
    def command_errors() -> Iterator[None]:
        try:
            yield
        except CommandError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    # ------------------------------------------------------------ lectura
    @api.get("/api/health")
    async def health_public() -> dict[str, Any]:
        return {"ok": True}  # sin datos: sirve para saber si el server está arriba

    @r.get("/api/session")
    async def session() -> dict[str, Any]:
        return {
            "mode": app.cfg.mode,
            "symbols": list(app.cfg.strategy.symbols),
            "timeframe": app.cfg.strategy.timeframe,
        }

    @r.get("/api/overview")
    async def overview() -> dict[str, Any]:
        return {"overview": state.overview(app), "health": state.health(app)}

    @r.get("/api/positions")
    async def positions() -> dict[str, Any]:
        return {"positions": state.positions(app), "orders": state.open_orders(app)}

    @r.get("/api/candles")
    async def candles(symbol: str, limit: int = Query(500, ge=1, le=2000)) -> dict[str, Any]:
        if symbol not in app.cfg.strategy.symbols:
            raise HTTPException(404, "Símbolo desconocido")
        candles = state.candles(app, symbol, limit)
        since = candles[0]["time"] * 1000 if candles else 0  # solo fills del rango visible
        return {
            "candles": candles,
            "markers": state.markers(app, symbol, since),
            "stops": state.stop_lines(app, symbol),
            "timeframe_ms": timeframe_to_ms(app.cfg.strategy.timeframe),
        }

    @r.get("/api/trades")
    async def trades(
        symbol: str | None = None,
        side: Literal["long", "short"] | None = None,
        result: Literal["win", "loss"] | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> dict[str, Any]:
        return {"trades": state.trades(app, symbol, side, result, since, until, limit)}

    @r.get("/api/metrics")
    async def metrics() -> dict[str, Any]:
        return state.metrics(app)

    @r.get("/api/risk")
    async def risk() -> dict[str, Any]:
        return state.risk_view(app)

    @r.get("/api/events")
    async def events(
        kind: Literal["veto", "alert", "command"] | None = None,
        limit: int = Query(200, ge=1, le=2000),
    ) -> dict[str, Any]:
        return {"events": state.events(app, kind, limit)}

    @r.get("/api/config")
    async def config() -> dict[str, Any]:
        return state.config_view(app)

    # ------------------------------------------------------------ control
    @r.post("/api/control/kill", status_code=202)
    async def kill(body: KillBody) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(400, "Falta confirmación")
        with command_errors():
            await app.cmd_kill(body.operator, body.reason, body.flatten)
        return {"status": app.status}

    @r.post("/api/control/pause")
    async def pause(body: PauseBody) -> dict[str, Any]:
        with command_errors():
            app.cmd_pause(body.operator, body.reason)
        return {"status": app.status}

    @r.post("/api/control/resume")
    async def resume(body: Operator) -> dict[str, Any]:
        with command_errors():
            app.cmd_resume(body.operator)
        return {"status": app.status}

    @r.post("/api/control/rearm")
    async def rearm(body: RearmBody) -> dict[str, Any]:
        with command_errors():
            app.cmd_rearm(body.operator, body.confirm_text)
        return {"status": app.status}

    @r.put("/api/metrics/reference")
    async def set_reference(body: ReferenceBody) -> dict[str, Any]:
        job = jobs.jobs.get(body.job_id)
        if job is None or job.status != "done" or not job.result:
            raise HTTPException(409, "El backtest no existe o no terminó")
        ref = state.reference_from_job(job.id, job.summary() | {"result": job.result})
        app.cmd_set_reference(body.operator, ref)
        return {"reference": {k: ref[k] for k in ("job_id", "created_at", "n_bars")}}

    @r.patch("/api/risk/params")
    async def risk_params(body: RiskParamsBody) -> dict[str, Any]:
        with command_errors():
            diff = app.cmd_update_risk(body.operator, body.changes, body.confirm_relax)
        return {
            "applied": [
                {"name": c.name, "old": c.old, "new": c.new, "relaxes": c.relaxes} for c in diff
            ],
            "risk": state.risk_view(app),
        }

    # ------------------------------------------------------------ backtests
    @r.post("/api/backtests", status_code=202)
    async def backtest_submit(body: BacktestBody) -> dict[str, Any]:
        try:
            job = jobs.submit(body.model_dump())
        except JobError as exc:
            raise HTTPException(422, str(exc)) from exc
        return job.summary()

    @r.get("/api/backtests/datasets")
    async def backtest_datasets() -> dict[str, Any]:
        return {
            "data_dir": app.cfg.api.data_dir,
            "datasets": jobs.datasets(),
            "symbols": list(app.cfg.strategy.symbols),
        }

    @r.get("/api/backtests")
    async def backtest_list() -> dict[str, Any]:
        return {"jobs": [j.summary() for j in reversed(jobs.jobs.values())]}

    @r.get("/api/backtests/{job_id}")
    async def backtest_get(job_id: str) -> dict[str, Any]:
        job = jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inexistente")
        return job.summary() | {"result": job.result}

    # ------------------------------------------------------------ demo
    @r.post("/api/demo/outage")
    async def demo_outage(body: OutageBody) -> dict[str, Any]:
        sim = getattr(app.feed, "simulate_outage", None)
        if app.cfg.mode != "demo" or sim is None:
            raise HTTPException(404, "Solo disponible en modo demo")
        sim(body.seconds)
        return {"outage_s": body.seconds}

    api.include_router(r)

    # ------------------------------------------------------------ websocket
    @api.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        ip = socket.client.host if socket.client else "?"
        # Auth en el primer mensaje (no en la URL: las URLs terminan en logs/historial).
        try:
            first = json.loads(await asyncio.wait_for(socket.receive_text(), timeout=5))
            guard.check(ip, first.get("token") if first.get("type") == "auth" else None)
        except (TimeoutError, ValueError, HTTPException, WebSocketDisconnect):
            with contextlib.suppress(Exception):
                await socket.close(code=4401)
            return

        outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)

        def on_event(ev: dict[str, Any]) -> None:
            if outbox.full():  # cliente lento: se descarta lo más viejo
                with contextlib.suppress(asyncio.QueueEmpty):
                    outbox.get_nowait()
            outbox.put_nowait({"type": "event", "event": ev})

        app.ui_listeners.append(on_event)
        interval = app.cfg.api.snapshot_interval_s

        async def snapshots() -> None:
            while True:
                await outbox.put({"type": "snapshot", **snapshot(app)})
                await asyncio.sleep(interval)

        async def sender() -> None:
            while True:
                await socket.send_text(json.dumps(await outbox.get(), default=str))

        async def receiver() -> None:
            while True:
                msg = json.loads(await socket.receive_text())
                if msg.get("type") == "ping":
                    await outbox.put({"type": "pong", "t": msg.get("t")})

        try:
            await socket.send_text(json.dumps({"type": "hello", **session_info(app)}))
            async with asyncio.TaskGroup() as tg:
                tg.create_task(snapshots())
                tg.create_task(sender())
                tg.create_task(receiver())
        except* Exception:  # desconexión / cliente roto: solo se libera el listener
            pass
        finally:
            with contextlib.suppress(ValueError):
                app.ui_listeners.remove(on_event)

    # ------------------------------------------------------ dashboard estático
    if DASHBOARD_DIST.is_dir():
        api.mount("/assets", StaticFiles(directory=DASHBOARD_DIST / "assets"), name="assets")

        @api.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            if path.startswith(("api/", "ws")):
                raise HTTPException(404)
            candidate = (DASHBOARD_DIST / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(DASHBOARD_DIST):
                return FileResponse(candidate)
            return FileResponse(DASHBOARD_DIST / "index.html")

    return api


def session_info(app: TradingApp) -> dict[str, Any]:
    return {
        "mode": app.cfg.mode,
        "symbols": list(app.cfg.strategy.symbols),
        "timeframe": app.cfg.strategy.timeframe,
    }


def snapshot(app: TradingApp) -> dict[str, Any]:
    """Lo que el WebSocket empuja cada segundo (chico: sin historial)."""
    risk = state.risk_view(app)
    return {
        "overview": state.overview(app),
        "health": state.health(app),
        "positions": state.positions(app),
        "orders": state.open_orders(app),
        "breakers": risk["breakers"],
        "stops": {s: state.stop_lines(app, s) for s in app.cfg.strategy.symbols},
    }


# ----------------------------------------------------------------- serve
def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def serve_api(app: TradingApp, stop: asyncio.Event) -> None:
    import uvicorn

    cfg = app.cfg.api
    if not _is_loopback(cfg.host):
        log.warning(
            "API escuchando en %s (NO loopback): ponerla detrás de un proxy con TLS y firewall",
            cfg.host,
        )
    fastapi_app = create_api(app, app.secrets.dashboard_token)

    class _Server(uvicorn.Server):
        # El bot es dueño de SIGTERM/SIGINT (safe shutdown). uvicorn no los toca.
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    server = _Server(
        uvicorn.Config(
            fastapi_app,
            host=cfg.host,
            port=cfg.port,
            access_log=False,
            log_level="warning",
            lifespan="off",
            ws_max_size=64 * 1024,
        )
    )

    async def watch_stop() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(watch_stop())
    log.info("Dashboard API en http://%s:%d", cfg.host, cfg.port)
    try:
        await server.serve()
    finally:
        watcher.cancel()
        fastapi_app.state.jobs.shutdown()
