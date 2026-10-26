"""Runtime configuration for the Voice Bridge control plane.

Values are read from the environment with safe development defaults so the
platform boots with zero configuration (SQLite file DB + in-process fallbacks).
In production these are supplied by the environment / a secrets manager.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _db_default() -> str:
    return str(REPO_ROOT / "data" / "voicebridge.db")


# Database (SQLite for local/dev; a Postgres DSN can be layered in later).
DB_PATH = os.environ.get("VB_DB", _db_default())

# Authentication / crypto.
JWT_SECRET = os.environ.get("VB_JWT_SECRET", "dev-insecure-jwt-secret-change-me")
JWT_ALG = "HS256"
ACCESS_TTL_SEC = int(os.environ.get("VB_ACCESS_TTL", 3600))
REFRESH_TTL_SEC = int(os.environ.get("VB_REFRESH_TTL", 30 * 24 * 3600))

# Media / telephony domain defaults.
DEFAULT_CODEC = os.environ.get("VB_DEFAULT_CODEC", "opus")
DEFAULT_SAMPLE_RATE = int(os.environ.get("VB_DEFAULT_SAMPLE_RATE", 16000))
CORRELATION_WINDOW_SEC = float(os.environ.get("VB_CORRELATION_WINDOW", 5.0))
# A ring that arrives for an open session with no live audio link and older than this is a
# NEW call, not a repeat: the old session is closed and a fresh one opened. find_active_session
# correlates to any non-terminal session regardless of age, so without this every later call
# was folded into the first one (and that row never ended) - observed as calls 2 and 3 never
# starting a session. Generous enough that a duplicate/retransmitted ring is still idempotent.
STALE_RING_SEC = float(os.environ.get("VB_STALE_RING_SEC", 45.0))

# Billing rounding: 60/60 => round up each started minute to a full minute.
BILLING_ROUND_SEC = int(os.environ.get("VB_BILLING_ROUND_SEC", 60))

# PSTN / carrier trunk (Phase 2). A ``pstn`` destination dials this carrier's SBC.
# Per-route/override options (``carrier_host``, ``transport``, …) override these.
PSTN_CARRIER = os.environ.get("VB_PSTN_CARRIER", "")           # e.g. "sbc.carrier.example:5060"
PSTN_TRANSPORT = os.environ.get("VB_PSTN_TRANSPORT", "udp")
PSTN_DEFAULT_COUNTRY = os.environ.get("VB_PSTN_DEFAULT_COUNTRY", "US")

# The operator's own tenant and first admin user. A real console starts with exactly these
# and nothing else - no example devices, no sample calls. Those live behind VB_DEMO_DATA.
TENANT_ID = os.environ.get("VB_TENANT_ID", "ten_1")
TENANT_NAME = os.environ.get("VB_TENANT_NAME", "Gigies")
TENANT_SLUG = os.environ.get("VB_TENANT_SLUG", "gigies")
ADMIN_EMAIL = os.environ.get("VB_ADMIN_EMAIL", "admin@gigies.lk")
ADMIN_NAME = os.environ.get("VB_ADMIN_NAME", "Console Admin")
CONSOLE_URL = os.environ.get("VB_CONSOLE_URL", "https://console.site.gigies.lk")

# Service identity.
API_TITLE = "Voice Bridge API"
API_VERSION = "1.0.0"
