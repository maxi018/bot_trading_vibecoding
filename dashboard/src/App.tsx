import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { auth } from "./api/client";
import { LiveConnection, useLive, wsUrl, type LiveState } from "./api/live";
import { ModeBadge, StatusPill } from "./components/Badges";
import { Controls } from "./components/Controls";
import { ToastProvider } from "./components/Toasts";
import { HistoryPage } from "./pages/History";
import { Login } from "./pages/Login";
import { MetricsPage } from "./pages/Metrics";
import { ChartPage } from "./pages/Chart";
import { Overview } from "./pages/Overview";
import { PositionsPage } from "./pages/Positions";
import { RiskPage } from "./pages/Risk";

const PAGES = [
  { id: "overview", label: "Overview", phase: 1 },
  { id: "chart", label: "Gráfico", phase: 2 },
  { id: "positions", label: "Posiciones y órdenes", phase: 3 },
  { id: "history", label: "Historial", phase: 4 },
  { id: "metrics", label: "Métricas", phase: 5 },
  { id: "risk", label: "Risk engine", phase: 6 },
  { id: "backtests", label: "Backtests", phase: 7 },
] as const;
type PageId = (typeof PAGES)[number]["id"];

function currentPage(): PageId {
  const id = location.hash.replace("#/", "");
  return (PAGES.find((p) => p.id === id)?.id ?? "overview") as PageId;
}

function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem("qb.theme") ?? "dark"; } catch { return "dark"; }
  });
  // Layout effect: el atributo se aplica ANTES de los efectos de los hijos
  // (el gráfico lee los colores del tema activo al crearse).
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("qb.theme", theme); } catch { /* sin storage */ }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))];
}

export default function App() {
  const [token, setToken] = useState(auth.token);
  useEffect(() => auth.onLogout(() => setToken("")), []);
  return (
    <ToastProvider>
      {token ? <Terminal token={token} /> : <Login onLogin={() => setToken(auth.token)} />}
    </ToastProvider>
  );
}

function Terminal({ token }: { token: string }) {
  const conn = useMemo(() => new LiveConnection(wsUrl(), token), [token]);
  useEffect(() => { conn.start(); return () => conn.stop(); }, [conn]);
  const live = useLive(conn);
  const [page, setPage] = useState<PageId>(currentPage);
  const [theme, toggleTheme] = useTheme();
  useEffect(() => {
    const onHash = () => setPage(currentPage());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => { if (live.conn === "unauthorized") auth.logout(); }, [live.conn]);

  const o = live.snapshot?.overview ?? null;
  const status = o?.status ?? null;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">quant<span>bot</span></div>
        {o && <ModeBadge mode={o.mode} testnet={o.testnet} />}
        {status && <span className="hide-mobile"><StatusPill status={status} /></span>}
        <ConnIndicator live={live} />
        <div className="spacer" />
        {status && <span className="show-mobile-only"><StatusPill status={status} /></span>}
        <button className="btn hide-mobile" onClick={toggleTheme} aria-label="Cambiar tema" title="Cambiar tema">
          {theme === "dark" ? "☾" : "☀"}
        </button>
        <button className="btn hide-mobile" onClick={() => auth.logout()}>Salir</button>
        <div className="topbar-controls">
          <Controls status={status} mode={o?.mode ?? null} killReason={o?.kill?.reason} />
        </div>
      </header>

      {live.stale && (
        <div className="stale-banner" role="alert">
          <span aria-hidden>⚠</span> DATOS DESACTUALIZADOS <small>— {live.staleReason}</small>
        </div>
      )}
      {o?.kill && !live.stale && (
        <div className="kill-banner" role="alert">■ KILL SWITCH ACTIVO — el bot no opera. Motivo: {o.kill.reason}</div>
      )}

      <nav className="nav" aria-label="Pantallas">
        {PAGES.map((p) => (
          <button key={p.id} aria-current={page === p.id ? "page" : undefined}
            onClick={() => { location.hash = `#/${p.id}`; }}>
            {p.label}
          </button>
        ))}
      </nav>

      <main className={live.stale ? "is-stale" : ""}>
        {page === "overview" ? (
          <Overview live={live} />
        ) : page === "risk" ? (
          <RiskPage live={live} conn={conn} />
        ) : page === "metrics" ? (
          <MetricsPage conn={conn} theme={theme} />
        ) : page === "history" ? (
          <HistoryPage conn={conn} symbols={o?.symbols ?? []} />
        ) : page === "positions" ? (
          <PositionsPage live={live} />
        ) : page === "chart" ? (
          <ChartPage live={live} conn={conn} theme={theme} />
        ) : (
          <div className="placeholder card">
            Pantalla <strong>{PAGES.find((p) => p.id === page)?.label}</strong>: llega en la fase{" "}
            {PAGES.find((p) => p.id === page)?.phase}.
          </div>
        )}
      </main>

      <div className="mobile-controls">
        <Controls status={status} mode={o?.mode ?? null} killReason={o?.kill?.reason} />
      </div>
    </div>
  );
}

function ConnIndicator({ live }: { live: LiveState }) {
  const ok = live.conn === "open" && !live.stale;
  return (
    <span className={`conn ${ok ? "ok" : "bad"}`} aria-live="polite">
      <span className={`dot${ok ? " pulse" : ""}`} aria-hidden />
      <span className="hide-mobile">{ok ? "en vivo" : live.conn === "open" ? "datos viejos" : "reconectando"}</span>
    </span>
  );
}
