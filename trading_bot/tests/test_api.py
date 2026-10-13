"""Endpoints del dashboard: auth, controles (kill / pause / rearm / parámetros), WS."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quantbot.api.server import create_api
from quantbot.app import TradingApp, _KillExecute
from quantbot.config import Secrets, load_config
from quantbot.core.journal import Journal, TradeRecorder
from quantbot.core.kill_switch import KillSwitch
from quantbot.core.models import Direction, Fill, OrderRequest, OrderType, Side, Signal, TimeInForce
from quantbot.risk.params import RiskParamError, apply_changes

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OP = {"operator": "maxi"}


class StubFeed:
    reconnects = 0
    connected = True
    last_msg_ms = 0

    async def run(self, stop):
        await stop.wait()


@pytest.fixture
def app(tmp_path):
    cfg = load_config(mode="paper", state_dir=str(tmp_path / "state"), api={"enabled": False})
    secrets = Secrets(
        api_key="SUPERSECRETKEY123",
        api_secret="SECRETSECRET",
        dashboard_token=TOKEN,
        telegram_token="TGTOKEN999",
    )
    return TradingApp(cfg, secrets, feed=StubFeed())  # type: ignore[arg-type]


@pytest.fixture
async def client(app):
    api = create_api(app, TOKEN)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def drain(app: TradingApp) -> None:
    while not app.queue.empty():
        await app._dispatch(app.queue.get_nowait())


# ------------------------------------------------------------------ auth
async def test_requires_token(client):
    assert (await client.get("/api/health")).status_code == 200  # público, sin datos
    assert (await client.get("/api/overview")).status_code == 401
    bad = {"Authorization": "Bearer " + "x" * 32}
    assert (await client.get("/api/overview", headers=bad)).status_code == 401
    assert (await client.get("/api/overview", headers=AUTH)).status_code == 200


async def test_bruteforce_is_throttled(client):
    bad = {"Authorization": "Bearer nope"}
    codes = [(await client.get("/api/risk", headers=bad)).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[-1] == 429
    # Incluso el token correcto queda bloqueado durante la ventana
    assert (await client.get("/api/risk", headers=AUTH)).status_code == 429


def test_short_token_refuses_to_start(app):
    with pytest.raises(RuntimeError):
        create_api(app, "corto")


async def test_secrets_never_reach_the_frontend(client):
    for path in ("/api/config", "/api/overview", "/api/risk", "/api/positions"):
        body = (await client.get(path, headers=AUTH)).text
        for secret in ("SUPERSECRETKEY123", "SECRETSECRET", "TGTOKEN999", TOKEN):
            assert secret not in body, f"{secret} filtrado en {path}"


def _all_paths(routes) -> list[str]:
    out = []
    for r in routes:
        if getattr(r, "path", None):
            out.append(r.path)
        inner = getattr(r, "original_router", None) or getattr(r, "router", None) or r
        out += _all_paths(getattr(inner, "routes", None) or [])
    return out


def test_no_endpoint_creates_orders(app):
    paths = _all_paths(create_api(app, TOKEN).routes)
    assert "/api/control/kill" in paths  # el recorrido encuentra las rutas protegidas
    assert not [p for p in paths if "order" in p or "trade/" in p or "submit" in p]


# ------------------------------------------------------------------ kill
async def test_kill_requires_confirmation(client, app):
    r = await client.post("/api/control/kill", headers=AUTH, json=OP | {"confirm": False})
    assert r.status_code == 400 and not app.risk.killed


async def test_kill_engages_immediately_and_flattens_via_queue(client, app):
    # Posición abierta con stop nativo
    app.exchange.on_price("BTC/USDT", 100.0)
    app.orders.set_desired_stop("BTC/USDT", 95.0)
    await app.orders.submit(
        OrderRequest(
            "BTC/USDT",
            Side.BUY,
            2.0,
            OrderType.LIMIT,
            price=101,
            tif=TimeInForce.IOC,
            decision_price=100,
        )
    )
    await drain(app)
    assert app.portfolio.position("BTC/USDT").qty == pytest.approx(2.0)

    r = await client.post(
        "/api/control/kill", headers=AUTH, json=OP | {"confirm": True, "reason": "test"}
    )
    assert r.status_code == 202 and r.json()["status"] == "KILLED"
    # El flag está activo YA (persistido) aunque el flatten todavía esté en cola
    assert app.risk.killed and KillSwitch(app.cfg.state_dir).engaged
    assert any(isinstance(x, _KillExecute) for x in list(app.queue._queue))  # type: ignore[attr-defined]
    await drain(app)
    assert app.portfolio.position("BTC/USDT").qty == pytest.approx(0.0)
    assert app.exchange.positions.get("BTC/USDT", 0) == pytest.approx(0.0)
    # Auditoría
    ev = (await client.get("/api/events?kind=command", headers=AUTH)).json()["events"]
    assert ev and "KILL por maxi" in ev[0]["title"]
    # Un segundo kill es conflicto, no un error silencioso
    r2 = await client.post("/api/control/kill", headers=AUTH, json=OP | {"confirm": True})
    assert r2.status_code == 409


async def test_risk_engine_vetoes_everything_after_kill(client, app):
    await client.post("/api/control/kill", headers=AUTH, json=OP | {"confirm": True})
    sig = Signal("t", "BTC/USDT", app.clock.now_ms(), Direction.LONG, 100, stop_price=90)
    assert app.risk.evaluate(sig, app.portfolio).reason == "kill_switch_engaged"


# ----------------------------------------------------------- pause / resume
async def test_pause_and_resume(client, app):
    r = await client.post("/api/control/pause", headers=AUTH, json=OP | {"reason": "almuerzo"})
    assert r.json()["status"] == "PAUSED" and app.risk.paused and not app.risk.trading_enabled
    r = await client.post("/api/control/resume", headers=AUTH, json=OP)
    assert r.json()["status"] == "RUNNING" and not app.risk.paused


async def test_resume_does_not_clear_other_halts(client, app):
    app.risk.mark_data_stale("ws", "test")
    await client.post("/api/control/pause", headers=AUTH, json=OP)
    r = await client.post("/api/control/resume", headers=AUTH, json=OP)
    assert r.json()["status"] == "HALTED"  # la pausa se fue, el halt por datos no


async def test_resume_after_kill_is_rejected(client, app):
    await client.post("/api/control/kill", headers=AUTH, json=OP | {"confirm": True})
    assert (await client.post("/api/control/resume", headers=AUTH, json=OP)).status_code == 409
    assert (await client.post("/api/control/pause", headers=AUTH, json=OP)).status_code == 409


# ------------------------------------------------------------------ rearm
async def test_rearm_requires_exact_text_and_comes_back_paused(client, app):
    assert (
        await client.post(
            "/api/control/rearm", headers=AUTH, json=OP | {"confirm_text": "REACTIVAR"}
        )
    ).status_code == 409  # no hay kill
    await client.post("/api/control/kill", headers=AUTH, json=OP | {"confirm": True})
    await drain(app)
    for wrong in ("reactivar", "REACTIVAR ", "si", ""):
        r = await client.post("/api/control/rearm", headers=AUTH, json=OP | {"confirm_text": wrong})
        assert r.status_code == 409 and app.risk.killed
    r = await client.post(
        "/api/control/rearm", headers=AUTH, json=OP | {"confirm_text": "REACTIVAR"}
    )
    assert r.status_code == 200 and r.json()["status"] == "PAUSED"
    assert not app.risk.killed and not KillSwitch(app.cfg.state_dir).engaged
    audit = app.cfg.state_dir + "/kill_switch_audit.log"
    with open(audit) as fh:  # noqa: ASYNC230 (test)
        entry = json.loads(fh.read().splitlines()[-1])
    assert entry["operator"] == "maxi" and entry["source"] == "dashboard"
    r = await client.post("/api/control/resume", headers=AUTH, json=OP)
    assert r.json()["status"] == "RUNNING"


# ------------------------------------------------------------ risk params
async def test_tightening_is_allowed_while_running(client, app):
    r = await client.patch(
        "/api/risk/params", headers=AUTH, json=OP | {"changes": {"max_risk_per_trade": 0.005}}
    )
    assert r.status_code == 200
    assert (
        app.risk.cfg.max_risk_per_trade == 0.005 and app.risk.sizer.cfg.max_risk_per_trade == 0.005
    )
    assert r.json()["applied"][0]["relaxes"] is False


async def test_relaxing_requires_pause_and_confirmation(client, app):
    body = OP | {"changes": {"max_risk_per_trade": 0.015}}
    r = await client.patch("/api/risk/params", headers=AUTH, json=body)
    assert r.status_code == 409 and "PAUSADO" in r.json()["detail"]
    await client.post("/api/control/pause", headers=AUTH, json=OP)
    r = await client.patch("/api/risk/params", headers=AUTH, json=body)
    assert r.status_code == 409 and "confirmación" in r.json()["detail"]
    r = await client.patch("/api/risk/params", headers=AUTH, json=body | {"confirm_relax": True})
    assert r.status_code == 200 and app.risk.cfg.max_risk_per_trade == 0.015


async def test_mixed_change_with_one_relax_is_rejected_atomically(client, app):
    body = OP | {"changes": {"max_position_pct": 0.1, "total_drawdown_kill": 0.2}}
    r = await client.patch("/api/risk/params", headers=AUTH, json=body)
    assert r.status_code == 409
    assert app.risk.cfg.max_position_pct == 0.20  # nada se aplicó


async def test_hard_ceilings_and_non_editable(client, app):
    await client.post("/api/control/pause", headers=AUTH, json=OP)
    r = await client.patch(
        "/api/risk/params",
        headers=AUTH,
        json=OP | {"changes": {"max_gross_leverage": 10}, "confirm_relax": True},
    )
    assert r.status_code == 409  # techo duro de config
    r = await client.patch(
        "/api/risk/params", headers=AUTH, json=OP | {"changes": {"market_protection_bps": 1}}
    )
    assert r.status_code == 409 and "no es editable" in r.json()["detail"]
    r = await client.patch(
        "/api/risk/params", headers=AUTH, json=OP | {"changes": {"max_risk_per_trade": "mucho"}}
    )
    assert r.status_code == 422


def test_param_direction_rules():
    cfg = load_config()
    with pytest.raises(RiskParamError):
        apply_changes(cfg, {"daily_drawdown_halt": 0.05}, paused=False, confirm_relax=True)
    new, diff = apply_changes(cfg, {"daily_drawdown_halt": 0.02}, paused=False, confirm_relax=False)
    assert new.daily_drawdown_halt == 0.02 and not diff[0].relaxes


# ----------------------------------------------------------- lecturas
async def test_overview_and_risk_shapes(client, app):
    ov = (await client.get("/api/overview", headers=AUTH)).json()
    assert ov["overview"]["status"] == "RUNNING" and ov["overview"]["mode"] == "paper"
    assert ov["overview"]["drawdown_limit"] == 0.15
    assert ov["health"]["stale"] is True  # nunca llegó un dato: no se muestra como fresco
    risk = (await client.get("/api/risk", headers=AUTH)).json()
    keys = {b["key"] for b in risk["breakers"]}
    assert {"total_drawdown", "daily_drawdown", "slippage", "data_age", "desync"} <= keys
    assert all(b["status"] in ("green", "yellow", "red") for b in risk["breakers"])


async def test_breaker_turns_red_near_limit(client, app):
    app.portfolio.peak_equity = 10_000
    app.portfolio.cash = 8_700  # DD 13% de un límite de 15% -> rojo
    risk = (await client.get("/api/risk", headers=AUTH)).json()
    dd = next(b for b in risk["breakers"] if b["key"] == "total_drawdown")
    assert dd["status"] == "red" and not dd["tripped"]


async def test_trades_history_with_filters(client, app, tmp_path):
    rec = app.recorder
    rec.on_fill(Fill("a", "BTC/USDT", Side.BUY, 1, 100, 0.1, 1_000, decision_price=99.9), "entry")
    rec.on_fill(Fill("b", "BTC/USDT", Side.SELL, 1, 110, 0.1, 2_000, decision_price=110.1), "exit")
    rec.on_fill(Fill("c", "BTC/USDT", Side.BUY, 1, 100, 0.1, 3_000), "entry")
    rec.on_fill(Fill("d", "BTC/USDT", Side.SELL, 1, 95, 0.1, 4_000), "stop")
    all_t = (await client.get("/api/trades", headers=AUTH)).json()["trades"]
    assert len(all_t) == 2
    win = (await client.get("/api/trades?result=win", headers=AUTH)).json()["trades"]
    assert len(win) == 1 and win[0]["pnl"] == pytest.approx(10 - 0.2)
    assert win[0]["slippage_bps"] is not None and win[0]["expected_slippage_bps"] == 5.0
    loss = (await client.get("/api/trades?result=loss", headers=AUTH)).json()["trades"]
    assert loss[0]["exit_reason"] == "stop"


def test_trade_recorder_flip(tmp_path):
    j = Journal(tmp_path / "j.sqlite")
    r = TradeRecorder(j, 5.0)
    r.on_fill(Fill("a", "X", Side.BUY, 1, 100, 0, 1), "entry")
    r.on_fill(Fill("b", "X", Side.SELL, 3, 90, 0, 2), "exit")  # cierra 1 y abre corto 2
    t = j.query("SELECT * FROM trades")
    assert len(t) == 1 and t[0]["pnl"] == pytest.approx(-10)
    assert r.open_trade_entry("X") == (2, 90)


async def test_demo_outage_only_in_demo(client):
    r = await client.post("/api/demo/outage", headers=AUTH, json={"seconds": 5})
    assert r.status_code == 404


# ------------------------------------------------------------------ WebSocket
def test_websocket_auth_and_stream(app):
    tc = TestClient(create_api(app, TOKEN))
    with tc.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "malo" * 8}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 4401
    with tc.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": TOKEN}))
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello" and hello["mode"] == "paper"
        snap = json.loads(ws.receive_text())
        assert snap["type"] == "snapshot"
        assert {"overview", "health", "positions", "orders", "breakers", "stops"} <= set(snap)
        assert "SUPERSECRETKEY123" not in json.dumps(snap)
        # Los eventos del bot llegan en vivo
        app._publish({"type": "alert", "title": "hola"})
        for _ in range(5):
            msg = json.loads(ws.receive_text())
            if msg["type"] == "event":
                assert msg["event"]["title"] == "hola"
                break
        else:
            pytest.fail("no llegó el evento")


# ------------------------------------------------------------------ backtests
async def test_backtest_job_runs_in_separate_process(client):
    r = await client.post(
        "/api/backtests", headers=AUTH, json={"synthetic_bars": 1500, "mc_sims": 200}
    )
    assert r.status_code == 202
    job_id = r.json()["id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = (await client.get(f"/api/backtests/{job_id}", headers=AUTH)).json()
        if job["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.2)
    assert job["status"] == "done", job.get("error")
    res = job["result"]
    assert res["n_bars"] == 1500 and "sharpe" in res["report"] and res["equity"]


async def test_backtest_rejects_path_traversal_and_bad_params(client):
    r = await client.post(
        "/api/backtests",
        headers=AUTH,
        json={"data_files": [{"path": "../../etc/passwd", "symbol": "X"}]},
    )
    assert r.status_code == 422
    r = await client.post("/api/backtests", headers=AUTH, json={"risk": {"max_gross_leverage": 50}})
    assert r.status_code == 422
    r = await client.post("/api/backtests", headers=AUTH, json={"synthetic_bars": 10})
    assert r.status_code == 422


# ------------------------------------------------------------ métricas / referencia
async def test_reference_backtest_roundtrip(client, app):
    r = await client.put("/api/metrics/reference", headers=AUTH, json=OP | {"job_id": "bt-999"})
    assert r.status_code == 409
    job = (
        await client.post(
            "/api/backtests", headers=AUTH, json={"synthetic_bars": 1500, "mc_sims": 200}
        )
    ).json()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        st = (await client.get(f"/api/backtests/{job['id']}", headers=AUTH)).json()["status"]
        if st in ("done", "error"):
            break
        await asyncio.sleep(0.2)
    assert st == "done"
    r = await client.put("/api/metrics/reference", headers=AUTH, json=OP | {"job_id": job["id"]})
    assert r.status_code == 200
    m = (await client.get("/api/metrics", headers=AUTH)).json()
    ref = m["reference"]
    assert ref["job_id"] == job["id"] and ref["equity"] and "sharpe" in ref["report"]
    assert ref["montecarlo"] is None or "histogram" not in ref["montecarlo"]
    assert m["timeframe_ms"] == 3_600_000 and m["drawdown_kill"] == 0.15
    ev = (await client.get("/api/events?kind=command", headers=AUTH)).json()["events"]
    assert ev[0]["title"].startswith("SET_REFERENCE")


async def test_equity_curve_skips_warmup(app):
    from quantbot.core.models import Bar

    app.core.trading_start_ts = 3 * 3_600_000
    for i in range(5):
        bar = Bar("BTC/USDT", i * 3_600_000, (i + 1) * 3_600_000, 100, 101, 99, 100, 1)
        app._after_bar(bar)
    rows = app.journal.query("SELECT ts FROM equity ORDER BY ts")
    assert [r["ts"] for r in rows] == [3 * 3_600_000, 4 * 3_600_000, 5 * 3_600_000]


# ------------------------------------------------------------ backtests desde la UI
@pytest.fixture
def data_app(tmp_path):
    from quantbot.data.historical import write_csv
    from quantbot.data.synthetic import generate_bars

    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    write_csv(data / "BTCUSDT_1h.csv", generate_bars("BTC/USDT", 1500, seed=3))
    (data / "notas.txt").write_text("no es un dataset")
    (data / "sub" / "ETH.parquet").write_bytes(b"x")
    cfg = load_config(
        mode="paper",
        state_dir=str(tmp_path / "state"),
        api={"enabled": False, "data_dir": str(data)},
    )
    return TradingApp(cfg, Secrets(dashboard_token=TOKEN), feed=StubFeed())  # type: ignore[arg-type]


@pytest.fixture
async def data_client(data_app):
    transport = httpx.ASGITransport(app=create_api(data_app, TOKEN))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def wait_job(c, job_id, limit_s=90):
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        job = (await c.get(f"/api/backtests/{job_id}", headers=AUTH)).json()
        if job["status"] in ("done", "error"):
            return job
        await asyncio.sleep(0.2)
    raise AssertionError("timeout")


async def test_datasets_listing_only_inside_data_dir(data_client):
    r = (await data_client.get("/api/backtests/datasets", headers=AUTH)).json()
    assert [d["path"] for d in r["datasets"]] == ["BTCUSDT_1h.csv", "sub/ETH.parquet"]
    assert "BTC/USDT" in r["symbols"]


async def test_walkforward_budget_is_enforced(client):
    big = {
        "grid": {"fast_ema": [5, 10, 15, 20, 25], "slow_ema": [50, 60, 70, 80, 90, 100]},
        "train": 1000,
        "test": 300,
    }
    r = await client.post(
        "/api/backtests", headers=AUTH, json={"synthetic_bars": 6000, "walkforward": big}
    )
    assert r.status_code == 422 and "demasiado grande" in r.json()["detail"]
    r = await client.post(
        "/api/backtests",
        headers=AUTH,
        json={
            "synthetic_bars": 600,
            "walkforward": {"grid": {"fast_ema": [10]}, "train": 500, "test": 300},
        },
    )
    assert r.status_code == 422 and "no alcanzan" in r.json()["detail"]


async def test_walkforward_on_a_data_file_end_to_end(data_client):
    body = {
        "data_files": [{"path": "BTCUSDT_1h.csv", "symbol": "BTC/USDT"}],
        "symbols": ["BTC/USDT"],
        "mc_sims": 200,
        "strategy": {"fast_ema": 10, "slow_ema": 50},
        "walkforward": {"grid": {"fast_ema": [10, 20]}, "train": 600, "test": 300, "warmup": 200},
    }
    r = await data_client.post("/api/backtests", headers=AUTH, json=body)
    assert r.status_code == 202, r.text
    assert r.json()["spec"]["data_files"] == [
        ["BTCUSDT_1h.csv", "BTC/USDT"]
    ]  # sin rutas del server
    job = await wait_job(data_client, r.json()["id"])
    assert job["status"] == "done", job["error"]
    res = job["result"]
    assert res["n_bars"] == 1500
    wf = res["walkforward"]
    assert len(wf["windows"]) == 3 and wf["n_trials"] == 6
    assert res["n_trials"] == 6  # el Deflated Sharpe del backtest principal también las cuenta
    assert set(wf["param_stability"]) == {"fast_ema"}
    assert "sharpe" in wf["oos_report"] and len(wf["oos_equity"]) > 10
