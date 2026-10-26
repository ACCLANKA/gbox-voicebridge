"""Live bridge control and CDR endpoints.

A *bridge* is one audio path: ESP32 (Bluetooth/HFP/SCO) <-> cloud <-> destination
(SIP/PSTN/WebRTC/webhook). Starting a bridge selects a route, records QoS, and
on stop writes a CDR + usage record (see services/billing.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..deps import audit, get_tenant, require_permission
from ..services import billing, events, routing

router = APIRouter(prefix="/v1", tags=["bridges"])


class BridgeStart(BaseModel):
    device_id: str
    direction: str = "inbound"
    from_number: str | None = None
    to_number: str | None = None
    route_id: str | None = None
    codec: str = "opus"
    record: bool = False
    agent_id: str | None = None


@router.post("/bridges", status_code=201)
def start_bridge(body: BridgeStart, tenant_id: str = Depends(get_tenant),
                 principal: dict = Depends(require_permission("calls.control"))) -> dict:
    device = db.one("SELECT * FROM devices WHERE (id=? OR serial=?) AND tenant_id=?",
                    (body.device_id, body.device_id, tenant_id))
    if not device:
        raise HTTPException(404, "device not found")
    if device["status"] == "offline":
        raise HTTPException(409, "device offline")
    if body.route_id:
        route = routing.route_view(db.one("SELECT * FROM routes WHERE id=? AND tenant_id=?",
                                          (body.route_id, tenant_id)))
        if not route:
            raise HTTPException(404, "route not found")
    else:
        route = routing.select_route(tenant_id, device, body.direction, body.from_number)

    spend = billing.spend_status(tenant_id)
    if spend.get("suspended"):
        raise HTTPException(402, "tenant suspended: balance below credit limit")

    bridge_id = db.new_id("br")
    ts = db.now()
    route_id = route["id"] if route else None
    dest_uri = (route or {}).get("destination", {}).get("uri")
    db.execute("""INSERT INTO bridges(id,tenant_id,device_id,project_id,route_id,direction,
                  from_number,to_number,state,codec,started_at,agent_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
               (bridge_id, tenant_id, device["id"], device.get("project_id"), route_id,
                body.direction, body.from_number, body.to_number or dest_uri,
                "initiated", body.codec, ts, body.agent_id))
    # link any active call session to this bridge
    session = db.one("SELECT * FROM call_sessions WHERE device_id=? AND ended_at IS NULL "
                     "ORDER BY started_at DESC LIMIT 1", (device["id"],))
    if session:
        db.execute("UPDATE call_sessions SET bridge_id=?, route_id=?, route=?, state='bridged' "
                   "WHERE id=?",
                   (bridge_id, route_id, routing.route_description(route), session["id"]))
    events.emit(tenant_id, "bridge.started",
                {"bridge_id": bridge_id, "device_id": device["id"], "route_id": route_id,
                 "route": routing.route_description(route)})
    audit(tenant_id, principal.get("user_id"), "bridge.start", "bridge", bridge_id)
    return _bridge_view(db.one("SELECT * FROM bridges WHERE id=?", (bridge_id,)))


@router.post("/bridges/{bridge_id}/answer")
def answer_bridge(bridge_id: str, tenant_id: str = Depends(get_tenant),
                  _p: dict = Depends(require_permission("calls.control"))) -> dict:
    b = _get(bridge_id, tenant_id)
    db.execute("UPDATE bridges SET state='active', answered_at=? WHERE id=?",
               (db.now(), bridge_id))
    return _bridge_view(db.one("SELECT * FROM bridges WHERE id=?", (bridge_id,)))


@router.post("/bridges/{bridge_id}/stop")
def stop_bridge(bridge_id: str, tenant_id: str = Depends(get_tenant),
                principal: dict = Depends(require_permission("calls.control"))) -> dict:
    b = _get(bridge_id, tenant_id)
    if b["state"] == "ended":
        cdr = db.one("SELECT * FROM cdr WHERE bridge_id=?", (bridge_id,))
        return {"bridge": _bridge_view(b), "cdr": cdr}
    end = db.now()
    duration = _duration(b.get("answered_at") or b.get("started_at"), end)
    db.execute("""UPDATE bridges SET state='ended', ended_at=?, duration_sec=? WHERE id=?""",
               (end, duration, bridge_id))
    cdr = billing.record_bridge(bridge_id)
    db.execute("UPDATE call_sessions SET state='ended', ended_at=?, duration_sec=? "
               "WHERE bridge_id=?", (end, duration, bridge_id))
    audit(tenant_id, principal.get("user_id"), "bridge.stop", "bridge", bridge_id)
    return {"bridge": _bridge_view(db.one("SELECT * FROM bridges WHERE id=?", (bridge_id,))),
            "cdr": cdr}


