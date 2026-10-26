"""Routing configuration endpoints (docs/design/ROUTING-INTEGRATIONS.md)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from ..deps import audit, get_tenant, require_permission
from ..services import routing

router = APIRouter(prefix="/v1/routes", tags=["routes"])

DEST_TYPES = {"sip", "pstn", "webrtc", "webhook", "device", "recording"}


class Destination(BaseModel):
    type: str
    uri: str


class RouteCreate(BaseModel):
    name: str
    project_id: str | None = None
    priority: int = 100
    match: dict = {}
    destination: Destination
    options: dict = {}
    fallback: dict | None = None
    enabled: bool = True


class RoutePatch(BaseModel):
    name: str | None = None
    project_id: str | None = None
    priority: int | None = None
    match: dict | None = None
    destination: Destination | None = None
    options: dict | None = None
    fallback: dict | None = None
    enabled: bool | None = None


def _store(data: dict) -> dict:
    return {"match_json": json.dumps(data.get("match", {})),
            "options_json": json.dumps(data.get("options", {})),
            "fallback_json": json.dumps(data["fallback"]) if data.get("fallback") else None}


@router.get("")
def list_routes(tenant_id: str = Depends(get_tenant),
                _p: dict = Depends(require_permission("routes"))) -> list[dict]:
    rows = db.query("SELECT * FROM routes WHERE tenant_id=? ORDER BY priority DESC", (tenant_id,))
    return [routing.route_view(r) for r in rows]


@router.post("", status_code=201)
def create_route(body: RouteCreate, tenant_id: str = Depends(get_tenant),
                 principal: dict = Depends(require_permission("routes"))) -> dict:
    if body.destination.type not in DEST_TYPES:
        raise HTTPException(400, f"unsupported destination type '{body.destination.type}'")
    rid = db.new_id("rt")
    db.execute("""INSERT INTO routes(id,tenant_id,project_id,name,priority,match_json,
                  dest_type,dest_uri,options_json,fallback_json,enabled,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
               (rid, tenant_id, body.project_id, body.name, body.priority,
                json.dumps(body.match), body.destination.type, body.destination.uri,
                json.dumps(body.options),
                json.dumps(body.fallback) if body.fallback else None,
                int(body.enabled), db.now()))
    audit(tenant_id, principal.get("user_id"), "route.create", "route", rid)
    return routing.route_view(db.one("SELECT * FROM routes WHERE id=?", (rid,)))


@router.get("/{route_id}")
def get_route(route_id: str, tenant_id: str = Depends(get_tenant),
              _p: dict = Depends(require_permission("routes"))) -> dict:
    r = db.one("SELECT * FROM routes WHERE id=? AND tenant_id=?", (route_id, tenant_id))
    if not r:
        raise HTTPException(404, "route not found")
    return routing.route_view(r)


@router.patch("/{route_id}")
def patch_route(route_id: str, body: RoutePatch, tenant_id: str = Depends(get_tenant),
                principal: dict = Depends(require_permission("routes"))) -> dict:
    r = db.one("SELECT * FROM routes WHERE id=? AND tenant_id=?", (route_id, tenant_id))
    if not r:
        raise HTTPException(404, "route not found")
    data = body.model_dump(exclude_none=True)
    updates: dict = {}
    if "name" in data:
        updates["name"] = data["name"]
    if "project_id" in data:
        updates["project_id"] = data["project_id"]
    if "priority" in data:
        updates["priority"] = data["priority"]
    if "match" in data:
        updates["match_json"] = json.dumps(data["match"])
    if "options" in data:
        updates["options_json"] = json.dumps(data["options"])
    if "fallback" in data:
        updates["fallback_json"] = json.dumps(data["fallback"]) if data["fallback"] else None
    if "enabled" in data:
        updates["enabled"] = int(data["enabled"])
    if "destination" in data:
        if data["destination"]["type"] not in DEST_TYPES:
            raise HTTPException(400, "unsupported destination type")
        updates["dest_type"] = data["destination"]["type"]
        updates["dest_uri"] = data["destination"]["uri"]
    if updates:
        fields = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE routes SET {fields} WHERE id=?", (*updates.values(), route_id))
    return routing.route_view(db.one("SELECT * FROM routes WHERE id=?", (route_id,)))


@router.delete("/{route_id}", status_code=204)
def delete_route(route_id: str, tenant_id: str = Depends(get_tenant),
                 principal: dict = Depends(require_permission("routes"))) -> None:
    r = db.one("SELECT id FROM routes WHERE id=? AND tenant_id=?", (route_id, tenant_id))
    if not r:
        raise HTTPException(404, "route not found")
    db.execute("DELETE FROM routes WHERE id=?", (route_id,))
    audit(tenant_id, principal.get("user_id"), "route.delete", "route", route_id)


class RouteTestBody(BaseModel):
    device_id: str
    direction: str = "inbound"
    caller_number: str | None = None


@router.post("/{route_id}/test")
def test_route(route_id: str, body: RouteTestBody, tenant_id: str = Depends(get_tenant),
               _p: dict = Depends(require_permission("routes"))) -> dict:
    r = db.one("SELECT id FROM routes WHERE id=? AND tenant_id=?", (route_id, tenant_id))
    if not r:
        raise HTTPException(404, "route not found")
    device = db.one("SELECT * FROM devices WHERE (id=? OR serial=?) AND tenant_id=?",
                    (body.device_id, body.device_id, tenant_id))
    if not device:
        raise HTTPException(404, "device not found")
    result = routing.test_route(route_id, device, body.direction, body.caller_number)
    return result


class ReorderBody(BaseModel):
    order: list[str]


@router.post("/reorder")
def reorder_routes(body: ReorderBody, tenant_id: str = Depends(get_tenant),
                   principal: dict = Depends(require_permission("routes"))) -> list[dict]:
    total = len(body.order)
    for idx, rid in enumerate(body.order):
        db.execute("UPDATE routes SET priority=? WHERE id=? AND tenant_id=?",
                   (total - idx, rid, tenant_id))
    rows = db.query("SELECT * FROM routes WHERE tenant_id=? ORDER BY priority DESC", (tenant_id,))
    return [routing.route_view(r) for r in rows]
