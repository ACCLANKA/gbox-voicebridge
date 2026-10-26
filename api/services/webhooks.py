"""Signed webhook delivery: retries with backoff, delivery history, SSRF guards.

Why this exists
---------------
``services/events.py`` used to POST inline with a 2 s timeout and no record of
what happened. That is fine for a dashboard toy and useless for a developer
platform: a customer cannot tell whether an event arrived, a slow endpoint
blocks nothing, a failed delivery is lost forever, and any tenant could point a
webhook at ``169.254.169.254`` or ``localhost``.

This module adds the missing half:

* ``validate_url`` refuses non-HTTPS, credentials in the URL, and any host that
  resolves to a private / loopback / link-local / reserved address (SSRF).
* ``sign`` produces ``t=<unix>,v1=<hex hmac>`` so receivers can verify both the
  body and the timestamp (replay window is the receiver's call).
* ``enqueue`` hands delivery to a bounded worker queue. The request path never
  waits on a customer endpoint.
* every attempt (including failures and drops) is written to
  ``webhook_deliveries`` so ``GET /v1/webhooks/{id}/deliveries`` can show the
  truth, and ``POST /v1/webhooks/{id}/test`` returns the live status code.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import db

MAX_ATTEMPTS = 5
BACKOFF_S = (1, 5, 30, 120, 600)      # wait before attempt 2..6
TIMEOUT_S = 5.0
QUEUE_MAX = 500
MAX_BODY_BYTES = 256 * 1024

EVENTS = (
    "device.claimed",
    "device.online",
    "device.offline",
    "device.telemetry",
    "session.started",
    "session.connected",
    "session.ended",
    "session.failed",
    "command.completed",
    "command.failed",
    "call.enriched",
)

_q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=QUEUE_MAX)
_worker_started = False
_start_lock = threading.Lock()


class WebhookError(ValueError):
    """Raised when a destination is not allowed."""


def _ip_is_public(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def validate_url(url: str, *, allow_http: bool = False) -> str:
    """Return a normalised URL or raise WebhookError.

    Blocks everything that could reach the server's own network: localhost,
    RFC1918, link-local (cloud metadata), CGNAT, and IPv6 equivalents. The
    hostname is resolved, so ``https://localtest.me`` cannot slip through.
    """
    url = (url or "").strip()
    if not url:
        raise WebhookError("url is required")
    if len(url) > 2048:
        raise WebhookError("url is too long")
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("https", "http"):
        raise WebhookError("url must start with https://")
    if scheme == "http" and not allow_http:
        raise WebhookError("https:// is required for webhook endpoints")
    if parts.username or parts.password:
        raise WebhookError("credentials in the url are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise WebhookError("url has no host")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise WebhookError("localhost destinations are not allowed")

    # Literal IPs are checked directly; names are resolved and every answer must be public.
    if _looks_like_ip(host):
        if not _ip_is_public(host):
            raise WebhookError("private, loopback or reserved addresses are not allowed")
    else:
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if scheme == "https" else 80),
                                       proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise WebhookError("host does not resolve")
        if not infos:
            raise WebhookError("host does not resolve")
        for info in infos:
            addr = info[4][0]
            if not _ip_is_public(addr):
                raise WebhookError("host resolves to a private or reserved address")

    port = parts.port
    if port is not None and not (1 <= port <= 65535):
        raise WebhookError("port out of range")
    return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over ``<timestamp>.<body>`` as ``t=..,v1=..`` (Stripe-style)."""
    mac = hmac.new(secret.encode(), b"%d." % timestamp + body, hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (timestamp, mac)


def verify(secret: str, header: str, body: bytes, tolerance_s: int = 300) -> bool:
    """Receiver-side helper used by the SDK and the tests."""
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
        ts = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError):
        return False
    if tolerance_s is not None and abs(time.time() - ts) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), b"%d." % ts + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def _record(delivery_id: str, **fields: Any) -> None:
    sets = ", ".join("%s=?" % k for k in fields)
    db.execute("UPDATE webhook_deliveries SET %s WHERE id=?" % sets,
               tuple(fields.values()) + (delivery_id,))


def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int | None, str | None]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            resp.read(2048)
            return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, e.read(512).decode("utf-8", "replace")[:300]
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return None, str(e)[:300]


