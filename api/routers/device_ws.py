"""Device WebSocket ingest edge (docs/design/DEVICE-PROTOCOL.md).

The ESP32 connects here, authenticates with a tenant API key + its serial, then
streams control JSON and binary audio frames. Call events are handed to the
detection service; audio is bridged to a media leg while a call is up.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import db, protocol, security
from ..services import detection, egress, forwarding

logger = logging.getLogger("voicebridge.device_ws")

router = APIRouter(prefix="/v1/device", tags=["device"])

# Session states in which a media leg should be open.
LIVE_STATES = ("answered", "bridged", "held")


class _Conn:
    """A live device socket plus a send lock.

    Sends are serialised because a control frame can arrive from an HTTP request
    while the device's own task is also writing acks on the same socket.
    """

    __slots__ = ("ws", "lock")

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.lock = asyncio.Lock()


class DeviceHub:
    """Directory of connected devices (tenant, serial) -> socket.

    Without this the control plane can only *read* from devices; there is nowhere
    to write to them. Everything here runs on the server's single event loop.
    """

    def __init__(self) -> None:
        self._conns: dict[tuple[str, str], _Conn] = {}

    def add(self, tenant_id: str, serial: str, ws: WebSocket) -> None:
        self._conns[(tenant_id, serial)] = _Conn(ws)

    def remove(self, tenant_id: str, serial: str, ws: WebSocket | None = None) -> None:
        cur = self._conns.get((tenant_id, serial))
        if cur is not None and (ws is None or cur.ws is ws):
            self._conns.pop((tenant_id, serial), None)

    def connected(self, tenant_id: str, serial: str) -> bool:
        return (tenant_id, serial) in self._conns

    async def send_text(self, tenant_id: str, serial: str, text: str) -> bool:
        conn = self._conns.get((tenant_id, serial))
        if conn is None:
            return False
        async with conn.lock:
            await conn.ws.send_text(text)
        return True

    async def send_bytes(self, tenant_id: str, serial: str, data: bytes) -> bool:
        conn = self._conns.get((tenant_id, serial))
        if conn is None:
            return False
        async with conn.lock:
            await conn.ws.send_bytes(data)
        return True


hub = DeviceHub()


def authenticate(serial: str | None, token: str | None) -> dict | None:
    """Resolve the device for a tenant API key + serial, or ``None``."""
    if not serial or not token or not token.startswith("vb_"):
        return None
    key = db.one("SELECT * FROM api_keys WHERE key_hash=?", (security.hash_api_key(token),))
    if not key or (key.get("expires_at") and key["expires_at"] < db.now()):
        return None
    return db.one("SELECT * FROM devices WHERE serial=? AND tenant_id=?",
                  (serial, key["tenant_id"]))


def _client_ip(ws: WebSocket) -> str | None:
    """Real device IP: X-Forwarded-For (Caddy/nginx) if present, else peer addr."""
    fwd = ws.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return ws.client.host if ws.client else None


def _sync_leg(session_id: str, device: dict) -> None:
    """Open/close the egress leg so it matches the session's current state."""
    session = db.one("SELECT * FROM call_sessions WHERE id=?", (session_id,))
    if not session:
        return
    leg = forwarding.registry.get(session_id)
    if session["state"] in LIVE_STATES and leg is None:
        try:
            forwarding.registry.open(session, device)
            logger.info("forwarding leg opened for %s (%s)", session_id,
                        forwarding.registry.source(session_id))
        except egress.EgressError as exc:
            logger.warning("forwarding leg failed for %s: %s", session_id, exc)
    elif session["state"] not in LIVE_STATES and leg is not None:
        forwarding.registry.close(session_id)
        logger.info("forwarding leg closed for %s", session_id)


