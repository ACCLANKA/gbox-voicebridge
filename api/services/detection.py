"""Call detection & identification service.

Implements ``docs/design/CALL-DETECTION.md`` and ``docs/api/CALL-EVENTS-API.md``.

Three detection layers feed a single server-side source of truth (``call_sessions``):

* **Layer A - SCO** (``call.audio_link_up/down``): a call is happening on a device.
* **Layer B - HFP** (``call.ringing`` / ``call.answered`` / ``call.ended`` /
  ``call.direction``): direction and best-effort CLI.
* **Layer C - companion app** (``POST /call/enrich``): app + caller identity.

Events are correlated by ``device_id`` inside a rolling time window; enrichment
upgrades an anonymous session into an identified one and bumps confidence.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import config, db

# Android package -> logical source (docs/design/CALL-DETECTION.md#4).
SOURCE_MAP = {
    "com.whatsapp": "whatsapp",
    "com.whatsapp.w4b": "whatsapp",
    "com.android.phone": "cellular",
    "com.samsung.android.dialer": "cellular",
    "com.google.android.dialer": "cellular",
    "org.linphone": "sip_app",
    "com.sip": "sip_app",
    "org.telegram.messenger": "telegram",
}

# Confidence by signal set (docs/api/CALL-EVENTS-API.md#8).
CONFIDENCE = {
    "whatsapp_companion_sco": 0.95,
    "clip_sco": 0.70,
    "sco_only": 0.40,
    "hfp_ringing_only": 0.30,
}

TERMINAL_STATES = {"ended", "failed"}

# Every state a session can be in *before* it terminates. A call routinely lasts
# longer than the short window used to disambiguate near-simultaneous pre-answer
# events, so once a session exists the next events for that device must keep
# correlating to it instead of spawning duplicates.
ONGOING_STATES = {"detected", "ringing", "enriched", "answered", "active", "bridged", "held"}


def normalize_source(app: str | None) -> str:
    if not app:
        return "unknown"
    app = app.strip().lower()
    if app in ("whatsapp", "cellular", "sip_app", "telegram", "unknown"):
        return app
    if app in SOURCE_MAP:
        return SOURCE_MAP[app]
    for pkg, src in SOURCE_MAP.items():
        if app.startswith(pkg):
            return src
    if "whatsapp" in app:
        return "whatsapp"
    if "telegram" in app:
        return "telegram"
    if "sip" in app or "linphone" in app:
        return "sip_app"
    if "dialer" in app or "phone" in app:
        return "cellular"
    return "unknown"


def _age_seconds(ts: str | None) -> float:
    if not ts:
        return 1e9
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except ValueError:
        return 1e9


def find_active_session(device_id: str, window: float = config.CORRELATION_WINDOW_SEC) -> dict | None:
    rows = db.query(
        "SELECT * FROM call_sessions WHERE device_id=? ORDER BY started_at DESC LIMIT 5",
        (device_id,))
    for r in rows:
        if r["state"] in TERMINAL_STATES:
            continue
        # An open (non-terminal) session is still ongoing -> correlate to it
        # regardless of age. The time window only helps correlate by recency for
        # any state we don't recognise as ongoing.
        if r["state"] in ONGOING_STATES or _age_seconds(r["started_at"]) <= window:
            return r
    return None


def _create_session(device: dict, direction: str = "incoming") -> dict:
    sid = db.new_id("cs")
    ts = db.now()
    db.execute("""INSERT INTO call_sessions(id,tenant_id,device_id,project_id,state,source,
                  direction,detection_sco,detection_hfp,detection_companion,detection_confidence,
                  aec_enabled,started_at,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (sid, device["tenant_id"], device["id"], device.get("project_id"),
                "detected", "unknown", direction, 0, 0, 0,
                CONFIDENCE["sco_only"], device.get("aec_enabled", 1), ts, ts))
    return db.one("SELECT * FROM call_sessions WHERE id=?", (sid,))


