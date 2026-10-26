"""Dashboard BFF ("ui") endpoints — moved out of the legacy /api/* surface.

Served under /v1/ui so the bundled dashboard SPA shares the /v1 namespace with
the rest of the control plane. Handlers present the shapes the vanilla-JS SPA
renders and share the DB layer (api.db) with the /v1 routers. Single-tenant by
design (the bundled demo has one active tenant); auth/scoping is a follow-up.
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

from contextvars import ContextVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import config, db as store, protocol, security
from ..services import dids, egress, forwarding
from .device_ws import hub as device_hub

REPO_ROOT = Path(__file__).resolve().parents[2]

_COOKIE = "vb_ui_token"

# The current request's tenant, published so the sync handlers can scope their
# queries without threading a parameter through every route.
_TENANT: ContextVar[str | None] = ContextVar("ui_tenant", default=None)


async def _principal(request: Request) -> dict:
    """Authenticate a dashboard request.

    Accepts a bearer header, a ``?token=`` query param (used by the SSE
    ``EventSource``, which cannot set headers) or the login cookie. Declared
    ``async`` on purpose: the ContextVar set here propagates to the sync
    endpoint handlers, which run in a copied context.
    """
    token = None
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            token = value.strip()
    token = token or request.query_params.get("token") or request.cookies.get(_COOKIE)
    if not token:
        raise HTTPException(401, "authentication required")
    if token.startswith("vb_"):
        key = store.one("SELECT * FROM api_keys WHERE key_hash=?",
                        (security.hash_api_key(token),))
        if not key or (key.get("expires_at") and key["expires_at"] < store.now()):
            raise HTTPException(401, "invalid api key")
        principal = {"kind": "api_key", "user_id": None,
                     "tenant_id": key["tenant_id"], "role": "admin"}
    else:
        payload = security.decode_token(token)
        if not payload or payload.get("type") != "access":
            raise HTTPException(401, "invalid or expired token")
        principal = {"kind": "user", "user_id": payload.get("sub"),
                     "tenant_id": payload.get("tenant_id"),
                     "role": payload.get("role", "viewer")}
    if not principal.get("tenant_id"):
        raise HTTPException(403, "no tenant bound to principal")
    _TENANT.set(principal["tenant_id"])
    return principal


def current_tenant() -> str:
    """The tenant of the request in flight (set by :func:`_principal`)."""
    tid = _TENANT.get()
    if not tid:
        raise HTTPException(401, "authentication required")
    return tid


# Everything below requires auth; /login and /logout sit on the public router.
router = APIRouter(prefix="/v1/ui", tags=["ui"], dependencies=[Depends(_principal)])
public = APIRouter(prefix="/v1/ui", tags=["ui"])

HELP_TOPICS: dict[str, dict[str, str]] = {}


class LoginBody(BaseModel):
    email: str
    password: str


@public.post("/login")
def ui_login(body: LoginBody, response: Response) -> dict:
    user = store.one("SELECT * FROM users WHERE email=?", (body.email.lower(),))
    if not user or not security.verify_password(body.password, user.get("password_hash")):
        raise HTTPException(401, "invalid credentials")
    token = security.create_token(user["id"], tenant_id=user["tenant_id"],
                                  role=user["role"], token_type="access")
    response.set_cookie(_COOKIE, token, httponly=True, samesite="lax",
                        max_age=config.ACCESS_TTL_SEC, path="/")
    store.execute("UPDATE users SET last_login_at=? WHERE id=?", (store.now(), user["id"]))
    return {"ok": True, "token": token,
            "user": {"email": user["email"], "name": user.get("name"),
                     "role": user["role"], "tenant_id": user["tenant_id"]}}


@public.post("/logout")
def ui_logout(response: Response) -> dict:
    response.delete_cookie(_COOKIE, path="/")
    return {"ok": True}


def _load_help() -> None:
    """Load help topics from docs/help/*.md (title = first heading)."""
    hdir = REPO_ROOT / "docs" / "help"
    if not hdir.exists():
        return
    for md in sorted(hdir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        title = md.stem.replace("-", " ").title()
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        HELP_TOPICS[md.stem] = {"slug": md.stem, "title": title, "body": text}


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class DeviceCreate(BaseModel):
    serial: str | None = None
    name: str = Field(default="New Gbox")
    project: str = "Default"
    tenant_id: str = "ten_1"


class DevicePatch(BaseModel):
    name: str | None = None
    project: str | None = None
    volume: int | None = None
    mic_gain: int | None = None
    aec_enabled: bool | None = None
    muted: bool | None = None


class WifiProfileCreate(BaseModel):
    ssid: str
    psk: str | None = None
    security: str = "wpa2"
    hidden: bool = False
    priority: int = 10
    auto_connect: bool = True


class WifiSetup(BaseModel):
    ssid: str
    psk: str | None = None
    security: str = "wpa2"
    save_profile: bool = True


class NetworkRequest(BaseModel):
    password: str | None = None


class ControlBody(BaseModel):
    action: str
    value: int | None = None


class DeviceIdConfig(BaseModel):
    prefix: str = Field(default="GBX", min_length=2, max_length=8)


class ClaimBody(BaseModel):
    claim_token: str


class ForwardingBody(BaseModel):
    dest_type: str
    dest_uri: str = ""
    options: dict = Field(default_factory=dict)


class DidBody(BaseModel):
    number: str
    label: str | None = None
    country: str | None = None
    device_id: str | None = None


class DidAssign(BaseModel):
    device_id: str | None = None


class CallControl(BaseModel):
    action: str  # answer | hangup | hold | resume | mute | unmute


class RouteBody(BaseModel):
    route: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _device_or_404(device_id: str) -> dict:
    d = store.one("SELECT * FROM devices WHERE (id=? OR serial=?) AND tenant_id=?",
                  (device_id, device_id, current_tenant()))
    if not d:
        raise HTTPException(404, "device not found")
    return d


def _socket_live(tenant_id: str | None, serial: str | None) -> bool:
    """True when the device has a socket open *right now*.

    ``devices.status`` is only the last hello the device sent, so it goes stale
    when a socket closes without the disconnect handler running (or is still
    'offline' from a previous session when the device has reconnected). The hub
    is the live truth; the panel must not contradict it.
    """
    return bool(tenant_id and serial and device_hub.connected(tenant_id, serial))


def _device_view(d: dict) -> dict:
    d = dict(d)
    d["live"] = _socket_live(d.get("tenant_id"), d.get("serial"))
    d["online"] = d["live"] or d.get("status") in ("online", "degraded")
    d["aec_enabled"] = bool(d.get("aec_enabled"))
    d["muted"] = bool(d.get("muted"))
    return d


def _drops(device_id: str) -> int:
    row = store.one("SELECT COUNT(*) c FROM link_events WHERE device_id=? AND type='disconnect'",
                    (device_id,))
    return int(row["c"]) if row else 0


def _latest_telemetry(device_id: str) -> dict | None:
    return store.one("SELECT * FROM telemetry WHERE device_id=? ORDER BY ts DESC LIMIT 1",
                     (device_id,))


def _quality(device_id: str, limit: int = 20) -> dict:
    """Voice quality for one device.

    Nothing measures MOS, jitter, loss or RTT today. The board's telemetry carries rssi, heap,
    uptime and its own frame counters, and the ingest stores only the first group; the values
    this function used to return on a "real" device existed only because POST
    /v1/ui/demo/tick invented a telemetry row every 6 s. So a device with no samples gets
    "not measured", said in as many words, rather than four dashes that read like a fault.
    """
    rows = store.query("SELECT * FROM telemetry WHERE device_id=? ORDER BY ts DESC LIMIT ?",
                       (device_id, limit))
    if not rows:
        return {"samples": 0, "available": False,
                "reason": "the board does not report MOS, jitter, loss or RTT",
                "mos": None, "jitter_ms": None, "loss_pct": None,
                "latency_ms": None, "stability_pct": None}
    ok = [r for r in rows if r["connection_state"] == "online"]

    def avg(col, digits=2):
        """Average over MEASURED samples only. mos/jitter/loss/latency are NULL on every row we
        write today, and sum() over Nones raised TypeError - which turned the whole device page
        into a 500 for any device that had telemetry at all."""
        vals = [r[col] for r in rows if r[col] is not None]
        return round(sum(vals) / len(vals), digits) if vals else None

    return {
        "samples": len(rows),
        "available": True,
        "mos": avg("mos"),
        "jitter_ms": avg("jitter_ms", 1),
        "loss_pct": avg("loss_pct"),
        "latency_ms": avg("latency_ms", 0),
        "stability_pct": round(100 * len(ok) / len(rows), 1),
        "rssi": rows[0]["rssi"],
    }


def _sound_balance(d: dict) -> dict:
    vol = d.get("volume") or 0
    mic = d.get("mic_gain") or 0
    # balance: 0 = perfectly matched tx/rx, negative = mic hot, positive = speaker hot
    imbalance = mic - vol
    return {
        "device_id": d["id"],
        "speaker_volume": vol,
        "mic_gain": mic,
        "imbalance": imbalance,
        "balanced": abs(imbalance) <= 15,
        "echo_risk": "high" if (vol >= 80 and not d.get("aec_enabled")) else "low",
        "aec_enabled": bool(d.get("aec_enabled")),
        "erle_db": d.get("echo_erle_db"),
        "recommendation": (
            "reduce speaker volume or enable AEC" if vol >= 80 and not d.get("aec_enabled")
            else "increase mic gain" if imbalance < -15
            else "reduce mic gain" if imbalance > 15
            else "levels balanced"),
    }


def _monitor_device(d: dict) -> dict:
    tel = _latest_telemetry(d["id"])
    q = _quality(d["id"])
    return {
        "device_id": d["id"],
        "serial": d["serial"],
        "name": d["name"],
        "project": d["project"],
        "bluetooth_name": d["bluetooth_name"],
        "status": d["status"],
        "live": _socket_live(d.get("tenant_id"), d["serial"]),
        "online": _socket_live(d.get("tenant_id"), d["serial"])
                  or d.get("status") in ("online", "degraded"),
        "connected_ip": d.get("ip"),
        "ssid": d.get("ssid"),
        "rssi": tel["rssi"] if tel else d.get("rssi"),
        "latency_ms": tel["latency_ms"] if tel else None,
        "jitter_ms": tel["jitter_ms"] if tel else None,
        "loss_pct": tel["loss_pct"] if tel else None,
        "mos": tel["mos"] if tel else None,
        "uptime_s": d.get("uptime_s"),
        "last_seen": d.get("last_seen"),
        "drops": _drops(d["id"]),
        "stability_pct": q.get("stability_pct"),
        "quality": q,
        "sound_balance": _sound_balance(d),
    }


# --------------------------------------------------------------------------- #
# Summary / tenant
# --------------------------------------------------------------------------- #
@public.get("/health")
def health() -> dict:
    return {"status": "ok", "time": store.now()}


@router.get("/tenant")
def tenant() -> dict:
    t = store.one("SELECT * FROM tenants WHERE id=?", (current_tenant(),))
    if not t:
        raise HTTPException(404, "no tenant")
    return t


@router.get("/summary")
def summary() -> dict:
    tid = current_tenant()
    owned = "device_id IN (SELECT id FROM devices WHERE tenant_id=?)"
    devices = [_device_view(d) for d in
               store.query("SELECT * FROM devices WHERE tenant_id=? ORDER BY serial", (tid,))]
    active = _calls_for_tenant(" AND ended_at IS NULL")
    ended = _calls_for_tenant(" AND ended_at IS NOT NULL")
    # mos is NULL for every frame we do not measure (see db.record_telemetry), so summing the raw
    # column raised TypeError: unsupported operand type(s) for +: 'int' and 'NoneType' and the whole
    # Overview returned 500. Keep only measured values.
    mos_vals = [t["mos"] for t in store.query(
        f"SELECT mos FROM telemetry WHERE {owned} ORDER BY ts DESC LIMIT 40", (tid,))
        if t["mos"] is not None]
    total_drops = store.one(
        f"SELECT COUNT(*) c FROM link_events WHERE type='disconnect' AND {owned}", (tid,))["c"]
    return {
        "demo": store.demo_enabled(),
        "devices_total": len(devices),
        "devices_online": sum(1 for d in devices if d["online"]),
        "devices_degraded": sum(1 for d in devices if d["status"] == "degraded"),
        "devices_offline": sum(1 for d in devices if d["status"] == "offline"),
        "calls_active": len(active),
        "calls_today": len(active) + len(ended),
        "avg_mos": round(sum(mos_vals) / len(mos_vals), 2) if mos_vals else None,
        "total_drops": int(total_drops),
        "active_calls": [_call_view(c) for c in active],
    }


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #
@router.get("/devices")
def list_devices(status: str | None = None, project: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM devices WHERE tenant_id=?", [current_tenant()]
    clauses = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if project:
        clauses.append("project=?")
        params.append(project)
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY serial"
    return [_device_view(d) for d in store.query(sql, tuple(params))]


@router.post("/devices", status_code=201)
def create_device(body: DeviceCreate) -> dict:
    prefix = store.get_setting("device_id_prefix", "GBX")
    serial = body.serial or f"{prefix}-{store.new_id('x').split('_')[1][:6].upper()}"
    if store.one("SELECT id FROM devices WHERE serial=? AND tenant_id=?", (serial, current_tenant())):
        raise HTTPException(409, "serial already exists")
    did = store.new_id("dev")
    # device-id scheme: BT/AP names derive from the serial suffix
    suffix = serial.replace(prefix + "-", "")[-4:]
    store.execute("""INSERT INTO devices(id,serial,name,tenant_id,project,bluetooth_name,status,
                     codec,sample_rate,aec_enabled,volume,mic_gain,last_seen,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (did, serial, body.name, current_tenant(), body.project,
                   f"Gbox-{suffix}", "provisioning", "opus", 16000, 1, 70, 60,
                   store.now(), store.now()))
    return _device_view(store.one("SELECT * FROM devices WHERE id=?", (did,)))


@router.get("/devices/{device_id}")
def get_device(device_id: str) -> dict:
    d = _device_or_404(device_id)
    view = _device_view(d)
    view["quality"] = _quality(d["id"])
    view["sound_balance"] = _sound_balance(d)
    view["drops"] = _drops(d["id"])
    view["wifi_profiles"] = store.query(
        "SELECT * FROM wifi_profiles WHERE device_id=? ORDER BY priority DESC", (d["id"],))
    return view


@router.patch("/devices/{device_id}")
def patch_device(device_id: str, body: DevicePatch) -> dict:
    d = _device_or_404(device_id)
    fields, params = [], []
    for k, v in body.model_dump(exclude_none=True).items():
        fields.append(f"{k}=?")
        params.append(int(v) if isinstance(v, bool) else v)
    if not fields:
        return _device_view(d)
    params.append(d["id"])
    store.execute(f"UPDATE devices SET {', '.join(fields)} WHERE id=?", tuple(params))
    return _device_view(store.one("SELECT * FROM devices WHERE id=?", (d["id"],)))


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: str) -> None:
    d = _device_or_404(device_id)
    store.execute("DELETE FROM wifi_profiles WHERE device_id=?", (d["id"],))
    store.execute("DELETE FROM devices WHERE id=?", (d["id"],))


@router.get("/devices/{device_id}/status")
def device_status(device_id: str) -> dict:
    return _monitor_device(_device_or_404(device_id))


@router.get("/devices/{device_id}/telemetry")
def device_telemetry(device_id: str, limit: int = Query(60, le=500)) -> list[dict]:
    d = _device_or_404(device_id)
    return store.query("SELECT * FROM telemetry WHERE device_id=? ORDER BY ts DESC LIMIT ?",
                       (d["id"], limit))


@router.get("/devices/{device_id}/link-events")
def device_link_events(device_id: str, limit: int = Query(50, le=500)) -> list[dict]:
    d = _device_or_404(device_id)
    return store.query("SELECT * FROM link_events WHERE device_id=? ORDER BY ts DESC LIMIT ?",
                       (d["id"], limit))


@router.get("/devices/{device_id}/quality")
def device_quality(device_id: str) -> dict:
    return _quality(_device_or_404(device_id)["id"])


@router.get("/devices/{device_id}/sound-balance")
def device_sound_balance(device_id: str) -> dict:
    return _sound_balance(_device_or_404(device_id))


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
@router.post("/devices/{device_id}/control")
async def device_control(device_id: str, body: ControlBody) -> dict:
    """Send a control command to the board - and only ever claim what was actually sent.

    These actions used to write into the database and change the device's reported status, so
    pressing Reboot on an offline device left the console showing "connecting" for ever: no
    frame was transmitted, and nothing was coming to reset it. The command now goes to the
    device's socket over the WebSocket (device_hub.send_text, as
    /v1/ops/devices/{serial}/control does), and an offline device is a 409 rather than a
    fabricated status. Note that the firmware's control vocabulary is small today (tone,
    answer, dial): a command it does not implement is delivered and ignored, which is why
    nothing here writes a status on the board's behalf.
    """
    d = _device_or_404(device_id)
    did, serial = d["id"], d["serial"]
    a = body.action.lower()
    extra = {} if body.value is None else {"value": body.value}

    if not await device_hub.send_text(d["tenant_id"], serial, protocol.control(a, **extra)):
        raise HTTPException(409, f"{serial} is not connected - nothing was sent")

    # The console's own record of what was asked for the settings it displays. Anything about
    # whether the board is up, rebooting or provisioned comes from the board's own reports.
    if a == "mute":
        store.execute("UPDATE devices SET muted=1 WHERE id=?", (did,))
    elif a == "unmute":
        store.execute("UPDATE devices SET muted=0 WHERE id=?", (did,))
    elif a == "set-volume" and body.value is not None:
        store.execute("UPDATE devices SET volume=? WHERE id=?", (max(0, min(100, body.value)), did))
    elif a == "set-mic-gain" and body.value is not None:
        store.execute("UPDATE devices SET mic_gain=? WHERE id=?", (max(0, min(100, body.value)), did))
    elif a == "enable-aec":
        store.execute("UPDATE devices SET aec_enabled=1 WHERE id=?", (did,))
    elif a == "disable-aec":
        store.execute("UPDATE devices SET aec_enabled=0 WHERE id=?", (did,))
    elif a == "reconnect":
        store.execute("INSERT INTO link_events(id,device_id,ts,type,detail) VALUES(?,?,?,?,?)",
                      (store.new_id("ev"), did, store.now(), "reconnect",
                       "Reconnect requested from the dashboard"))
    elif a not in ("reboot", "factory-reset"):
        raise HTTPException(400, f"unknown action '{body.action}'")

    store.execute("UPDATE devices SET last_seen=? WHERE id=?", (store.now(), did))
    return {"ok": True, "action": a, "sent": True,
            "device": _device_view(store.one("SELECT * FROM devices WHERE id=?", (did,)))}


# --------------------------------------------------------------------------- #
# Wi-Fi profiles & provisioning
# --------------------------------------------------------------------------- #
@router.get("/devices/{device_id}/wifi-profiles")
def list_wifi_profiles(device_id: str) -> list[dict]:
    d = _device_or_404(device_id)
    return store.query("SELECT * FROM wifi_profiles WHERE device_id=? ORDER BY priority DESC", (d["id"],))


@router.post("/devices/{device_id}/wifi-profiles", status_code=201)
def add_wifi_profile(device_id: str, body: WifiProfileCreate) -> dict:
    d = _device_or_404(device_id)
    pid = store.new_id("wp")
    store.execute("""INSERT INTO wifi_profiles(id,device_id,ssid,psk,security,hidden,priority,
                     auto_connect,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                  (pid, d["id"], body.ssid, body.psk, body.security, int(body.hidden),
                   body.priority, int(body.auto_connect), store.now()))
    return store.one("SELECT * FROM wifi_profiles WHERE id=?", (pid,))


@router.delete("/devices/{device_id}/wifi-profiles/{profile_id}", status_code=204)
def delete_wifi_profile(device_id: str, profile_id: str) -> None:
    store.execute("DELETE FROM wifi_profiles WHERE id=? AND device_id=?", (profile_id, device_id))


@router.get("/networks")
def list_networks() -> list[dict]:
    return store.query("SELECT * FROM networks WHERE tenant_id=? "
                       "ORDER BY is_requested DESC, is_open DESC, ssid", (current_tenant(),))


@router.post("/networks/scan")
def scan_networks() -> dict:
    """Simulate a Wi-Fi scan: refresh discovered networks (open + requested)."""
    extra = [
        ("Lobby-AP", "wpa2", 0, 0, "2.4GHz", None),
        ("Guest-Open", "open", 1, 0, "2.4GHz", None),
    ]
    tid = current_tenant()
    for ssid, sec, openf, req, band, pw in extra:
        if not store.one("SELECT id FROM networks WHERE ssid=? AND tenant_id=?", (ssid, tid)):
            store.execute("""INSERT INTO networks(id,ssid,bssid,security,is_open,is_requested,band,
                             password,tenant_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (store.new_id("net"), ssid, "aa:bb:cc:dd:ee:ff", sec, openf, req,
                           band, pw, tid, store.now()))
    nets = store.query("SELECT * FROM networks WHERE tenant_id=?", (tid,))
    return {"scanned": len(nets), "open": sum(1 for n in nets if n["is_open"]),
            "requested": sum(1 for n in nets if n["is_requested"]), "networks": nets}


@router.post("/networks/{network_id}/request")
def request_network(network_id: str, body: NetworkRequest) -> dict:
    n = store.one("SELECT * FROM networks WHERE id=? AND tenant_id=?",
                  (network_id, current_tenant()))
    if not n:
        raise HTTPException(404, "network not found")
    store.execute("UPDATE networks SET is_requested=1, password=? WHERE id=?",
                  (body.password, network_id))
    return store.one("SELECT * FROM networks WHERE id=?", (network_id,))


@router.post("/devices/{device_id}/wifi-setup")
def wifi_setup(device_id: str, body: WifiSetup) -> dict:
    """Select a network for the device and connect (device-local 192.168.4.1 flow, server view)."""
    d = _device_or_404(device_id)
    net = store.one("SELECT * FROM networks WHERE ssid=? AND tenant_id=?",
                    (body.ssid, current_tenant()))
    net_security = net["security"] if net else body.security
    psk = body.psk or (net["password"] if net else None)
    if net_security != "open" and not psk:
        raise HTTPException(400, "password required for secured network")
    if body.save_profile:
        store.execute("""INSERT INTO wifi_profiles(id,device_id,ssid,psk,security,hidden,priority,
                         auto_connect,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (store.new_id("wp"), d["id"], body.ssid, psk, net_security, 0, 10, 1, store.now()))
    store.execute("UPDATE devices SET status='online', ssid=?, ip=?, last_seen=?, rssi=? WHERE id=?",
                  (body.ssid, f"192.168.1.{random.randint(20, 200)}", store.now(),
                   random.randint(-65, -45), d["id"]))
    store.execute("INSERT INTO link_events(id,device_id,ts,type,detail) VALUES(?,?,?,?,?)",
                  (store.new_id("ev"), d["id"], store.now(), "connect", f"Connected to {body.ssid}"))
    return {"ok": True, "device": _device_view(store.one("SELECT * FROM devices WHERE id=?", (d["id"],)))}


@router.post("/devices/{device_id}/provision/start")
def provision_start(device_id: str) -> dict:
    d = _device_or_404(device_id)
    store.execute("UPDATE devices SET status='provisioning' WHERE id=?", (d["id"],))
    suffix = d["serial"][-4:]
    return {
        "device_id": d["id"],
        "mode": "softap",
        "bluetooth_name": d["bluetooth_name"] or f"Gbox-{suffix}",
        "ap_ssid": f"GboxWifi-{suffix}",
        "portal": "http://192.168.4.1",
        "instruction": "Connect the phone to GboxWifi and open http://192.168.4.1 to pick a network.",
    }


@router.post("/devices/{device_id}/provision/complete")
def provision_complete(device_id: str, body: WifiSetup) -> dict:
    return wifi_setup(device_id, body)


# --------------------------------------------------------------------------- #
# Device ID scheme
# --------------------------------------------------------------------------- #
@router.get("/settings/device-id")
def get_device_id_config() -> dict:
    prefix = store.get_setting("device_id_prefix", "GBX")
    count = store.one("SELECT COUNT(*) c FROM devices WHERE tenant_id=?", (current_tenant(),))["c"]
    return {"prefix": prefix, "next_example": f"{prefix}-XXXXXX", "devices": int(count)}


@router.put("/settings/device-id")
def set_device_id_config(body: DeviceIdConfig) -> dict:
    store.set_setting("device_id_prefix", body.prefix.upper())
    return get_device_id_config()


@router.post("/devices/{device_id}/claim")
def claim_device(device_id: str, body: ClaimBody) -> dict:
    d = _device_or_404(device_id)
    if len(body.claim_token) < 6:
        raise HTTPException(400, "invalid claim token")
    store.execute("UPDATE devices SET status='connecting', last_seen=? WHERE id=?", (store.now(), d["id"]))
    return {"ok": True, "device_id": d["id"], "bound_tenant": d["tenant_id"],
            "certificate_issued": True}


@router.get("/devices/{device_id}/forwarding")
def get_device_forwarding(device_id: str) -> dict:
    """Resolved egress destination for a device (+ any per-device override)."""
    return forwarding.describe(_device_or_404(device_id))


@router.put("/devices/{device_id}/forwarding")
def set_device_forwarding(device_id: str, body: ForwardingBody) -> dict:
    device = _device_or_404(device_id)
    try:
        return forwarding.set_override(device, body.dest_type, body.dest_uri, body.options)
    except egress.EgressError as exc:
        raise HTTPException(422, str(exc))


@router.delete("/devices/{device_id}/forwarding", status_code=204)
def clear_device_forwarding(device_id: str) -> None:
    forwarding.clear_override(_device_or_404(device_id))


# --------------------------------------------------------------------------- #
# Numbers (DIDs)
# --------------------------------------------------------------------------- #
@router.get("/dids")
def ui_list_dids() -> list[dict]:
    return dids.list_dids(current_tenant())


@router.post("/dids", status_code=201)
def ui_create_did(body: DidBody) -> dict:
    try:
        return dids.create(current_tenant(), body.number, label=body.label,
                           country=body.country, device_id=body.device_id)
    except dids.DuplicateDid as exc:
        raise HTTPException(409, str(exc))
    except dids.DidError as exc:
        raise HTTPException(400, str(exc))


@router.post("/dids/{did_id}/assign")
def ui_assign_did(did_id: str, body: DidAssign) -> dict:
    did = dids.assign(current_tenant(), did_id, device_id=body.device_id)
    if not did:
        raise HTTPException(404, "did not found")
    return did


@router.delete("/dids/{did_id}", status_code=204)
def ui_delete_did(did_id: str) -> None:
    if not dids.delete(current_tenant(), did_id):
        raise HTTPException(404, "did not found")


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Console call reads and writes.
#
# `call_sessions` is the server-side source of truth (api/services/detection.py). The legacy
# `calls` table has had no writer since that became true, so the history panel could never show
# a real call no matter what the bench did. Real sessions are read here, aliased into the column
# names the views already expect; legacy rows are appended ONLY when demo data is enabled, which
# is the only place they come from. Writes have to go to the table the call is actually in, or
# the console's hangup button would silently do nothing to a real call.
_SESSION_CALL_COLS = ("id, device_id, direction, source, state, route, caller_name, "
                      "incoming_number, whatsapp_number, started_at, answered_at, ended_at, "
                      "duration_sec AS duration_s, mos, jitter_ms, loss_pct, erle_db, "
                      "aec_enabled, detection_confidence AS confidence")


def _calls_for_tenant(extra: str = "", params: tuple = ()) -> list[dict]:
    tid = current_tenant()
    rows = store.query(f"SELECT {_SESSION_CALL_COLS} FROM call_sessions "
                       f"WHERE tenant_id=?{extra}", (tid, *params))
    if store.demo_enabled():
        seen = {r["id"] for r in rows}
        legacy = store.query("SELECT * FROM calls WHERE device_id IN "
                             "(SELECT id FROM devices WHERE tenant_id=?)" + extra, (tid, *params))
        rows += [r for r in legacy if r["id"] not in seen]
    return rows


def _call_table(call_id: str) -> str:
    """Which table holds this call. Writes must follow the call, not the code age."""
    return "call_sessions" if store.one("SELECT 1 FROM call_sessions WHERE id=?", (call_id,)) else "calls"


def _call_view(c: dict) -> dict:
    c = dict(c)
    d = store.one("SELECT serial,name FROM devices WHERE id=?", (c["device_id"],))
    c["device_serial"] = d["serial"] if d else None
    c["device_name"] = d["name"] if d else None
    c["active"] = c["ended_at"] is None
    return c


def _call_or_404(call_id: str) -> dict:
    rows = _calls_for_tenant(" AND id=?", (call_id,))
    if not rows:
        raise HTTPException(404, "call not found")
    return rows[0]


@router.get("/calls")
def list_calls(active: bool | None = None, device_id: str | None = None) -> list[dict]:
    clauses, params = [], []
    if active is True:
        clauses.append("ended_at IS NULL")
    if active is False:
        clauses.append("ended_at IS NOT NULL")
    if device_id:
        clauses.append("device_id=?")
        params.append(device_id)
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""
    rows = _calls_for_tenant(extra, tuple(params))
    # Sorted here rather than in SQL: the real rows and the demo rows come from two tables and
    # the newest-first order has to hold across both.
    rows.sort(key=lambda c: c.get("started_at") or "", reverse=True)
    return [_call_view(c) for c in rows]


@router.get("/calls/{call_id}")
def get_call(call_id: str) -> dict:
    return _call_view(_call_or_404(call_id))


@router.post("/calls/{call_id}/control")
def call_control(call_id: str, body: CallControl) -> dict:
    _call_or_404(call_id)
    a = body.action.lower()
    table = _call_table(call_id)
    if a == "hangup":
        store.execute(f"UPDATE {table} SET state='ended', ended_at=? WHERE id=?",
                      (store.now(), call_id))
    elif table == "calls":
        # The legacy demo rows are the only place these actions still make sense: a real session
        # state is owned by the detection layer, and writing 'held' or 'bridged' into it would be
        # inventing a state that layer does not have.
        state = {"answer": "active", "hold": "held", "resume": "bridged"}.get(a)
        if state is None:
            raise HTTPException(400, f"unknown action '{body.action}'")
        store.execute(f"UPDATE {table} SET state=? WHERE id=?", (state, call_id))
    else:
        raise HTTPException(409, f"'{a}' is not part of a live session's lifecycle; "
                                 "only hangup is")
    return {"ok": True, "action": a,
            "call": _call_view(store.one(f"SELECT * FROM {table} WHERE id=?", (call_id,)))}


@router.post("/calls/{call_id}/route")
def call_route(call_id: str, body: RouteBody) -> dict:
    _call_or_404(call_id)
    table = _call_table(call_id)
    store.execute(f"UPDATE {table} SET route=? WHERE id=?", (body.route, call_id))
    return {"ok": True, "call": _call_view(store.one(f"SELECT * FROM {table} WHERE id=?", (call_id,)))}


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #
@router.get("/monitor/overview")
def monitor_overview() -> dict:
    tid = current_tenant()
    devices = [_monitor_device(d) for d in
               store.query("SELECT * FROM devices WHERE tenant_id=? ORDER BY serial", (tid,))]
    events = store.query("SELECT * FROM link_events WHERE device_id IN "
                         "(SELECT id FROM devices WHERE tenant_id=?) "
                         "ORDER BY ts DESC LIMIT 25", (tid,))
    return {"devices": devices, "recent_events": events}


@router.get("/monitor/events")
def monitor_events(limit: int = Query(50, le=500)) -> list[dict]:
    return store.query("SELECT * FROM link_events WHERE device_id IN "
                       "(SELECT id FROM devices WHERE tenant_id=?) "
                       "ORDER BY ts DESC LIMIT ?", (current_tenant(), limit))


# --------------------------------------------------------------------------- #
# Live stream (SSE — dependency-free, no websockets package required)
# --------------------------------------------------------------------------- #
_SSE_CLIENTS = 0


@router.get("/stream")
async def stream() -> StreamingResponse:
    async def gen():
        global _SSE_CLIENTS
        _SSE_CLIENTS += 1
        try:
            while True:
                tid = current_tenant()
                payload = {
                    "summary": summary(),
                    "devices": [_monitor_device(d) for d in
                                store.query("SELECT * FROM devices WHERE tenant_id=? "
                                            "ORDER BY serial", (tid,))],
                    "active_calls": [_call_view(c) for c in
                                     _calls_for_tenant(" AND ended_at IS NULL")],
                    "ts": store.now(),
                }
                yield f"event: tick\ndata: {json.dumps(payload)}\n\n"
                await asyncio.sleep(3)
        finally:
            _SSE_CLIENTS -= 1

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- #
# Demo simulator (makes the dashboard live without hardware)
# --------------------------------------------------------------------------- #
def _require_demo() -> None:
    """Demo endpoints fabricate data. On a real console they must not exist."""
    if not store.demo_enabled():
        from fastapi import HTTPException
        raise HTTPException(status_code=403,
                            detail="demo data is disabled (set VB_DEMO_DATA=1 to enable)")


@router.post("/demo/tick")
def demoTick() -> dict:
    _require_demo()
    for d in store.query("SELECT * FROM devices WHERE tenant_id=? "
                         "AND status IN ('online','degraded')", (current_tenant(),)):
        rssi = max(-88, min(-45, (d["rssi"] or -60) + random.randint(-4, 4)))
        state = "degraded" if rssi < -72 else "online"
        store.execute("UPDATE devices SET rssi=?, status=?, last_seen=?, uptime_s=uptime_s+3 WHERE id=?",
                      (rssi, state, store.now(), d["id"]))
        store.execute("""INSERT INTO telemetry(id,device_id,ts,rssi,latency_ms,jitter_ms,loss_pct,
                         mos,tx_kbps,rx_kbps,connection_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (store.new_id("tl"), d["id"], store.now(), rssi, random.randint(60, 170),
                       round(random.uniform(5, 30), 1), round(random.uniform(0, 2.0), 2),
                       round(random.uniform(3.4, 4.5), 1), round(random.uniform(18, 26), 1),
                       round(random.uniform(18, 26), 1), state))
        if random.random() < 0.04:
            store.execute("INSERT INTO link_events(id,device_id,ts,type,detail) VALUES(?,?,?,?,?)",
                          (store.new_id("ev"), d["id"], store.now(), "disconnect",
                           "Transient Wi-Fi drop (simulated)"))
    return {"ok": True, "ts": store.now()}


@router.post("/demo/reset")
def demoReset() -> dict:
    _require_demo()
    store.reset()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Help & markdown docs
# --------------------------------------------------------------------------- #
@router.get("/help")
def help_index() -> list[dict]:
    return [{"slug": k, "title": v["title"]} for k, v in HELP_TOPICS.items()]


@router.get("/help/{slug}")
def help_topic(slug: str) -> dict:
    if slug not in HELP_TOPICS:
        raise HTTPException(404, "help topic not found")
    return HELP_TOPICS[slug]


@router.get("/docs/tree")
def docs_tree() -> list[dict]:
    out = []
    for p in sorted((REPO_ROOT / "docs").rglob("*.md")):
        out.append({"path": str(p.relative_to(REPO_ROOT)).replace("\\", "/"),
                    "title": p.stem.replace("-", " ").replace("_", " ").title()})
    return out


@router.get("/docs/markdown", response_class=HTMLResponse)
def docs_markdown(path: str) -> HTMLResponse:
    target = (REPO_ROOT / path).resolve()
    if not str(target).startswith(str(REPO_ROOT)) or not target.exists() or target.suffix != ".md":
        raise HTTPException(404, "doc not found")
    return HTMLResponse(target.read_text(encoding="utf-8"))
