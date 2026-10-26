"""Call sessions, call-detection ingest and enrichment endpoints.

Implements the device-facing event contract in docs/api/CALL-EVENTS-API.md:
device events, companion-app enrichment, and the read model for the dashboard.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..deps import get_tenant, require_permission
from ..services import detection

router = APIRouter(prefix="/v1", tags=["calls"])

DEVICE_EVENTS = {"call.ringing", "call.answered", "call.ended", "call.direction",
                 "call.audio_link_up", "call.audio_link_down", "call.active",
                 "call.held", "call.resumed"}


class DeviceEvent(BaseModel):
    device_id: str
    event: str
    ts: str | None = None
    seq: int | None = None
    data: dict = {}


class DeviceEventBatch(BaseModel):
    events: list[DeviceEvent]


@router.post("/call-events")
def post_call_event(body: DeviceEvent, tenant_id: str = Depends(get_tenant),
                    _p: dict = Depends(require_permission("calls.control"))) -> dict:
    if body.event not in DEVICE_EVENTS:
        raise HTTPException(400, f"unsupported event '{body.event}'")
    device = db.one("SELECT * FROM devices WHERE (id=? OR serial=?) AND tenant_id=?",
                    (body.device_id, body.device_id, tenant_id))
    if not device:
        raise HTTPException(404, "device not found")
    return detection.ingest_device_event(tenant_id, device["id"], body.event, body.data,
                                         body.seq, body.ts)


@router.post("/call-events/batch")
def post_call_events_batch(body: DeviceEventBatch, tenant_id: str = Depends(get_tenant),
                           _p: dict = Depends(require_permission("calls.control"))) -> dict:
    results = []
    for ev in body.events:
        if ev.event not in DEVICE_EVENTS:
            results.append({"ack": False, "error": f"unsupported event '{ev.event}'"})
            continue
        device = db.one("SELECT * FROM devices WHERE (id=? OR serial=?) AND tenant_id=?",
                        (ev.device_id, ev.device_id, tenant_id))
        if not device:
            results.append({"ack": False, "error": "unknown device"})
            continue
        results.append(detection.ingest_device_event(tenant_id, device["id"], ev.event,
                                                      ev.data, ev.seq, ev.ts))
    return {"results": results}


class EnrichBody(BaseModel):
    device_id: str
    source_app: str | None = None
    caller_name: str | None = None
    caller_number: str | None = None
    direction: str | None = None
    confidence: float | None = None
    phone_ts: str | None = None


@router.post("/call/enrich")
def enrich_call(body: EnrichBody, tenant_id: str = Depends(get_tenant),
                _p: dict = Depends(require_permission("calls"))) -> dict:
    device = db.one("SELECT * FROM devices WHERE (id=? OR serial=?) AND tenant_id=?",
                    (body.device_id, body.device_id, tenant_id))
    if not device:
        raise HTTPException(404, "device not found")
    return detection.enrich(device["id"], source_app=body.source_app,
                            caller_name=body.caller_name, caller_number=body.caller_number,
                            direction=body.direction, confidence=body.confidence,
                            phone_ts=body.phone_ts)


@router.get("/calls")
def list_calls(tenant_id: str = Depends(get_tenant),
               _p: dict = Depends(require_permission("calls")),
               state: str | None = None, source: str | None = None,
               active: bool | None = None, limit: int = Query(100, le=1000),
               offset: int = 0) -> dict:
    sql = "SELECT * FROM call_sessions WHERE tenant_id=?"
    params: list = [tenant_id]
    if state:
        sql += " AND state=?"
        params.append(state)
    if source:
        sql += " AND source=?"
        params.append(source)
    if active:
        sql += " AND ended_at IS NULL"
    total = db.one(f"SELECT COUNT(*) c FROM ({sql})", tuple(params))["c"]
    rows = db.query(sql + " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset))
    return {"items": [detection.session_view(r) for r in rows], "total": total}


@router.get("/calls/active")
def active_calls(tenant_id: str = Depends(get_tenant),
                 _p: dict = Depends(require_permission("calls"))) -> dict:
    rows = db.query("SELECT * FROM call_sessions WHERE tenant_id=? AND ended_at IS NULL "
                    "ORDER BY started_at DESC", (tenant_id,))
    return {"items": [detection.session_view(r) for r in rows], "count": len(rows)}


@router.get("/calls/{call_id}")
def get_call(call_id: str, tenant_id: str = Depends(get_tenant),
             _p: dict = Depends(require_permission("calls"))) -> dict:
    row = db.one("SELECT * FROM call_sessions WHERE id=? AND tenant_id=?", (call_id, tenant_id))
    if not row:
        # fall back to the legacy `calls` table for seeded historical calls
        legacy = db.one("SELECT * FROM calls WHERE id=?", (call_id,))
        if not legacy:
            raise HTTPException(404, "call not found")
        return legacy
    view = detection.session_view(row)
    view["events"] = db.query("SELECT * FROM call_events WHERE call_session_id=? ORDER BY server_ts",
                              (call_id,))
    view["enrichments"] = db.query("SELECT * FROM enrichment_events WHERE call_session_id=? "
                                   "ORDER BY server_ts", (call_id,))
    return view
