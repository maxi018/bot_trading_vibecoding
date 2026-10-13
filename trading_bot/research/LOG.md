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

**Pruebas acumuladas: 9** (todas corren en el workflow → `TRIALS_PRIOR = 0`)

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
- **Resultado** ([run](https://github.com/maxi018/bot_trading_vibecoding/actions/runs/38462645190);
  base recalculada en la misma corrida, Deflated Sharpe con 4 pruebas):

  | | base 1h | **#3 filtro 1h** | base 4h | **#4 filtro 4h** |
  |---|---|---|---|---|
  | Backtest | −10.1 % | −9.9 % | +10.6 % | +11.3 % (CAGR 1.4 %) |
  | Sharpe · PF | −0.35 · 0.88 | −0.48 · 0.81 | 0.33 · 1.26 | 0.42 · 1.44 |
  | Max DD | 13.5 % | 12.4 % | 9.5 % | 10.5 % |
  | Trades | 734 | 407 | 171 | 103 |
  | Deflated Sharpe | 0.02 | 0.01 | 0.45 | 0.55 |
  | Costos ×2 | −14.2 %, kill | −14.7 %, kill | −4.5 % | 0.0 % |
  | Monte Carlo P(DD > 15 %) | 87.6 % | 67.4 % | 2.7 % | 0.3 % |
  | WF fuera de muestra BTC | −3.1 % (243 tr.) | +0.4 % (171) | −1.3 % (78) | −1.6 % (55) |
  | WF fuera de muestra ETH | −2.8 % (271) | −4.9 % (188) | +4.0 % (68) | +12.4 % (52) |

- **Veredicto**: **ambas refutadas** con los criterios fijados de antemano.
  - #3 (1h): no mejora nada relevante; con costos ×2 salta el kill igual que la base.
  - #4 (4h): DSR 0.55 < 0.95, walk-forward de BTC negativo, con costos ×2 el beneficio
    queda en cero y solo 52–55 trades fuera de muestra por símbolo.
  - Lo que **sí** hizo el filtro: bajar el riesgo de cola (P(DD > 15 %) de 2.7 % a 0.3 %
    en 4h) y apagarse en los tramos bajistas (6 de 26 ventanas sin operar). Es un efecto
    sobre el riesgo, no un edge.
  - ⚠️ El +12.4 % de ETH 4h es justo el tipo de resultado aislado que la bitácora existe
    para no perseguir: 52 trades, un solo símbolo, elegido después de ver los datos.
    Probarlo solo en ETH sería una prueba NUEVA (#5) y debería registrarse antes.
  - Efecto de calentar las ventanas de train: el walk-forward de la base cambió poco
    respecto de #1/#2 (p. ej. ETH 4h de +1.7 % a +4.0 %); las conclusiones de #1/#2 no cambian.

## #5 y #6 — Reversión a la media: comprar caídas dentro de una tendencia alcista (1h y 4h) · 2026-10-13

*Pre-registrado: este texto se commitea ANTES de escribir el código y de ver resultados.*

- **Hipótesis**: las 4 pruebas de trend following (comprar fuerza) no tienen edge. La
  familia opuesta: dentro de una tendencia alcista de largo plazo, las caídas bruscas de
  corto plazo tienden a rebotar. Comprar el sobrevendido y salir en el rebote.
- **Reglas** (estrategia nueva `mean_reversion`; solo largos; mismo risk engine):
  - **Tendencia**: solo con el cierre diario sobre su media de 200 días (el mismo filtro
    de #3/#4, por activo, días UTC completos).
  - **Entrada**: RSI de 2 períodos (Wilder) sobre los cierres de las barras < **10**.
  - **Salida**: cierre de barra **por encima de su media simple de 5 barras** (el rebote),
    o **10 barras** sin rebote (time stop), o el filtro de tendencia se apaga.
  - **Stop nativo** siempre: entrada − **2.5 ATR(14)**, fijo (sin trailing).
  - Tras una salida puede reentrar en la próxima señal: no espera nada extra.
- **Parámetros fijados ANTES**: RSI 2 / umbral 10 / salida SMA 5 / 10 barras / 2.5 ATR /
  200 días. **No se prueban otros valores** sin registrarlos acá como prueba nueva. (El
  walk-forward optimiza dentro de una grilla chica, umbral ∈ {5, 10, 15} y salida ∈
  {3, 5, 8}, y descuenta esas combinaciones en su propio Deflated Sharpe.)
- **Pruebas**: 1h y 4h = 2 nuevas → contador **6**. El workflow corre base, #3/#4 y esta
  lado a lado, y el Deflated Sharpe se calcula con 6.
- **Refuta**: los criterios de las Reglas (DSR < 0.95, walk-forward fuera de muestra ≤ 0
  en algún símbolo, muere con costos ×2, < 100 trades fuera de muestra). Ojo: la
  reversión opera seguido y gana poco por trade, así que el test de **costos ×2** es el
  que más la amenaza.
- **Resultado** ([run](https://github.com/maxi018/bot_trading_vibecoding/actions/runs/38468244798);
  Deflated Sharpe con 6 pruebas):

  | | **#5 reversión 1h** | **#6 reversión 4h** |
  |---|---|---|
  | Backtest | −14.6 %, **kill a 15.0 % de DD** | −14.2 %, **kill a 15.2 % de DD** |
  | Sharpe · PF | −1.23 · 0.59 | −0.79 · 0.69 |
  | Trades · win rate | 629 · 59 % | 674 · 65 % |
  | Ganancia / pérdida media | 5.7 / 13.9 USDT | 7.2 / 19.7 USDT |
  | Deflated Sharpe | 0.00 | 0.00 |
  | Costos ×2 | −15.0 %, kill | −14.3 %, kill |
  | Monte Carlo P(DD > 15 %) | 97.8 % | 79.5 % |
  | WF fuera de muestra BTC | −29.9 % (1 124 tr.) | −2.4 % (394) |
  | WF fuera de muestra ETH | −37.1 % (1 393 tr.) | −15.3 % (454) |

- **Veredicto**: **ambas refutadas**, y con más claridad que cualquier prueba anterior:
  falla **todos** los criterios menos el de cantidad de trades.
  - El clásico perfil de reversión mal pagada: gana seguido (59–65 %) pero gana poco y
    pierde mucho (la pérdida media es 2.5–2.7× la ganancia media). Los rebotes chicos no
    alcanzan a pagar los stops de 2.5 ATR ni los costos.
  - Ni siquiera la grilla del walk-forward la salva: en 1h el óptimo de cada ventana es
    casi siempre el umbral más extremo (RSI < 5), y aun así pierde fuera de muestra.
  - El filtro de 200 días no alcanza: "tendencia alcista de largo plazo" no impide que
    las caídas de corto plazo sigan cayendo en cripto (momentum de corto plazo).
  - Ni ajustar parámetros tiene sentido acá: todas las combinaciones de la grilla pierden.
    La familia "comprar sobrevendido en barras intradiarias" queda descartada para BTC/ETH.

## #7, #8 y #9 — Las mismas tres estrategias en velas diarias (1d) · 2026-10-13

*Pre-registrado: este texto se commitea ANTES de correr nada con datos reales.*

- **Hipótesis**: en 1h y 4h los costos y el ruido de corto plazo se comen la señal. En
  velas diarias hay muchas menos operaciones (costos totales mucho menores) y el
  movimiento por trade es mayor que el costo. Además, la reversión con RSI(2) es una
  estrategia diseñada originalmente para velas diarias (Connors): en 1h/4h la probamos
  fuera de su escala natural.
- **Qué cambia**: solo el timeframe. Los TOML quedan idénticos, así que los parámetros (en
  barras) pasan a ser días:
  - **#7** base trend following: EMA 20/100 días, stop 2.5 ATR(14 días).
  - **#8** + filtro de 200 días (#3/#4).
  - **#9** reversión a la media (#5/#6): RSI(2) < 10, salida sobre SMA 5, time stop 10 días.
  - Walk-forward: train 1 año (365 barras), test 3 meses (91), mismas grillas.
- **Pruebas**: 3 nuevas → contador **9**. El workflow pasa a correr 1h, 4h y 1d con las
  tres configs, y el Deflated Sharpe se calcula con 9.
- **Refuta**: los criterios de las Reglas, sin cambios.
- ⚠️ **Advertencia escrita de antemano — poder estadístico**: con ~2 800 velas por símbolo,
  el trend following va a hacer del orden de **20 trades fuera de muestra por símbolo**
  (lo vimos con datos sintéticos). El criterio de ≥ 100 trades es casi **inalcanzable por
  diseño** para #7/#8; #9 puede rondarlo. **El criterio no se relaja después de ver los
  resultados.** Si #7/#8 dieran positivos en ambos símbolos, eso no los aprueba: solo
  justificaría pre-registrar una prueba nueva con más activos (más trades por
  diversificación, no por más parámetros).
- **Resultado** ([run](https://github.com/maxi018/bot_trading_vibecoding/actions/runs/38484302647);
  2 830 velas diarias por símbolo, Deflated Sharpe con 9 pruebas):

  | | **#7 base 1d** | **#8 filtro 200d 1d** | **#9 reversión 1d** |
  |---|---|---|---|
  | Backtest | +10.0 % (CAGR 1.2 %) | +12.7 % (CAGR 1.6 %) | +0.4 % |
  | Sharpe · PF | 0.37 · 1.73 | 0.50 · 2.71 | 0.03 · 1.02 |
  | Max DD | 7.4 % | 3.7 % | 7.2 % |
  | Trades | 29 | 27 | 143 |
  | Deflated Sharpe | 0.31 | 0.45 | 0.08 |
  | Costos ×2 | +5.7 % | +11.7 % | −0.3 % |
  | Monte Carlo P(DD > 15 %) | 0.07 % | 0.00 % | 0.11 % |
  | WF fuera de muestra BTC | +10.4 % (24 tr.)¹ | +10.6 % (23)¹ | −0.5 % (72) |
  | WF fuera de muestra ETH | +3.4 % (29)¹ | +13.2 % (26)¹ | +5.8 % (67) |

  ¹ **Walk-forward sin optimizar** (ver Hallazgos): en 1d, el trend following no llega a
  5 trades en un año de train con ninguna combinación, así que en casi todas las ventanas
  se usó en silencio la primera de la grilla (EMA 10/100). Es fuera de muestra de verdad,
  pero de un parámetro fijo, no de una optimización. Corregido; la próxima corrida usa
  los parámetros del TOML en esas ventanas y lo avisa.

- **Veredicto**: **las tres refutadas** con los criterios fijados de antemano.
  - #9 (reversión 1d): DSR 0.08, walk-forward de BTC negativo, con costos ×2 pierde y no
    llega a 100 trades fuera de muestra por símbolo. Confirma #5/#6: no hay edge.
  - #7 y #8 (trend following 1d): DSR 0.31 y 0.45 < 0.95, y 23–29 trades. Fallan
    exactamente donde lo avisó el pre-registro: **no hay muestra para aprobar nada**.
  - Pero #8 es lo primero que **no muere** en ninguno de los otros tests: sobrevive
    costos ×2 casi intacto (+11.7 %), walk-forward positivo en ambos símbolos, DD 3.7 % y
    el backtest (EMA 20/100) coincide con el walk-forward (EMA 10/100). Con 27 trades eso
    puede ser suerte, y la regla de antemano dice qué hacer: **no se aprueba; justifica
    pre-registrar una prueba nueva con más activos** (más trades por diversificación).
  - Ojo con el número: CAGR 1.6 % con DD 3.7 % está por debajo de la tasa libre de riesgo.
    El sizing actual arriesga muy poco por trade; escalarlo no crea edge, solo amplifica
    lo que haya (o no haya).

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

Con velas diarias (#7–#9):

- **Walk-forward sin optimizar, en silencio**: una combinación necesita ≥ 5 trades en
  train para competir. Si ninguna llegaba, quedaba elegida la PRIMERA de la grilla y el
  reporte no decía nada (con 1d pasó en 24–27 de 27 ventanas del trend following). Ahora
  esas ventanas usan los parámetros del TOML, quedan marcadas y la salida avisa cuántas
  fueron. Lo mismo en el dashboard, donde además ese `-inf` rompía la respuesta JSON.
- **Alarma falsa en cada salida**: en backtest la salida se ejecuta en la apertura
  siguiente; mientras tanto el bot re-coloca el stop (correcto: nunca desnuda) pero lo
  logueaba como WARNING "una salida no se llenó". Eran miles de líneas por corrida (logs
  de 1h de 60 000 líneas, resúmenes de 7 MB). Ahora solo avisa si la salida terminó sin
  llenarse. No cambia ningún resultado.

## Próximas ideas (sin correr; cada una suma 1 al contador)

- ~~**1d**~~ → #7–#9, refutadas (el warmup lo resolvía ya el arreglo de #3/#4).
- **Trend following 1d + filtro 200d en una canasta de ~10 cripto líquidas**: la única
  pista viva (#8). Más activos = más trades sin tocar parámetros. Necesita definir el
  universo ANTES (p. ej. top por volumen al inicio de cada año, para no elegir con el
  diario del lunes) y probar que el risk engine maneja bien la correlación entre ellas.
- ~~Filtro de régimen de mercado (media de 200 días)~~ → #3/#4, refutadas.
- ~~Reversión a la media intradiaria (RSI 2)~~ → #5/#6, refutadas.
