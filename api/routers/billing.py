"""Billing, usage, invoices, plans and API-key endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import db, security
from ..deps import audit, get_tenant, require_permission
from ..services import billing

router = APIRouter(prefix="/v1", tags=["billing"])


# ----- Plans --------------------------------------------------------------- #
@router.get("/plans")
def list_plans(_tenant: str = Depends(get_tenant)) -> list[dict]:
    return [billing.plan_view(p) for p in
            db.query("SELECT * FROM plans WHERE is_public=1 ORDER BY base_price_cents")]


# ----- Subscription -------------------------------------------------------- #
@router.get("/subscription")
def get_subscription(tenant_id: str = Depends(get_tenant),
                     _p: dict = Depends(require_permission("billing"))) -> dict:
    sub = billing.current_subscription(tenant_id)
    plan = billing.effective_plan(tenant_id)
    return {"subscription": sub, "plan": plan}


class SubscribeBody(BaseModel):
    plan_id: str
    quantity: int = 1


@router.post("/subscription")
def set_subscription(body: SubscribeBody, tenant_id: str = Depends(get_tenant),
                     principal: dict = Depends(require_permission("billing"))) -> dict:
    plan = db.one("SELECT * FROM plans WHERE id=?", (body.plan_id,))
    if not plan:
        raise HTTPException(404, "plan not found")
    db.execute("UPDATE subscriptions SET status='canceled' WHERE tenant_id=?", (tenant_id,))
    sid = db.new_id("sub")
    db.execute("""INSERT INTO subscriptions(id,tenant_id,plan_id,status,period_start,
                  period_end,quantity,price_cents,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
               (sid, tenant_id, body.plan_id, "active", db.now(), db.now(),
                body.quantity, plan["base_price_cents"] * body.quantity, db.now()))
    db.execute("UPDATE tenants SET plan=? WHERE id=?", (plan["name"].lower(), tenant_id))
    audit(tenant_id, principal.get("user_id"), "subscription.change", "plan", body.plan_id)
    return {"subscription": db.one("SELECT * FROM subscriptions WHERE id=?", (sid,)),
            "plan": billing.plan_view(plan)}


# ----- Usage --------------------------------------------------------------- #
@router.get("/usage")
def get_usage(tenant_id: str = Depends(get_tenant),
              _p: dict = Depends(require_permission("billing")),
              period: str = "month") -> dict:
    return {"summary": billing.usage_summary(tenant_id),
            "rollups": db.query("""SELECT * FROM usage_rollups WHERE tenant_id=? AND period=?
                                   ORDER BY bucket DESC LIMIT 200""", (tenant_id, period))}


class UsageBody(BaseModel):
    metric: str
    quantity: float
    unit: str = "count"
    project_id: str | None = None
    device_id: str | None = None
    idempotency_key: str | None = None


@router.post("/usage", status_code=201)
def record_usage(body: UsageBody, tenant_id: str = Depends(get_tenant),
                 _p: dict = Depends(require_permission("billing"))) -> dict:
    return billing.record_usage(tenant_id, body.metric, body.quantity, body.unit,
                                project_id=body.project_id, device_id=body.device_id,
                                idempotency_key=body.idempotency_key)


# ----- Invoices ------------------------------------------------------------ #
@router.get("/invoices")
def list_invoices(tenant_id: str = Depends(get_tenant),
                  _p: dict = Depends(require_permission("billing")),
                  status: str | None = None) -> dict:
    sql = "SELECT * FROM invoices WHERE tenant_id=?"
    params: list = [tenant_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    rows = db.query(sql + " ORDER BY created_at DESC", tuple(params))
    return {"items": [billing.invoice_view(r) for r in rows]}


class InvoiceIssue(BaseModel):
    period_start: str | None = None
    period_end: str | None = None
    tax_rate: float = 0.16


@router.post("/invoices", status_code=201)
def create_invoice(body: InvoiceIssue, tenant_id: str = Depends(get_tenant),
                   principal: dict = Depends(require_permission("billing"))) -> dict:
    inv = billing.issue_invoice(tenant_id, period_start=body.period_start,
                                period_end=body.period_end, tax_rate=body.tax_rate)
    audit(tenant_id, principal.get("user_id"), "invoice.issue", "invoice", inv["id"])
    return inv


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, tenant_id: str = Depends(get_tenant),
                _p: dict = Depends(require_permission("billing"))) -> dict:
    inv = db.one("SELECT * FROM invoices WHERE id=? AND tenant_id=?", (invoice_id, tenant_id))
    if not inv:
        raise HTTPException(404, "invoice not found")
    return billing.invoice_view(inv)


@router.post("/invoices/{invoice_id}/pay")
def pay_invoice(invoice_id: str, tenant_id: str = Depends(get_tenant),
                principal: dict = Depends(require_permission("billing"))) -> dict:
    inv = db.one("SELECT * FROM invoices WHERE id=? AND tenant_id=?", (invoice_id, tenant_id))
    if not inv:
        raise HTTPException(404, "invoice not found")
    result = billing.mark_invoice_paid(invoice_id)
    audit(tenant_id, principal.get("user_id"), "invoice.pay", "invoice", invoice_id)
    return result


# ----- Balance / spend ----------------------------------------------------- #
@router.get("/balance")
def get_balance(tenant_id: str = Depends(get_tenant),
                _p: dict = Depends(require_permission("billing"))) -> dict:
    return billing.spend_status(tenant_id)


class CreditBody(BaseModel):
    amount_cents: int


@router.post("/credits")
def add_credits(body: CreditBody, tenant_id: str = Depends(get_tenant),
                principal: dict = Depends(require_permission("billing"))) -> dict:
    result = billing.grant_credits(tenant_id, body.amount_cents)
    audit(tenant_id, principal.get("user_id"), "billing.credit", "tenant", tenant_id,
          {"amount_cents": body.amount_cents})
    return result


# ----- API keys ------------------------------------------------------------ #
class ApiKeyBody(BaseModel):
    name: str
    scopes: list[str] = ["admin"]


@router.get("/api-keys")
def list_api_keys(tenant_id: str = Depends(get_tenant),
                  _p: dict = Depends(require_permission("admin"))) -> list[dict]:
    return db.query("SELECT id,tenant_id,name,prefix,scopes,last_used_at,expires_at,created_at "
                    "FROM api_keys WHERE tenant_id=?", (tenant_id,))


@router.post("/api-keys", status_code=201)
def create_api_key(body: ApiKeyBody, tenant_id: str = Depends(get_tenant),
                   principal: dict = Depends(require_permission("admin"))) -> dict:
    plaintext, key_hash, prefix = security.new_api_key()
    kid = db.new_id("key")
    db.execute("""INSERT INTO api_keys(id,tenant_id,name,key_hash,prefix,scopes,created_at)
                  VALUES(?,?,?,?,?,?,?)""",
               (kid, tenant_id, body.name, key_hash, prefix, ",".join(body.scopes), db.now()))
    audit(tenant_id, principal.get("user_id"), "api_key.create", "api_key", kid)
    return {"id": kid, "name": body.name, "prefix": prefix, "scopes": body.scopes,
            "api_key": plaintext, "note": "store this key now; it is not shown again"}


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(key_id: str, tenant_id: str = Depends(get_tenant),
                   principal: dict = Depends(require_permission("admin"))) -> None:
    k = db.one("SELECT id FROM api_keys WHERE id=? AND tenant_id=?", (key_id, tenant_id))
    if not k:
        raise HTTPException(404, "api key not found")
    db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    audit(tenant_id, principal.get("user_id"), "api_key.revoke", "api_key", key_id)
