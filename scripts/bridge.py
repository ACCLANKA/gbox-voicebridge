#!/usr/bin/env python3
"""Builder <-> Hermes agent bridge client.

Talks to the ``/v1/ops`` endpoints (api/routers/ops.py) on either the local API
or the VPS. Stdlib only, so it runs on the bench machine too.

Config (flags override env):
    VOICEBRIDGE_URL   e.g. https://vb.site.gigies.lk
    VOICEBRIDGE_KEY   an API key with the "ops" scope (or an owner/admin JWT)

Examples:
    bridge.py ping
    bridge.py send --subject "flash fw 0.3.0" --body-file docs/handoff/HERMES-NEXT-TASK.md \
        --serial GBX-BENCH01 --ref 2caf06c --attach firmware/build/handoff/voicebridge-app.bin
    bridge.py tasks --status pending
    bridge.py claim <id>
    bridge.py ack <id> --status done -m "flashed, distinct=1 (no mic)"
    bridge.py watch --serial GBX-BENCH01 --timeout 300
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(base: str, key: str | None, method: str, path: str,
         body: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"detail": raw}
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller
        return 0, {"detail": str(exc)}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _die(status: int, payload: dict) -> None:
    print(f"error (HTTP {status}): {payload.get('detail', payload)}", file=sys.stderr)
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Talk to the Voice Bridge agent bridge.")
    p.add_argument("--base", default=os.environ.get("VOICEBRIDGE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--key", default=os.environ.get("VOICEBRIDGE_KEY"))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping")

    s = sub.add_parser("send", help="enqueue a task for the other side")
    s.add_argument("--subject", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--body-file", type=Path)
    s.add_argument("--serial")
    s.add_argument("--ref")
    s.add_argument("--attach", type=Path, action="append", default=[])

    t = sub.add_parser("tasks")
    t.add_argument("--status", choices=["pending", "claimed", "done", "failed"])
    t.add_argument("--limit", type=int, default=20)

    for name in ("show", "claim"):
        a = sub.add_parser(name)
        a.add_argument("task_id")

    ack = sub.add_parser("ack")
    ack.add_argument("task_id")
    ack.add_argument("--status", choices=["done", "failed"], default="done")
    ack.add_argument("-m", "--message", default="")
    ack.add_argument("--report-file", type=Path)

    w = sub.add_parser("watch", help="poll a device until it is online on the VPS")
    w.add_argument("--serial", required=True)
    w.add_argument("--interval", type=float, default=5.0)
    w.add_argument("--timeout", type=float, default=300.0)

    d = sub.add_parser("devices")
    d.add_argument("--serial")

    c = sub.add_parser("control", help="write a control frame to a live device")
    c.add_argument("--serial", required=True)
    c.add_argument("--action", required=True, help="tone | answer | hangup | dial ...")
    c.add_argument("--hz", type=int)
    c.add_argument("--ms", type=int)
    c.add_argument("--number")
    c.add_argument("--json", default="", help="extra JSON merged into the frame")

    au = sub.add_parser("audio", help="write a binary downlink audio frame to a live device")
    au.add_argument("--serial", required=True)
    au.add_argument("--pcm-file", type=Path, help="raw PCM16 payload")
    au.add_argument("--pcm-hex", default="", help="PCM16 payload as hex")
    au.add_argument("--kind", type=int, default=1)

    lv = sub.add_parser("live", help="is the device socket open right now?")
    lv.add_argument("--serial", required=True)

    args = p.parse_args(argv)
    key = args.key

    if args.cmd == "ping":
        st, body = call(args.base, None, "GET", "/v1/ops/ping")
        print(json.dumps(body, indent=2))
        return 0 if st == 200 else 1

    if args.cmd == "devices":
        path = f"/v1/ops/devices/{args.serial}" if args.serial else "/v1/ops/devices"
        st, body = call(args.base, key, "GET", path)
        if st != 200:
            _die(st, body)
        print(json.dumps(body, indent=2))
        return 0

    if args.cmd == "send":
        body_text = args.body_file.read_text() if args.body_file else args.body
        atts = [{"name": f.name, "sha256": _sha256(f), "bytes": f.stat().st_size}
                for f in args.attach if f.exists()]
        payload = {"subject": args.subject, "body": body_text, "serial": args.serial,
                   "ref": args.ref, "attachments": atts}
        st, body = call(args.base, key, "POST", "/v1/ops/tasks", payload)
        if st != 201:
            _die(st, body)
        print(f"queued {body['id']} ({len(atts)} attachment(s))")
        return 0

    if args.cmd == "tasks":
        st, body = call(args.base, key, "GET",
                        "/v1/ops/tasks" + (f"?status={args.status}" if args.status else "")
                        + (("&" if args.status else "?") + f"limit={args.limit}"))
        if st != 200:
            _die(st, body)
        for task in body["tasks"]:
            print(f"{task['id']}  {task['status']:<8} {task['subject']}"
                  + (f"  [{task['serial']}]" if task["serial"] else ""))
        print(f"-- {body['total']} task(s)")
        return 0

    if args.cmd in ("show", "claim"):
        method = "GET" if args.cmd == "show" else "POST"
        path = f"/v1/ops/tasks/{args.task_id}" + ("" if args.cmd == "show" else "/claim")
        st, body = call(args.base, key, method, path, {} if method == "POST" else None)
        if st not in (200, 201):
            _die(st, body)
        if args.cmd == "show":
            print(json.dumps(body, indent=2))
        else:
            print(f"{body['id']} -> {body['status']}")
        return 0

    if args.cmd == "ack":
        report = args.report_file.read_text() if args.report_file else args.message
        st, body = call(args.base, key, "POST", f"/v1/ops/tasks/{args.task_id}/ack",
                        {"status": args.status, "report": report})
        if st != 200:
            _die(st, body)
        print(f"{body['id']} -> {body['status']}")
        return 0

    if args.cmd == "watch":
        deadline = time.time() + args.timeout
        last = None
        while time.time() < deadline:
            st, body = call(args.base, key, "GET", f"/v1/ops/devices/{args.serial}")
            if st == 200:
                state = (body.get("online"), body.get("ip"), body.get("fw"))
                if state != last:
                    ts = time.strftime("%H:%M:%S")
                    print(f"{ts}  {args.serial}  online={body.get('online')}  "
                          f"ip={body.get('ip')}  fw={body.get('fw')}  last_seen={body.get('last_seen')}")
                    last = state
                if body.get("online"):
                    print("device is online on the VPS.")
                    return 0
            elif st and st != 404:
                _die(st, body)
            time.sleep(args.interval)
        print(f"timed out after {args.timeout:.0f}s waiting for {args.serial}", file=sys.stderr)
        return 1

    if args.cmd == "control":
        payload: dict = {"action": args.action}
        if args.json:
            payload.update(json.loads(args.json))
        for k in ("hz", "ms", "number"):
            v = getattr(args, k)
            if v is not None:
                payload[k] = v
        st, body = call(args.base, key, "POST",
                        f"/v1/ops/devices/{args.serial}/control", payload)
        if st != 200:
            _die(st, body)
        print(f"sent to {args.serial}: {json.dumps(body['sent'])}")
        return 0

    if args.cmd == "audio":
        if args.pcm_file:
            pcm = args.pcm_file.read_bytes()
        elif args.pcm_hex:
            pcm = bytes.fromhex(args.pcm_hex)
        else:
            _die(422, {"detail": "give --pcm-file or --pcm-hex"})
        st, body = call(args.base, key, "POST", f"/v1/ops/devices/{args.serial}/audio",
                        {"pcm_base64": base64.b64encode(pcm).decode(), "kind": args.kind})
        if st != 200:
            _die(st, body)
        print(f"sent {body['bytes']} bytes (kind {body['kind']}) to {args.serial}")
        return 0

    if args.cmd == "live":
        st, body = call(args.base, key, "GET", f"/v1/ops/devices/{args.serial}/live")
        if st != 200:
            _die(st, body)
        print(f"{args.serial}: connected={body['connected']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
