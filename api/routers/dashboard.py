"""Dashboard aggregate + live streaming endpoints (docs/design/DASHBOARD.md).

Provides the single-shot ``/summary`` aggregate the SPA renders, plus an SSE
stream and a WebSocket carrying the same snapshot/event objects so the web app
can update live without polling.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect

from .. import db
from ..deps import get_tenant, require_permission
from ..services import billing, detection, events

router = APIRouter(prefix="/v1", tags=["dashboard"])


def summary_for(tenant_id: str) -> dict:
    tenant = db.one("SELECT * FROM tenants WHERE id=?", (tenant_id,)) or {}
    devices = db.query("SELECT * FROM devices WHERE tenant_id=?", (tenant_id,))
    online = [d for d in devices if d["status"] == "online"]
    degraded = [d for d in devices if d["status"] == "degraded"]
    offline = [d for d in devices if d["status"] == "offline"]
    active = db.query("SELECT * FROM call_sessions WHERE tenant_id=? AND ended_at IS NULL "
                      "ORDER BY started_at DESC", (tenant_id,))
    recent_calls = db.query("SELECT * FROM call_sessions WHERE tenant_id=? "
                            "ORDER BY started_at DESC LIMIT 20", (tenant_id,))
    cdrs = db.query("SELECT * FROM cdr WHERE tenant_id=? ORDER BY started_at DESC LIMIT 20",
                    (tenant_id,))
    routes = db.query("SELECT COUNT(*) c FROM routes WHERE tenant_id=? AND enabled=1",
                      (tenant_id,))[0]["c"]
    total_minutes = db.one("""SELECT SUM(quantity) q FROM usage_records
                              WHERE tenant_id=? AND metric='bridge_seconds'""", (tenant_id,))
    minutes = round((total_minutes["q"] or 0) / 60.0, 1) if total_minutes else 0.0
    spend = billing.spend_status(tenant_id)
    avg_mos_row = db.one("SELECT AVG(mos) m FROM telemetry WHERE device_id IN "
                         "(SELECT id FROM devices WHERE tenant_id=?)", (tenant_id,))
    return {
        "tenant": {"id": tenant.get("id"), "name": tenant.get("name"),
                   "plan": tenant.get("plan")},
        "kpis": {
            "devices_total": len(devices),
            "devices_online": len(online),
            "devices_degraded": len(degraded),
            "devices_offline": len(offline),
            "active_calls": len(active),
            "routes_enabled": routes,
            "bridge_minutes": minutes,
            "avg_mos": round(avg_mos_row["m"], 2) if avg_mos_row and avg_mos_row["m"] else None,
            "balance_cents": spend.get("balance_cents"),
            "currency": spend.get("currency"),
            "low_balance": spend.get("low_balance"),
        },
        "devices": devices,
        "active_calls": [detection.session_view(c) for c in active],
        "recent_calls": [detection.session_view(c) for c in recent_calls],
        "recent_cdr": cdrs,
        "health": device_health(tenant_id),
        "activity": events.recent(25),
        "generated_at": db.now(),
    }


def device_health(tenant_id: str) -> list[dict]:
    out = []
    for d in db.query("SELECT * FROM devices WHERE tenant_id=?", (tenant_id,)):
        tl = db.one("SELECT * FROM telemetry WHERE device_id=? ORDER BY ts DESC LIMIT 1",
                    (d["id"],))
        out.append({
            "device_id": d["id"], "name": d.get("name"), "status": d.get("status"),
            "rssi": (tl or {}).get("rssi"), "mos": (tl or {}).get("mos"),
            "jitter_ms": (tl or {}).get("jitter_ms"), "loss_pct": (tl or {}).get("loss_pct"),
            "fw": d.get("fw"),
        })
    return out


@router.get("/dashboard/summary")
def dashboard_summary(tenant_id: str = Depends(get_tenant),
                      _p: dict = Depends(require_permission("calls"))) -> dict:
    return summary_for(tenant_id)


@router.get("/dashboard/instances")
def dashboard_instances(tenant_id: str = Depends(get_tenant),
                        _p: dict = Depends(require_permission("calls"))) -> list[dict]:
    """Alias required by the SPA's sidebar (projects -> instances)."""
    return [{"id": p["id"], "name": p["name"], "tenant_id": p["tenant_id"]}
            for p in db.query("SELECT * FROM projects WHERE tenant_id=? ORDER BY name",
                              (tenant_id,))]


@router.get("/dashboard/stream")
async def dashboard_stream(request: Request,
                           tenant_id: str = Depends(get_tenant),
                           _p: dict = Depends(require_permission("calls")),
                           interval: float = Query(3.0, ge=0.5, le=60),
                           frames: int | None = Query(None, ge=1, le=10000)):
    """Server-Sent Events stream of dashboard snapshots.

    Emits one frame immediately, then repeats every ``interval`` seconds until
    the client disconnects. Checking ``is_disconnected()`` lets the generator
    terminate (rather than spin forever) once the client goes away.

    ``frames`` optionally caps the number of emitted frames and then closes the
    stream. It defaults to ``None`` (stream forever) and exists so finite
    consumers — health checks, snapshotting tools and tests using a blocking
    client — can read a bounded number of frames instead of hanging.
    """
    from fastapi.responses import StreamingResponse

    async def gen():
        sent = 0
        while True:
            payload = json.dumps(summary_for(tenant_id))
            yield f"event: summary\ndata: {payload}\n\n"
            sent += 1
            if frames is not None and sent >= frames:
                break
            if await request.is_disconnected():
                break
            await asyncio.sleep(interval)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket):
    """WebSocket variant of the SSE stream (token passed as a query param)."""
    from .. import security
    await ws.accept()
    token = ws.query_params.get("token", "")
    payload = security.decode_token(token)
    if not payload:
        await ws.send_json({"error": "unauthorized"})
        await ws.close(code=4401)
        return
    tenant_id = payload.get("tenant_id")
    try:
        while True:
            await ws.send_json(summary_for(tenant_id))
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        return
    except Exception:
        await ws.close()