def deliver(hook: dict, event: str, payload: dict, *, attempts: int = MAX_ATTEMPTS,
            block: bool = True) -> dict:
    """Deliver one event to one hook, retrying with backoff.

    With ``block=False`` the retries happen on the worker thread and the call
    returns as soon as the first attempt is queued.
    """
    delivery_id = db.new_id("whd")
    body = json.dumps({"event": event, "data": payload, "ts": db.now()},
                      separators=(",", ":"), default=str).encode()[:MAX_BODY_BYTES]
    db.execute("""INSERT INTO webhook_deliveries(id,webhook_id,tenant_id,event,status,
                  attempts,status_code,error,created_at,request_body)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""",
               (delivery_id, hook["id"], hook.get("tenant_id"), event, "pending", 0,
                None, None, db.now(), body.decode("utf-8", "replace")))

    status_code, error = None, None
    for n in range(1, attempts + 1):
        ts = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "GBox-Webhooks/1.0",
            "X-GBox-Event": event,
            "X-GBox-Delivery": delivery_id,
            "X-GBox-Signature": sign(hook.get("secret") or "", ts, body),
            "X-GBox-Timestamp": str(ts),
            # legacy header kept so existing receivers do not break
            "X-VB-Signature": hmac.new((hook.get("secret") or "").encode(), body,
                                       hashlib.sha256).hexdigest(),
        }
        status_code, error = _post(hook["url"], body, headers)
        ok = status_code is not None and 200 <= status_code < 300
        _record(delivery_id, attempts=n, status_code=status_code, error=error,
                status="delivered" if ok else ("retrying" if n < attempts else "failed"),
                delivered_at=db.now() if ok else None)
        db.execute("UPDATE webhooks SET last_status=?, last_attempt_at=? WHERE id=?",
                   (status_code, db.now(), hook["id"]))
        if ok:
            return {"ok": True, "delivery_id": delivery_id, "status_code": status_code,
                    "attempts": n}
        if n < attempts and block:
            time.sleep(BACKOFF_S[min(n - 1, len(BACKOFF_S) - 1)])
    return {"ok": False, "delivery_id": delivery_id, "status_code": status_code,
            "attempts": attempts, "error": error}


def _attempt(hook: dict, event: str, body: bytes, delivery_id: str, n: int) -> tuple[bool, int | None, str | None]:
    """One HTTP attempt; records it and reports the outcome."""
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "GBox-Webhooks/1.0",
        "X-GBox-Event": event,
        "X-GBox-Delivery": delivery_id,
        "X-GBox-Signature": sign(hook.get("secret") or "", ts, body),
        "X-GBox-Timestamp": str(ts),
        # legacy header kept so existing receivers do not break
        "X-VB-Signature": hmac.new((hook.get("secret") or "").encode(), body,
                                   hashlib.sha256).hexdigest(),
    }
    status_code, error = _post(hook["url"], body, headers)
    ok = status_code is not None and 200 <= status_code < 300
    _record(delivery_id, attempts=n, status_code=status_code, error=error,
            status="delivered" if ok else "retrying",
            delivered_at=db.now() if ok else None)
    db.execute("UPDATE webhooks SET last_status=?, last_attempt_at=? WHERE id=?",
               (status_code, db.now(), hook["id"]))
    return ok, status_code, error


# --- scheduler ------------------------------------------------------------- #
# Retries must not sleep inside the worker: with a 600 s backoff a single slow
# endpoint would block every other tenant's events behind it. Jobs live in a
# heap ordered by their next-attempt time and the worker waits only until the
# next one is due.
import heapq as _heap
import itertools as _it

_sched: list = []
_sched_cv = threading.Condition()
_seq = _it.count()


def _push(job: dict, delay: float = 0.0) -> None:
    with _sched_cv:
        _heap.heappush(_sched, (time.time() + delay, next(_seq), job))
        _sched_cv.notify()


def _worker() -> None:
    while True:
        with _sched_cv:
            while not _sched:
                _sched_cv.wait()
            due, _, job = _sched[0]
            wait = due - time.time()
            if wait > 0:
                _sched_cv.wait(wait)
                continue
            _heap.heappop(_sched)
        try:
            n = job["attempt"]
            body = job["body"]
            ok, status, error = _attempt(job["hook"], job["event"], body,
                                         job["delivery_id"], n)
            if not ok and n < MAX_ATTEMPTS:
                _push({**job, "attempt": n + 1}, BACKOFF_S[min(n - 1, len(BACKOFF_S) - 1)])
            elif not ok:
                _record(job["delivery_id"], status="failed")
        except Exception:
            pass


def start_worker() -> None:
    global _worker_started
    with _start_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, name="gbox-webhooks", daemon=True).start()
        _worker_started = True


def enqueue(hook: dict, event: str, payload: dict) -> bool:
    """Queue the first attempt without blocking the caller. False = queue is full."""
    start_worker()
    with _sched_cv:
        if len(_sched) >= QUEUE_MAX:
            full = True
        else:
            full = False
    if full:
        db.execute("""INSERT INTO webhook_deliveries(id,webhook_id,tenant_id,event,status,
                      attempts,created_at,error) VALUES(?,?,?,?,?,?,?,?)""",
                   (db.new_id("whd"), hook["id"], hook.get("tenant_id"), event, "dropped",
                    0, db.now(), "delivery queue full"))
        return False

    delivery_id = db.new_id("whd")
    body = json.dumps({"event": event, "data": payload, "ts": db.now()},
                      separators=(",", ":"), default=str).encode()[:MAX_BODY_BYTES]
    db.execute("""INSERT INTO webhook_deliveries(id,webhook_id,tenant_id,event,status,
                  attempts,status_code,error,created_at,request_body)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""",
               (delivery_id, hook["id"], hook.get("tenant_id"), event, "pending", 0,
                None, None, db.now(), body.decode("utf-8", "replace")))
    _push({"hook": hook, "event": event, "body": body, "delivery_id": delivery_id,
           "attempt": 1})
    return True