def ingest_device_event(tenant_id: str, device_id: str, event: str, data: dict,
                        seq: int | None = None, device_ts: str | None = None) -> dict:
    """Process one device call event; returns ``{ack, seq, call_session_id, action}``."""
    device = db.one("SELECT * FROM devices WHERE id=? OR serial=?", (device_id, device_id))
    if not device:
        return {"ack": False, "error": "unknown device"}
    data = data or {}
    event = (event or "").strip()

    # The device queues state events until the server acks them and re-sends the unacked
    # ones after a reconnect - a call.ended generated while the socket was down used to be
    # lost entirely, leaving the session open for ever. That makes delivery at-least-once,
    # so the receiver must be idempotent: a seq already processed is acked again but not
    # applied a second time.
    if seq is not None:
        prev = db.one("SELECT call_session_id FROM call_events WHERE device_id=? AND seq=?"
                      " LIMIT 1", (device["id"], seq))
        if prev is not None:
            return {"ack": True, "seq": seq, "call_session_id": prev["call_session_id"],
                    "action": "dup"}

    session = find_active_session(device["id"])
    action = "none"
    now = db.now()

    # A ring for a device whose open session has NO live audio link and is not fresh is a
    # new call: close the stale session and open a new one. find_active_session correlates
    # to any non-terminal session regardless of age, so a session left open by a link loss
    # (the device could not report call.ended) swallowed every later call - calls 2 and 3
    # were folded into call 1 and never became sessions. A live audio link keeps folding
    # (that is a real re-notify / call waiting), and so does a ring inside STALE_RING_SEC
    # (that is the same ring delivered twice).
    if event == "call.ringing" and session and not session["detection_sco"] \
            and _age_seconds(session["started_at"]) > config.STALE_RING_SEC:
        stale_age = max(0, int(_age_seconds(session["started_at"])))
        _apply(session["id"], {"state": "ended", "ended_at": now,
                               "duration_sec": stale_age})
        session = None

    if event in ("call.ringing", "call.audio_link_up", "call.direction") and not session:
        session = _create_session(device, data.get("direction") or "incoming")

    if session is None:
        # stray event with no session — log it, ack, no session
        db.execute("""INSERT INTO call_events(id,device_id,tenant_id,event,data_json,device_ts,
                      server_ts,seq) VALUES(?,?,?,?,?,?,?,?)""",
                   (db.new_id("ce"), device["id"], device["tenant_id"], event,
                    json.dumps(data), device_ts, now, seq))
        return {"ack": True, "seq": seq, "call_session_id": None, "action": "none"}

    sid = session["id"]
    updates: dict = {}

    if event == "call.ringing":
        updates["state"] = "ringing"
        updates["detection_hfp"] = 1
        if data.get("direction"):
            updates["direction"] = data["direction"]
        if data.get("clip"):
            updates["incoming_number"] = data["clip"]
    elif event == "call.audio_link_up":
        updates["detection_sco"] = 1
        if session["state"] in ("ringing", None):
            updates["state"] = "detected"
        if data.get("codec"):
            pass
    elif event in ("call.answered", "call.active"):
        updates["state"] = "answered"
        updates["answered_at"] = now
        updates["detection_hfp"] = 1
    elif event == "call.held":
        updates["state"] = "held"
    elif event == "call.resumed":
        updates["state"] = "bridged" if session.get("bridge_id") else "answered"
    elif event == "call.direction":
        if data.get("direction"):
            updates["direction"] = data["direction"]
    elif event == "call.audio_link_down":
        # Layer A goes back down. This used to be a no-op, so detection_sco stayed 1 for
        # the rest of the session's life - the session looked like it still had a live
        # audio link long after the SCO was gone, which is what kept a dead session from
        # ever being recognised as stale.
        updates["detection_sco"] = 0
    elif event == "call.ended":
        updates["state"] = "ended"
        updates["ended_at"] = now
        duration = data.get("duration_ms")
        duration_sec = int(duration // 1000) if duration else int(_age_seconds(session["started_at"]))
        updates["duration_sec"] = max(0, duration_sec)
        action = "none"

    # recompute confidence from current signal set
    sco = updates.get("detection_sco", session["detection_sco"])
    hfp = updates.get("detection_hfp", session["detection_hfp"])
    comp = updates.get("detection_companion", session["detection_companion"])
    source = updates.get("source", session["source"])
    updates["detection_confidence"] = _confidence(sco, hfp, comp, source, session)

    _apply(sid, updates)

    db.execute("""INSERT INTO call_events(id,device_id,tenant_id,call_session_id,event,
                  data_json,device_ts,server_ts,seq) VALUES(?,?,?,?,?,?,?,?,?)""",
               (db.new_id("ce"), device["id"], device["tenant_id"], sid, event,
                json.dumps(data), device_ts, now, seq))

    # when the call ends, write a bridge + CDR and emit webhooks
    if updates.get("state") == "ended":
        _finalize_session(db.one("SELECT * FROM call_sessions WHERE id=?", (sid,)))

    from . import events as bus
    bus.emit(device["tenant_id"], f"call.session.{_phase(updates.get('state', session['state']))}",
             session_view(db.one("SELECT * FROM call_sessions WHERE id=?", (sid,))))

    return {"ack": True, "seq": seq, "call_session_id": sid, "action": action}


def _confidence(sco, hfp, comp, source, session) -> float:
    if comp and source == "whatsapp" and sco:
        return CONFIDENCE["whatsapp_companion_sco"]
    if comp:
        return 0.90
    if session.get("incoming_number") and sco:
        return CONFIDENCE["clip_sco"]
    if sco:
        return CONFIDENCE["sco_only"]
    if hfp:
        return CONFIDENCE["hfp_ringing_only"]
    return session.get("detection_confidence", 0.3)


def _apply(sid: str, updates: dict) -> None:
    if not updates:
        return
    fields = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE call_sessions SET {fields} WHERE id=?",
               (*updates.values(), sid))


