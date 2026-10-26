"""DID (inbound number) inventory.

A **DID** (Direct Inward Dialling number) is an E.164 number a tenant owns and can
point at a device/project/route. This module is the store + lookup layer used by
the API (``/v1/dids``), the dashboard and inbound call handling.

See ``docs/design/PSTN-AND-DIDS.md``.
"""
from __future__ import annotations

from .. import db
from . import numbers


class DidError(ValueError):
    """Invalid DID operation (bad number, unknown device, …)."""


class DuplicateDid(DidError):
    """The number already exists in the inventory."""


def view(row: dict | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    d["e164"] = d.get("number")
    d["region"] = numbers.region(d["number"]) if numbers.is_e164(d.get("number") or "") else None
    return d


def list_dids(tenant_id: str) -> list[dict]:
    return [view(r) for r in db.query(
        "SELECT * FROM dids WHERE tenant_id=? ORDER BY number", (tenant_id,))]


def get(tenant_id: str, did_id: str) -> dict | None:
    return view(db.one("SELECT * FROM dids WHERE id=? AND tenant_id=?", (did_id, tenant_id)))


def get_by_number(tenant_id: str, number: str) -> dict | None:
    """Look up a DID by any acceptable spelling of the number (used inbound)."""
    e164 = numbers.try_normalize(number) or number
    return view(db.one("SELECT * FROM dids WHERE tenant_id=? AND number=?",
                       (tenant_id, e164)))


def for_device(tenant_id: str, device_id: str) -> list[dict]:
    return [view(r) for r in db.query(
        "SELECT * FROM dids WHERE tenant_id=? AND device_id=? ORDER BY number",
        (tenant_id, device_id))]


def create(tenant_id: str, number: str, *, label: str | None = None,
           country: str | None = None, device_id: str | None = None,
           project_id: str | None = None, route_id: str | None = None,
           capabilities: str = "voice") -> dict:
    try:
        e164 = numbers.normalize(number, country or "US")
    except numbers.NumberError as exc:
        raise DidError(str(exc)) from exc
    if db.one("SELECT id FROM dids WHERE number=?", (e164,)):
        raise DuplicateDid(f"DID {e164} already exists")
    did_id = db.new_id("did")
    db.execute("""INSERT INTO dids(id,tenant_id,number,country,label,device_id,
                  project_id,route_id,capabilities,enabled,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
               (did_id, tenant_id, e164, country or numbers.region(e164), label,
                device_id, project_id, route_id, capabilities, db.now()))
    return get(tenant_id, did_id)


def update(tenant_id: str, did_id: str, **fields) -> dict | None:
    if not db.one("SELECT id FROM dids WHERE id=? AND tenant_id=?", (did_id, tenant_id)):
        return None
    updates: dict = {}
    if fields.get("number"):
        try:
            updates["number"] = numbers.normalize(fields["number"], fields.get("country") or "US")
        except numbers.NumberError as exc:
            raise DidError(str(exc)) from exc
    for key in ("label", "country", "device_id", "project_id", "route_id", "capabilities"):
        if key in fields and fields[key] is not None:
            updates[key] = fields[key]
    if "enabled" in fields and fields["enabled"] is not None:
        updates["enabled"] = int(bool(fields["enabled"]))
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        try:
            db.execute(f"UPDATE dids SET {sets} WHERE id=?", (*updates.values(), did_id))
        except Exception as exc:  # sqlite UNIQUE on number
            raise DuplicateDid(f"DID {updates.get('number')} already exists") from exc
    return get(tenant_id, did_id)


def assign(tenant_id: str, did_id: str, *, device_id: str | None = None,
           project_id: str | None = None, route_id: str | None = None) -> dict | None:
    """Attach a DID to a device/project/route (``None`` clears a binding)."""
    if not db.one("SELECT id FROM dids WHERE id=? AND tenant_id=?", (did_id, tenant_id)):
        return None
    db.execute("UPDATE dids SET device_id=?, project_id=?, route_id=? WHERE id=?",
               (device_id, project_id, route_id, did_id))
    return get(tenant_id, did_id)


def delete(tenant_id: str, did_id: str) -> bool:
    if not db.one("SELECT id FROM dids WHERE id=? AND tenant_id=?", (did_id, tenant_id)):
        return False
    db.execute("DELETE FROM dids WHERE id=?", (did_id,))
    return True
