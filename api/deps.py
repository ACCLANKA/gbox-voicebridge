"""FastAPI dependencies: authentication, RBAC, tenant scoping, request helpers."""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, Header, HTTPException, Request

from . import db, security


def client_ip(request: Request) -> str | None:
    if request.client:
        return request.client.host
    return None


def current_principal(request: Request,
                      authorization: str | None = Header(default=None)) -> dict | None:
    """Resolve the caller from ``Authorization: Bearer <jwt|api-key>``."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    token = token.strip()
    if token.startswith("vb_"):
        key = db.one("SELECT * FROM api_keys WHERE key_hash=?",
                     (security.hash_api_key(token),))
        if not key:
            return None
        if key.get("expires_at") and key["expires_at"] < db.now():
            return None
        db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (db.now(), key["id"]))
        return {"kind": "api_key", "user_id": None, "tenant_id": key["tenant_id"],
                "role": "admin", "scopes": (key.get("scopes") or "").split(","),
                "api_key_id": key["id"]}
    payload = security.decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    return {"kind": "user", "user_id": payload.get("sub"),
            "tenant_id": payload.get("tenant_id"), "role": payload.get("role", "viewer"),
            "scopes": []}


def require_auth(principal: dict | None = Depends(current_principal)) -> dict:
    if not principal:
        raise HTTPException(401, "authentication required")
    return principal


def require_permission(permission: str) -> Callable:
    """Dependency factory enforcing the RBAC permission matrix."""
    def _dep(principal: dict = Depends(require_auth)) -> dict:
        if principal["kind"] == "api_key":
            scopes = set(principal.get("scopes") or [])
            if "admin" in scopes or "*" in scopes or permission in scopes:
                return principal
            raise HTTPException(403, f"api key missing scope '{permission}'")
        if not security.has_permission(principal["role"], permission):
            raise HTTPException(
                403, f"role '{principal['role']}' lacks permission '{permission}'")
        return principal
    return _dep


def get_tenant(principal: dict = Depends(require_auth)) -> str:
    tenant_id = principal.get("tenant_id")
    if not tenant_id:
        raise HTTPException(403, "no tenant bound to principal")
    return tenant_id


def require_platform_admin(principal: dict = Depends(require_auth)) -> dict:
    tenant = db.one("SELECT * FROM tenants WHERE id=?", (principal.get("tenant_id"),))
    if principal["kind"] == "api_key" and "admin" in set(principal.get("scopes") or []):
        return principal
    if principal["kind"] == "user" and "admin" in security.permissions_for(principal["role"]):
        return principal
    raise HTTPException(403, "platform admin privileges required")


def audit(tenant_id: str | None, actor_user_id: str | None, action: str,
          target_type: str | None = None, target_id: str | None = None,
          metadata: dict | None = None, ip: str | None = None) -> None:
    import json
    db.execute("""INSERT INTO audit_logs(id,tenant_id,actor_user_id,action,target_type,
                  target_id,metadata_json,ip,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
               (db.new_id("aud"), tenant_id, actor_user_id, action, target_type,
                target_id, json.dumps(metadata or {}), ip, db.now()))
