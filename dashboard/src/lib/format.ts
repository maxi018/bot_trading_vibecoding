// Formato de números para la terminal. null/undefined -> "—" (nunca "NaN" ni "0" inventado).

const DASH = "—";

export function money(v: number | null | undefined, opts: { signed?: boolean; compact?: boolean } = {}): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  const abs = Math.abs(v);
  const compact = opts.compact && abs >= 100_000;
  const s = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: compact ? "compact" : "standard",
    minimumFractionDigits: compact ? 1 : 2,
    maximumFractionDigits: compact ? 1 : 2,
  }).format(abs);
  const sign = v < 0 ? "−" : opts.signed && v > 0 ? "+" : "";
  return sign + s;
}

export function pct(v: number | null | undefined, opts: { signed?: boolean; digits?: number } = {}): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  const d = opts.digits ?? 2;
  const s = `${Math.abs(v * 100).toFixed(d)}%`;
  const sign = v < 0 ? "−" : opts.signed && v > 0 ? "+" : "";
  return sign + s;
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function ms(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return v < 1 ? "<1 ms" : v < 1000 ? `${v.toFixed(0)} ms` : `${(v / 1000).toFixed(1)} s`;
}

export function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return DASH;
  if (seconds < 1) return "< 1 s";
  if (seconds < 60) return `${seconds.toFixed(0)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function duration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

export function clock(tsMs: number): string {
  return new Date(tsMs).toLocaleTimeString("es-AR", { hour12: false });
}

export function dirClass(v: number | null | undefined): string {
  if (!v) return "";
  return v > 0 ? "up" : "down";
}
