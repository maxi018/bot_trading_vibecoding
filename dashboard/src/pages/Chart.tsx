import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineStyle,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { LiveConnection, LiveState } from "../api/live";
import type { Candle } from "../api/types";
import { prepareMarkers, snapToCandle, toMarker, type FillLike, type MarkerColors } from "../chart/markers";
import { num, pct } from "../lib/format";

interface CandlesResponse {
  candles: Candle[];
  markers: FillLike[];
  stops: { stop: number | null; trailing: number | null };
  timeframe_ms: number;
}

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Colores por ROL, leídos del tema activo (cambian en un solo lugar: styles.css). */
function palette(theme: string) {
  const dark = theme !== "light";
  return {
    text: cssVar("--text-secondary"),
    grid: cssVar("--border"),
    surface: cssVar("--surface-1"),
    up: cssVar("--up"),
    down: cssVar("--down"),
    markers: {
      entry: dark ? "#3987e5" : "#2a78d6", // categórico 1 (azul)
      exit: dark ? "#d95926" : "#eb6834", // categórico 2 (naranja)
      stop: cssVar("--critical"),
    } satisfies MarkerColors,
    stopLine: cssVar("--critical"),
    trailingLine: cssVar("--warning"),
    entryLine: cssVar("--text-secondary"),
  };
}

interface Legend { time: number; o: number; h: number; l: number; c: number; fills: FillLike[] }

