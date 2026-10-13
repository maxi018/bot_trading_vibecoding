"""Alertas a Telegram / Discord.

Diseño:
- ``alert()`` es no bloqueante: encola y vuelve. El envío lo hace un worker.
- Una alerta que falla NUNCA tira abajo el bot (pero se loguea).
- Dedupe por clave para no spamear (y no comerse el rate limit de Telegram).
- Solo stdlib (urllib en un thread) para no sumar dependencias al camino crítico.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

log = logging.getLogger(__name__)


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


_EMOJI = {Severity.INFO: "ℹ️", Severity.WARNING: "⚠️", Severity.CRITICAL: "🚨"}


@dataclass(frozen=True, slots=True)
class Alert:
    severity: Severity
    title: str
    body: str = ""

    def render(self) -> str:
        return f"{_EMOJI[self.severity]} [{self.severity}] {self.title}\n{self.body}".strip()


class AlertChannel(Protocol):
    def send(self, text: str) -> None:  # bloqueante; se corre en un thread
        ...


def _post(url: str, data: bytes, content_type: str, timeout: float = 10.0) -> None:
    req = urllib.request.Request(url, data=data, headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")


class TelegramChannel:
    def __init__(self, token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    def send(self, text: str) -> None:
        body = urllib.parse.urlencode({"chat_id": self._chat_id, "text": text[:4000]}).encode()
        _post(self._url, body, "application/x-www-form-urlencoded")


class DiscordChannel:
    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    def send(self, text: str) -> None:
        _post(self._url, json.dumps({"content": text[:1900]}).encode(), "application/json")


class Alerter:
    def __init__(
        self,
        channels: list[AlertChannel] | None = None,
        min_interval_s: float = 30.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._channels = channels or []
        self._min_interval = min_interval_s
        self._time = time_fn
        self._last_sent: dict[str, float] = {}
        self._queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=1000)
        self.sent: list[Alert] = []  # historial (útil en tests y en backtest)
        self.listeners: list[Callable[[Alert], None]] = []  # dashboard / journal

    def alert(self, severity: Severity, title: str, body: str = "", key: str | None = None) -> None:
        dedupe_key = key or title
        now = self._time()
        last = self._last_sent.get(dedupe_key)
        # CRITICAL siempre pasa: un kill-switch nunca se deduplica.
        if severity is not Severity.CRITICAL and last is not None:
            if now - last < self._min_interval:
                return
        self._last_sent[dedupe_key] = now
        a = Alert(severity, title, body)
        self.sent.append(a)
        if len(self.sent) > 5000:
            del self.sent[:1000]
        for listener in self.listeners:
            try:
                listener(a)
            except Exception:
                log.exception("alert listener falló")
        log_fn = {
            Severity.CRITICAL: log.critical,
            Severity.WARNING: log.warning,
            Severity.INFO: log.info,
        }[severity]
        log_fn("ALERTA: %s", title, extra={"alert_body": body, "severity": str(severity)})
        try:
            self._queue.put_nowait(a)
        except asyncio.QueueFull:
            log.error("Cola de alertas llena; se descarta: %s", title)

    async def run(self, stop: asyncio.Event) -> None:
        """Worker de envío. Corre como tarea del main loop."""
        while not stop.is_set() or not self._queue.empty():
            try:
                a = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            await self._dispatch(a)

    async def flush(self) -> None:
        while not self._queue.empty():
            await self._dispatch(self._queue.get_nowait())

    async def _dispatch(self, a: Alert) -> None:
        text = a.render()
        for ch in self._channels:
            try:
                await asyncio.to_thread(ch.send, text)
            except Exception:  # el canal de alertas no puede matar al bot
                log.exception("Fallo enviando alerta por %s", type(ch).__name__)
