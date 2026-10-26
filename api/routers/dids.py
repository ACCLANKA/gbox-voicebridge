"""DID inventory endpoints — inbound numbers a tenant owns.

See ``docs/design/PSTN-AND-DIDS.md`` (E.164 + inventory) and
``docs/design/ROUTING-INTEGRATIONS.md`` (destination model).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps import audit, get_tenant, require_permission
from ..services import dids, numbers

router = APIRouter(prefix="/v1/dids", tags=["dids"])


class DidCreate(BaseModel):
    number: str
    label: str | None = None
    country: str | None = None
    device_id: str | None = None
    project_id: str | None = None
    route_id: str | None = None
    capabilities: str = "voice"


class DidPatch(BaseModel):
    number: str | None = None
    label: str | None = None
    country: str | None = None
    device_id: str | None = None
    project_id: str | None = None
    route_id: str | None = None
    capabilities: str | None = None
    enabled: bool | None = None


class NumberBody(BaseModel):
    number: str
    country: str | None = None


class AssignBody(BaseModel):
    device_id: str | None = None
    project_id: str | None = None
    route_id: str | None = None


def _dupe_or_bad(exc: ValueError) -> None:
    raise HTTPException(409 if isinstance(exc, dids.DuplicateDid) else 400, str(exc))


@router.get("")
def list_dids(tenant_id: str = Depends(get_tenant),
              _p: dict = Depends(require_permission("routes"))) -> list[dict]:
    return dids.list_dids(tenant_id)


@router.post("", status_code=201)
def create_did(body: DidCreate, tenant_id: str = Depends(get_tenant),
               principal: dict = Depends(require_permission("routes"))) -> dict:
    try:
        did = dids.create(tenant_id, body.number, label=body.label, country=body.country,
                          device_id=body.device_id, project_id=body.project_id,
                          route_id=body.route_id, capabilities=body.capabilities)
    except ValueError as exc:
        _dupe_or_bad(exc)
    audit(tenant_id, principal.get("user_id"), "did.create", "did", did["id"])
    return did


@router.post("/validate")
def validate_number(body: NumberBody,
                    _p: dict = Depends(require_permission("routes"))) -> dict:
    """Normalise/validate a number without storing it."""
    return numbers.validate(body.number, body.country or "US")


@router.get("/{did_id}")
def get_did(did_id: str, tenant_id: str = Depends(get_tenant),
            _p: dict = Depends(require_permission("routes"))) -> dict:
    did = dids.get(tenant_id, did_id)
    if not did:
        raise HTTPException(404, "did not found")
    return did


@router.patch("/{did_id}")
def patch_did(did_id: str, body: DidPatch, tenant_id: str = Depends(get_tenant),
              principal: dict = Depends(require_permission("routes"))) -> dict:
    data = body.model_dump(exclude_none=True)
    try:
        did = dids.update(tenant_id, did_id, **data)
    except ValueError as exc:
        _dupe_or_bad(exc)
    if not did:
        raise HTTPException(404, "did not found")
    audit(tenant_id, principal.get("user_id"), "did.update", "did", did_id)
    return did


@router.post("/{did_id}/assign")
def assign_did(did_id: str, body: AssignBody, tenant_id: str = Depends(get_tenant),
               principal: dict = Depends(require_permission("routes"))) -> dict:
    """Attach a DID to a device / project / route."""
    did = dids.assign(tenant_id, did_id, device_id=body.device_id,
                      project_id=body.project_id, route_id=body.route_id)
    if not did:
        raise HTTPException(404, "did not found")
    audit(tenant_id, principal.get("user_id"), "did.assign", "did", did_id)
    return did


@router.delete("/{did_id}", status_code=204)
def delete_did(did_id: str, tenant_id: str = Depends(get_tenant),
               principal: dict = Depends(require_permission("routes"))) -> None:
    if not dids.delete(tenant_id, did_id):
        raise HTTPException(404, "did not found")
    audit(tenant_id, principal.get("user_id"), "did.delete", "did", did_id)