export function ChartPage({ live, conn, theme }: { live: LiveState; conn: LiveConnection; theme: string }) {
  const symbols = live.snapshot?.overview.symbols ?? [];
  const [symbol, setSymbol] = useState<string | null>(null);
  const sym = symbol ?? symbols[0] ?? null;
  const [error, setError] = useState("");
  const [legend, setLegend] = useState<Legend | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const linesRef = useRef<Record<string, IPriceLine | null>>({});

  // Al reconectar el WebSocket se recarga el histórico: no dejar huecos de velas.
  const prevConn = useRef(live.conn);
  useEffect(() => {
    if (live.conn === "open" && prevConn.current !== "open") setReloadKey((k) => k + 1);
    prevConn.current = live.conn;
  }, [live.conn]);

  // ------------------------------------------------- crear gráfico + datos
  useEffect(() => {
    if (!sym || !box.current) return;
    const p = palette(theme);
    const chart = createChart(box.current, {
      autoSize: true,
      layout: { background: { color: p.surface }, textColor: p.text, fontFamily: "ui-monospace, Menlo, monospace" },
      grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
      rightPriceScale: { borderColor: p.grid },
      timeScale: { borderColor: p.grid, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      priceLineVisible: false, // la línea del último precio se confundía con la de entrada
      upColor: p.up, downColor: p.down, borderUpColor: p.up, borderDownColor: p.down,
      wickUpColor: p.up, wickDownColor: p.down,
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false,
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chartRef.current = chart;
    candleRef.current = candles;
    linesRef.current = {};
    let markersApi: ISeriesMarkersPluginApi<Time> | null = null;
    let markers: SeriesMarker<Time>[] = [];
    let fills: FillLike[] = [];
    let tfMs = 3_600_000;
    let last: Candle | null = null;
    let disposed = false;

    const volColor = (c: Candle) => (c.close >= c.open ? p.up : p.down) + "55";
    const toCandle = (c: Candle): CandlestickData<Time> => ({ ...c, time: c.time as UTCTimestamp });

    api.get<CandlesResponse>(`/api/candles?symbol=${encodeURIComponent(sym)}&limit=1000`)
      .then((r) => {
        if (disposed) return;
        setError("");
        tfMs = r.timeframe_ms;
        candles.setData(r.candles.map(toCandle));
        volume.setData(r.candles.map((c) => ({ time: c.time as UTCTimestamp, value: c.volume, color: volColor(c) })));
        last = r.candles[r.candles.length - 1] ?? null;
        fills = [...r.markers];
        markers = prepareMarkers(r.markers.map((f) => toMarker(f, tfMs, last?.time ?? null, p.markers)));
        markersApi = createSeriesMarkers(candles, markers);
        if (last) setLegend({ time: last.time, o: last.open, h: last.high, l: last.low, c: last.close, fills: [] });
        chart.timeScale().scrollToRealTime();
      })
      .catch((e) => !disposed && setError(e instanceof ApiError ? e.message : "No se pudo cargar el gráfico"));

    // Eventos en vivo del bot
    const off = conn.onEvent((ev) => {
      if (ev.type === "bar" && ev.symbol === sym) {
        if (last && ev.bar.time < last.time) return; // fuera de orden: se ignora
        candles.update(toCandle(ev.bar));
        volume.update({ time: ev.bar.time as UTCTimestamp, value: ev.bar.volume, color: volColor(ev.bar) });
        last = ev.bar;
      } else if (ev.type === "fill" && ev.symbol === sym) {
        fills.push(ev);
        markers = prepareMarkers([...markers, toMarker(ev, tfMs, last?.time ?? null, p.markers)]);
        if (markersApi) markersApi.setMarkers(markers);
        else markersApi = createSeriesMarkers(candles, markers);
      }
    });

    const onMove = (param: MouseEventParams<Time>) => {
      const d = param.seriesData.get(candles) as CandlestickData<Time> | undefined;
      const src = d ?? (last ? toCandle(last) : null);
      if (!src) return;
      const t = src.time as number;
      const here = fills.filter((f) => snapToCandle(f.ts, tfMs, last?.time ?? null) === t);
      setLegend({ time: t, o: src.open, h: src.high, l: src.low, c: src.close, fills: here });
    };
    chart.subscribeCrosshairMove(onMove);

    return () => {
      disposed = true;
      off();
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
    };
  }, [sym, theme, conn, reloadKey]);

  // ---------------------------------------- líneas de stop / trailing / entrada
  const stops = sym ? live.snapshot?.stops[sym] : undefined;
  const position = live.snapshot?.positions.find((p) => p.symbol === sym) ?? null;
  const stopPx = stops?.stop ?? null;
  const trailPx = stops?.trailing ?? null;
  const entryPx = position?.avg_entry ?? null;
  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    const p = palette(theme);
    const same = stopPx !== null && trailPx !== null && Math.abs(stopPx - trailPx) < 1e-9;
    const want: Record<string, { price: number | null; color: string; style: LineStyle; title: string }> = {
      stop: { price: stopPx, color: p.stopLine, style: LineStyle.Solid, title: same ? "STOP · TRAILING" : "STOP" },
      trailing: { price: same ? null : trailPx, color: p.trailingLine, style: LineStyle.Dashed, title: "TRAILING" },
      entry: { price: entryPx, color: p.entryLine, style: LineStyle.Dotted, title: "ENTRADA" },
    };
    for (const [key, w] of Object.entries(want)) {
      const existing = linesRef.current[key] ?? null;
      if (w.price === null) {
        if (existing) series.removePriceLine(existing);
        linesRef.current[key] = null;
      } else if (existing) {
        existing.applyOptions({ price: w.price, title: w.title });
      } else {
        linesRef.current[key] = series.createPriceLine({
          price: w.price, color: w.color, lineWidth: 2, lineStyle: w.style, axisLabelVisible: true, title: w.title,
        });
      }
    }
  }, [stopPx, trailPx, entryPx, theme, sym, reloadKey]);

  const change = legend ? legend.c / legend.o - 1 : null;
  return (
    <div className="grid" style={{ gap: 12 }}>
      <div className="chart-toolbar">
        <div className="seg" role="tablist" aria-label="Activo">
          {symbols.map((s) => (
            <button key={s} role="tab" aria-selected={s === sym} onClick={() => setSymbol(s)}>{s}</button>
          ))}
        </div>
        <span className="muted mono">{live.snapshot?.overview.timeframe}</span>
        {legend && (
          <span className="mono num secondary chart-ohlc live-value">
            A {num(legend.o)} · M {num(legend.h)} · m {num(legend.l)} · C {num(legend.c)}{" "}
            <span className={change !== null && change >= 0 ? "up" : "down"}>{pct(change, { signed: true })}</span>
          </span>
        )}
        {legend?.fills.map((f, i) => (
          <span key={i} className="mono chart-fill-chip">
            {f.tag.toUpperCase()} {f.side} {num(f.qty, 4)} @ {num(f.price)}
          </span>
        ))}
      </div>
      <section className="card chart-card" aria-label={`Gráfico de velas ${sym ?? ""}`}>
        <div ref={box} className="chart-box live-value" />
        {live.stale && <div className="chart-frozen">DATOS CONGELADOS</div>}
        {error && <div className="chart-frozen error">{error}</div>}
      </section>
      <div className="chart-legend secondary">
        <span><i className="sw arrow" style={{ color: palette(theme).markers.entry }}>▲</i> entrada</span>
        <span><i className="sw arrow" style={{ color: palette(theme).markers.exit }}>▼</i> salida</span>
        <span><i className="sw arrow" style={{ color: palette(theme).markers.stop }}>▼</i> stop / kill</span>
        <span><i className="sw line" style={{ borderColor: palette(theme).stopLine }} /> stop nativo en el exchange</span>
        <span><i className="sw line dashed" style={{ borderColor: palette(theme).trailingLine }} /> trailing deseado</span>
        <span><i className="sw line dotted" style={{ borderColor: palette(theme).entryLine }} /> precio de entrada</span>
        {position && (
          <span className="mono">
            Posición {position.side} {num(position.qty, 4)} · stop a {pct(position.distance_to_stop_pct)}
            {!position.protected && <strong className="down"> · SIN STOP</strong>}
          </span>
        )}
      </div>
    </div>
  );
}
