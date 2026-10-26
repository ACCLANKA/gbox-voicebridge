"""Agent bridge - a durable task/report channel between the builder agent and the
bench agent (Hermes), plus device-presence lookups so either side can tell when
the board is flashed and connected to the VPS.

This is NOT the audio *bridge* (see ``bridges.py``). It rides this control plane
so neither side needs a third-party account: both can already reach this API.

Auth: an API key with the ``ops`` (or ``admin``/``*``) scope, or an owner/admin
user. ``/ping`` is public so either side can prove the channel is alive.
"""
from __future__ import annotations

import base64
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from .. import db, protocol
from ..deps import get_tenant, require_auth
from .device_ws import hub as device_hub

router = APIRouter(prefix="/v1/ops", tags=["ops"])

OPS_SCOPES = {"ops", "admin", "*"}
OPS_ROLES = {"owner", "admin"}
TASK_STATES = ("pending", "claimed", "done", "failed")


def require_ops(principal: dict = Depends(require_auth)) -> dict:
    if principal["kind"] == "api_key":
        if set(principal.get("scopes") or []) & OPS_SCOPES:
            return principal
        raise HTTPException(403, "api key missing 'ops' scope")
    if principal["role"] in OPS_ROLES:
        return principal
    raise HTTPException(403, "ops access required")


class Attachment(BaseModel):
    name: str
    sha256: str | None = None
    bytes: int | None = None


class TaskIn(BaseModel):
    subject: str
    body: str = ""
    serial: str | None = None
    ref: str | None = None
    attachments: list[Attachment] | None = None


class AckIn(BaseModel):
    status: str = "done"      # done | failed
    report: str = ""


class DeviceCommand(BaseModel):
    """A control frame to write to a live device. Extra keys (hz, ms, number, ...)
    are passed through as-is, so the device's own parser sees a normal control
    frame: {"type":"control","action":"tone","hz":1000,"ms":2000}."""

    model_config = ConfigDict(extra="allow")

    action: str


class DeviceAudio(BaseModel):
    """A binary audio frame to write to a live device (downlink)."""

    pcm_base64: str
    kind: int = protocol.AUDIO_PCM16


def _task(row: dict) -> dict:
    return {
        "id": row["id"], "status": row["status"], "subject": row["subject"],
        "body": row["body"], "serial": row["serial"], "ref": row["ref"],
        "attachments": json.loads(row.get("attachments_json") or "[]"),
        "report": row.get("report"), "created_at": row["created_at"],
        "claimed_at": row.get("claimed_at"), "done_at": row.get("done_at"),
    }


@router.get("/ping", include_in_schema=False)
def ping() -> dict:
    """Unauthenticated liveness probe for the channel."""
    return {"service": "voicebridge-ops", "ok": True, "time": db.now()}


@router.get("/devices")
def list_devices(tenant_id: str = Depends(get_tenant),
                 _: dict = Depends(require_ops)) -> dict:
    rows = db.query(
        "SELECT serial,name,status,ip,fw,fw_version,rssi,uptime_s,last_seen,last_seen_at "
        "FROM devices WHERE tenant_id=? ORDER BY serial", (tenant_id,))
    return {"devices": rows, "total": len(rows)}


@router.get("/devices/{serial}")
def device_status(serial: str, tenant_id: str = Depends(get_tenant),
                  _: dict = Depends(require_ops)) -> dict:
    row = db.one("SELECT * FROM devices WHERE serial=? AND tenant_id=?", (serial, tenant_id))
    if not row:
        raise HTTPException(404, "device not found")
    return {
        "serial": row["serial"], "status": row["status"],
        "online": row["status"] == "online", "ip": row["ip"],
        "ssid": row["ssid"], "fw": row["fw"], "fw_version": row["fw_version"],
        "rssi": row["rssi"], "uptime_s": row["uptime_s"],
        "last_seen": row["last_seen"], "last_seen_at": row["last_seen_at"],
    }


def _resolve_device(serial: str, tenant_id: str) -> dict:
    row = db.one("SELECT id,serial FROM devices WHERE serial=? AND tenant_id=?",
                 (serial, tenant_id))
    if not row:
        raise HTTPException(404, "device not found")
    return row


@router.get("/devices/{serial}/live")
def device_live(serial: str, tenant_id: str = Depends(get_tenant),
                _: dict = Depends(require_ops)) -> dict:
    """Whether the device has a socket open *right now* (status is the last hello)."""
    _resolve_device(serial, tenant_id)
    return {"serial": serial, "connected": device_hub.connected(tenant_id, serial)}


