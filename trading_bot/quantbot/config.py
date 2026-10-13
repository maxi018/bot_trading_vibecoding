"""Configuración tipada, cargada de TOML + variables de entorno para secretos.

Reglas:
1. Todo parámetro de riesgo es configurable y su default es CONSERVADOR.
2. Hay techos duros (``_HARD_CEILINGS``) que ni el archivo de config puede superar:
   si alguien pone ``max_gross_leverage = 20`` el bot se niega a arrancar.
3. Los secretos (API keys, tokens) NUNCA van en el TOML: solo en variables de entorno.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

Mode = Literal["backtest", "paper", "live", "demo"]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RiskConfig:
    # --- Position sizing -----------------------------------------------------
    target_vol_annual: float = 0.15  # volatility targeting: 15% anual
    vol_ewma_span: int = 30
    kelly_fraction: float = 0.25  # 1/4 Kelly como TECHO, nunca como objetivo
    kelly_min_trades: int = 30  # sin muestra suficiente, Kelly no se usa
    max_leverage_per_asset: float = 1.0
    rebalance_threshold: float = 0.25  # no re-tradear si |delta| < 25% del target

    # --- Límites duros (sección 3.2) -------------------------------------------
    max_risk_per_trade: float = 0.01  # 1% del equity (distancia al stop x tamaño)
    max_position_pct: float = 0.20  # ningún activo pesa más del 20%
    max_gross_leverage: float = 1.0  # spec permite 2.0; arrancamos en 1.0
    max_correlated_exposure: float = 0.40
    correlation_threshold: float = 0.70
    min_order_notional: float = 10.0

    # --- Circuit breakers (sección 3.4) ----------------------------------------
    daily_drawdown_halt: float = 0.03  # -3% diario -> halt hasta el día siguiente
    total_drawdown_kill: float = 0.15  # -15% desde el pico -> KILL (humano reactiva)
    expected_slippage_bps: float = 5.0
    slippage_anomaly_multiplier: float = 3.0
    slippage_window: int = 20
    max_slippage_bps: float = 30.0  # pre-trade: slippage estimado máximo por orden
    consecutive_losses_reduce: int = 8
    size_reduction_factor: float = 0.5
    max_data_age_s: float = 5.0
    max_reject_rate: float = 0.10
    reject_window: int = 50
    min_orders_for_reject_rate: int = 10

    # --- Fat finger / Knight Capital ---------------------------------------------
    max_price_deviation_pct: float = 0.05  # precio de orden vs último trade
    max_order_notional_pct: float = 0.25  # una sola orden no supera 25% del equity
    max_orders_per_minute: int = 20
    max_order_size_vs_avg: float = 5.0  # tamaño > 5x el promedio reciente -> veto

    # --- Ejecución protegida -------------------------------------------------------
    market_protection_bps: float = 10.0  # "market con protección": limit agresiva
    # Salidas: priorizar CERTEZA de ejecución (perderse una entrada no cuesta nada;
    # no poder salir, sí).
    exit_protection_bps: float = 100.0


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    name: str = "trend_following"
    symbols: tuple[str, ...] = ("BTC/USDT",)
    timeframe: str = "1h"
    fast_ema: int = 20
    slow_ema: int = 100
    atr_period: int = 14
    atr_stop_mult: float = 2.5
    time_stop_bars: int = 0  # 0 = desactivado
    allow_short: bool = False  # por defecto solo largo: menos formas de romperse
    # Filtro de régimen
    vol_lookback: int = 30
    vol_history: int = 500
    max_vol_percentile: float = 0.90  # vol por encima del p90 histórico -> no entrar
    min_efficiency_ratio: float = 0.25  # Kaufman ER: debajo = mercado lateral
    er_period: int = 30


@dataclass(frozen=True, slots=True)
class CostConfig:
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    half_spread_bps: float = 2.0
    slippage_bps: float = 3.0  # impacto adicional estimado por orden a mercado
    latency_ms: int = 250


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    name: str = "binance"
    testnet: bool = True
    rate_limit_per_s: float = 5.0  # más conservador que el límite del exchange
    rate_limit_burst: int = 10
    request_timeout_s: float = 10.0
    max_retries: int = 4
    reconcile_interval_s: float = 30.0
    heartbeat_timeout_s: float = 15.0  # sin mensajes WS en X s -> feed muerto
    dead_man_switch_s: int = 60  # cancelAllAfter nativo si el exchange lo soporta
    ws_url: str = "wss://stream.binance.com:9443/stream"


@dataclass(frozen=True, slots=True)
class AlertConfig:
    enabled: bool = True
    min_interval_s: float = 30.0  # dedupe: misma alerta no más de 1 vez cada 30s


@dataclass(frozen=True, slots=True)
class ApiConfig:
    enabled: bool = True
    host: str = "127.0.0.1"  # SOLO local por defecto. Exponer = poner un proxy con TLS delante.
    port: int = 8765
    snapshot_interval_s: float = 1.0
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    data_dir: str = "./data"  # único directorio desde el que la UI puede backtestear archivos


@dataclass(frozen=True, slots=True)
class DemoConfig:
    """Modo demo: datos sintéticos reproducidos en tiempo acelerado, sin red."""

    bars_per_second: float = 4.0
    warmup_bars: int = 600
    seed: int = 7
    start_price: float = 30_000.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    mode: Mode = "paper"
    initial_cash: float = 10_000.0
    state_dir: str = "./state"
    log_level: str = "INFO"
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    demo: DemoConfig = field(default_factory=DemoConfig)


@dataclass(frozen=True, slots=True)
class Secrets:
    """Leídos SOLO del entorno. ``repr`` enmascarado para no filtrarlos a los logs."""

    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    discord_webhook: str = field(default="", repr=False)
    heartbeat_url: str = field(default="", repr=False)  # healthchecks.io o similar
    dashboard_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> Secrets:
        return cls(
            api_key=os.environ.get("QB_API_KEY", ""),
            api_secret=os.environ.get("QB_API_SECRET", ""),
            telegram_token=os.environ.get("QB_TELEGRAM_TOKEN", ""),
            telegram_chat_id=os.environ.get("QB_TELEGRAM_CHAT_ID", ""),
            discord_webhook=os.environ.get("QB_DISCORD_WEBHOOK", ""),
            heartbeat_url=os.environ.get("QB_HEARTBEAT_URL", ""),
            dashboard_token=os.environ.get("QB_DASHBOARD_TOKEN", ""),
        )


# Techos que ningún archivo de configuración puede superar.
_HARD_CEILINGS: dict[str, float] = {
    "max_gross_leverage": 3.0,
    "max_leverage_per_asset": 3.0,
    "kelly_fraction": 0.5,
    "max_risk_per_trade": 0.02,
    "max_position_pct": 0.50,
    "daily_drawdown_halt": 0.10,
    "total_drawdown_kill": 0.35,
    "target_vol_annual": 0.60,
}


def validate(cfg: AppConfig) -> None:
    r = cfg.risk
    for name, ceiling in _HARD_CEILINGS.items():
        value = getattr(r, name)
        if not 0 < value <= ceiling:
            raise ConfigError(f"risk.{name}={value} fuera de rango (0, {ceiling}]")
    if r.max_correlated_exposure > r.max_gross_leverage:
        raise ConfigError("max_correlated_exposure no puede superar max_gross_leverage")
    s = cfg.strategy
    if s.fast_ema >= s.slow_ema:
        raise ConfigError("fast_ema debe ser < slow_ema")
    if s.atr_stop_mult <= 0:
        raise ConfigError("Toda posición necesita stop: atr_stop_mult > 0")
    if not 0 < cfg.api.port < 65536:
        raise ConfigError("api.port inválido")
    if cfg.initial_cash <= 0:
        raise ConfigError("initial_cash debe ser > 0")


T = TypeVar("T")


def _build(cls: type[T], data: dict[str, Any]) -> T:
    """Construye un dataclass anidado desde un dict, rechazando claves desconocidas.

    Una clave mal escrita (``max_drawdwon``) silenciosamente ignorada es un bug de
    riesgo: preferimos fallar fuerte.
    """
    assert is_dataclass(cls)
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"Claves desconocidas en [{cls.__name__}]: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        default = getattr(cls(), name)
        if is_dataclass(default) and isinstance(value, dict):
            kwargs[name] = _build(type(default), value)
        elif isinstance(default, tuple) and isinstance(value, list):
            kwargs[name] = tuple(value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None = None, **overrides: Any) -> AppConfig:
    data: dict[str, Any] = {}
    if path is not None:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    data.update(overrides)
    cfg = _build(AppConfig, data)
    validate(cfg)
    return cfg
