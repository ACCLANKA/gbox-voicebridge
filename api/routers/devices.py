"""Device fleet, provisioning, telemetry and network endpoints."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..deps import audit, get_tenant, require_permission

router = APIRouter(prefix="/v1", tags=["devices"])


def _device_view(d: dict) -> dict:
    v = dict(d)
    v["project"] = {"id": d.get("project_id"), "name": d.get("project")}
    return v


@router.get("/devices")
def list_devices(tenant_id: str = Depends(get_tenant),
                 _p: dict = Depends(require_permission("devices")),
                 status: str | None = None, project_id: str | None = None,
                 limit: int = Query(200, le=1000), offset: int = 0) -> dict:
    sql = "SELECT * FROM devices WHERE tenant_id=?"
    params: list = [tenant_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    if project_id:
        sql += " AND project_id=?"
        params.append(project_id)
    total = db.one(f"SELECT COUNT(*) c FROM ({sql})", tuple(params))["c"]
    sql += " ORDER BY created_at LIMIT ? OFFSET ?"
    rows = db.query(sql, (*params, limit, offset))
    return {"items": [_device_view(d) for d in rows], "total": total,
            "limit": limit, "offset": offset}


@router.post("/devices/{device_id}/claim")
def claim_device(device_id: str, tenant_id: str = Depends(get_tenant),
                 principal: dict = Depends(require_permission("devices"))) -> dict:
    d = db.one("SELECT * FROM devices WHERE id=? OR serial=?", (device_id, device_id))
    if not d:
        raise HTTPException(404, "device not found")
    if d.get("tenant_id") and d["tenant_id"] != tenant_id:
        raise HTTPException(409, "device already claimed by another tenant")
    db.execute("UPDATE devices SET tenant_id=?, status='offline' WHERE id=?",
               (tenant_id, d["id"]))
    audit(tenant_id, principal.get("user_id"), "device.claim", "device", d["id"])
    return _device_view(db.one("SELECT * FROM devices WHERE id=?", (d["id"],)))


class ProvisionBody(BaseModel):
    name: str | None = None
    project_id: str | None = None
    ssid: str | None = None
    psk: str | None = None
    codec: str | None = None
    sample_rate: int | None = None
    aec_enabled: bool | None = None


@router.post("/devices/{device_id}/provision")
def provision_device(device_id: str, body: ProvisionBody,
                     tenant_id: str = Depends(get_tenant),
                     principal: dict = Depends(require_permission("devices"))) -> dict:
    d = db.one("SELECT * FROM devices WHERE id=? AND tenant_id=?", (device_id, tenant_id))
    if not d:
        raise HTTPException(404, "device not found")
    data = body.model_dump(exclude_none=True)
    if data.get("project_id"):
        prj = db.one("SELECT id FROM projects WHERE id=? AND tenant_id=?",
                     (data["project_id"], tenant_id))
        if not prj:
            raise HTTPException(400, "unknown project")
        data["project"] = prj["id"]
    if body.aec_enabled is not None:
        data["aec_enabled"] = int(body.aec_enabled)
    if body.ssid:
        db.execute("""INSERT INTO wifi_profiles(id,device_id,ssid,psk,security,hidden,
                      priority,auto_connect,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                   (db.new_id("wp"), device_id, body.ssid, body.psk or "", "wpa2", 0, 10, 1, db.now()))
        data["ssid"] = body.ssid
    if data:
        fields = ", ".join(f"{k}=?" for k in data)
        db.execute(f"UPDATE devices SET {fields} WHERE id=?", (*data.values(), device_id))
    db.execute("UPDATE devices SET status='provisioned' WHERE id=? AND status='unprovisioned'",
               (device_id,))
    audit(tenant_id, principal.get("user_id"), "device.provision", "device", device_id, data)
    return _device_view(db.one("SELECT * FROM devices WHERE id=?", (device_id,)))


@router.get("/devices/{device_id}")
def get_device(device_id: str, tenant_id: str = Depends(get_tenant),
               _p: dict = Depends(require_permission("devices"))) -> dict:
    d = db.one("SELECT * FROM devices WHERE id=? AND tenant_id=?", (device_id, tenant_id))
    if not d:
        raise HTTPException(404, "device not found")
    v = _device_view(d)
    v["telemetry"] = db.query("SELECT * FROM telemetry WHERE device_id=? ORDER BY ts DESC LIMIT 60",
                              (device_id,))
    v["link_events"] = db.query("SELECT * FROM link_events WHERE device_id=? ORDER BY ts DESC LIMIT 20",
                                (device_id,))
    return v


class DevicePatch(BaseModel):
    name: str | None = None
    project_id: str | None = None
    codec_pref: str | None = None
    volume: int | None = None
    mic_gain: int | None = None
    muted: bool | None = None
    aec_enabled: bool | None = None


