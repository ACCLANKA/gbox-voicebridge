#!/usr/bin/env python3
"""Bridge the builder agent to the desktop agent (Hermes).

Two transports, chosen with --mode:

  git   Push this repo and the handoff bundle to a remote Hermes can pull.
        Needs a remote URL (and optionally a token baked into it).

  http  POST a JSON task envelope to an endpoint Hermes listens on.
        Needs a URL and optionally a bearer token.

Nothing is hard-coded: credentials come from flags or the environment, so this
file is safe to commit. Use --dry-run to inspect what would be sent.

Examples:
  ./scripts/hermes-bridge.py git  --remote https://x-access-token:$GITHUB_TOKEN@github.com/acme/voicebridge.git
  ./scripts/hermes-bridge.py http --url https://desk.local:8787/task --token $HERMES_TOKEN \
                                --subject "flash fw 0.2.0" --body-file docs/handoff/HERMES-NEXT-TASK.md \
                                --attach firmware/build/voicebridge-esp32-merged.bin \
                                --attach firmware/build/handoff/voicebridge.bundle
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def rev() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def push_git(remote: str, branch: str, bundle: Path | None, tags: bool) -> int:
    print(f"pushing {branch} -> {remote}")
    refspecs = [branch]
    if tags:
        refspecs.append("--tags")
    rc = subprocess.run(["git", "-C", str(ROOT), "push", remote, *refspecs]).returncode
    if rc != 0:
        print("ERROR: git push failed", file=sys.stderr)
        return rc
    if bundle and bundle.exists():
        # Hermes may pull the bundle directly if the remote is unreachable.
        print(f"bundle ready at {bundle} ({bundle.stat().st_size} bytes)")
    print(f"OK: Hermes can now `git fetch {remote} {branch}`")
    return 0


def send_http(url: str, token: str | None, subject: str, body: str,
              attachments: list[Path], dry_run: bool) -> int:
    envelope = {
        "from": "builder",
        "to": "hermes",
        "ts": int(time.time()),
        "rev": rev(),
        "subject": subject,
        "body": body,
        "attachments": [
            {"name": p.name, "bytes": p.stat().st_size,
             "sha256": _sha256(p)}
            for p in attachments if p.exists()
        ],
    }
    payload = json.dumps(envelope, indent=2)
    if dry_run:
        print("--- envelope (dry run, not sent) ---")
        print(payload)
        for p in attachments:
            print(f"would upload: {p} ({p.stat().st_size} bytes)")
        return 0

    data = json.dumps({**envelope, "attachments": [
        {"name": p.name, "content_b64": base64.b64encode(p.read_bytes()).decode()}
        for p in attachments if p.exists()
    ]}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(f"POST {url} -> {r.status}")
            print(r.read().decode(errors="replace")[:500])
        return 0
    except Exception as exc:
        print(f"ERROR: POST failed: {exc}", file=sys.stderr)
        return 1


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Send work to the desktop agent (Hermes).")
    ap.add_argument("--mode", choices=["git", "http"], required=True)
    ap.add_argument("--remote", help="git remote URL (mode=git)")
    ap.add_argument("--branch", default="master")
    ap.add_argument("--bundle", type=Path,
                    default=ROOT / "firmware/build/handoff/voicebridge.bundle")
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("--url", help="endpoint Hermes listens on (mode=http)")
    ap.add_argument("--token", default=os.environ.get("HERMES_TOKEN"))
    ap.add_argument("--subject", default=f"voicebridge handoff @{rev()}")
    ap.add_argument("--body-file", type=Path)
    ap.add_argument("--attach", type=Path, action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.mode == "git":
        remote = args.remote or os.environ.get("HERMES_GIT_REMOTE")
        if not remote:
            print("ERROR: --remote or HERMES_GIT_REMOTE required for git mode", file=sys.stderr)
            return 2
        return push_git(remote, args.branch, args.bundle, args.tags)

    url = args.url or os.environ.get("HERMES_URL")
    if not url:
        print("ERROR: --url or HERMES_URL required for http mode", file=sys.stderr)
        return 2
    body = args.body_file.read_text() if args.body_file else ""
    return send_http(url, args.token, args.subject, body, args.attach, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
