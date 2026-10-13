# quantbot

Bot de trading cuantitativo (cripto spot) con **risk engine con poder de veto**, dashboard
web y backtests sobre datos reales. Diseñado para que la seguridad del capital vaya antes
que cualquier ejecución: ante la duda, no opera.

> ⚠️ Material de ingeniería y estadística, **no asesoramiento financiero**.

```bash
./run.sh            # demo: datos sintéticos, sin red ni API keys → http://127.0.0.1:8765
```

| | |
|---|---|
| [`trading_bot/`](trading_bot/README.md) | El bot (Python 3.11+, asyncio): arquitectura, risk engine, backtesting, deployment, dashboard API |
| [`dashboard/`](dashboard) | Interfaz web (React + Vite + TS): KILL siempre visible, posiciones, métricas, backtests |
| [`trading_bot/research/LOG.md`](trading_bot/research/LOG.md) | Bitácora de investigación: qué se probó contra datos reales y con qué resultado |
| [`.github/workflows/`](.github/workflows) | CI (tests, tipos, Docker) y backtest mensual con datos reales de Binance |

## Estado

- **Infraestructura**: lista para paper; para live falta correr `python -m quantbot testnet-check`
  con una key de testnet (ver [deployment](trading_bot/README.md#7-deployment-en-producción)).
- **Estrategia**: ninguna de las probadas tiene edge: trend following (con y sin filtro de
  200 días) y reversión a la media, en 1h y 4h — 6 pruebas, 6 refutadas
  ([detalle](trading_bot/research/LOG.md)). No usar con capital real.

Historial: el proyecto empezó dentro de `maxi018/backend_lumax` y se movió acá con todos sus commits.