def _log_call_event(tenant_id: str, device_id: str, session_id: str | None, event: str,
                    data: dict, ts: str) -> None:
    """Append one row to the raw, append-only ``call_events`` log."""
    db.execute("""INSERT INTO call_events(id,device_id,tenant_id,call_session_id,event,
                  data_json,device_ts,server_ts) VALUES(?,?,?,?,?,?,?,?)""",
               (db.new_id("ce"), device_id, tenant_id, session_id, event,
                json.dumps(data or {}), None, ts))


def enrich(device_id: str, *, source_app: str | None, caller_name: str | None = None,
           caller_number: str | None = None, direction: str | None = None,
           confidence: float | None = None, phone_ts: str | None = None) -> dict:
    """Layer C: correlate phone-side identity onto an existing session."""
    device = db.one("SELECT * FROM devices WHERE id=? OR serial=?", (device_id, device_id))
    if not device:
        return {"matched": False, "error": "unknown device"}
    source = normalize_source(source_app)
    now = db.now()
    session = find_active_session(device["id"], window=max(config.CORRELATION_WINDOW_SEC, 5.0))

    enrichment_id = db.new_id("en")
    if not session:
        # phone-only session (call seen by the app, not by the device) — flagged for review
        sid = db.new_id("cs")
        db.execute("""INSERT INTO call_sessions(id,tenant_id,device_id,project_id,state,source,
                      direction,caller_name,caller_number,detection_companion,
                      detection_confidence,started_at,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (sid, device["tenant_id"], device["id"], device.get("project_id"),
                    "enriched", source, direction or "incoming", caller_name, caller_number,
                    1, confidence or 0.5, phone_ts or now, now))
        db.execute("""INSERT INTO enrichment_events(id,device_id,call_session_id,source_app,
                      caller_name,caller_number,direction,confidence,phone_ts,server_ts)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (enrichment_id, device["id"], sid, source_app, caller_name, caller_number,
                    direction, confidence or 0.5, phone_ts, now))
        _log_call_event(device["tenant_id"], device["id"], sid, "call.enriched",
                        {"source": source, "caller_name": caller_name,
                         "caller_number": caller_number, "phone_only": True}, now)
        return {"call_session_id": sid, "matched": False, "source": source,
                "phone_only": True}

    updates = {
        "source": source,
        "detection_companion": 1,
        "detection_confidence": confidence or CONFIDENCE["whatsapp_companion_sco"],
    }
    if session["state"] in ("detected", "ringing"):
        updates["state"] = "enriched"
    if caller_name:
        updates["caller_name"] = caller_name
    if caller_number:
        updates["caller_number"] = caller_number
        if source == "whatsapp":
            updates["whatsapp_number"] = caller_number
        else:
            updates["incoming_number"] = caller_number
    if direction:
        updates["direction"] = direction
    # identify_ms = time from session start to enrichment
    updates["identify_ms"] = int(_age_seconds(session["started_at"]) * 1000)
    _apply(session["id"], updates)

    db.execute("""INSERT INTO enrichment_events(id,device_id,call_session_id,source_app,
                  caller_name,caller_number,direction,confidence,phone_ts,server_ts)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""",
               (enrichment_id, device["id"], session["id"], source_app, caller_name,
                caller_number, direction, updates["detection_confidence"], phone_ts, now))
    _log_call_event(device["tenant_id"], device["id"], session["id"], "call.enriched",
                    {"source": source, "caller_name": caller_name,
                     "caller_number": caller_number}, now)

    from . import events as bus
    bus.emit(device["tenant_id"], "call.enriched",
             session_view(db.one("SELECT * FROM call_sessions WHERE id=?", (session["id"],))))
    return {"call_session_id": session["id"], "matched": True, "source": source}


