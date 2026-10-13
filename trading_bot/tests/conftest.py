from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from quantbot.config import AppConfig, CostConfig, RiskConfig, load_config
from quantbot.core.alerting import Alerter
from quantbot.core.clock import SimClock
from quantbot.core.kill_switch import KillSwitch
from quantbot.core.models import Bar
from quantbot.core.portfolio import Portfolio
from quantbot.risk.engine import RiskEngine

H = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % H)


def make_bar(i: int, close: float, symbol: str = "BTC/USDT", spread: float = 0.002) -> Bar:
    o = close
    return Bar(
        symbol,
        T0 + i * H,
        T0 + (i + 1) * H,
        o,
        close * (1 + spread),
        close * (1 - spread),
        close,
        100.0,
    )


@pytest.fixture
def cfg() -> AppConfig:
    return load_config(mode="backtest")


@pytest.fixture
def clock() -> SimClock:
    return SimClock(T0)


@pytest.fixture
def risk_factory(tmp_path: Path, clock: SimClock) -> Callable[..., RiskEngine]:
    def build(**risk_overrides: Any) -> RiskEngine:
        rc = RiskConfig(**risk_overrides)
        return RiskEngine(
            rc, CostConfig(), KillSwitch(tmp_path / "state"), clock, 8760.0, Alerter([], 0)
        )

    return build


def warm_risk(
    risk: RiskEngine,
    clock: SimClock,
    n: int = 60,
    price: float = 100.0,
    vol: float = 0.01,
    symbol: str = "BTC/USDT",
) -> float:
    """Alimenta barras alternando +/-vol para tener una volatilidad conocida."""
    p = price
    for i in range(n):
        p = price * (1 + (vol if i % 2 else -vol))
        bar = make_bar(i, p, symbol)
        clock.advance_to(bar.ts)
        risk.on_bar(bar)
    return p


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio(10_000.0)
