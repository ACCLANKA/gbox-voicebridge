"""Voice Bridge control plane API + web dashboard host.

Run:  uvicorn api.main:app --reload --port 8080   (from the repo root)
Docs: http://127.0.0.1:8080/docs
Web:  http://127.0.0.1:8080/

The REST API lives under /v1 (auth, tenants, devices, routes, bridges, calls,
billing, recordings, dashboard). The bundled vanilla-JS dashboard SPA is served
from /web and talks to the /v1/ui BFF (api/routers/ui.py).
"""
from __future__ import annotations

from pathlib import Path
import asyncio
import logging
import os
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db as store
from .services import detection, forwarding
from .services import egress as egress_service
from .routers import auth as auth_router
from .routers import toy as toy_router
from .routers import webhooks as webhooks_router
from .routers import billing as billing_router
from .routers import bridges as bridges_router
from .routers import calls as calls_router
from .routers import dashboard as dashboard_router
from .routers import devices as devices_router
from .routers import recordings as recordings_router
from .routers import routes as routes_router
from .routers import device_ws as device_ws_router
from .routers import dids as dids_router
from .routers import forwarding as forwarding_router
from .routers import ops as ops_router
from .routers import tenants as tenants_router
from .routers import ui as ui_router
from .auth_service import router as gbox_auth_router

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Voice Bridge API", version="1.0.0",
              description="Control plane for the Voice Bridge ESP32-to-cloud voice router.")
