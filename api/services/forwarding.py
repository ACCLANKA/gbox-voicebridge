"""Voice forwarding: resolve a device to an egress destination and run the leg.

Resolution order (first hit wins):

1. a **per-device override** (``device_forwarding``) set from the dashboard;
2. the best matching **route** (``routing.select_route``);
3. ``simulated`` — no downstream leg, the safe default.

See ``docs/design/VOICE-FORWARDING.md``.
"""
from __future__ import annotations

import json
import logging

from .. import db
from . import egress, routing
from .media import MediaLeg, SimulatedLeg

logger = logging.getLogger("voicebridge.forwarding")


def _blob(blob: str | None) -> dict:
    try:
        return json.loads(blob) if blob else {}
    except (ValueError, TypeError):
        return {}


def _override(device_id: str) -> dict | None:
    return db.one("SELECT * FROM device_forwarding WHERE device_id=? AND enabled=1",
                  (device_id,))


def _override_view(row: dict | None) -> dict | None:
    if not row:
        return None
    return {"type": row["dest_type"], "uri": row["dest_uri"],
            "options": _blob(row.get("options_json"))}


def resolve(device: dict) -> tuple[egress.EgressTarget, str]:
    """Return ``(target, source)`` for a device; never raises (falls back)."""
    candidates: list[tuple[str, str, str | None, dict]] = []
    row = _override(device["id"])
    if row:
        candidates.append(("device-override", row["dest_type"], row["dest_uri"],
                           _blob(row.get("options_json"))))
    # try matching routes best-first, skipping types with no egress leg yet
    # (e.g. webhook) so a later wired route can still win.
    for route in routing.match_routes(device["tenant_id"], device, "inbound"):
        dest = route.get("destination") or {}
        name = f"route:{route.get('name') or route.get('id')}"
        candidates.append((name, dest.get("type") or "", dest.get("uri"),
                           route.get("options") or {}))
        fb = route.get("fallback") or {}
        if fb.get("type") or fb.get("uri"):
            candidates.append((f"{name}:fallback", fb.get("type") or "",
                               fb.get("uri"), fb.get("options") or {}))
    candidates.append(("default", "simulated", "simulated", {}))

    last_error = "no destination"
    for source, dtype, duri, opts in candidates:
        try:
            return egress.parse_destination(dtype, duri, opts), source
        except egress.EgressError as exc:
            last_error = str(exc)
    raise egress.EgressError(last_error)  # unreachable: "simulated" always parses


def describe(device: dict) -> dict:
    """Resolved destination + the raw override, for the dashboard."""
    target, source = resolve(device)
    return {"destination": target.as_dict(), "source": source,
            "override": _override_view(_override(device["id"]))}


def set_override(device: dict, dest_type: str, dest_uri: str,
                 options: dict | None = None) -> dict:
    """Validate and store a per-device forwarding override."""
    egress.parse_destination(dest_type, dest_uri, options)  # raises EgressError
    db.execute(
        """INSERT INTO device_forwarding(device_id,tenant_id,dest_type,dest_uri,
           options_json,enabled,updated_at) VALUES(?,?,?,?,?,1,?)
           ON CONFLICT(device_id) DO UPDATE SET dest_type=excluded.dest_type,
             dest_uri=excluded.dest_uri, options_json=excluded.options_json,
             enabled=1, updated_at=excluded.updated_at""",
        (device["id"], device["tenant_id"], dest_type, dest_uri,
         json.dumps(options or {}), db.now()))
    return describe(device)


def clear_override(device: dict) -> None:
    db.execute("DELETE FROM device_forwarding WHERE device_id=?", (device["id"],))


class _Registry:
    """Legs currently bridged, keyed by call-session id."""

    def __init__(self) -> None:
        self._legs: dict[str, tuple[MediaLeg, egress.EgressTarget, str]] = {}

    def open(self, session: dict, device: dict) -> MediaLeg:
        target, source = resolve(device)
        leg = egress.build_leg(session["id"], target)
        try:
            leg.start()
        except (OSError, egress.EgressError) as exc:
            # Unreachable destination must not break the bridge — degrade to a
            # no-op leg (ROUTING-INTEGRATIONS §8: "destination unreachable").
            logger.warning("egress %s unreachable (%s); using simulated leg",
                           target.as_dict(), exc)
            leg = SimulatedLeg(session["id"])
            leg.start()
            source = f"{source}:unreachable"
        self._legs[session["id"]] = (leg, target, source)
        db.execute("UPDATE call_sessions SET route=? WHERE id=?", (source, session["id"]))
        return leg

    def get(self, session_id: str | None) -> MediaLeg | None:
        entry = self._legs.get(session_id) if session_id else None
        return entry[0] if entry else None

    def source(self, session_id: str | None) -> str | None:
        entry = self._legs.get(session_id) if session_id else None
        return entry[2] if entry else None

    def close(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        entry = self._legs.pop(session_id, None)
        return entry[0].stop() if entry else None

    def count(self) -> int:
        return len(self._legs)


registry = _Registry()
