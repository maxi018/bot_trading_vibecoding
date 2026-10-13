// Medidor "valor vs. límite". El relleno lleva la severidad (verde -> ámbar -> rojo)
// y la pista es un tono tenue del mismo color, para que el estado se lea en toda la barra.

export type Severity = "good" | "warning" | "critical";

export function severityFor(ratio: number): Severity {
  if (ratio >= 0.8) return "critical";
  if (ratio >= 0.5) return "warning";
  return "good";
}

const SEV_LABEL: Record<Severity, string> = { good: "OK", warning: "Atención", critical: "Cerca del límite" };
const SEV_ICON: Record<Severity, string> = { good: "✓", warning: "!", critical: "▲" };

interface Props {
  label: string;
  value: number | null;
  limit: number;
  format: (v: number) => string;
  hint?: string;
}

export function Meter({ label, value, limit, format, hint }: Props) {
  const v = value ?? 0;
  const ratio = limit > 0 ? Math.max(0, v) / limit : 0;
  const sev = severityFor(ratio);
  return (
    <div className="meter" data-sev={sev} role="meter" aria-label={label} aria-valuemin={0}
      aria-valuemax={limit} aria-valuenow={v} aria-valuetext={`${format(v)} de ${format(limit)}`}>
      <div className="meter-head">
        <span className="stat-label">{label}</span>
        <span className={`tag sev-${sev}`} style={{ color: `var(--${sev === "good" ? "good" : sev})` }}>
          <span aria-hidden>{SEV_ICON[sev]}</span> {SEV_LABEL[sev]}
        </span>
      </div>
      <div className="meter-head live-value">
        <span className="stat-value">{value === null ? "—" : format(v)}</span>
        <span className="secondary num">límite {format(limit)}</span>
      </div>
      <div className="meter-track live-value">
        <div className="meter-fill" style={{ width: `${Math.min(100, ratio * 100)}%` }} />
      </div>
      <div className="meter-scale">
        <span>0</span>
        <span>{hint ?? `${Math.round(ratio * 100)}% del límite`}</span>
        <span>{format(limit)}</span>
      </div>
    </div>
  );
}
