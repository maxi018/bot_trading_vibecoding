"""El kill-switch: persiste, sobrevive reinicios y SOLO un humano lo rearma."""

from __future__ import annotations

import pytest

from quantbot.core.kill_switch import KillSwitch, human_reset
from quantbot.core.models import Direction, Signal
from quantbot.core.portfolio import Portfolio
from quantbot.risk.engine import RiskEngine

from .conftest import warm_risk


def test_engage_persists_across_restarts(tmp_path):
    ks = KillSwitch(tmp_path)
    ks.engage("DD total", flatten=True, now_ms=1)
    assert KillSwitch(tmp_path).engaged  # "reinicio": nueva instancia, mismo disco
    assert KillSwitch(tmp_path).record.reason == "DD total"


def test_first_reason_wins(tmp_path):
    ks = KillSwitch(tmp_path)
    ks.engage("primera", flatten=False)
    ks.engage("segunda", flatten=True)
    assert ks.record.reason == "primera"


def test_corrupt_file_is_treated_as_engaged(tmp_path):
    (tmp_path / KillSwitch.FILENAME).write_text("{basura")
    assert KillSwitch(tmp_path).engaged


def test_no_programmatic_reset_api():
    # El objeto no expone forma de desactivarse: solo human_reset (CLI).
    public = {n for n in dir(KillSwitch) if not n.startswith("_")}
    assert not any(w in n for n in public for w in ("reset", "disengage", "clear", "enable"))
    assert not any(
        w in n for n in dir(RiskEngine) for w in ("reset_kill", "disengage", "rearm", "unkill")
    )


def test_human_reset_requires_operator_and_audits(tmp_path):
    KillSwitch(tmp_path).engage("x", flatten=False)
    with pytest.raises(ValueError):
        human_reset(tmp_path, "  ")
    assert human_reset(tmp_path, "maxi")
    assert not KillSwitch(tmp_path).engaged
    assert "maxi" in (tmp_path / "kill_switch_audit.log").read_text()


def test_engine_vetoes_everything_after_kill_even_after_restart(risk_factory, clock):
    risk = risk_factory()
    px = warm_risk(risk, clock)
    risk.manual_kill("test")
    s = Signal("t", "BTC/USDT", clock.now_ms(), Direction.LONG, px, stop_price=px * 0.9)
    assert risk.evaluate(s, Portfolio(10_000)).reason == "kill_switch_engaged"
    # Reinicio del engine con el mismo state dir
    risk2 = risk_factory()
    assert risk2.killed and not risk2.trading_enabled
    assert risk2.evaluate(s, Portfolio(10_000)).reason == "kill_switch_engaged"
