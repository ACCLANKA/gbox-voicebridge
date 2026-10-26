"""Call recording metadata + signed download URL endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..deps import audit, get_tenant, require_permission

router = APIRouter(prefix="/v1", tags=["recordings"])


@router.get("/recordings")
def list_recordings(tenant_id: str = Depends(get_tenant),
                    _p: dict = Depends(require_permission("recordings")),
                    device_id: str | None = None,
                    limit: int = Query(100, le=1000)) -> dict:
    sql = ("SELECT r.* FROM recordings r WHERE r.tenant_id=?")
    params: list = [tenant_id]
    rows = db.query(sql + " ORDER BY r.created_at DESC LIMIT ?", (*params, limit))
    return {"items": rows}


@router.get("/recordings/{recording_id}")
def get_recording(recording_id: str, tenant_id: str = Depends(get_tenant),
                  _p: dict = Depends(require_permission("recordings"))) -> dict:
    r = db.one("SELECT * FROM recordings WHERE id=? AND tenant_id=?",
               (recording_id, tenant_id))
    if not r:
        raise HTTPException(404, "recording not found")
    return r


@router.get("/recordings/{recording_id}/download")
def download_recording(recording_id: str, tenant_id: str = Depends(get_tenant),
                       _p: dict = Depends(require_permission("recordings"))) -> dict:
    r = db.one("SELECT * FROM recordings WHERE id=? AND tenant_id=?",
               (recording_id, tenant_id))
    if not r:
        raise HTTPException(404, "recording not found")
    from .. import security
    signed = security.create_token(f"rec:{recording_id}", tenant_id=tenant_id,
                                   role="recording", token_type="access", ttl=300)
    return {"recording_id": recording_id,
            "url": f"/v1/recordings/{recording_id}/media?token={signed}",
            "expires_in": 300}


class RetentionBody(BaseModel):
    retention_until: str


@router.patch("/recordings/{recording_id}/retention")
def set_retention(recording_id: str, body: RetentionBody,
                  tenant_id: str = Depends(get_tenant),
                  principal: dict = Depends(require_permission("recordings.manage"))) -> dict:
    r = db.one("SELECT * FROM recordings WHERE id=? AND tenant_id=?",
               (recording_id, tenant_id))
    if not r:
        raise HTTPException(404, "recording not found")
    db.execute("UPDATE recordings SET retention_until=? WHERE id=?",
               (body.retention_until, recording_id))
    audit(tenant_id, principal.get("user_id"), "recording.retention", "recording",
          recording_id, {"retention_until": body.retention_until})
    return db.one("SELECT * FROM recordings WHERE id=?", (recording_id,))


@router.delete("/recordings/{recording_id}", status_code=204)
def delete_recording(recording_id: str, tenant_id: str = Depends(get_tenant),
                     principal: dict = Depends(require_permission("recordings.manage"))) -> None:
    r = db.one("SELECT * FROM recordings WHERE id=? AND tenant_id=?",
               (recording_id, tenant_id))
    if not r:
        raise HTTPException(404, "recording not found")
    db.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
    audit(tenant_id, principal.get("user_id"), "recording.delete", "recording", recording_id)