@router.post("/devices/{serial}/control")
async def send_control(serial: str, body: DeviceCommand,
                       tenant_id: str = Depends(get_tenant),
                       _: dict = Depends(require_ops)) -> dict:
    """Write a control frame to a *connected* device.

    This is the missing direction: the control plane could read from devices but
    never write to them, so nothing could trigger a tone, answer a call, or feed
    downlink audio. Returns 409 if the device has no live socket.
    """
    _resolve_device(serial, tenant_id)
    frame = protocol.control(body.action, **(body.model_extra or {}))
    if not await device_hub.send_text(tenant_id, serial, frame):
        raise HTTPException(409, "device is not connected")
    return {"serial": serial, "action": body.action, "sent": json.loads(frame)}


@router.post("/devices/{serial}/audio")
async def send_audio(serial: str, body: DeviceAudio,
                     tenant_id: str = Depends(get_tenant),
                     _: dict = Depends(require_ops)) -> dict:
    """Push one binary audio frame (downlink) to a *connected* device."""
    _resolve_device(serial, tenant_id)
    try:
        pcm = base64.b64decode(body.pcm_base64, validate=True)
    except (ValueError, TypeError):
        raise HTTPException(422, "pcm_base64 is not valid base64")
    try:
        frame = protocol.encode_audio(pcm, body.kind)
    except protocol.ProtocolError as exc:
        raise HTTPException(422, str(exc))
    if not await device_hub.send_bytes(tenant_id, serial, frame):
        raise HTTPException(409, "device is not connected")
    return {"serial": serial, "kind": body.kind, "bytes": len(pcm)}


@router.post("/tasks", status_code=201)
def create_task(body: TaskIn, tenant_id: str = Depends(get_tenant),
                principal: dict = Depends(require_ops)) -> dict:
    task_id = db.new_id("task")
    db.execute(
        "INSERT INTO ops_tasks(id,tenant_id,serial,ref,subject,body,attachments_json,"
        "status,created_at) VALUES(?,?,?,?,?,?,?,'pending',?)",
        (task_id, tenant_id, body.serial, body.ref, body.subject, body.body,
         json.dumps([a.model_dump() for a in body.attachments] if body.attachments else []),
         db.now()))
    return {"id": task_id, "status": "pending"}


@router.get("/tasks")
def list_tasks(status: str | None = Query(default=None),
               limit: int = Query(default=20, ge=1, le=200),
               tenant_id: str = Depends(get_tenant),
               _: dict = Depends(require_ops)) -> dict:
    if status and status not in TASK_STATES:
        raise HTTPException(422, f"status must be one of {TASK_STATES}")
    if status:
        rows = db.query(
            "SELECT * FROM ops_tasks WHERE tenant_id=? AND status=? "
            "ORDER BY created_at DESC LIMIT ?", (tenant_id, status, limit))
    else:
        rows = db.query("SELECT * FROM ops_tasks WHERE tenant_id=? "
                        "ORDER BY created_at DESC LIMIT ?", (tenant_id, limit))
    return {"tasks": [_task(r) for r in rows], "total": len(rows)}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, tenant_id: str = Depends(get_tenant),
             _: dict = Depends(require_ops)) -> dict:
    row = db.one("SELECT * FROM ops_tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id))
    if not row:
        raise HTTPException(404, "task not found")
    return _task(row)


@router.post("/tasks/{task_id}/claim")
def claim_task(task_id: str, tenant_id: str = Depends(get_tenant),
               _: dict = Depends(require_ops)) -> dict:
    row = db.one("SELECT * FROM ops_tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id))
    if not row:
        raise HTTPException(404, "task not found")
    if row["status"] != "pending":
        raise HTTPException(409, f"task is {row['status']}, not pending")
    db.execute("UPDATE ops_tasks SET status='claimed',claimed_at=? WHERE id=?",
               (db.now(), task_id))
    return {"id": task_id, "status": "claimed"}


@router.post("/tasks/{task_id}/ack")
def ack_task(task_id: str, body: AckIn, tenant_id: str = Depends(get_tenant),
             _: dict = Depends(require_ops)) -> dict:
    row = db.one("SELECT * FROM ops_tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id))
    if not row:
        raise HTTPException(404, "task not found")
    state = body.status if body.status in ("done", "failed") else "done"
    db.execute("UPDATE ops_tasks SET status=?,report=?,done_at=? WHERE id=?",
               (state, body.report, db.now(), task_id))
    return {"id": task_id, "status": state}