async def _handle_control(ws: WebSocket, device: dict, msg: dict) -> str | None:
    """Handle one control frame; return the call session id if one applies."""
    mtype = msg["type"]
    # Evidence line: proves which control frames the device sends (hello only? telemetry? event?).
    logger.warning("CTRL %s type=%s event=%s fields=%s", device["serial"], mtype, msg.get("event"),
                ",".join(sorted(k for k in msg if k not in ("type", "seq", "ts")))[:120])
    if mtype == "ping":
        await ws.send_text(protocol.pong())
        return None
    if mtype == "hello":
        fw = msg.get("fw")
        if fw:
            db.execute("UPDATE devices SET fw=?, fw_version=?, last_seen=? WHERE id=?",
                       (fw, fw, db.now(), device["id"]))
        else:
            db.execute("UPDATE devices SET last_seen=? WHERE id=?", (db.now(), device["id"]))
        await ws.send_text(protocol.ack(None, action="hello"))
        return None
    if mtype == "telemetry":
        cols, vals = ["last_seen=?"], [db.now()]
        for field in ("rssi", "free_heap_kb", "uptime_s", "ip", "ssid",
                      "tx_ok", "tx_drops", "tx_fail_streak", "tx_age_s", "ul_ovw"):
            if msg.get(field) is not None:
                cols.append(f"{field}=?")
                vals.append(msg[field])
        vals.append(device["id"])
        db.execute(f"UPDATE devices SET {', '.join(cols)} WHERE id=?", tuple(vals))
        # And keep a history row. The device has been sending these counters all along and the
        # server was discarding them, which left a mains-powered board unmeasurable.
        db.record_telemetry(device["id"], msg)
        await ws.send_text(protocol.ack(None, action="telemetry"))
        return None
    if mtype == "event":
        try:
            from ..agent import Agent, note_call_event
            note_call_event(device["serial"], msg.get("event", ""))
            if msg.get("event") in ("call.answered", "call.audio_link_up") and Agent.CURRENT:
                Agent.CURRENT.on_call_live()
            elif msg.get("event") in ("call.ended", "call.audio_link_down") and Agent.CURRENT:
                Agent.CURRENT.on_call_end()
        except Exception as exc:  # noqa: BLE001
            logger.warning("call event hook failed: %s", str(exc)[:80])
        result = detection.ingest_device_event(
            device["tenant_id"], device["id"], msg.get("event", ""),
            msg.get("data") or {}, seq=msg.get("seq"), device_ts=msg.get("ts"))
        sid = result.get("call_session_id")
        if sid:
            _sync_leg(sid, device)
        await ws.send_text(protocol.ack(msg.get("seq"), session_id=sid,
                                        action=result.get("action", "none")))
        return sid
    await ws.send_text(protocol.ack(msg.get("seq"), action="unsupported"))
    return None


def _bench_echo(w: WebSocket) -> bool:
    """Bench echo: send the device's own uplink audio straight back down to it.

    On the bench there is no brain/TTS, so the downlink carries nothing at all
    (measured from the board: dl_offered=0 across a 125 s call) and the cloud audio path
    could not be exercised in either direction. Echoing proves the whole chain - phone ->
    SCO -> board -> ws -> server -> ws -> board -> SCO -> phone - with no codec in the
    middle, because uplink and downlink frames share the same [kind][PCM16] layout. Off
    by default: in production the brain owns the downlink and an echo would talk over it.
    """
    v = (w.query_params.get("echo") or "").strip().lower()
    if v in ("1", "true", "on", "yes"):
        return True
    return os.environ.get("VB_BENCH_ECHO", "").strip().lower() in ("1", "true", "on", "yes")


