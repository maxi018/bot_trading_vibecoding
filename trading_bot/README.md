# quantbot — Bot de trading event-driven con risk engine con poder de veto

> ⚠️ Material de ingeniería y estadística, **no asesoramiento financiero**. El código
> prioriza la **seguridad del capital por encima de cualquier ejecución**: ante la duda, no opera.

Implementación de referencia de la especificación *"Bot de Trading Automatizado:
Arquitectura, Riesgo y Validación"*. Python 3.11+, `asyncio`, tipado estricto
(`mypy --strict` limpio), **núcleo sin dependencias externas** (solo stdlib).

```
82 tests · ruff ✔ · mypy --strict ✔ · backtest == paper == live (mismo pipeline)
```

---

## 1. Arquitectura y estructura de archivos

```
trading_bot/
├── pyproject.toml              # extras: [live] ccxt+websockets, [data] duckdb+pyarrow, [dev]
├── config/
│   ├── default.toml            # TODOS los parámetros, con defaults conservadores
│   ├── paper.toml
│   └── live.example.toml
├── .env.example                # secretos: SOLO por variables de entorno
├── quantbot/
│   ├── __main__.py             # CLI: backtest | walkforward | sensitivity | paper | live | reset-kill-switch
│   ├── config.py               # dataclasses tipadas + techos duros + rechazo de claves desconocidas
│   ├── app.py                  # ★ MAIN EVENT LOOP async (TaskGroup + cola single-writer + safe shutdown)
│   ├── trading_core.py         # ★ pipeline único: mercado → risk → estrategia → risk → órdenes
│   │
│   ├── core/                   # ── Core: modelos, reloj, logging, alertas, kill-switch
│   │   ├── models.py           #    Tick, Bar, OrderBook(L2+secuencias), Order, Fill, Position, AccountMetrics, Signal
│   │   ├── portfolio.py        #    estado de cuenta en tiempo real (equity, DD, rachas)
│   │   ├── clock.py            #    LiveClock / SimClock + guardas anti look-ahead
│   │   ├── kill_switch.py      #    persistente en disco; SOLO un humano lo rearma
│   │   ├── alerting.py         #    Telegram / Discord, no bloqueante, con dedupe
│   │   └── logging_setup.py    #    logs JSON estructurados
│   │
│   ├── data/                   # ── Data Feeds / Ingestion
│   │   ├── websocket_feed.py   #    WS resiliente: heartbeat/zombies, gaps de secuencia, backoff+jitter, resync
│   │   ├── historical.py       #    CSV / Parquet(DuckDB) + validación (huecos, duplicados, saltos)
│   │   ├── bars.py             #    barras por tiempo / volumen / dólar (López de Prado)
│   │   └── synthetic.py        #    datos sintéticos con regímenes (tests, NO validación)
│   │
│   ├── strategy/               # ── Strategy / Alpha Engine
│   │   ├── base.py             #    contrato: eventos → Signal (nunca órdenes ni tamaños)
│   │   ├── indicators.py       #    EMA, ATR, ER, percentiles: incrementales O(1)
│   │   ├── regime.py           #    filtro de régimen: WARMUP / HIGH_VOL / RANGE / TREND
│   │   └── trend_following.py  #    EMA cross + stop ATR + chandelier trailing + time stop
│   │
│   ├── risk/                   # ── Risk Manager (PODER DE VETO)
│   │   ├── sizing.py           #    volatility targeting + Kelly fraccional como TECHO
│   │   └── engine.py           #    pre-trade checks, límites, circuit breakers, kill
│   │
│   ├── execution/              # ── Execution Handler / Exchange API Wrapper
│   │   ├── base.py             #    protocolo ExchangeClient + jerarquía de errores (retryable o no)
│   │   ├── order_manager.py    #    clientOrderId idempotente, stops nativos, reconciliación, flatten
│   │   ├── rate_limiter.py     #    token bucket (más conservador que el exchange)
│   │   ├── cost_model.py       #    fees maker/taker, spread, slippage, limit "trade-through"
│   │   ├── paper.py            #    exchange simulado (backtest y paper) con inyección de fallas
│   │   └── ccxt_exchange.py    #    exchange real vía CCXT (validar en testnet primero)
│   │
│   └── backtest/
│       ├── engine.py           #    backtester event-driven sobre el MISMO TradingCore
│       ├── metrics.py          #    Sharpe, Sortino, Calmar, PF, expectancy, Deflated Sharpe
│       ├── walk_forward.py     #    walk-forward + sensibilidad de parámetros
│       └── montecarlo.py       #    bootstrap + slippage extra + fills perdidos → DD p95
├── tests/                      # look-ahead, reconexión, kill-switch, risk, OMS, app, backtest
├── scripts/download_data.py    # histórico OHLCV → Parquet/CSV vía CCXT
└── deploy/
    ├── setup_vps.sh            # provisión Ubuntu: usuario, firewall, chrony, venv, systemd
    ├── systemd/quantbot.service
    └── remote_watchdog.py      # watchdog en OTRA máquina (dead man's switch externo)
```

