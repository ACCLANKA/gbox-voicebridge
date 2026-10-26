"""Tenant profile, users, projects and audit log endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, security
from ..deps import audit, get_tenant, require_permission

router = APIRouter(prefix="/v1", tags=["tenants"])


def _tenant_view(t: dict) -> dict:
    devices = db.one("SELECT COUNT(*) c FROM devices WHERE tenant_id=?", (t["id"],))["c"]
    projects = db.one("SELECT COUNT(*) c FROM projects WHERE tenant_id=?", (t["id"],))["c"]
    users = db.one("SELECT COUNT(*) c FROM users WHERE tenant_id=?", (t["id"],))["c"]
    return {**t, "counts": {"devices": devices, "projects": projects, "users": users}}


@router.get("/tenant")
def get_tenant_profile(tenant_id: str = Depends(get_tenant)) -> dict:
    t = db.one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
    if not t:
        raise HTTPException(404, "tenant not found")
    return _tenant_view(t)


# ----- Users --------------------------------------------------------------- #
class UserCreate(BaseModel):
    email: str
    name: str = ""
    role: str = "viewer"
    password: str = Field(default="password123", min_length=6)


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    mfa_enabled: bool | None = None


def _public_user(row: dict) -> dict:
    """Serialise a user row, coercing the SQLite 0/1 flag to a JSON boolean."""
    row["mfa_enabled"] = bool(row["mfa_enabled"])
    return row


@router.get("/users")
def list_users(tenant_id: str = Depends(get_tenant),
               _p: dict = Depends(require_permission("users"))) -> list[dict]:
    rows = db.query("SELECT id,email,name,role,mfa_enabled,last_login_at,created_at "
                    "FROM users WHERE tenant_id=? ORDER BY created_at", (tenant_id,))
    return [_public_user(r) for r in rows]


@router.post("/users", status_code=201)
def create_user(body: UserCreate, tenant_id: str = Depends(get_tenant),
                principal: dict = Depends(require_permission("users"))) -> dict:
    if body.role not in security.ROLES:
        raise HTTPException(400, f"unknown role '{body.role}'")
    if db.one("SELECT id FROM users WHERE email=?", (body.email.lower(),)):
        raise HTTPException(409, "email already registered")
    uid = db.new_id("usr")
    db.execute("""INSERT INTO users(id,tenant_id,email,password_hash,name,role,
                  mfa_enabled,created_at) VALUES(?,?,?,?,?,?,0,?)""",
               (uid, tenant_id, body.email.lower(), security.hash_password(body.password),
                body.name, body.role, db.now()))
    audit(tenant_id, principal.get("user_id"), "user.create", "user", uid)
    return _public_user(db.one(
        "SELECT id,email,name,role,mfa_enabled,created_at FROM users WHERE id=?", (uid,)))


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, tenant_id: str = Depends(get_tenant),
               principal: dict = Depends(require_permission("users"))) -> dict:
    user = db.one("SELECT * FROM users WHERE id=? AND tenant_id=?", (user_id, tenant_id))
    if not user:
        raise HTTPException(404, "user not found")
    data = body.model_dump(exclude_none=True)
    if data.get("role") and data["role"] not in security.ROLES:
        raise HTTPException(400, f"unknown role '{data['role']}'")
    if data:
        fields = ", ".join(f"{k}=?" for k in data)
        db.execute(f"UPDATE users SET {fields} WHERE id=?",
                   (*[int(v) if isinstance(v, bool) else v for v in data.values()], user_id))
    audit(tenant_id, principal.get("user_id"), "user.update", "user", user_id, data)
    return _public_user(db.one(
        "SELECT id,email,name,role,mfa_enabled,created_at FROM users WHERE id=?", (user_id,)))


# ----- Projects ------------------------------------------------------------ #
class ProjectCreate(BaseModel):
    name: str
    description: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None


def _project_view(p: dict) -> dict:
    p["devices"] = db.one("SELECT COUNT(*) c FROM devices WHERE project_id=?", (p["id"],))["c"]
    p["routes"] = db.one("SELECT COUNT(*) c FROM routes WHERE project_id=?", (p["id"],))["c"]
    return p


@router.get("/projects")
def list_projects(tenant_id: str = Depends(get_tenant),
                  _p: dict = Depends(require_permission("devices"))) -> list[dict]:
    return [_project_view(p) for p in
            db.query("SELECT * FROM projects WHERE tenant_id=? ORDER BY created_at", (tenant_id,))]


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, tenant_id: str = Depends(get_tenant),
                   principal: dict = Depends(require_permission("devices"))) -> dict:
    pid = db.new_id("prj")
    db.execute("INSERT INTO projects(id,tenant_id,name,description,created_at) VALUES(?,?,?,?,?)",
               (pid, tenant_id, body.name, body.description, db.now()))
    audit(tenant_id, principal.get("user_id"), "project.create", "project", pid)
    return _project_view(db.one("SELECT * FROM projects WHERE id=?", (pid,)))


@router.get("/projects/{project_id}")
def get_project(project_id: str, tenant_id: str = Depends(get_tenant),
                _p: dict = Depends(require_permission("devices"))) -> dict:
    p = db.one("SELECT * FROM projects WHERE id=? AND tenant_id=?", (project_id, tenant_id))
    if not p:
        raise HTTPException(404, "project not found")
    return _project_view(p)


@router.patch("/projects/{project_id}")
def patch_project(project_id: str, body: ProjectPatch, tenant_id: str = Depends(get_tenant),
                  principal: dict = Depends(require_permission("devices"))) -> dict:
    p = db.one("SELECT * FROM projects WHERE id=? AND tenant_id=?", (project_id, tenant_id))
    if not p:
        raise HTTPException(404, "project not found")
    data = body.model_dump(exclude_none=True)
    if data:
        fields = ", ".join(f"{k}=?" for k in data)
        db.execute(f"UPDATE projects SET {fields} WHERE id=?",
                   (*data.values(), project_id))
    return _project_view(db.one("SELECT * FROM projects WHERE id=?", (project_id,)))


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, tenant_id: str = Depends(get_tenant),
                   principal: dict = Depends(require_permission("devices"))) -> None:
    p = db.one("SELECT * FROM projects WHERE id=? AND tenant_id=?", (project_id, tenant_id))
    if not p:
        raise HTTPException(404, "project not found")
    db.execute("UPDATE devices SET project_id=NULL WHERE project_id=?", (project_id,))
    db.execute("UPDATE routes SET project_id=NULL WHERE project_id=?", (project_id,))
    db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    audit(tenant_id, principal.get("user_id"), "project.delete", "project", project_id)


# ----- Audit log ----------------------------------------------------------- #
@router.get("/audit-logs")
def list_audit_logs(tenant_id: str = Depends(get_tenant),
                    _p: dict = Depends(require_permission("users"))) -> list[dict]:
    return db.query("SELECT * FROM audit_logs WHERE tenant_id=? ORDER BY created_at DESC LIMIT 200",
                    (tenant_id,))