@router.patch("/devices/{device_id}")
def patch_device(device_id: str, body: DevicePatch, tenant_id: str = Depends(get_tenant),
                 principal: dict = Depends(require_permission("devices"))) -> dict:
    d = db.one("SELECT * FROM devices WHERE id=? AND tenant_id=?", (device_id, tenant_id))
    if not d:
        raise HTTPException(404, "device not found")
    data = body.model_dump(exclude_none=True)
    for k in ("muted", "aec_enabled"):
        if k in data:
            data[k] = int(data[k])
    if data:
        fields = ", ".join(f"{k}=?" for k in data)
        db.execute(f"UPDATE devices SET {fields} WHERE id=?", (*data.values(), device_id))
    return _device_view(db.one("SELECT * FROM devices WHERE id=?", (device_id,)))


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: str, tenant_id: str = Depends(get_tenant),
                  principal: dict = Depends(require_permission("admin"))) -> None:
    d = db.one("SELECT * FROM devices WHERE id=? AND tenant_id=?", (device_id, tenant_id))
    if not d:
        raise HTTPException(404, "device not found")
    db.execute("DELETE FROM wifi_profiles WHERE device_id=?", (device_id,))
    db.execute("DELETE FROM devices WHERE id=?", (device_id,))
    audit(tenant_id, principal.get("user_id"), "device.delete", "device", device_id)


# ----- Device commands ----------------------------------------------------- #
class CommandBody(BaseModel):
    action: str
    value: int | str | bool | None = None


@router.post("/devices/{device_id}/commands", status_code=202)
def send_command(device_id: str, body: CommandBody, tenant_id: str = Depends(get_tenant),
                 principal: dict = Depends(require_permission("devices"))) -> dict:
    d = db.one("SELECT * FROM devices WHERE id=? AND tenant_id=?", (device_id, tenant_id))
    if not d:
        raise HTTPException(404, "device not found")
    if d["status"] == "offline":
        raise HTTPException(409, "device offline")
    allowed = {"set_volume", "set_mic_gain", "mute", "unmute", "reboot", "led", "set_codec"}
    if body.action not in allowed:
        raise HTTPException(400, f"unsupported action '{body.action}'")
    cmd_id = db.new_id("cmd")
    audit(tenant_id, principal.get("user_id"), "device.command", "device", device_id,
          {"action": body.action, "value": body.value})
    return {"command_id": cmd_id, "device_id": device_id, "action": body.action,
            "value": body.value, "status": "queued"}


@router.get("/devices/{device_id}/telemetry")
def device_telemetry(device_id: str, tenant_id: str = Depends(get_tenant),
                     _p: dict = Depends(require_permission("devices")),
                     limit: int = Query(120, le=2000)) -> dict:
    d = db.one("SELECT id FROM devices WHERE id=? AND tenant_id=?", (device_id, tenant_id))
    if not d:
        raise HTTPException(404, "device not found")
    return {"device_id": device_id,
            "samples": db.query("SELECT * FROM telemetry WHERE device_id=? ORDER BY ts DESC LIMIT ?",
                                (device_id, limit))}


@router.get("/devices/{device_id}/link-events")
def device_link_events(device_id: str, tenant_id: str = Depends(get_tenant),
                       _p: dict = Depends(require_permission("devices"))) -> dict:
    d = db.one("SELECT id FROM devices WHERE id=? AND tenant_id=?", (device_id, tenant_id))
    if not d:
        raise HTTPException(404, "device not found")
    return {"device_id": device_id,
            "events": db.query("SELECT * FROM link_events WHERE device_id=? ORDER BY ts DESC LIMIT 100",
                               (device_id,))}


# ----- Provisioning artifacts ---------------------------------------------- #
@router.get("/provisioning/claims")
def provisioning_claims(tenant_id: str = Depends(get_tenant),
                        _p: dict = Depends(require_permission("devices"))) -> dict:
    unclaimed = db.query("SELECT id,serial,name,status FROM devices WHERE tenant_id IS NULL")
    claim_code = secrets.token_hex(4).upper()
    return {"claim_code": f"VB-{claim_code}", "expires_in": 900,
            "unclaimed_devices": unclaimed}


@router.get("/provisioning/token")
def provisioning_token(tenant_id: str = Depends(get_tenant),
                       _p: dict = Depends(require_permission("devices"))) -> dict:
    from .. import security
    token = security.create_token(f"prov:{tenant_id}", tenant_id=tenant_id, role="device",
                                  token_type="access", ttl=900)
    return {"provisioning_token": token, "expires_in": 900, "tenant_id": tenant_id}


@router.get("/firmware/latest")
def firmware_latest(channel: str = "stable") -> dict:
    fw = db.one("SELECT * FROM firmware WHERE channel=? ORDER BY created_at DESC LIMIT 1",
                (channel,))
    if not fw:
        raise HTTPException(404, "no firmware for channel")
    return fw


# ----- Networks ------------------------------------------------------------ #
@router.get("/networks")
def list_networks(tenant_id: str = Depends(get_tenant),
                  _p: dict = Depends(require_permission("devices"))) -> list[dict]:
    return db.query("SELECT * FROM networks WHERE tenant_id=? ORDER BY ssid", (tenant_id,))


class NetworkCreate(BaseModel):
    ssid: str
    security: str = "wpa2"
    band: str = "2.4GHz"
    password: str | None = None
    is_requested: bool = False


@router.post("/networks", status_code=201)
def create_network(body: NetworkCreate, tenant_id: str = Depends(get_tenant),
                   principal: dict = Depends(require_permission("devices"))) -> dict:
    nid = db.new_id("net")
    db.execute("""INSERT INTO networks(id,ssid,security,is_open,is_requested,band,password,
                  tenant_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
               (nid, body.ssid, body.security, int(body.security == "open"),
                int(body.is_requested), body.band, body.password, tenant_id, db.now()))
    return db.one("SELECT * FROM networks WHERE id=?", (nid,))
