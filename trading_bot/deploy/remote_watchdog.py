"""Watchdog REMOTO: correr en OTRA máquina (spec §1.4 "Redundancia").

Si el bot principal deja de dar señales de vida (heartbeat en healthchecks.io),
este proceso usa su PROPIA API key (solo trading) para:
  - cancelar órdenes que no sean stops (por defecto: los stops protegen las posiciones),
  - opcionalmente (--flatten) cerrar todas las posiciones,
  - alertar por Telegram.

    QB_API_KEY=... QB_API_SECRET=... HC_API_KEY=... HC_CHECK_UUID=... \
    QB_TELEGRAM_TOKEN=... QB_TELEGRAM_CHAT_ID=... \
    python remote_watchdog.py --exchange binance --symbols BTC/USDT,ETH/USDT

Requiere: pip install ccxt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.parse
import urllib.request


def hc_status(api_key: str, uuid: str) -> str:
    req = urllib.request.Request(
        f"https://healthchecks.io/api/v3/checks/{uuid}", headers={"X-Api-Key": api_key}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return str(json.loads(r.read())["status"])


def telegram(text: str) -> None:
    tok, chat = os.environ.get("QB_TELEGRAM_TOKEN"), os.environ.get("QB_TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print(text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, 10)


async def act(exchange: str, symbols: list[str], flatten: bool) -> None:
    import ccxt.async_support as ccxt_async

    ex = getattr(ccxt_async, exchange)(
        {"apiKey": os.environ["QB_API_KEY"], "secret": os.environ["QB_API_SECRET"]}
    )
    try:
        for s in symbols:
            for o in await ex.fetch_open_orders(s):
                is_stop = bool(o.get("triggerPrice") or o.get("stopPrice"))
                if flatten or not is_stop:
                    await ex.cancel_order(o["id"], s)
        if flatten:
            bal = await ex.fetch_balance()
            for s in symbols:
                qty = float(bal["total"].get(s.split("/")[0]) or 0)
                if qty > 0:
                    await ex.create_order(s, "market", "sell", qty)
    finally:
        await ex.close()


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--exchange", default="binance")
    p.add_argument("--symbols", required=True)
    p.add_argument("--flatten", action="store_true")
    p.add_argument("--interval", type=int, default=60)
    a = p.parse_args()
    acted = False
    while True:
        try:
            status = hc_status(os.environ["HC_API_KEY"], os.environ["HC_CHECK_UUID"])
        except Exception as exc:  # el watchdog no puede morir por un error de red
            print("watchdog: no se pudo consultar heartbeat:", exc)
            status = "unknown"
        if status == "down" and not acted:
            telegram("🚨 WATCHDOG REMOTO: bot sin heartbeat. Cancelando órdenes.")
            try:
                await act(a.exchange, a.symbols.split(","), a.flatten)
                telegram("Watchdog: acción completada. Revisar manualmente.")
            except Exception as exc:
                telegram(f"Watchdog: FALLÓ la acción: {exc!r}")
            acted = True
        elif status == "up":
            acted = False
        await asyncio.sleep(a.interval)


if __name__ == "__main__":
    asyncio.run(main())
