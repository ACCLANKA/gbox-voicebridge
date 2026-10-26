"""Direct device audio <-> Qwen Omni Realtime.

Replaces the STT -> LLM -> TTS chain: the handset's 16 kHz PCM goes straight into the model over a
WebSocket, and the model's 24 kHz speech comes back for the downlink. No separate recogniser, no
separate synthesiser, no text round trip.

Measured on the live endpoint (token-plan, qwen-audio-3.0-realtime-plus):
  first audio 2.09 s after the caller stops, Sinhala text and speech correct.
"""
from __future__ import annotations

import array
import asyncio
import base64
import json
import logging
import threading

log = logging.getLogger("vb.realtime")

REALTIME_URL = ("wss://token-plan.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime"
                "?model=qwen-audio-3.0-realtime-plus")
VOICE = "longanqian"        # the only family this deployment offers; speaks Sinhala
OUT_RATE = 24000
IN_RATE = 16000


def resample_24k_to_16k(pcm: bytes) -> bytes:
    """3:2 linear decimation in pure Python (audioop is gone in 3.13, numpy is not installed)."""
    if not pcm:
        return b""
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) // 2 * 2])
    n = len(src)
    out = array.array("h", bytes(2 * ((n * 2) // 3)))
    m = len(out)
    for i in range(m):
        pos = i * 1.5
        j = int(pos)
        frac = pos - j
        s0 = src[j] if j < n else 0
        s1 = src[j + 1] if j + 1 < n else s0
        out[i] = int(s0 + (s1 - s0) * frac)
    return out.tobytes()


def resample_16k_to_24k(pcm: bytes) -> bytes:
    """2:3 linear interpolation: the handset speaks 16 kHz, the realtime API wants 24 kHz."""
    if not pcm:
        return b""
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) // 2 * 2])
    n = len(src)
    if n < 2:
        return pcm
    out = array.array("h", bytes(2 * ((n * 3) // 2)))
    m = len(out)
    for i in range(m):
        pos = i * 2.0 / 3.0
        j = int(pos)
        frac = pos - j
        s0 = src[j] if j < n else src[-1]
        s1 = src[j + 1] if j + 1 < n else s0
        out[i] = int(s0 + (s1 - s0) * frac)
    return out.tobytes()


class OmniBridge:
    """One live call = one WebSocket session.

    start() opens the socket and configures the session (system prompt, voice, VAD);
    feed() pushes 20 ms of 16 kHz PCM16 from the handset;
    on_audio(bytes @ 24 kHz) is called from the socket thread for every chunk of speech.
    """

    def __init__(self, api_key: str, instructions: str, on_audio, url: str = REALTIME_URL,
                 voice: str = VOICE, greet: str | None = None):
        self.api_key = api_key
        self.instructions = instructions
        self.on_audio = on_audio
        self.url = url
        self.voice = voice
        self.greet = greet
        self._out: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._busy = False          # a response is in flight
        self._closed = threading.Event()

    # ---- public ---------------------------------------------------------
    def start(self, timeout: float = 12.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="omni", daemon=True)
        self._thread.start()
        ok = self._ready.wait(timeout)
        if not ok:
            log.warning("realtime session did not become ready in %.0fs", timeout)
        return ok and not self._closed.is_set()

    def feed(self, pcm16: bytes) -> None:
        if not self._ready.is_set() or self._out is None or self._closed.is_set():
            return
        payload = base64.b64encode(pcm16).decode()
        try:
            self._loop.call_soon_threadsafe(self._out.put_nowait, payload)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        if self._loop and not self._closed.is_set():
            try:
                self._loop.call_soon_threadsafe(self._out.put_nowait, None)
            except Exception:  # noqa: BLE001
                pass

    @property
    def live(self) -> bool:
        return self._ready.is_set() and not self._closed.is_set()

    # ---- internals ------------------------------------------------------
    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001
            log.warning("realtime bridge died: %s: %s", type(exc).__name__, str(exc)[:140])

    async def _serve(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        self._out = asyncio.Queue(maxsize=600)
        headers = {"Authorization": "Bearer " + self.api_key}
        async with websockets.connect(self.url, additional_headers=headers, max_size=None,
                                      open_timeout=15) as ws:
            first = await asyncio.wait_for(ws.recv(), timeout=10)
            log.warning("realtime session: %s", str(first)[:60].replace("\n", " "))

            await ws.send(json.dumps({
                "type": "session.update",
                "session": {"modalities": ["text", "audio"],
                            "voice": self.voice,
                            "instructions": self.instructions,
                            "input_audio_format": "pcm16",
                            "output_audio_format": "pcm16",
                            "turn_detection": {"type": "server_vad"}}}))
            self._ready.set()

            if self.greet:
                pass  # deferred until audio is flowing (see _send_loop)
                await ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {"type": "message", "role": "user",
                             "content": [{"type": "input_text",
                                          "text": "(ඇමතුම ආරම්භ විය - කෙටියෙන් සිංහලෙන් ආයුබෝවන් කියන්න)"}]}}))
                await ws.send(json.dumps({"type": "response.create"}))

            sender = asyncio.create_task(self._send_loop(ws, greet=self.greet))
            try:
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        continue
                    ev = json.loads(msg)
                    t = ev.get("type")
                    if t == "response.audio.delta":
                        pcm = base64.b64decode(ev.get("delta") or "")
                        if pcm:
                            self.on_audio(pcm)
                    elif t == "response.created":
                        self._busy = True
                    elif t == "response.done":
                        self._busy = False
                    elif t == "response.audio_transcript.done":
                        log.warning("omni said: %s", (ev.get("transcript") or "")[:120])
                    elif t == "conversation.item.input_audio_transcription.completed":
                        log.warning("omni heard: %s", (ev.get("transcript") or "")[:120])
                    elif t == "error":
                        log.warning("omni error: %s",
                                    (ev.get("error") or {}).get("message", "")[:160])
            finally:
                sender.cancel()
                self._closed.set()
                self._ready.clear()

    async def _send_loop(self, ws, greet: str | None = None) -> None:
        first = True
        while True:
            item = await self._out.get()
            if item is None:
                await ws.close()
                return
            if first:
                first = False
                if greet:
                    # The SCO link is up once the handset sends audio; give it a moment more.
                    asyncio.create_task(self._greet_later(ws))
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": item}))

    async def _greet_later(self, ws) -> None:
        await asyncio.sleep(1.2)
        # wait for an idle session: a response.create during another response is rejected
        for _ in range(20):
            if not self._busy:
                break
            await asyncio.sleep(0.3)
        try:
            await ws.send(json.dumps({
                "type": "response.create",
                "response": {"instructions":
                             "ඇමතුම ආරම්භ විය. කෙටියෙන් සිංහලෙන් ආයුබෝවන් කියන්න, පසුව විකල්ප තුන කියන්න: "
                             "පී ෂොට් ගැන දැනගන්න එක, වේලාවක් වෙන්කරගන්න දෙක, වෙනත් ප්‍රතිකාර ගැන දැනගන්න තුන."}}))
            log.warning("omni greeting requested")
        except Exception as exc:  # noqa: BLE001
            log.warning("omni greeting failed: %s", str(exc)[:90])
