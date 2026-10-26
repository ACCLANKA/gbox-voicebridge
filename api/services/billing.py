"""Billing, usage metering and invoicing.

Implements ``docs/design/BILLING-MULTITENANCY.md``:

* Append-only ``usage_records`` (idempotency key) with hourly/day/month rollups.
* A CDR is written for every completed bridge (1:1 with a finished bridge).
* Plans, subscriptions, invoices (with immutable lines), payments.
* Spend controls: balance / credit limit, rate caps.
* Money is stored as integer minor units (cents) + currency.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import config, db

# Default telephony/bridge rate in cents per minute if no plan overage applies.
DEFAULT_RATE_CENTS_PER_MIN = 5


def plan_view(plan: dict | None) -> dict | None:
    if not plan:
        return None
    p = dict(plan)
    p["features"] = _parse(p.pop("features_json", None))
    p["is_public"] = bool(p.get("is_public"))
    return p


def _parse(blob: str | None):
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except (ValueError, TypeError):
        return {}


def current_subscription(tenant_id: str) -> dict | None:
    return db.one("SELECT * FROM subscriptions WHERE tenant_id=? ORDER BY created_at DESC LIMIT 1",
                  (tenant_id,))


def effective_plan(tenant_id: str) -> dict | None:
    sub = current_subscription(tenant_id)
    if sub:
        return plan_view(db.one("SELECT * FROM plans WHERE id=?", (sub["plan_id"],)))
    tenant = db.one("SELECT plan FROM tenants WHERE id=?", (tenant_id,))
    if tenant and tenant.get("plan"):
        return plan_view(db.one("SELECT * FROM plans WHERE id=? OR name LIKE ?",
                                (tenant["plan"], tenant["plan"])))
    return None


def qos_from_device(device: dict) -> dict:
    """Best-effort QoS pull from the latest telemetry row for a device."""
    row = db.one("SELECT * FROM telemetry WHERE device_id=? ORDER BY ts DESC LIMIT 1",
                 (device.get("id"),))
    if not row:
        return {"mos": None, "jitter_ms": None, "loss_pct": None, "rtt_ms": None}
    return {"mos": row["mos"], "jitter_ms": row["jitter_ms"],
            "loss_pct": row["loss_pct"], "rtt_ms": row["latency_ms"]}


def record_usage(tenant_id: str, metric: str, quantity: float, unit: str, *,
                 project_id: str | None = None, device_id: str | None = None,
                 idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
    """Append a usage record (idempotent) and update hourly/daily rollups."""
    idem = idempotency_key or db.new_id("idem")
    if db.one("SELECT id FROM usage_records WHERE idempotency_key=?", (idem,)):
        return {"deduped": True, "idempotency_key": idem}
    occurred_at = occurred_at or db.now()
    db.execute("""INSERT INTO usage_records(id,tenant_id,project_id,device_id,metric,
                  quantity,unit,occurred_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)""",
               (db.new_id("ur"), tenant_id, project_id, device_id, metric,
                quantity, unit, occurred_at, idem))
    _rollup(tenant_id, project_id, metric, quantity, "hour", occurred_at[:13])
    _rollup(tenant_id, project_id, metric, quantity, "day", occurred_at[:10])
    _rollup(tenant_id, project_id, metric, quantity, "month", occurred_at[:7])
    return {"deduped": False, "idempotency_key": idem, "quantity": quantity}


def _rollup(tenant_id: str, project_id: str | None, metric: str, qty: float,
            period: str, bucket: str) -> None:
    existing = db.one("""SELECT quantity FROM usage_rollups WHERE tenant_id=? AND metric=?
                         AND period=? AND bucket=? AND project_id IS ?""",
                      (tenant_id, metric, period, bucket, project_id))
    if existing:
        db.execute("""UPDATE usage_rollups SET quantity=quantity+?, computed_at=?
                      WHERE tenant_id=? AND metric=? AND period=? AND bucket=? AND project_id IS ?""",
                   (qty, db.now(), tenant_id, metric, period, bucket, project_id))
    else:
        db.execute("""INSERT INTO usage_rollups(tenant_id,project_id,metric,period,bucket,
                      quantity,computed_at) VALUES(?,?,?,?,?,?,?)""",
                   (tenant_id, project_id, metric, period, bucket, qty, db.now()))


def round_minutes(duration_sec: int) -> int:
    """Billing rounding: round up each started minute (60/60)."""
    step = max(1, config.BILLING_ROUND_SEC)
    return max(1, -(-int(duration_sec) // step)) if duration_sec > 0 else 0


def rate_cents_per_min(tenant_id: str) -> int:
    plan = effective_plan(tenant_id)
    if plan and plan.get("overage_per_min_cents"):
        return int(plan["overage_per_min_cents"])
    return DEFAULT_RATE_CENTS_PER_MIN


def record_bridge(bridge_id: str, *, idempotency_key: str | None = None) -> dict | None:
    """Write the CDR + usage for a completed bridge (idempotent by bridge id)."""
    bridge = db.one("SELECT * FROM bridges WHERE id=?", (bridge_id,))
    if not bridge:
        return None
    if db.one("SELECT id FROM cdr WHERE bridge_id=?", (bridge_id,)):
        return db.one("SELECT * FROM cdr WHERE bridge_id=?", (bridge_id,))

    duration = int(bridge.get("duration_sec") or 0)
    minutes = round_minutes(duration)
    rate = rate_cents_per_min(bridge["tenant_id"])
    cost = minutes * rate
    components = {"bridge_minutes": minutes, "rate_cents": rate}
    device = db.one("SELECT * FROM devices WHERE id=?", (bridge["device_id"],)) or {}
    qos = qos_from_device(device)
    cdr_id = db.new_id("cdr")
    db.execute("""INSERT INTO cdr(id,bridge_id,tenant_id,project_id,device_id,direction,
                  from_number,to_number,route_id,started_at,answered_at,ended_at,duration_sec,
                  codec,packets,bytes,loss_pct,jitter_ms,mos,cost_cents,currency,
                  billing_components_json,rounded_minutes,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (cdr_id, bridge_id, bridge["tenant_id"], bridge.get("project_id"),
                bridge["device_id"], bridge.get("direction"), bridge.get("from_number"),
                bridge.get("to_number"), bridge.get("route_id"), bridge.get("started_at"),
                bridge.get("answered_at"), bridge.get("ended_at"), duration,
                bridge.get("codec"), bridge.get("packets", 0), bridge.get("bytes", 0),
                bridge.get("loss_pct") or qos["loss_pct"],
                bridge.get("jitter_ms") or qos["jitter_ms"],
                bridge.get("mos") or qos["mos"], cost, "USD",
                json.dumps(components), minutes, db.now()))
    record_usage(bridge["tenant_id"], "bridge_seconds", duration, "seconds",
                 project_id=bridge.get("project_id"), device_id=bridge["device_id"],
                 idempotency_key=idempotency_key or f"bridge:{bridge_id}",
                 occurred_at=bridge.get("ended_at") or db.now())
    record_usage(bridge["tenant_id"], "telephony_seconds", duration, "seconds",
                 project_id=bridge.get("project_id"), device_id=bridge["device_id"],
                 idempotency_key=f"telephony:{bridge_id}",
                 occurred_at=bridge.get("ended_at") or db.now())
    _charge(bridge["tenant_id"], cost)
    from . import events as bus
    bus.emit(bridge["tenant_id"], "bridge.ended",
             {"bridge_id": bridge_id, "cdr_id": cdr_id, "duration_sec": duration,
              "cost_cents": cost, "rounded_minutes": minutes})
    return db.one("SELECT * FROM cdr WHERE id=?", (cdr_id,))


