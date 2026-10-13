"""Reconexión del feed: backoff, resync tras cada conexión, zombies, huecos de secuencia,
y que un bug del consumidor NO se confunda con un problema de red."""

from __future__ import annotations

import asyncio
import json
import random
from contextlib import asynccontextmanager

import pytest

from quantbot.core.models import OrderBook, SequenceGapError, Side
from quantbot.data.websocket_feed import (
    BinanceSpotAdapter,
    Heartbeat,
    ResilientWebSocketFeed,
    SinkError,
)


class FakeWs:
    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)  # silencio total -> zombie
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeAdapter:
    url = "wss://fake"

    def __init__(self) -> None:
        self.resyncs = 0

    def subscribe_messages(self) -> list[str]:
        return ["SUB"]

    async def resync(self):
        self.resyncs += 1
        return []

    def parse(self, raw):
        if raw == "GAP":
            raise SequenceGapError("hueco")
        if raw == "BAD":
            raise ValueError("json roto")
        return [Heartbeat("BTC/USDT", 1)]


def make_feed(scripts, adapter, stop, sink=None, heartbeat=0.05):
    conns = iter(scripts)
    events = {"stale": [], "fresh": [], "sleeps": [], "received": []}

    @asynccontextmanager
    async def connect(url):
        script = next(conns, None)
        if script is None:  # no más conexiones previstas -> terminar test
            stop.set()
            raise ConnectionRefusedError("fin del guion")
        if isinstance(script, BaseException):
            raise script
        yield FakeWs(script)

    async def fake_sleep(d):
        events["sleeps"].append(d)

    async def default_sink(ev):
        events["received"].append(ev)

    feed = ResilientWebSocketFeed(
        "test",
        adapter,
        sink or default_sink,
        on_stale=lambda n, r: events["stale"].append(r),
        on_fresh=lambda n: events["fresh"].append(n),
        heartbeat_timeout_s=heartbeat,
        max_backoff_s=8,
        connect=connect,
        sleep=fake_sleep,
        rng=random.Random(0),
    )
    return feed, events


async def test_reconnects_with_exponential_backoff_and_resyncs_each_time():
    stop = asyncio.Event()
    adapter = FakeAdapter()
    scripts = [
        ConnectionResetError("handshake falló"),
        ["m1", ConnectionResetError("se cortó")],
        ["m2", "GAP"],
        ["m3"],  # luego silencio -> zombie timeout
    ]
    feed, ev = make_feed(scripts, adapter, stop)
    await asyncio.wait_for(feed.run(stop), timeout=5)

    assert adapter.resyncs == 3  # una por cada conexión exitosa
    assert len(ev["fresh"]) == 3
    assert feed.reconnects >= 4
    assert any("zombie" in r for r in ev["stale"])
    assert any("hueco" in r for r in ev["stale"])
    # cada espera = backoff + jitter, dentro de [1s, max_backoff]
    assert all(1 <= d <= 8 for d in ev["sleeps"])
    assert len(ev["received"]) == 3


async def test_backoff_resets_after_successful_connection():
    stop = asyncio.Event()
    scripts = [OSError("x"), OSError("x"), OSError("x"), ["ok"], OSError("x")]
    feed, ev = make_feed(scripts, FakeAdapter(), stop)
    await asyncio.wait_for(feed.run(stop), timeout=5)
    s = ev["sleeps"]
    assert s[2] > s[0]  # creció
    assert s[3] < s[2]  # tras conectar OK vuelve a empezar desde 1s


async def test_malformed_message_is_skipped_not_reconnect():
    stop = asyncio.Event()
    feed, ev = make_feed([["BAD", "m1"]], FakeAdapter(), stop)
    await asyncio.wait_for(feed.run(stop), timeout=5)
    assert len(ev["received"]) == 1


async def test_sink_bug_propagates_and_marks_stale():
    stop = asyncio.Event()

    async def broken_sink(_ev):
        raise ZeroDivisionError("bug en el núcleo")

    feed, ev = make_feed([["m1"]], FakeAdapter(), stop, sink=broken_sink)
    with pytest.raises(SinkError):
        await asyncio.wait_for(feed.run(stop), timeout=5)
    assert ev["stale"]  # se marcó stale al morir


async def test_feed_marks_risk_stale_and_engine_vetoes(risk_factory, clock):
    risk = risk_factory()
    stop = asyncio.Event()
    adapter = FakeAdapter()

    @asynccontextmanager
    async def connect(url):
        stop.set()
        raise ConnectionRefusedError("exchange caído")
        yield  # pragma: no cover

    async def no_sleep(_d):
        return None

    async def sink(_):
        return None

    feed = ResilientWebSocketFeed(
        "ws",
        adapter,
        sink,
        risk.mark_data_stale,
        risk.mark_data_fresh,
        connect=connect,
        sleep=no_sleep,
    )
    await feed.run(stop)
    assert not risk.trading_enabled and "data_stale:ws" in risk.halt_reasons()


# ------------------------------------------------------------- libro L2
def test_orderbook_sequence_gap_detection():
    b = OrderBook("BTC/USDT")
    with pytest.raises(SequenceGapError):
        b.apply_delta([], [], 1, 1, 0)  # sin snapshot
    b.apply_snapshot([(99, 1)], [(101, 1)], seq=10, ts=0)
    b.apply_delta([(99, 2)], [], first_seq=5, last_seq=10, ts=1)  # viejo: ignorado
    assert b.bids[99] == 1
    b.apply_delta([(99, 2)], [], first_seq=9, last_seq=11, ts=1)  # solapa: OK
    assert b.bids[99] == 2
    with pytest.raises(SequenceGapError):
        b.apply_delta([], [], first_seq=13, last_seq=14, ts=2)  # se perdió el 12


def test_orderbook_features():
    b = OrderBook("X")
    b.apply_snapshot([(99, 3), (98, 5)], [(101, 1), (102, 10)], 1, 0)
    assert b.mid == 100
    assert b.microprice() == pytest.approx((99 * 1 + 101 * 3) / 4)
    assert b.imbalance(1) == pytest.approx(0.5)
    avg, filled = b.walk_book(Side.BUY, 3)
    assert filled == 3 and avg == pytest.approx((101 + 2 * 102) / 3)


def test_binance_adapter_parses_and_detects_gap():
    async def snap(_raw):
        return {"lastUpdateId": 100, "bids": [["99", "1"]], "asks": [["101", "1"]]}

    ad = BinanceSpotAdapter(("BTC/USDT",), "1h", snapshot_fetcher=snap)
    asyncio.run(ad.resync())
    kline = {
        "data": {
            "e": "kline",
            "E": 5,
            "s": "BTCUSDT",
            "k": {
                "t": 0,
                "T": 3_599_999,
                "o": "1",
                "h": "2",
                "l": "0.5",
                "c": "1.5",
                "v": "10",
                "x": True,
            },
        }
    }
    evs = ad.parse(json.dumps(kline))
    assert any(type(e).__name__ == "Bar" and e.ts == 3_600_000 for e in evs)
    ok = {
        "data": {
            "e": "depthUpdate",
            "E": 6,
            "s": "BTCUSDT",
            "U": 101,
            "u": 102,
            "b": [["99", "2"]],
            "a": [],
        }
    }
    ad.parse(json.dumps(ok))
    gap = {
        "data": {"e": "depthUpdate", "E": 7, "s": "BTCUSDT", "U": 110, "u": 111, "b": [], "a": []}
    }
    with pytest.raises(SequenceGapError):
        ad.parse(json.dumps(gap))