### Flujo de un evento

```
          ┌──────────── asyncio.Queue (single writer) ─────────────┐
 WS feed ─┤ Bar/Tick/Book/Heartbeat · Fills · WatchdogTick · Reconcile├─► consumidor único
          └─────────────────────────────────────────────────────────┘          │
                                                                               ▼
   risk.on_bar ─► marks ─► CIRCUIT BREAKERS ─► strategy.on_bar ─► Signal(dir, stop)
                                                                       │
                                     RiskEngine.evaluate ◄─────────────┘
                     (sizing · límites · fat-finger · rate · slippage · halt/kill)
                          │ VETO → log            │ APPROVE/REDUCE
                          ▼                       ▼
                       alerta           OrderManager.submit (clientOrderId, retries idempotentes)
                                                  │
                                         Exchange (paper / CCXT)
                                                  │ fill
                         portfolio ◄── OrderManager.on_fill ──► stop NATIVO reduce-only en el exchange
```

**Por qué una cola single-writer:** el estado de cuenta y del risk engine solo se muta
desde el consumidor. Watchdog, reconciliación y fills también entran por la cola, así que
no hay carreras ni estados intermedios observables. Lo que corre en paralelo (alertas,
heartbeat, dead man's switch) no muta estado.

---

## 2. Modelo de datos y gestión de estado

| Estructura | Archivo | Claves de diseño |
|---|---|---|
| `Tick`, `Bar` | `core/models.py` | inmutables (`frozen`, `slots`); `Bar.ts` = **cierre** (la barra no existe antes); validación OHLC en `__post_init__` |
| `OrderBook` | `core/models.py` | L2 con `last_seq`; `apply_delta` lanza `SequenceGapError` ante huecos o libro cruzado; `microprice`, `imbalance`, `walk_book` |
| `Order` | `core/models.py` | máquina de estados (`PENDING_NEW → OPEN → PARTIALLY_FILLED → FILLED`, + `UNKNOWN` tras timeout); `apply_fill` rechaza overfills |
| `Fill` | `core/models.py` | `decision_price` (arrival) → `slippage_bps` real que alimenta el circuit breaker |
| `Position` | `core/models.py` | qty con signo; PnL realizado en reducciones y flips |
| `AccountMetrics` | `core/models.py` | snapshot inmutable: `daily_pnl_pct`, `drawdown_from_peak`, `gross_leverage` |
| `Signal` | `core/models.py` | dirección + stop **obligatorio** y del lado correcto; sin tamaño |
| `Portfolio` | `core/portfolio.py` | caja, posiciones, marks, pico, equity de inicio de día UTC, rachas perdedoras |

La **verdad la tiene el exchange**: `OrderManager.reconcile()` compara cada N segundos; una
discrepancia confirmada en **dos** chequeos seguidos (uno solo puede ser un fill en vuelo)
dispara kill + flatten usando las posiciones **del exchange**.

---

## 3. Risk engine (código crítico)

`risk/engine.py` es **puro** (sin I/O): decide y devuelve acciones; el núcleo las ejecuta.
Es **fail-closed**: cualquier excepción interna = veto.

**Sizing** (`risk/sizing.py`):
```
qty = min( equity · min(target_vol/vol_EWMA, max_lev) / price,     ← volatility targeting
           equity · max_position_pct / price,                      ← límite por activo
           equity · min(max_risk_per_trade, ¼·Kelly) / |price−stop| ) ← riesgo al stop
      · strength · size_multiplier(rachas)
```
Kelly solo actúa como **techo** y solo con ≥ 30 trades de muestra.

**Pre-trade checks** (en orden): kill-switch → look-ahead → datos presentes y frescos →
fat-finger de precio → sizing → flip en dos pasos → halt (solo salidas) → banda de
no-rebalanceo → exposición por activo / bruta / correlacionada (reduce, no rechaza) →
fat-finger de notional y de tamaño vs. promedio → órdenes por minuto (anti Knight Capital) →
**slippage estimado** con walk-the-book (achica hasta 4 veces o veta) → orden
"market protegida" (limit IOC).

**Circuit breakers** (`check_circuit_breakers`, tras cada evento + watchdog cada 1 s):

| Condición | Acción |
|---|---|
| DD desde el pico ≥ 15% | **KILL + flatten** (persistente, solo humano rearma) |
| PnL diario ≤ −3% | HALT hasta el próximo día UTC (salidas permitidas) |
| Slippage medio (20 fills) > 3× esperado | HALT 1 h |
| ≥ 8 pérdidas seguidas | tamaño × 0.5 (se restaura con una ganancia) |
| Datos > 5 s sin actualizar / feed caído | HALT hasta que vuelvan los datos |
| Tasa de rechazos > 10% | HALT 15 min |
| Desync local vs exchange | **KILL + flatten** |

---

## 4. Estrategia y señales

`TrendFollowingStrategy`: cruce EMA rápida/lenta, stop inicial `k·ATR`, trailing
chandelier que solo se mueve a favor, salida por cruce inverso o time stop, **sin take
profit fijo**. Tras un stop-out exige un cruce nuevo. Si una salida no se ejecuta, la
reintenta (`exit_retry`).

**Filtro de régimen** (`strategy/regime.py`): no abre posiciones si la volatilidad realizada
está sobre su percentil 90 histórico (crisis/flash crash) ni si el Efficiency Ratio de
Kaufman indica mercado lateral. Se puede reemplazar por un HMM implementando `update(bar)`.

Para agregar una estrategia: heredar de `Strategy`, implementar `on_bar` devolviendo
`Signal`s y registrarla en `strategy/__init__.py`.

---

## 5. Ejecución y manejo de errores en tiempo real

| Falla | Manejo |
|---|---|
| WS cae / handshake falla | `mark_data_stale` → veto de órdenes nuevas; backoff exponencial con jitter (1 s → 60 s) |
| Zombie socket | `recv()` con timeout de heartbeat (15 s) → reconexión |
| Hueco de secuencia en el libro | `SequenceGapError` → reconexión + snapshot REST (**resync**) |
| Mensaje malformado | se descarta y se loguea (no reconecta) |
| Bug en el consumidor | `SinkError` → **no** se reintenta: TaskGroup cae → safe shutdown |
| Timeout al enviar orden | estado `UNKNOWN` → consulta por `clientOrderId` → reintento con el **mismo** id |
| Rechazo del exchange | no se reintenta; alimenta el breaker de tasa de rechazos |
| 429 / rate limit | token bucket local + `penalize()` |
| Credenciales revocadas | kill + exit code 3 |
| Fill duplicado tras reconexión | dedupe por `trade_id` |
| Bot muere | stops nativos siguen en el exchange · dead man's switch nativo · watchdog remoto |
| Salida en spot con stop activo | se libera el stop, se sale; si la salida no se llena, el último stop se re-coloca |
| SIGTERM / excepción no manejada | `safe_shutdown`: cancela entradas, **mantiene stops**, reconcilia, alerta |

---

## 6. Hoja de ruta: backtesting riguroso

```bash
cd trading_bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,data,live]"
pytest                                   # 1) todo verde antes de cualquier cosa
```

1. **Datos**: descargar y validar (huecos, duplicados, saltos > 50%).
   ```bash
   python scripts/download_data.py --symbol BTC/USDT --timeframe 1h --since 2019-01-01 --out data/BTCUSDT_1h.parquet
   python scripts/download_data.py --symbol ETH/USDT --timeframe 1h --since 2019-01-01 --out data/ETHUSDT_1h.parquet
   python -m quantbot validate-data --data data/BTCUSDT_1h.parquet --symbol BTC/USDT
   ```
   Para universos multi-activo usar datasets **point-in-time con deslistados** (survivorship bias).
2. **Backtest base** con costos realistas (fill en la apertura siguiente, fees, spread, slippage):
   ```bash
   python -m quantbot backtest --data data/BTCUSDT_1h.parquet,data/ETHUSDT_1h.parquet \
       --symbol BTC/USDT,ETH/USDT --out results/base.json
   ```
3. **Stress de costos**: `--stress 1.5` y `--stress 2`. Si el edge desaparece, no había edge.
4. **Sensibilidad**: buscar **meseta**, no pico.
   ```bash
   python -m quantbot sensitivity --data ... --grid "fast_ema=15,20,25;slow_ema=80,100,120"
   ```
5. **Walk-forward**: solo cuentan los tramos de test concatenados; mirar estabilidad de parámetros.
   ```bash
   python -m quantbot walkforward --data ... --grid "fast_ema=10,20,30;slow_ema=100,200;atr_stop_mult=2.5,3" \
       --train 8760 --test 2190
   ```
6. **Montecarlo + Deflated Sharpe**: el sizing debe tolerar el DD p95, no el del backtest.
   ```bash
   python -m quantbot backtest --data ... --montecarlo --trials <TOTAL de configuraciones probadas>
   ```
7. **Escenarios de stress**: correr sobre marzo 2020, mayo 2022 (LUNA) y noviembre 2022 (FTX).
8. **Definir ANTES de prender** el criterio de apagado (la CLI sugiere `1.5 × DD p95`).

Banderas rojas que el reporte marca solo: Sharpe > 3, < 100 trades, profit factor > 3.

---

## 7. Deployment en producción

1. **VPS** en la región del matching engine (muchos exchanges: AWS Tokio `ap-northeast-1`;
   confirmar por exchange). Ubuntu 22.04/24.04, 1 vCPU / 1 GB alcanza para baja frecuencia.
2. **Provisión**: copiar `trading_bot/` a `/opt/quantbot` y correr `sudo bash deploy/setup_vps.sh`
   (usuario sin shell, UFW solo SSH, SSH sin password, **chrony** para el reloj, venv, systemd endurecido).
3. **Secretos** en `/etc/quantbot/quantbot.env` (`chmod 640`, owner `root:quantbot`):
   - API key **sin permiso de retiro**, con **whitelist de IP** del VPS, una key por bot.
   - Nunca en el repo ni en el TOML (`Secrets.__repr__` las enmascara en logs).
4. **Alertas**: crear bot con @BotFather → `QB_TELEGRAM_TOKEN` + `QB_TELEGRAM_CHAT_ID`
   y/o `QB_DISCORD_WEBHOOK`. Se alerta: arranque/parada, halts, kill, stops ejecutados,
   feed caído/recuperado, órdenes en estado desconocido, posiciones sin stop.
5. **Heartbeat externo**: check en healthchecks.io → `QB_HEARTBEAT_URL`. Solo late si el
   consumidor procesa eventos (no solo si el proceso existe).
6. **Watchdog remoto** en otra máquina con otra API key:
   `python deploy/remote_watchdog.py --symbols BTC/USDT` (cancela órdenes si el bot no late).
7. **Monitoreo**: `journalctl -u quantbot -f | jq` — cada 60 s se loguea `account` con
   equity, DD, apalancamiento, halts, reconexiones y tamaño de cola (listo para Loki/Grafana).
8. **Fases** (spec §6.4):
   - Paper ≥ 1–3 meses: `systemctl start quantbot` (config `paper.toml`). Comparar contra el backtest.
   - Testnet live: `live.toml` con `testnet = true`. Probar: entrada, stop nativo, cancelación,
     corte de red, reinicio con posición abierta, reconciliación.
   - Live con capital mínimo 3–6 meses: `QB_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK`.
   - Escalar de a poco solo si live ≈ backtest.
9. **Kill-switch**: si se activa, el servicio sale con código 2 y systemd **no** lo reinicia.
   Revisar causa, logs y posiciones; recién entonces:
   ```bash
   sudo -u quantbot /opt/quantbot/.venv/bin/python -m quantbot reset-kill-switch \
       --state-dir /var/lib/quantbot --operator maxi --confirm
   ```
10. **Deploys controlados** (lección Knight Capital): `systemctl stop` → actualizar → `pytest`
    → `systemctl start`. Nunca dos versiones corriendo contra la misma cuenta.

---

## Limitaciones conocidas (honestas)

- `ccxt_exchange.py` usa la API unificada de CCXT; nombres de parámetros de stops,
  reduce-only y cancel-after varían por exchange: **validar en testnet** antes de usar dinero.
- Fills en live por polling (`fetch_my_trades`); para latencia menor integrar WS privado (ccxt.pro).
- En paper en vivo, la ejecución es contra el último libro/precio (sin cola de limit orders real).
- La estrategia de ejemplo es una **referencia**, no una recomendación: en datos sintéticos
  aleatorios pierde (como debe ser).