def _charge(tenant_id: str, cost_cents: int) -> None:
    if cost_cents:
        db.execute("UPDATE tenants SET balance_cents=balance_cents-? WHERE id=?",
                   (cost_cents, tenant_id))


def usage_summary(tenant_id: str) -> dict:
    rows = db.query("""SELECT metric, SUM(quantity) q, unit FROM usage_records
                       WHERE tenant_id=? GROUP BY metric, unit ORDER BY metric""",
                    (tenant_id,))
    return {"tenant_id": tenant_id,
            "metrics": [{"metric": r["metric"], "quantity": r["q"], "unit": r["unit"]}
                        for r in rows]}


def spend_status(tenant_id: str) -> dict:
    t = db.one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
    if not t:
        return {}
    balance = t.get("balance_cents", 0) or 0
    limit = t.get("credit_limit_cents", 0) or 0
    return {
        "balance_cents": balance,
        "currency": t.get("currency", "USD"),
        "credit_limit_cents": limit,
        "low_balance": balance < 10000,
        "suspended": balance < 0 and balance <= -limit,
        "concurrent_call_cap": 10,
    }


def issue_invoice(tenant_id: str, *, period_start: str | None = None,
                  period_end: str | None = None, tax_rate: float = 0.16) -> dict:
    """Generate an invoice from the current subscription + metered usage."""
    sub = current_subscription(tenant_id)
    plan = effective_plan(tenant_id)
    period_end = period_end or db.now()
    period_start = period_start or period_end
    subtotal = 0
    lines: list[dict] = []
    if plan:
        base = int(plan.get("base_price_cents") or 0)
        subtotal += base
        lines.append({"description": f"{plan['name']} plan - base fee",
                      "metric": "subscription", "quantity": 1, "unit_price_cents": base,
                      "amount_cents": base})
    # metered bridge minutes beyond included quota
    minutes_row = db.one("""SELECT SUM(quantity) q FROM usage_records
                            WHERE tenant_id=? AND metric='bridge_seconds'""", (tenant_id,))
    total_seconds = float(minutes_row["q"] or 0) if minutes_row else 0.0
    total_minutes = round_minutes(int(total_seconds))
    included = int(plan.get("included_minutes") or 0) if plan else 0
    overage_minutes = max(0, total_minutes - included)
    if overage_minutes and plan:
        rate = int(plan.get("overage_per_min_cents") or DEFAULT_RATE_CENTS_PER_MIN)
        amount = overage_minutes * rate
        subtotal += amount
        lines.append({"description": "Bridge minutes overage", "metric": "bridge_minutes",
                      "quantity": overage_minutes, "unit_price_cents": rate,
                      "amount_cents": amount})
    tax = int(round(subtotal * tax_rate))
    total = subtotal + tax
    inv_id = db.new_id("inv")
    number = f"INV-{1000 + (db.one('SELECT COUNT(*) c FROM invoices')['c'] or 0) + 1}"
    db.execute("""INSERT INTO invoices(id,tenant_id,number,status,subtotal_cents,tax_cents,
                  total_cents,currency,period_start,period_end,issued_at,due_at,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (inv_id, tenant_id, number, "open", subtotal, tax, total, "USD",
                period_start, period_end, db.now(), db.now(), db.now()))
    for line in lines:
        db.execute("""INSERT INTO invoice_lines(id,invoice_id,description,metric,quantity,
                      unit_price_cents,amount_cents) VALUES(?,?,?,?,?,?,?)""",
                   (db.new_id("il"), inv_id, line["description"], line["metric"],
                    line["quantity"], line["unit_price_cents"], line["amount_cents"]))
    from . import events as bus
    bus.emit(tenant_id, "invoice.created", {"invoice_id": inv_id, "number": number,
                                            "total_cents": total})
    return invoice_view(db.one("SELECT * FROM invoices WHERE id=?", (inv_id,)))


def invoice_view(invoice: dict | None) -> dict | None:
    if not invoice:
        return None
    inv = dict(invoice)
    inv["lines"] = db.query("SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id",
                            (inv["id"],))
    return inv


def grant_credits(tenant_id: str, amount_cents: int) -> dict:
    db.execute("UPDATE tenants SET balance_cents=balance_cents+? WHERE id=?",
               (amount_cents, tenant_id))
    return db.one("SELECT id,name,balance_cents,currency FROM tenants WHERE id=?", (tenant_id,))


def mark_invoice_paid(invoice_id: str, *, provider: str = "manual",
                      provider_ref: str | None = None) -> dict | None:
    inv = db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    if not inv:
        return None
    db.execute("UPDATE invoices SET status='paid' WHERE id=?", (invoice_id,))
    db.execute("""INSERT INTO payments(id,tenant_id,invoice_id,provider,provider_ref,
                  amount_cents,currency,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
               (db.new_id("pay"), inv["tenant_id"], invoice_id, provider, provider_ref,
                inv["total_cents"], inv.get("currency", "USD"), "succeeded", db.now()))
    from . import events as bus
    bus.emit(inv["tenant_id"], "invoice.paid",
             {"invoice_id": invoice_id, "amount_cents": inv["total_cents"]})
    return invoice_view(db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,)))
