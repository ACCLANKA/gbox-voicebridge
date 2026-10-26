"""Route evaluation engine.

Implements the model in ``docs/design/ROUTING-INTEGRATIONS.md``:

* A route = match conditions + destination + options + optional fallback.
* Evaluation order: **priority -> specificity -> first match**.
* Destination types: sip | pstn | webrtc | webhook | device | recording.

Routes are stored with JSON blobs for ``match`` / ``options`` / ``fallback``.
Matching is tenant-scoped.
"""
from __future__ import annotations

import json
from datetime import datetime

from .. import db

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _to_minutes(text: str) -> int | None:
    parts = text.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def _expand_days(spec: str) -> set[int]:
    """``"Mon-Fri"`` / ``"Mon,Wed,Fri"`` / ``"Sat"`` → weekday numbers (Mon=0)."""
    spec = spec.strip().lower()
    if "-" in spec:
        a, _, b = spec.partition("-")
        lo, hi = _DAYS.get(a[:3]), _DAYS.get(b[:3])
        if lo is None or hi is None:
            return set()
        return {d % 7 for d in range(lo, hi + 1)} if lo <= hi \
            else {d % 7 for d in range(lo, 7)} | {d % 7 for d in range(0, hi + 1)}
    return {_DAYS[p.strip()[:3]] for p in spec.split(",") if p.strip()[:3] in _DAYS}


def _time_window_matches(match: dict, now: datetime | None = None) -> bool:
    """Whether ``now`` falls in a route's ``time_window`` (absent ⇒ always).

    Format ``"Mon-Fri 09:00-18:00"``; several windows via ``;`` (or a JSON list).
    A window whose start is after its end wraps past midnight.
    """
    tw = match.get("time_window")
    if not tw:
        return True
    windows = [tw] if isinstance(tw, str) else list(tw)
    now = now or datetime.now()
    dow, mins = now.weekday(), now.hour * 60 + now.minute
    for window in windows:
        dayspec, _, times = window.rpartition(" ")
        days = _expand_days(dayspec)
        start_s, _, end_s = times.partition("-")
        start, end = _to_minutes(start_s), _to_minutes(end_s)
        if not days or start is None or end is None or dow not in days:
            continue
        if start <= end:
            if start <= mins < end:
                return True
        elif mins >= start or mins < end:      # wraps midnight
            return True
    return False


def _parse(blob: str | None) -> dict:
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except (ValueError, TypeError):
        return {}


def route_view(row: dict | None) -> dict | None:
    if not row:
        return None
    r = dict(row)
    r["match"] = _parse(r.pop("match_json", None))
    r["options"] = _parse(r.pop("options_json", None))
    r["fallback"] = _parse(r.pop("fallback_json", None))
    r["enabled"] = bool(r.get("enabled"))
    r["destination"] = {"type": r.get("dest_type"), "uri": r.get("dest_uri")}
    return r


def normalize_direction(direction: str | None) -> str:
    d = (direction or "both").lower()
    return {"inbound": "inbound", "incoming": "inbound",
            "outbound": "outbound", "outgoing": "outbound"}.get(d, d)


def _device_matches(match: dict, device: dict) -> bool:
    devices = match.get("devices")
    if devices and device.get("id") not in devices and device.get("serial") not in devices:
        return False
    tags = match.get("tags")
    if tags:
        dev_tags = _parse(device.get("tags_json")) if device.get("tags_json") else []
        if not set(tags) & set(dev_tags or []):
            return False
    return True


def _direction_matches(match: dict, direction: str) -> bool:
    md = normalize_direction(match.get("direction"))
    if md in ("", "both"):
        return True
    return md == normalize_direction(direction)


def _pattern_matches(pattern: str | None, value: str | None) -> bool:
    if not pattern:
        return True
    if value is None:
        return False
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


def match_routes(tenant_id: str, device: dict, direction: str = "inbound",
                 caller_number: str | None = None,
                 now: datetime | None = None) -> list[dict]:
    """Return *all* enabled routes matching the given context, best first."""
    rows = db.query(
        "SELECT * FROM routes WHERE tenant_id=? AND enabled=1 ORDER BY priority DESC, id",
        (tenant_id,))
    matched: list[tuple[int, int, dict]] = []
    for row in rows:
        route = route_view(row)
        m = route["match"]
        if not _device_matches(m, device):
            continue
        if not _direction_matches(m, direction):
            continue
        if not _pattern_matches(m.get("caller_number"), caller_number):
            continue
        if not _time_window_matches(m, now):
            continue
        if route.get("project_id") and device.get("project_id") \
                and route["project_id"] != device["project_id"]:
            continue
        # specificity = number of populated match keys
        specificity = len([k for k, v in m.items() if v not in (None, "", [], {})])
        matched.append((route["priority"], specificity, route))
    matched.sort(key=lambda t: (-t[0], -t[1]))
    return [r for _, _, r in matched]


def select_route(tenant_id: str, device: dict, direction: str = "inbound",
                 caller_number: str | None = None) -> dict | None:
    """Best matching route (priority -> specificity -> first), or ``None``."""
    routes = match_routes(tenant_id, device, direction, caller_number)
    return routes[0] if routes else None


def route_description(route: dict | None) -> str | None:
    if not route:
        return None
    dest = route.get("destination") or {}
    label = route.get("name") or route.get("id")
    return f"{label} -> {dest.get('uri') or dest.get('type')}"


def test_route(route_id: str, device: dict, direction: str = "inbound",
               caller_number: str | None = None) -> dict:
    """Dry-run a single route against a device (``POST /routes/{id}/test``)."""
    row = db.one("SELECT * FROM routes WHERE id=?", (route_id,))
    if not row:
        return {"matched": False, "reason": "route not found"}
    route = route_view(row)
    m = route["match"]
    checks = {
        "device": _device_matches(m, device),
        "direction": _direction_matches(m, direction),
        "caller_number": _pattern_matches(m.get("caller_number"), caller_number),
        "project": not route.get("project_id") or route["project_id"] == device.get("project_id"),
        "time_window": _time_window_matches(m),
        "enabled": route.get("enabled", False),
    }
    return {"matched": all(checks.values()), "checks": checks, "route": route}
