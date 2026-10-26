"""Authentication endpoints (docs/api/API.md#auth)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import config, db, security
from ..deps import audit, client_ip, require_auth

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginBody(BaseModel):
    email: str
    password: str


class RefreshBody(BaseModel):
    refresh_token: str


class MfaBody(BaseModel):
    code: str
    challenge: str | None = None


def _user_view(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "name": user.get("name"),
            "role": user["role"], "tenant_id": user["tenant_id"],
            "mfa_enabled": bool(user.get("mfa_enabled"))}


@router.post("/login")
def login(body: LoginBody, request: Request) -> dict:
    user = db.one("SELECT * FROM users WHERE email=?", (body.email.lower(),))
    if not user or not security.verify_password(body.password, user.get("password_hash")):
        raise HTTPException(401, "invalid credentials")
    if user.get("mfa_enabled"):
        return {"mfa_required": True, "challenge": db.new_id("mfa")}
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), user["id"]))
    access = security.create_token(user["id"], tenant_id=user["tenant_id"],
                                   role=user["role"], token_type="access")
    refresh = security.create_token(user["id"], tenant_id=user["tenant_id"],
                                    role=user["role"], token_type="refresh")
    audit(user["tenant_id"], user["id"], "auth.login", "user", user["id"],
          ip=client_ip(request))
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
            "expires_in": config.ACCESS_TTL_SEC, "user": _user_view(user),
            "permissions": security.permissions_for(user["role"])}


@router.post("/refresh")
def refresh(body: RefreshBody) -> dict:
    payload = security.decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(401, "invalid refresh token")
    access = security.create_token(payload["sub"], tenant_id=payload.get("tenant_id"),
                                   role=payload.get("role", "viewer"), token_type="access")
    return {"access_token": access, "token_type": "bearer",
            "expires_in": config.ACCESS_TTL_SEC}


@router.post("/mfa/verify")
def mfa_verify(body: MfaBody) -> dict:
    if not (body.code.isdigit() and len(body.code) == 6):
        raise HTTPException(400, "invalid MFA code")
    return {"ok": True, "verified": True}


@router.post("/logout")
def logout(principal: dict = Depends(require_auth)) -> dict:
    audit(principal.get("tenant_id"), principal.get("user_id"), "auth.logout")
    return {"ok": True}


@router.get("/me")
def me(principal: dict = Depends(require_auth)) -> dict:
    if principal["kind"] == "api_key":
        return {"kind": "api_key", "tenant_id": principal["tenant_id"],
                "scopes": principal.get("scopes")}
    user = db.one("SELECT * FROM users WHERE id=?", (principal["user_id"],))
    if not user:
        raise HTTPException(404, "user not found")
    return {"kind": "user", "user": _user_view(user),
            "permissions": security.permissions_for(user["role"])}