# The public site (gigies.lk) calls this API from a browser with credentials, and a browser refuses
# `Access-Control-Allow-Origin: *` on a credentialed request - which is exactly why the console's
# sign-in failed with "API problem". Named origins + credentials is the only combination that works.
app.add_middleware(CORSMiddleware,
                   allow_origins=["https://gigies.lk", "https://www.gigies.lk",
                                  "https://console.site.gigies.lk", "https://vb.site.gigies.lk"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# --- Dedicated audio channel -------------------------------------------------------
# Audio arrives as UDP datagrams, one per 20 ms frame:
#   [GBX1][serial 12][seq u16 LE][kind 1][payload]
# UDP rather than the control WebSocket: a late packet is worthless to a live call, and TCP
# would retransmit and head-of-line-block every packet behind it. This is also the shape a
# SIP/RTP leg consumes, so the same feed serves the PBX work.
import asyncio as _asyncio

from .services import audio_sink as _audio_sink


_UDP_TRANSPORT = None
_DEVICE_ADDR = {}


class _AudioUdpProtocol(_asyncio.DatagramProtocol):
    def connection_made(self, transport):  # noqa: ANN001 - stdlib callback
        global _UDP_TRANSPORT
        _UDP_TRANSPORT = transport

    def datagram_received(self, data, addr):  # noqa: ANN001 - stdlib callback
        if len(data) < 20 or data[:4] != b"GBX1":
            return
        serial = data[4:16].split(b"\x00")[0].decode("utf-8", "replace")
        _DEVICE_ADDR[serial] = addr          # where to send the agent's voice back
        seq = int.from_bytes(data[16:18], "little")
        kind = data[18]
        _audio_sink.feed(serial, seq, kind, data[19:])
        if kind in (0x01, 0x03):
            _bridge.on_uplink(serial, data[19:])
            _agent.on_uplink(serial, data[19:])
            udp_forward(serial, data[19:])


def send_downlink(serial: str, pcm16: bytes, kind: int = 1) -> bool:
    """Queue one downlink chunk for a device: [GBD1][kind][PCM16 16 kHz]."""
    addr = _DEVICE_ADDR.get(serial)
    if addr is None or _UDP_TRANSPORT is None:
        return False
    _UDP_TRANSPORT.sendto(b"GBD1" + bytes([kind]) + pcm16, addr)
    return True


from .bridge import Bridge  # noqa: E402  (needs send_downlink defined first)

_bridge = Bridge(send_downlink)

logger = logging.getLogger("voicebridge.main")


# ---------------------------------------------------------------------------- UDP -> egress
UDP_EGRESS = str(os.environ.get("UDP_EGRESS", "0")).lower() in ("1", "true", "yes")
EGRESS_GAP_S = 4.0
_EG_SESS: dict[str, dict] = {}


def _pcm_8k(pcm16: bytes) -> bytes:
    """16 kHz PCM16 -> 8 kHz PCM16 by averaging sample pairs (what the board does on downlink)."""
    import array
    a = array.array("h")
    a.frombytes(pcm16[: len(pcm16) // 2 * 2])
    out = array.array("h", [(a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2)])
    return out.tobytes()


async def _egress_reaper() -> None:
    """Close forwarding legs whose audio stopped.

    A leg left open leaks a socket; for file:// destinations it also leaves the recording
    unflushed, so the WAV looks truncated until the process exits.
    """
    while True:
        await asyncio.sleep(1.0)
        now = time.time()
        for serial, ent in list(_EG_SESS.items()):
            if now - ent["last"] > EGRESS_GAP_S:
                try:
                    forwarding.registry.close(ent["sid"])
                    logger.warning("EGRESS %s closed (session %s, %d frames)",
                                   serial, ent["sid"], ent.get("frames", 0))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("EGRESS %s close failed: %s", serial, exc)
                _EG_SESS.pop(serial, None)


def udp_forward(serial: str, pcm16: bytes) -> None:
    """Feed one captured frame to whatever destination this device is configured for.

    Never raises: a broken destination must not kill the audio sink or the agent.
    """
    if not UDP_EGRESS:
        return
    import time as _time

    now = _time.time()
    ent = _EG_SESS.get(serial)
    if ent is not None and now - ent["last"] > EGRESS_GAP_S:
        try:
            forwarding.registry.close(ent["sid"])
            logger.warning("EGRESS %s closed (session %s, %d frames)",
                           serial, ent["sid"], ent.get("frames", 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("EGRESS %s close failed: %s", serial, exc)
        _EG_SESS.pop(serial, None)
        ent = None

    if ent is None:
        try:
            device = store.one("SELECT * FROM devices WHERE serial=?", (serial,))
            if device is None:
                return
            session = detection.find_active_session(device["id"]) or \
                detection._create_session(device)
            forwarding.registry.open(session, device)
            src = forwarding.registry.source(session["id"])
            ent = {"sid": session["id"], "last": now, "frames": 0}
            _EG_SESS[serial] = ent
            logger.warning("EGRESS %s opened (session %s via %s)", serial, session["id"], src)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EGRESS %s open failed: %s: %s", serial, type(exc).__name__,
                           str(exc)[:120])
            return

    ent["last"] = now
    leg = forwarding.registry.get(ent["sid"])
    if leg is None:
        return
    try:
        leg.write(pcm16)
        ent["frames"] = ent.get("frames", 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("EGRESS %s write failed: %s", serial, str(exc)[:120])
        _EG_SESS.pop(serial, None)

from .agent import Agent  # noqa: E402

_agent = Agent(send_downlink)


@app.on_event("startup")
async def _start_audio_udp() -> None:
    loop = _asyncio.get_event_loop()
    # Port is configurable (and 0 = disabled) so a second instance - or a test
    # run - can start without fighting the live one for udp/8891.
    port = int(os.environ.get("VB_AUDIO_PORT", "8891"))
    if port == 0:
        _audio_sink.logger.warning("audio channel disabled (VB_AUDIO_PORT=0)")
        return
    await loop.create_datagram_endpoint(_AudioUdpProtocol, local_addr=("0.0.0.0", port))
    _audio_sink.logger.warning("audio channel listening on udp/%d", port)


@app.on_event("startup")
async def _start_bridge() -> None:
    await _bridge.start()
    _agent.start()
    asyncio.create_task(_egress_reaper())


@app.on_event("startup")
def _startup() -> None:
    # `store` is api.db (imported as `store`): its init() runs the schema,
    # the column upgrades and the baseline seed - so migrations do run on boot.
    store.init()
    ui_router._load_help()


# v1 control-plane routers + the dashboard BFF.
for _router in (auth_router.router, tenants_router.router, devices_router.router,
                routes_router.router, bridges_router.router, calls_router.router,
                billing_router.router, recordings_router.router,
                dashboard_router.router, ui_router.router, ui_router.public,
                device_ws_router.router, forwarding_router.router, dids_router.router,
                webhooks_router.router, toy_router.router, ops_router.router):
    app.include_router(_router)


def _tts_pcm16_16k(text: str) -> bytes:
    """Ask the local TTS container for speech and return 16 kHz PCM16 mono.

    Plain-Python resampling: the service runs Python 3.13, where audioop no longer exists.
    """
    import array
    import io as _io
    import json as _json
    import urllib.request
    import wave

    req = urllib.request.Request(
        "http://tts:8766/tts",
        data=_json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    with wave.open(_io.BytesIO(data), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        a = array.array("h")
        a.frombytes(raw)
    elif width == 1:
        a = array.array("h", [(b - 128) << 8 for b in raw])
    else:                       # 24/32-bit: keep the top two bytes
        a = array.array("h", [int.from_bytes(raw[i:i + width], "little", signed=True) >> (8 * (width - 2))
                              for i in range(0, len(raw) - width + 1, width)])
    if ch == 2:
        a = array.array("h", [(a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2)])
    if sr != 16000 and len(a):
        step = sr / 16000.0
        n_out = max(1, int(len(a) / step))
        out = array.array("h", bytes(2 * n_out))
        for i in range(n_out):
            pos = i * step
            j = int(pos)
            frac = pos - j
            s0 = a[j] if j < len(a) else 0
            s1 = a[j + 1] if j + 1 < len(a) else s0
            out[i] = int(s0 + (s1 - s0) * frac)
        a = out
    return a.tobytes()


@app.get("/v1/audio/say", include_in_schema=False)
def audio_say(serial: str = "GBX-BENCH01", text: str = "ආයුබෝවන්! මම ගොබොක්ස්. ඔබට කොහොමද?"):
    """Speak `text` on the device (TTS -> UDP downlink). Returns what was queued."""
    import threading
    import time as _time

    if serial not in _DEVICE_ADDR:
        return {"error": "device has not sent audio yet", "serial": serial}

    try:
        pcm = _tts_pcm16_16k(text)
    except Exception as exc:  # noqa: BLE001
        return {"error": "tts failed: %s" % exc, "serial": serial}

    def run() -> None:
        step = 640                      # 20 ms at 16 kHz mono PCM16
        for i in range(0, len(pcm) - step + 1, step):
            send_downlink(serial, pcm[i:i + step])
            _time.sleep(0.02)

    threading.Thread(target=run, daemon=True).start()
    return {"queued_s": round(len(pcm) / 32000.0, 2), "serial": serial, "chars": len(text)}


@app.get("/v1/devices/{serial}/transcript", include_in_schema=False)
def device_transcript(serial: str, limit: int = 50, session: int = 0):
    """What this device's voice was recognised as, newest last.

    The platform's first developer-facing output: capture -> STT -> text, readable by API.
    """
    from . import transcript as _t
    rows = _t.read(serial, limit=max(1, min(limit, 500)), session=session)
    return {"serial": serial, "count": len(rows), "lines": rows}


@app.get("/v1/audio/tone", include_in_schema=False)
def audio_tone(serial: str = "GBX-BENCH01", ms: int = 1000, hz: int = 440):
    """Play a paced tone on the device - the smallest test of the UDP downlink."""
    import math
    import struct
    import threading
    import time as _time

    def run() -> None:
        frames = max(1, ms // 20)
        for i in range(frames):
            pcm = b"".join(
                struct.pack("<h", int(7000 * math.sin(
                    2 * math.pi * hz * ((i * 320 + n) / 16000.0))))
                for n in range(320)
            )
            send_downlink(serial, pcm)
            _time.sleep(0.02)

    threading.Thread(target=run, daemon=True).start()
    return {"queued": ms, "serial": serial, "known_addr": serial in _DEVICE_ADDR}


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok", "time": store.now()}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/web/index.html")


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


# Real accounts: only the NEW paths (/signup, /forgot, /reset, /me) are used, because the
# app's own /v1/ui/login is the one that mints a JWT its protected routes accept.
app.include_router(gbox_auth_router)


# --- OpenAPI contract for developers -----------------------------------------
# The generated schema had no securitySchemes, no servers entry and no per-path
# security, so /docs showed 146 endpoints that all looked public. A developer
# could not tell which route needs a JWT, which needs a project API key and
# which is genuinely open. This closes that gap and pins the doc to production.
from fastapi.openapi.utils import get_openapi  # noqa: E402

_PUBLIC_PATHS = {
    "/v1/ui/signup", "/v1/ui/login", "/v1/ui/forgot", "/v1/ui/reset",
    "/v1/webhooks/events", "/health",
}


def _developer_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title="GBox API",
        version="1.0.0",
        description=(
            "Programmable voice bridge. A GBox device carries a real Bluetooth HFP "
            "call and streams its audio to your endpoint, 20 ms per frame, with the "
            "reply sent back the same way.\n\n"
            "**Auth**: console routes take `Authorization: Bearer <jwt>` from "
            "`POST /v1/ui/login`. Machine-to-machine routes take a project API key in "
            "`X-API-Key` (or `Authorization: Bearer gbx_...`).\n\n"
            "**Webhooks**: subscribe with `POST /v1/webhooks`; every delivery carries "
            "`X-GBox-Signature: t=<unix>,v1=<hmac-sha256 of \"<t>.<body>\">`."
        ),
        routes=app.routes,
        servers=[{"url": "https://vb.site.gigies.lk", "description": "Production"},
                 {"url": "http://127.0.0.1:8080", "description": "Local container"}],
        contact={"name": "GBox by Gigies (Pvt) Ltd", "url": "https://gigies.lk",
                 "email": "info@gigies.lk"},
        license_info={"name": "Proprietary"},
    )
    comps = schema.setdefault("components", {})
    comps.setdefault("securitySchemes", {}).update({
        "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                       "description": "Console session token from POST /v1/ui/login"},
        "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key",
                   "description": "Project API key (gbx_...); Bearer gbx_... also accepted"},
    })
    for path, ops in schema.get("paths", {}).items():
        if path in _PUBLIC_PATHS:
            continue
        for op in ops.values():
            op.setdefault("security", [{"bearerAuth": []}, {"apiKey": []}])
    schema["tags"] = [
        {"name": "auth", "description": "Sign-up, sign-in, reset, session"},
        {"name": "devices", "description": "Claim, configure and control devices"},
        {"name": "calls", "description": "Sessions, call history, CDR"},
        {"name": "webhooks", "description": "Signed event subscriptions"},
        {"name": "tenants", "description": "Account, users, projects, audit log"},
        {"name": "billing", "description": "Plans, subscription, invoices, usage"},
        {"name": "dashboard", "description": "Console aggregates"},
    ]
    app.openapi_schema = schema
    return schema


app.openapi = _developer_openapi
