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

**Pruebas acumuladas: 4** (todas corren en el workflow → `TRIALS_PRIOR = 0`)

---

## #1 — Trend following 1h (parámetros del TOML) · 2026-10-13

- **Hipótesis**: cruce EMA 20/100 + filtro de régimen (vol < p90, Kaufman ER > 0.25) +
  stop 2.5 ATR tiene edge en BTC/ETH spot 1h.
- **Refuta**: walk-forward fuera de muestra ≤ 0 o muerte con costos ×2.
- **Datos**: Binance spot, 2019-01 → 2026-09, 67 861 velas por símbolo, SHA-256 verificado.
- **Resultado** ([run](https://github.com/maxi018/backend_lumax/actions/runs/38339532921), en el repo anterior):

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
- **Resultado** ([run](https://github.com/maxi018/backend_lumax/actions/runs/38340385772), en el repo anterior):

  | | |
  |---|---|
  | Backtest | +10.6 % (CAGR 1.3 %), Sharpe 0.33, PF 1.26, 171 trades, max DD 9.5 % |
  | Costos ×2 | −4.5 % |
  | Walk-forward OOS | BTC −1.3 % (75 trades), ETH +1.7 % (68 trades), DSR ≈ 0 |
  | Monte Carlo | P(DD > 15 %) = 2.7 % · DSR del backtest 0.66 (2 pruebas) |

- **Veredicto**: **refutada**. Mejor que 1h, pero el edge desaparece con costos ×2 y el
  walk-forward queda en cero. CAGR por debajo de la tasa libre de riesgo.

## #3 y #4 — Filtro de tendencia de largo plazo: media de 200 días (1h y 4h) · 2026-10-13

*Pre-registrado: este texto se commitea ANTES de escribir el código y de ver resultados.*

- **Hipótesis**: el trend following pierde sobre todo en mercados bajistas y laterales
  largos (2022). Si cada activo solo opera largo cuando su cierre DIARIO está por encima
  de su media simple de 200 cierres diarios, se evitan esos tramos y aparece el edge.
- **Qué cambia** (y nada más; el resto, idéntico a #1/#2 con los parámetros del TOML):
  - Filtro **por activo** (BTC mira a BTC, ETH a ETH), con días UTC **completos**: el
    cierre del día es el de la última barra que cierra a las 00:00 UTC (sin look-ahead).
  - Filtro apagado → **no hay entradas** y una posición larga abierta **se cierra**.
  - Menos de 200 días de historia → no opera (fail-closed). Esto le saca a #3/#4 los
    primeros ~200 días de 2019 que #1/#2 sí operan: comparación algo desfavorable.
  - Parámetro fijado ANTES: 200 días. **No se va a probar otro valor** (150, 250…) sin
    registrarlo acá como prueba nueva.
- **Pruebas**: 1h y 4h = 2 pruebas nuevas → contador 4. El workflow corre baseline y
  filtro lado a lado, y el Deflated Sharpe se calcula con 4.
- **Refuta**: cualquiera de los criterios de las Reglas (DSR < 0.95, walk-forward fuera
  de muestra ≤ 0 en algún símbolo, muere con costos ×2, < 100 trades fuera de muestra).
  "Mejora respecto de #1/#2" **no alcanza**: tiene que pasar los criterios por sí misma.
- **Notas de implementación** (escritas después del pre-registro; no cambian la regla):
  - El cierre diario también se detecta con barras que no cierran a las 00:00 (al llegar
    la primera barra del día siguiente). Con datos de Binance, alineados, es idéntico a
    lo pre-registrado; sin esto, con datos desalineados el filtro nunca se encendía.
  - El walk-forward ahora usa como warmup al menos lo que pide la estrategia (200 días
    para este filtro) y calienta también las ventanas de train. Esto cambia un poco el
    walk-forward de la base respecto de #1/#2: por eso el workflow la recalcula al lado,
    con las mismas reglas.
- **Resultado**: *(pendiente)*

---

## Hallazgos de infraestructura (no son pruebas, no suman al contador)

Los datos reales destaparon bugs que los sintéticos nunca mostraron:

- **Posición sin stop**: si el stop viejo se ejecutaba con una entrada nueva pendiente,
  se borraba el stop de esa entrada. Afectaba también a live. Corregido + test.
- **Monte Carlo**: cobraba el slippage extra como si cada trade usara el 100 % del
  equity (5–10× de más). Corregido + test.
- **CI**: `python … | tee` corría sin `pipefail`; un backtest roto daba verde. Corregido.

Revisión del adaptador de Binance con tests de contrato (CCXT con la capa HTTP
interceptada, sin red), antes de tocar la testnet:

- **Posiciones spot leídas de futuros**: Binance declara `fetchPositions` (por sus
  futuros) y el adaptador lo usaba también en spot → "sin posiciones" con BTC en la
  cuenta → kill falso, y `flatten_all` no cerraba nada. Ahora spot = saldo del activo
  base (ignorando polvo menor al mínimo del mercado).
- **Fee en BTC**: la posición local quedaba más grande que el saldo real → discrepancia
  en la reconciliación (tolerancia 1e-6) y stops que intentaban vender BTC inexistente.
  Ahora el fill trae `base_fee` y el portfolio lo descuenta de la posición.
- **Fee en BNB** se registraba como USDT; ahora se valúa con el ticker.
- **Cursor de trades compartido** entre símbolos: podía saltear trades. Ahora es por símbolo.
- **Testnet con datos de mainnet**: en live+testnet el feed usaba streams de mainnet.

Primera corrida de `testnet-check` contra la testnet REAL de Binance (8/9 OK):

- **"Orden no encontrada" tratada como rechazo**: en CCXT `OrderNotFound` es subclase de
  `InvalidOrder` y el `except` de rechazos iba primero. Cancelar todo sin órdenes abiertas
  (Binance -2011) fallaba; peor, la consulta por clientOrderId tras un timeout (la base de
  la idempotencia) habría dado "rechazada" en vez de "no llegó, reintentar". Corregido.
- **Fondos que no son del bot**: la testnet regala ~1 BTC. El bot "adoptaba" todo el saldo
  como posición propia (8× el equity en USDT) y el riesgo la habría reducido vendiendo
  BTC ajeno. Ahora: `[exchange] reserved` para declararlos, y si encuentra una posición
  mayor que la que el bot puede armar (o no la puede valuar), no arranca.
- **Warmup con velas de mainnet** en live+testnet: ahora usa las de la testnet.

Segunda corrida: **9/9 OK** contra la testnet real. Dejó a la vista un último detalle:

- **Ruido de punto flotante + truncado del exchange**: `0.00007 + 0.00011` da
  `0.000179999…` y Binance trunca al step → 0.00017. Una salida dejaba polvo de un step,
  menor al mínimo de orden (imposible de proteger con stop). Ahora la cantidad se
  redondea a 12 decimales antes de mandarla.

## Próximas ideas (sin correr; cada una suma 1 al contador)

- **1d**: ~2 800 barras por símbolo. Los parámetros están en barras (EMA 100 = 100 días).
  Ojo: el warmup del walk-forward del workflow sería 91 barras (3 meses) y la estrategia
  necesita 131–200 (`TrendFollowing.warmup_bars`): habría que fijarlo explícitamente.
- ~~Filtro de régimen de mercado (media de 200 días)~~ → en curso como #3/#4.
