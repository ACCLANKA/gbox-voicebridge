"""Per-device transcript store — the first platform surface built on captured voice.

Every utterance the platform recognises is appended with a timestamp, so a developer (and the
console) can read what the device heard without touching logs. One JSONL file per device.

Line shape:
    {"ts": "2026-10-21T07:02:11Z", "role": "heard"|"said", "text": "...", "session": 3}
"""
from __future__ import annotations

import json
import os
import threading
import time

ROOT = os.environ.get("TRANSCRIPT_DIR", "/opt/voicebridge/data/transcripts")
_lock = threading.Lock()


def _path(serial: str) -> str:
    safe = "".join(c for c in serial if c.isalnum() or c in "-_") or "unknown"
    return os.path.join(ROOT, safe + ".jsonl")


def record(serial: str, role: str, text: str, session: int = 0) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    try:
        os.makedirs(ROOT, exist_ok=True)
        line = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "role": role, "text": text, "session": int(session or 0),
        }, ensure_ascii=False)
        with _lock, open(_path(serial), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except OSError:
        return False


def read(serial: str, limit: int = 50, session: int = 0) -> list[dict]:
    path = _path(serial)
    if not os.path.exists(path):
        return []
    with _lock, open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    out = []
    for raw in lines[-(limit * 3 if session else limit):]:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if session and int(row.get("session") or 0) != session:
            continue
        out.append(row)
    return out[-limit:]
