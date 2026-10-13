import type { BotStatus, Mode } from "../api/types";

const MODE_LABEL: Record<Mode, string> = { paper: "PAPER", demo: "DEMO", live: "LIVE" };

export function ModeBadge({ mode, testnet }: { mode: Mode; testnet?: boolean | null }) {
  const icon = mode === "live" ? "●" : mode === "demo" ? "◆" : "◇";
  return (
    <span className={`badge mode-${mode}`} title={mode === "live" ? "Opera con dinero real" : "Sin dinero real"}>
      <span aria-hidden>{icon}</span>
      {MODE_LABEL[mode]}
      {mode === "live" && testnet ? " · TESTNET" : ""}
    </span>
  );
}

const STATUS_ICON: Record<BotStatus, string> = {
  RUNNING: "▶",
  PAUSED: "❚❚",
  HALTED: "⚠",
  KILLED: "■",
};

const STATUS_HELP: Record<BotStatus, string> = {
  RUNNING: "Operando normalmente",
  PAUSED: "Pausado por un operador: no abre posiciones, los stops siguen activos",
  HALTED: "Circuit breaker activo: no abre posiciones nuevas",
  KILLED: "Kill-switch activo: no opera hasta que un humano lo reactive",
};

export function StatusPill({ status }: { status: BotStatus }) {
  return (
    <span className={`status status-${status}`} title={STATUS_HELP[status]}>
      <span aria-hidden>{STATUS_ICON[status]}</span>
      {status}
    </span>
  );
}

export function statusHelp(status: BotStatus): string {
  return STATUS_HELP[status];
}
