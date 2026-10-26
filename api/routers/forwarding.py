"""Voice-forwarding endpoints — configure where a device's audio is sent.

See ``docs/design/VOICE-FORWARDING.md`` and ``docs/design/ROUTING-INTEGRATIONS.md``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..deps import get_tenant
from ..services import egress, forwarding

router = APIRouter(prefix="/v1/forwarding", tags=["forwarding"])


class Destination(BaseModel):
    dest_type: str = Field(..., description="sip | socket | webhook | recording | simulated")
    dest_uri: str = Field("", description="e.g. sip:1000@pbx:5060;transport=tls;register=1")
    options: dict = Field(default_factory=dict,
                          description="protocol options; see /v1/forwarding/capabilities")


def _device(tenant_id: str, device_id: str) -> dict:
    d = db.one("SELECT * FROM devices WHERE (id=? OR serial=?) AND tenant_id=?",
               (device_id, device_id, tenant_id))
    if not d:
        raise HTTPException(404, "device not found")
    return d


@router.get("/capabilities")
def capabilities() -> dict:
    """Supported destination types, transports and options (public, no secrets)."""
    return egress.capabilities()


@router.post("/validate")
def validate(body: Destination) -> dict:
    """Parse a destination without storing it (dashboard 'test' button)."""
    try:
        target = egress.parse_destination(body.dest_type, body.dest_uri, body.options)
    except egress.EgressError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "destination": target.as_dict()}


@router.get("/devices/{device_id}")
def get_forwarding(device_id: str, tenant_id: str = Depends(get_tenant)) -> dict:
    return forwarding.describe(_device(tenant_id, device_id))


@router.put("/devices/{device_id}")
def set_forwarding(device_id: str, body: Destination,
                   tenant_id: str = Depends(get_tenant)) -> dict:
    device = _device(tenant_id, device_id)
    try:
        return forwarding.set_override(device, body.dest_type, body.dest_uri, body.options)
    except egress.EgressError as exc:
        raise HTTPException(422, str(exc))


@router.delete("/devices/{device_id}", status_code=204)
def clear_forwarding(device_id: str, tenant_id: str = Depends(get_tenant)) -> None:
    forwarding.clear_override(_device(tenant_id, device_id))