def mark_bridged(session_id: str, route_id: str | None, route_desc: str | None,
                 bridge_id: str | None = None) -> None:
    session = db.one("SELECT * FROM call_sessions WHERE id=?", (session_id,))
    if not session:
        return
    updates = {"state": "bridged", "route_id": route_id, "route": route_desc}
    if bridge_id:
        updates["bridge_id"] = bridge_id
    _apply(session_id, updates)


def _phase(state: str) -> str:
    return {"detected": "created", "ringing": "created", "enriched": "enriched",
            "answered": "answered", "active": "answered", "bridged": "bridged",
            "held": "updated", "ended": "ended", "failed": "failed"}.get(state, "updated")


def _finalize_session(session: dict) -> None:
    """Write a bridge + CDR for an ended session (idempotent)."""
    if not session:
        return
    existing = db.one("SELECT id FROM cdr WHERE bridge_id=?",
                      (session.get("bridge_id"),)) if session.get("bridge_id") else None
    if existing:
        return
    bridge_id = session.get("bridge_id") or db.new_id("br")
    device = db.one("SELECT * FROM devices WHERE id=?", (session["device_id"],)) or {}
    device_view = device or {}
    from . import routing, billing
    route = routing.select_route(session["tenant_id"], device_view,
                                 session.get("direction") or "inbound",
                                 session.get("caller_number"))
    route_id = session.get("route_id") or (route["id"] if route else None)
    duration = session.get("duration_sec") or int(_age_seconds(session["started_at"]))
    qos = billing.qos_from_device(device_view)
    db.execute("""INSERT INTO bridges(id,tenant_id,device_id,project_id,route_id,direction,
                  from_number,to_number,state,codec,started_at,answered_at,ended_at,
                  duration_sec,loss_pct,jitter_ms,mos,agent_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (bridge_id, session["tenant_id"], session["device_id"],
                session.get("project_id"), route_id, session.get("direction"),
                session.get("caller_number"), session.get("route"),
                "ended", device.get("codec", "opus"), session.get("started_at"),
                session.get("answered_at"), session.get("ended_at"), duration,
                qos["loss_pct"], qos["jitter_ms"], qos["mos"], "agent-1"))
    billing.record_bridge(bridge_id)
    _apply(session["id"], {"bridge_id": bridge_id, "route_id": route_id})


def session_view(session: dict | None) -> dict | None:
    if not session:
        return None
    s = dict(session)
    s["detection"] = {
        "sco": bool(s.pop("detection_sco", 0)),
        "hfp": bool(s.pop("detection_hfp", 0)),
        "companion": bool(s.pop("detection_companion", 0)),
        "confidence": s.pop("detection_confidence", None),
        "identify_ms": s.pop("identify_ms", None),
    }
    s["qos"] = {"mos": s.pop("mos", None), "jitter_ms": s.pop("jitter_ms", None),
                "loss_pct": s.pop("loss_pct", None)}
    s["echo"] = {"aec_enabled": bool(s.pop("aec_enabled", 0)),
                 "erle_db": s.pop("erle_db", None)}
    s["active"] = s.get("ended_at") is None
    return s
