# Bitácora de investigación

Cada idea que se prueba contra datos reales queda acá, **haya salido bien o mal**.
Es lo que hace honesto al Deflated Sharpe: si probaste 10 cosas y mostrás la mejor,
el número tiene que saber que hubo 10.

## Reglas

1. **Antes de correr**: escribir la hipótesis, qué cambia y qué resultado la refutaría.
2. **Contar**: cada variante evaluada (timeframe, filtro, parámetro, universo) suma una
   prueba al contador de abajo, aunque se descarte enseguida.
3. **El workflow lo sabe**: `TRIALS_PRIOR` en `.github/workflows/backtest-real-data.yml`
   = pruebas acumuladas que NO corre ese workflow. El Deflated Sharpe usa
   `TRIALS_PRIOR + cantidad de timeframes`.
4. **Nada pasa a paper** sin: Deflated Sharpe ≥ 0.95, walk-forward fuera de muestra
   positivo en cada símbolo, sobrevivir costos ×2, y ≥ 100 trades fuera de muestra.

**Pruebas acumuladas: 2** (ambas corren en el workflow → `TRIALS_PRIOR = 0`)

---

## #1 — Trend following 1h (parámetros del TOML) · 2026-10-13

- **Hipótesis**: cruce EMA 20/100 + filtro de régimen (vol < p90, Kaufman ER > 0.25) +
  stop 2.5 ATR tiene edge en BTC/ETH spot 1h.
- **Refuta**: walk-forward fuera de muestra ≤ 0 o muerte con costos ×2.
- **Datos**: Binance spot, 2019-01 → 2026-09, 67 861 velas por símbolo, SHA-256 verificado.
- **Resultado** ([run](https://github.com/maxi018/backend_lumax/actions/runs/38339532921)):

  | | |
  |---|---|
  | Backtest | −10.1 %, Sharpe −0.35, PF 0.88, 734 trades, max DD 13.5 % |
  | Costos ×2 | −14.2 %, kill-switch a 15.4 % de DD |
  | Walk-forward OOS | BTC −3.2 %, ETH −2.8 %, DSR ≈ 0 |
  | Monte Carlo | P(DD > 15 %) = 87.6 % |

- **Veredicto**: **refutada**. Sin edge; los parámetros óptimos cambian en casi cada ventana.

## #2 — Mismo sistema en 4h · 2026-10-13

- **Hipótesis**: en 1h el ruido y los costos se comen la señal; en 4h la tendencia
  domina. Mismos parámetros del TOML (en barras, o sea horizontes 4× más largos).
- **Refuta**: igual que #1.
- **Resultado** ([run](https://github.com/maxi018/backend_lumax/actions/runs/38340385772)):

  | | |
  |---|---|
  | Backtest | +10.6 % (CAGR 1.3 %), Sharpe 0.33, PF 1.26, 171 trades, max DD 9.5 % |
  | Costos ×2 | −4.5 % |
  | Walk-forward OOS | BTC −1.3 % (75 trades), ETH +1.7 % (68 trades), DSR ≈ 0 |
  | Monte Carlo | P(DD > 15 %) = 2.7 % · DSR del backtest 0.66 (2 pruebas) |

- **Veredicto**: **refutada**. Mejor que 1h, pero el edge desaparece con costos ×2 y el
  walk-forward queda en cero. CAGR por debajo de la tasa libre de riesgo.

---

## Hallazgos de infraestructura (no son pruebas, no suman al contador)

Los datos reales destaparon bugs que los sintéticos nunca mostraron:

- **Posición sin stop**: si el stop viejo se ejecutaba con una entrada nueva pendiente,
  se borraba el stop de esa entrada. Afectaba también a live. Corregido + test.
- **Monte Carlo**: cobraba el slippage extra como si cada trade usara el 100 % del
  equity (5–10× de más). Corregido + test.
- **CI**: `python … | tee` corría sin `pipefail`; un backtest roto daba verde. Corregido.

## Próximas ideas (sin correr; cada una suma 1 al contador)

- **1d**: ~2 800 barras por símbolo. Los parámetros están en barras (EMA 100 = 100 días).
  Ojo: el warmup del walk-forward del workflow sería 91 barras (3 meses) y la estrategia
  necesita 131–200 (`TrendFollowing.warmup_bars`): habría que fijarlo explícitamente.
- **Filtro de régimen de mercado**: operar solo con BTC sobre su media de 200 días.
  Necesita código nuevo en la estrategia.