class BridgePatch(BaseModel):
    state: str | None = None
    agent_id: str | None = None
    mos: float | None = None
    jitter_ms: float | None = None
    loss_pct: float | None = None


VALID_STATES = {"initiated", "ringing", "active", "held", "ended", "failed"}


@router.patch("/bridges/{bridge_id}")
def patch_bridge(bridge_id: str, body: BridgePatch, tenant_id: str = Depends(get_tenant),
                 _p: dict = Depends(require_permission("calls.control"))) -> dict:
    _get(bridge_id, tenant_id)
    data = body.model_dump(exclude_none=True)
    if data.get("state") and data["state"] not in VALID_STATES:
        raise HTTPException(400, "invalid state")
    if data:
        fields = ", ".join(f"{k}=?" for k in data)
        db.execute(f"UPDATE bridges SET {fields} WHERE id=?", (*data.values(), bridge_id))
    return _bridge_view(db.one("SELECT * FROM bridges WHERE id=?", (bridge_id,)))


@router.get("/bridges")
def list_bridges(tenant_id: str = Depends(get_tenant),
                 _p: dict = Depends(require_permission("calls")),
                 state: str | None = None, active: bool | None = None,
                 limit: int = Query(100, le=1000)) -> dict:
    sql = "SELECT * FROM bridges WHERE tenant_id=?"
    params: list = [tenant_id]
    if state:
        sql += " AND state=?"
        params.append(state)
    if active:
        sql += " AND ended_at IS NULL"
    sql += " ORDER BY started_at DESC LIMIT ?"
    rows = db.query(sql, (*params, limit))
    return {"items": [_bridge_view(b) for b in rows]}


@router.get("/bridges/{bridge_id}")
def get_bridge(bridge_id: str, tenant_id: str = Depends(get_tenant),
               _p: dict = Depends(require_permission("calls"))) -> dict:
    return _bridge_view(_get(bridge_id, tenant_id))


@router.get("/cdr")
def list_cdr(tenant_id: str = Depends(get_tenant),
             _p: dict = Depends(require_permission("cdr")),
             device_id: str | None = None, limit: int = Query(100, le=1000),
             offset: int = 0) -> dict:
    sql = "SELECT * FROM cdr WHERE tenant_id=?"
    params: list = [tenant_id]
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    total = db.one(f"SELECT COUNT(*) c FROM ({sql})", tuple(params))["c"]
    rows = db.query(sql + " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset))
    return {"items": rows, "total": total}


@router.get("/cdr/{cdr_id}")
def get_cdr(cdr_id: str, tenant_id: str = Depends(get_tenant),
            _p: dict = Depends(require_permission("cdr"))) -> dict:
    row = db.one("SELECT * FROM cdr WHERE id=? AND tenant_id=?", (cdr_id, tenant_id))
    if not row:
        raise HTTPException(404, "cdr not found")
    return row


def _get(bridge_id: str, tenant_id: str) -> dict:
    b = db.one("SELECT * FROM bridges WHERE id=? AND tenant_id=?", (bridge_id, tenant_id))
    if not b:
        raise HTTPException(404, "bridge not found")
    return b


def _bridge_view(b: dict) -> dict:
    v = dict(b)
    v["active"] = b.get("ended_at") is None
    return v


def _duration(start_iso: str | None, end_iso: str) -> int:
    from datetime import datetime, timezone
    if not start_iso:
        return 0
    try:
        s = datetime.fromisoformat(start_iso)
        e = datetime.fromisoformat(end_iso)
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc)
        if e.tzinfo is None:
            e = e.replace(tzinfo=timezone.utc)
        return max(0, int((e - s).total_seconds()))
    except ValueError:
        return 0
