"""Developer webhook subscriptions: CRUD, secret rotation, live test, delivery log."""
from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..deps import audit, get_tenant, require_auth, require_permission
from ..services import webhooks as wh
from collections.abc import Iterable

router = APIRouter(prefix="/v1", tags=["webhooks"])


def _mask(secret: str) -> str:
    return (secret[:10] + "…") if secret else ""


def _hook_view(h: dict, *, reveal: bool = False) -> dict:
    events = [e for e in (h.get("events") or "").split(",") if e]
    out = {
        "id": h["id"],
        "url": h["url"],
        "events": events,
        "enabled": bool(h.get("enabled")),
        "secret": h["secret"] if reveal else _mask(h.get("secret") or ""),
        "created_at": h.get("created_at"),
        "last_status": h.get("last_status"),
        "last_attempt_at": h.get("last_attempt_at"),
    }
    return out


class HookIn(BaseModel):
    url: str
    events: list[str] = Field(default_factory=list)


class HookPatch(BaseModel):
    url: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None


def _clean_events(events: Iterable[str]) -> str:
    bad = [e for e in events if e not in wh.EVENTS]
    if bad:
        raise HTTPException(422, "unknown event(s): %s. see GET /v1/webhooks/events" % ", ".join(bad))
    return ",".join(sorted(set(events)))


@router.get("/webhooks/events")
def list_event_types() -> dict:
    """Catalogue of events a subscription can receive."""
    return {"events": list(wh.EVENTS)}


@router.get("/webhooks")
def list_hooks(tenant_id: str = Depends(get_tenant),
               _p: dict = Depends(require_permission("webhooks"))) -> list[dict]:
    rows = db.query("SELECT * FROM webhooks WHERE tenant_id=? ORDER BY created_at DESC",
                    (tenant_id,))
    return [_hook_view(r) for r in rows]


@router.post("/webhooks", status_code=201)
def create_hook(body: HookIn, tenant_id: str = Depends(get_tenant),
                principal: dict = Depends(require_permission("webhooks"))) -> dict:
    try:
        url = wh.validate_url(body.url, allow_http=_allow_http())
    except wh.WebhookError as e:
        raise HTTPException(422, str(e))
    events = _clean_events(body.events or list(wh.EVENTS))
    hook_id = db.new_id("whk")
    secret = "whsec_" + secrets.token_hex(24)
    db.execute("""INSERT INTO webhooks(id,tenant_id,url,secret,events,enabled,created_at)
                  VALUES(?,?,?,?,?,1,?)""", (hook_id, tenant_id, url, secret, events, db.now()))
    audit(tenant_id, principal.get("user_id"), "webhook.create", "webhook", hook_id,
          {"url": url, "events": events})
    hook = db.one("SELECT * FROM webhooks WHERE id=?", (hook_id,))
    out = _hook_view(hook, reveal=True)
    out["warning"] = "store this secret now - it is not shown again"
    return out


@router.get("/webhooks/{hook_id}")
def get_hook(hook_id: str, tenant_id: str = Depends(get_tenant),
             _p: dict = Depends(require_permission("webhooks"))) -> dict:
    h = _own(hook_id, tenant_id)
    return _hook_view(h)


@router.patch("/webhooks/{hook_id}")
def patch_hook(hook_id: str, body: HookPatch, tenant_id: str = Depends(get_tenant),
               principal: dict = Depends(require_permission("webhooks"))) -> dict:
    h = _own(hook_id, tenant_id)
    fields: dict = {}
    if body.url is not None:
        try:
            fields["url"] = wh.validate_url(body.url, allow_http=_allow_http())
        except wh.WebhookError as e:
            raise HTTPException(422, str(e))
    if body.events is not None:
        fields["events"] = _clean_events(body.events)
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if not fields:
        return _hook_view(h)
    sets = ", ".join("%s=?" % k for k in fields)
    db.execute("UPDATE webhooks SET %s WHERE id=? AND tenant_id=?" % sets,
               tuple(fields.values()) + (hook_id, tenant_id))
    audit(tenant_id, principal.get("user_id"), "webhook.update", "webhook", hook_id, fields)
    return _hook_view(db.one("SELECT * FROM webhooks WHERE id=?", (hook_id,)))


@router.delete("/webhooks/{hook_id}", status_code=204)
def delete_hook(hook_id: str, tenant_id: str = Depends(get_tenant),
                principal: dict = Depends(require_permission("webhooks"))) -> None:
    _own(hook_id, tenant_id)
    db.execute("DELETE FROM webhooks WHERE id=? AND tenant_id=?", (hook_id, tenant_id))
    audit(tenant_id, principal.get("user_id"), "webhook.delete", "webhook", hook_id)


@router.post("/webhooks/{hook_id}/rotate")
def rotate_secret(hook_id: str, tenant_id: str = Depends(get_tenant),
                  principal: dict = Depends(require_permission("webhooks"))) -> dict:
    _own(hook_id, tenant_id)
    secret = "whsec_" + secrets.token_hex(24)
    db.execute("UPDATE webhooks SET secret=? WHERE id=? AND tenant_id=?",
               (secret, hook_id, tenant_id))
    audit(tenant_id, principal.get("user_id"), "webhook.rotate", "webhook", hook_id)
    return {"id": hook_id, "secret": secret,
            "warning": "the previous secret stops working immediately"}


@router.post("/webhooks/{hook_id}/test")
def test_hook(hook_id: str, tenant_id: str = Depends(get_tenant),
              _p: dict = Depends(require_permission("webhooks"))) -> dict:
    """Send one signed event now and return the endpoint's real status code."""
    h = _own(hook_id, tenant_id)
    try:
        wh.validate_url(h["url"], allow_http=_allow_http())
    except wh.WebhookError as e:
        raise HTTPException(422, str(e))
    result = wh.deliver(h, "webhook.test", {"message": "GBox webhook test",
                                            "webhook_id": hook_id}, attempts=1)
    return result


@router.get("/webhooks/{hook_id}/deliveries")
def deliveries(hook_id: str, tenant_id: str = Depends(get_tenant),
               _p: dict = Depends(require_permission("webhooks")),
               limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
    _own(hook_id, tenant_id)
    rows = db.query("""SELECT id,event,status,attempts,status_code,error,created_at,
                              delivered_at FROM webhook_deliveries
                       WHERE webhook_id=? ORDER BY created_at DESC LIMIT ?""",
                    (hook_id, limit))
    return rows


@router.post("/webhooks/{hook_id}/deliveries/{delivery_id}/redeliver")
def redeliver(hook_id: str, delivery_id: str, tenant_id: str = Depends(get_tenant),
              _p: dict = Depends(require_permission("webhooks"))) -> dict:
    """Replay a stored delivery body - safe because the body is immutable."""
    h = _own(hook_id, tenant_id)
    row = db.one("SELECT * FROM webhook_deliveries WHERE id=? AND webhook_id=?",
                 (delivery_id, hook_id))
    if not row:
        raise HTTPException(404, "delivery not found")
    if not row.get("request_body"):
        raise HTTPException(409, "this delivery has no stored body")
    payload = json.loads(row["request_body"]).get("data", {})
    return wh.deliver(h, row["event"], payload, attempts=1)


def _own(hook_id: str, tenant_id: str) -> dict:
    h = db.one("SELECT * FROM webhooks WHERE id=? AND tenant_id=?", (hook_id, tenant_id))
    if not h:
        raise HTTPException(404, "webhook not found")
    return h


def _allow_http() -> bool:
    import os
    return os.environ.get("VB_WEBHOOK_ALLOW_HTTP", "0") == "1"
