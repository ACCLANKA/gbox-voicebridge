"""Bridge: Gbox UDP audio <-> the Sinhala call-bot (STT -> brain -> TTS).

The call-bot speaks a simple device protocol: connect a WebSocket with a token, send 20 ms
PCM16 frames at 8 kHz as binary messages, and its answers arrive as binary messages too.
The board speaks 20 ms PCM16 at 16 kHz over its own UDP channel.

Two conversions, nothing else:
  uplink   16 kHz / 320 samples -> average pairs -> 8 kHz / 160 samples -> call-bot
  downlink 8 kHz / 160 samples  -> duplicate     -> 16 kHz / 320 samples -> board

The sink never blocks: uplink frames go into a bounded queue and are dropped when full,
because a stale frame is worth less than a current one.
"""
from __future__ import annotations

import array
import asyncio
import logging
import os

log = logging.getLogger("bridge")

CALLBOT_URL = os.environ.get("CALLBOT_WS_URL", "ws://docker-callbot-1:8890")
CALLBOT_TOKEN = os.environ.get("CALLBOT_TOKEN", "")
SERIAL = os.environ.get("BRIDGE_SERIAL", "GBX-BENCH01")


def to_8k(pcm16: bytes) -> bytes:
    a = array.array("h")
    a.frombytes(pcm16[: len(pcm16) // 2 * 2])
    out = array.array("h", [(a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2)])
    return out.tobytes()


def to_16k(pcm16: bytes) -> bytes:
    a = array.array("h")
    a.frombytes(pcm16[: len(pcm16) // 2 * 2])
    out = array.array("h", bytes(4 * len(a)))
    for i, sample in enumerate(a):
        out[2 * i] = sample
        out[2 * i + 1] = sample
    return out.tobytes()


class Bridge:
    def __init__(self, send_downlink) -> None:
        self._send_downlink = send_downlink
        self.q: asyncio.Queue | None = None
        self.task: asyncio.Task | None = None
        self.enabled = bool(CALLBOT_TOKEN)
        self.stats = {"ul": 0, "dl": 0, "drop": 0, "links": 0, "events": 0}

    # called from the UDP datagram handler (hot path: must never raise)
    def on_uplink(self, serial: str, pcm16: bytes) -> None:
        if not self.enabled or serial != SERIAL or self.q is None:
            return
        try:
            self.q.put_nowait(pcm16)
        except asyncio.QueueFull:
            self.stats["drop"] += 1

    async def start(self) -> None:
        if not self.enabled:
            log.warning("bridge disabled: CALLBOT_TOKEN is empty")
            return
        self.q = asyncio.Queue(maxsize=100)
        self.task = asyncio.create_task(self._run())
        log.warning("bridge started -> %s (serial %s)", CALLBOT_URL, SERIAL)

    async def _run(self) -> None:
        import websockets

        uri = "%s/?token=%s" % (CALLBOT_URL, CALLBOT_TOKEN)
        while True:
            try:
                async with websockets.connect(uri, max_size=None, open_timeout=8) as ws:
                    self.stats["links"] += 1
                    log.warning("bridge: link up to call-bot")
                    rx = asyncio.create_task(self._rx(ws))
                    try:
                        await self._tx(ws)
                    finally:
                        rx.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("bridge: link down (%s: %s)", type(exc).__name__, str(exc)[:120])
            await asyncio.sleep(2)

    async def _tx(self, ws) -> None:
        assert self.q is not None
        while True:
            pcm16 = await self.q.get()
            await ws.send(to_8k(pcm16))
            self.stats["ul"] += 1

    async def _rx(self, ws) -> None:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                if self._send_downlink(SERIAL, to_16k(bytes(msg))):
                    self.stats["dl"] += 1
            else:
                self.stats["events"] += 1
                log.info("bridge event: %s", str(msg)[:200])