@router.websocket("/ws")
async def device_ws(ws: WebSocket) -> None:
    device = authenticate(ws.query_params.get("serial"), ws.query_params.get("token"))
    if not device:
        # 4401 = application-level "unauthorized"; close before accepting.
        await ws.close(code=4401)
        return
    await ws.accept()
    ip = _client_ip(ws)
    echo = _bench_echo(ws)
    if echo:
        logger.info("bench echo enabled for %s: uplink audio returns as downlink",
                    device["serial"])
    echo_rx = 0
    echo_tx = 0
    echo_last_log = 0.0
    try:
        import os as _os
        import time as _time
        _dir = "/app/data/stt"
        _os.makedirs(_dir, exist_ok=True)
        stt_path = _os.path.join(
            _dir, "%s-%s.pcm" % (device["serial"],
                                 _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime())))
        stt_f = open(stt_path, "wb")
        stt_frames = 0
        stt_bytes = 0
        stt_last = 0.0
        stt_max_gap = 0.0
        stt_pad_s = 0.0
    except Exception as _e:  # never let recording break a call
        logger.warning("STT recorder unavailable: %s", _e)
        stt_path = None
        stt_f = None
        stt_frames = stt_bytes = 0
        stt_last = 0.0
        stt_max_gap = 0.0
    db.execute("UPDATE devices SET status='online', ip=?, last_seen=? WHERE id=?",
               (ip, db.now(), device["id"]))
    hub.add(device["tenant_id"], device["serial"], ws)
    logger.info("device connected: %s (%s)", device["serial"], ip)
    session_id: str | None = None
    try:
        while True:
            frame = await ws.receive()
            if frame["type"] == "websocket.disconnect":
                # 1000 = clean close by one end, 1006 = transport died with no close frame, 1011 = app error.
                # The board reconnects every 5-15 s and prints nothing, so this is the only evidence.
                logger.warning("DEVWS %s disconnect code=%s reason=%r",
                             device["serial"], frame.get("code"), frame.get("reason"))
                if stt_f is not None:
                    try:
                        stt_f.close()
                        logger.warning("STT %s frames=%d bytes=%d audio_s=%.2f max_gap_ms=%.0f pad_s=%.2f file=%s",
                                       device["serial"], stt_frames, stt_bytes,
                                       stt_bytes / 16000.0, stt_max_gap * 1000.0,
                                       stt_pad_s, stt_path)
                    except Exception:
                        pass
                break
            if frame.get("text") is not None:
                try:
                    msg = protocol.parse_control(frame["text"])
                except protocol.ProtocolError:
                    await ws.send_text(protocol.ack(None, action="error"))
                    continue
                sid = await _handle_control(ws, device, msg)
                if sid:
                    session_id = sid
            elif frame.get("bytes") is not None:
                try:
                    audio = protocol.decode_audio(frame["bytes"])
                except protocol.ProtocolError:
                    continue
                if stt_f is not None:
                    try:
                        import time as _t2
                        _n = _t2.time()
                        if stt_last and (_n - stt_last) > stt_max_gap:
                            stt_max_gap = _n - stt_last
                        # Pad the gap with silence: the caller's pauses and any lost audio must
                        # occupy real time, or the recording plays as one run-on utterance.
                        if stt_last:
                            _gap = _n - stt_last
                            if _gap > 0.030:
                                _pad_s = min(_gap - 0.020, 2.0)
                                stt_f.write(b"\x00\x00" * int(_pad_s * 16000))
                                stt_pad_s += _pad_s
                                if _gap > stt_max_gap:
                                    stt_max_gap = _gap
                        stt_last = _n
                        stt_f.write(audio.payload)
                        stt_frames += 1
                        stt_bytes += len(audio.payload)
                    except Exception:
                        pass
                if echo:
                    payload, kind = protocol.to_downlink_16k(audio)
                    echo_rx += 1
                    await hub.send_bytes(device["tenant_id"], device["serial"],
                                         protocol.encode_audio(payload, kind))
                    echo_tx += 1
                    import time as _t
                    _now = _t.time()
                    if _now - echo_last_log >= 5.0:
                        echo_last_log = _now
                        logger.warning("ECHO %s rx=%d tx=%d last_kind=0x%02x",
                                       device["serial"], echo_rx, echo_tx, kind)
                leg = forwarding.registry.get(session_id)
                if leg is not None:
                    leg.write(audio.payload)
    except WebSocketDisconnect as exc:
        logger.warning("DEVWS %s dropped (WebSocketDisconnect code=%s)",
                       device["serial"], getattr(exc, "code", "?"))
    except Exception as exc:  # noqa: BLE001 - we want the reason, not a clean exit
        logger.warning("DEVWS %s dropped (%s: %s)", device["serial"], type(exc).__name__, exc)
    finally:
        hub.remove(device["tenant_id"], device["serial"], ws)
        forwarding.registry.close(session_id)
        db.execute("UPDATE devices SET status='offline', ip=NULL WHERE id=?", (device["id"],))
        logger.info("device disconnected: %s", device["serial"])
