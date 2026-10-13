"""Kill-switch persistente.

Propiedades (spec §0.15 y §3.4):
- Una vez activado, sobrevive a reinicios: el estado vive en disco, no en memoria.
- El código del bot puede ACTIVARLO pero no tiene API para desactivarlo.
  La única forma de rearmarlo es ``human_reset()``, invocado desde la CLI por un
  operador con ``quantbot reset-kill-switch --operator <nombre> --confirm``.
- Escritura atómica (tmp + rename + fsync) para no dejar archivos a medias.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KillRecord:
    reason: str
    engaged_at_ms: int
    flatten: bool


class KillSwitch:
    FILENAME = "KILL_SWITCH.json"

    def __init__(self, state_dir: str | Path) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / self.FILENAME
        self._record: KillRecord | None = self._load()
        if self._record:
            log.critical("Kill-switch ACTIVO desde el arranque: %s", self._record.reason)

    @property
    def engaged(self) -> bool:
        return self._record is not None

    @property
    def record(self) -> KillRecord | None:
        return self._record

    def engage(self, reason: str, flatten: bool, now_ms: int | None = None) -> KillRecord:
        if self._record is not None:
            return self._record  # idempotente: la primera causa es la que importa
        rec = KillRecord(reason, now_ms if now_ms is not None else int(time.time() * 1000), flatten)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(rec), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)
        self._record = rec
        log.critical("KILL SWITCH ACTIVADO: %s (flatten=%s)", reason, flatten)
        return rec

    def _load(self) -> KillRecord | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return KillRecord(**data)
        except Exception:
            # Archivo corrupto: ante la duda, se considera ACTIVADO.
            return KillRecord("archivo de kill-switch ilegible", 0, False)


def human_reset(state_dir: str | Path, operator: str) -> bool:
    """Rearme manual. NO llamar desde el código del bot: solo desde la CLI."""
    if not operator.strip():
        raise ValueError("Se requiere el nombre del operador para auditar el reset")
    path = Path(state_dir) / KillSwitch.FILENAME
    if not path.exists():
        return False
    audit = Path(state_dir) / "kill_switch_audit.log"
    with open(audit, "a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "reset_at": int(time.time() * 1000),
                    "operator": operator,
                    "record": path.read_text(encoding="utf-8"),
                }
            )
            + "\n"
        )
    path.unlink()
    return True
