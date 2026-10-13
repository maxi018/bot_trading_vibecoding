// Controles del operador. Ninguno manda órdenes al exchange: todos son comandos al
// bot, que los ejecuta a través del risk engine (igual que una señal de la estrategia).
import { useState } from "react";
import { ApiError, commands } from "../api/client";
import type { BotStatus, Mode } from "../api/types";
import { Dialog } from "./Dialog";
import { useToast } from "./Toasts";

const REARM_WORD = "REACTIVAR";

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.message : "Error inesperado";
}

export function Controls({ status, mode, killReason }: { status: BotStatus | null; mode: Mode | null; killReason?: string }) {
  const [dialog, setDialog] = useState<"kill" | "rearm" | "resume" | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const killed = status === "KILLED";

  async function run(fn: () => Promise<unknown>, okText: string) {
    setBusy(true);
    try {
      await fn();
      toast("ok", okText);
      setDialog(null);
    } catch (e) {
      toast("error", errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {killed ? (
        <button className="btn warn" onClick={() => setDialog("rearm")}>
          <span aria-hidden>↺</span> Reactivar
        </button>
      ) : status === "PAUSED" ? (
        <button className="btn" disabled={busy} onClick={() => setDialog("resume")}>
          <span aria-hidden>▶</span> Reanudar
        </button>
      ) : (
        <button className="btn" disabled={busy || status === null}
          onClick={() => run(() => commands.pause("pausa desde el dashboard"), "Bot pausado: no abre posiciones nuevas")}>
          <span aria-hidden>❚❚</span> Pausar
        </button>
      )}
      {/* El KILL nunca depende del WebSocket: va por REST y se habilita siempre salvo si ya está activo. */}
      <button className="kill-btn" disabled={killed} onClick={() => setDialog("kill")}
        title={killed ? "Kill-switch ya activo" : "Detener todo: cancelar órdenes y cerrar posiciones"}>
        <span aria-hidden>■</span> {killed ? "KILLED" : "KILL"}
      </button>

      {dialog === "kill" && <KillDialog mode={mode} busy={busy} onClose={() => setDialog(null)}
        onConfirm={(reason) => run(() => commands.kill(reason || "kill manual desde el dashboard"),
          "KILL SWITCH ACTIVADO: cancelando órdenes y cerrando posiciones")} />}
      {dialog === "rearm" && <RearmDialog busy={busy} killReason={killReason} onClose={() => setDialog(null)}
        onConfirm={(text) => run(() => commands.rearm(text), "Kill-switch rearmado. El bot queda PAUSADO: revisá y reanudá.")} />}
      {dialog === "resume" && (
        <Dialog title="¿Reanudar el trading?" onClose={() => setDialog(null)}>
          <p className="secondary">
            El bot vuelve a abrir posiciones según la estrategia
            {mode === "live" ? <strong className="down"> con dinero real</strong> : null}.
            Los circuit breakers activos siguen mandando.
          </p>
          <div className="dialog-actions">
            <button className="btn" data-autofocus onClick={() => setDialog(null)}>Cancelar</button>
            <button className="btn primary" disabled={busy} onClick={() => run(() => commands.resume(), "Bot reanudado")}>
              Reanudar
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}

function KillDialog({ mode, busy, onClose, onConfirm }: {
  mode: Mode | null; busy: boolean; onClose: () => void; onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  return (
    <Dialog title="⚠ Activar KILL SWITCH" onClose={onClose} danger>
      {mode === "live" && <p className="down"><strong>Modo LIVE: esto afecta dinero real.</strong></p>}
      <p>Esto va a:</p>
      <ul>
        <li>Bloquear toda operación nueva (el risk engine veta todo).</li>
        <li>Cancelar todas las órdenes pendientes.</li>
        <li>Cerrar todas las posiciones abiertas con órdenes límite agresivas.</li>
        <li>Quedar activo aunque se reinicie el bot. Solo un humano lo reactiva escribiendo {REARM_WORD}.</li>
      </ul>
      <div className="field">
        <label htmlFor="kill-reason">Motivo (queda en la auditoría)</label>
        <input id="kill-reason" className="input" maxLength={300} value={reason}
          onChange={(e) => setReason(e.target.value)} placeholder="ej: comportamiento anómalo" />
      </div>
      <div className="dialog-actions">
        <button className="btn" data-autofocus onClick={onClose}>Cancelar</button>
        <button className="btn danger" disabled={busy} onClick={() => onConfirm(reason.trim())}>
          {busy ? "Enviando…" : "CONFIRMAR KILL"}
        </button>
      </div>
    </Dialog>
  );
}

function RearmDialog({ busy, killReason, onClose, onConfirm }: {
  busy: boolean; killReason?: string; onClose: () => void; onConfirm: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const ok = text === REARM_WORD;
  return (
    <Dialog title="Reactivar después de un KILL" onClose={onClose} danger>
      {killReason && <p className="secondary">Motivo del kill: <span className="mono">{killReason}</span></p>}
      <p>Antes de reactivar, revisá:</p>
      <ul>
        <li>Que entendés por qué saltó el kill-switch.</li>
        <li>Posiciones y órdenes en el exchange.</li>
        <li>Que la causa (datos, exchange, estrategia) está resuelta.</li>
      </ul>
      <p className="secondary">El bot vuelve <strong>PAUSADO</strong>: para operar hay que apretar Reanudar aparte.</p>
      <div className="field">
        <label htmlFor="rearm-text">Escribí <span className="mono">{REARM_WORD}</span> para confirmar</label>
        <input id="rearm-text" className="input mono" data-autofocus autoComplete="off" spellCheck={false}
          value={text} onChange={(e) => setText(e.target.value)} />
      </div>
      <div className="dialog-actions">
        <button className="btn" onClick={onClose}>Cancelar</button>
        <button className="btn warn" disabled={!ok || busy} onClick={() => onConfirm(text)}>Reactivar</button>
      </div>
    </Dialog>
  );
}
