"""Event bus + outbound webhook delivery.

Internal events (bridge.started, call.enriched, device.offline, ...) are
dispatched to tenant webhook subscriptions, signed with HMAC-SHA256 in the
``X-VB-Signature`` header (see docs/api/API.md#webhooks). Delivery is
best-effort with a short timeout so the control plane never blocks on a slow
customer endpoint.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .. import db, security
from . import webhooks

WEBHOOK_TIMEOUT = 2.0

# A small in-process ring buffer of recent platform events, used by the
# dashboard's activity feed without needing an external message bus.
_RECENT: list[dict[str, Any]] = []
_RECENT_MAX = 200


def recent(limit: int = 50) -> list[dict]:
    return list(reversed(_RECENT[-limit:]))


def emit(tenant_id: str, event: str, data: dict[str, Any], *, deliver: bool = True) -> dict:
    """Publish an event to the bus and fan it out to subscribed webhooks."""
    entry = {"tenant_id": tenant_id, "event": event, "data": data, "ts": db.now()}
    _RECENT.append(entry)
    if len(_RECENT) > _RECENT_MAX:
        del _RECENT[: len(_RECENT) - _RECENT_MAX]

    # Delivery goes through the queue in services/webhooks.py: it retries with
    # backoff, records every attempt in webhook_deliveries, and never blocks the
    # caller on a slow customer endpoint (the old inline POST waited up to 2 s).
    queued = 0
    if deliver:
        for hook in db.query("SELECT * FROM webhooks WHERE tenant_id=? AND enabled=1",
                             (tenant_id,)):
            events = [e for e in (hook.get("events") or "").split(",") if e]
            if event not in events and "*" not in events:
                continue
            queued += 1 if webhooks.enqueue(hook, event, data) else 0
    return {"event": event, "delivered": queued}


def _deliver(hook: dict, entry: dict) -> bool:
    body = json.dumps(entry, separators=(",", ":")).encode()
    signature = security.sign_webhook(hook.get("secret") or "", body)
    req = urllib.request.Request(
        hook["url"], data=body, method="POST",
        headers={"Content-Type": "application/json", "X-VB-Signature": signature,
                 "X-VB-Event": entry["event"]})
    status = None
    try:
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as resp:
            status = resp.status
        ok = 200 <= (status or 0) < 300
    except (urllib.error.URLError, ValueError, OSError, TimeoutError):
        ok = False
    db.execute("UPDATE webhooks SET last_status=?, last_attempt_at=? WHERE id=?",
               (status, db.now(), hook["id"]))
    return ok
