"""SQLite-backed store for Voice Bridge.

Implements the schema from ``docs/design/DATA-MODEL.md`` (tenants, users,
projects, devices, telemetry, routes, bridges, CDR, recordings, plans,
subscriptions, usage, invoices, payments, api keys, firmware, audit logs) plus
the call-detection additions from ``docs/api/CALL-EVENTS-API.md``
(call_sessions, call_events, enrichment_events, phone_devices).

The module exposes tiny connection helpers (``query`` / ``one`` / ``execute``)
guarded by a re-entrant lock, mirroring the original store but with a full
schema. Money is stored as integer minor units + currency, never floats.
"""
from __future__ import annotations

import os
import random
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config

DB_PATH = Path(os.environ.get("VB_DB", config.DB_PATH))


def now() -> str:
    """UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_conn = _connect()
_lock = threading.RLock()

# Columns added to `devices` after the initial dashboard-only schema. Applied
# idempotently so existing local databases upgrade in place.
_DEVICE_UPGRADES = {
    "mac": "TEXT",
    "cert_fingerprint": "TEXT",
    "codec_pref": "TEXT",
    "pop_region": "TEXT",
    "project_id": "TEXT",
    "fw_version": "TEXT",
    "last_seen_at": "TEXT",
    # Link health as the DEVICE measures it. These were being sent and
    # dropped on the floor, which is why a mains-powered board (no serial)
    # could not be measured at all.
    "tx_ok": "INTEGER",
    "tx_drops": "INTEGER",
    "tx_fail_streak": "INTEGER",
    "tx_age_s": "INTEGER",
    "ul_ovw": "INTEGER",
}


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        _conn.execute(sql, params)
        _conn.commit()


def insert(table: str, values: dict[str, Any]) -> str:
    """Insert a row from a dict; returns the row's ``id`` if present."""
    cols = ", ".join(values.keys())
    placeholders = ", ".join(["?"] * len(values))
    execute(f"INSERT INTO {table}({cols}) VALUES({placeholders})", tuple(values.values()))
    return str(values.get("id", ""))


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT,
  status TEXT DEFAULT 'active', plan TEXT DEFAULT 'starter',
  balance_cents INTEGER DEFAULT 0, currency TEXT DEFAULT 'USD',
  credit_limit_cents INTEGER DEFAULT 0,
  contact_email TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, tenant_id TEXT, email TEXT UNIQUE NOT NULL,
  password_hash TEXT, name TEXT, role TEXT DEFAULT 'viewer',
  mfa_enabled INTEGER DEFAULT 0, last_login_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, description TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, serial TEXT UNIQUE NOT NULL, name TEXT,
  tenant_id TEXT, project TEXT, project_id TEXT, bluetooth_name TEXT,
  status TEXT DEFAULT 'unprovisioned',
  ip TEXT, ssid TEXT, rssi INTEGER, uptime_s INTEGER DEFAULT 0,
  free_heap_kb INTEGER, fw TEXT, fw_version TEXT, codec TEXT DEFAULT 'opus',
  codec_pref TEXT, sample_rate INTEGER DEFAULT 16000, battery_pct INTEGER,
  aec_enabled INTEGER DEFAULT 1, volume INTEGER DEFAULT 70, mic_gain INTEGER DEFAULT 60,
  echo_erle_db INTEGER, muted INTEGER DEFAULT 0, mac TEXT, cert_fingerprint TEXT,
  pop_region TEXT, last_seen TEXT, last_seen_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS wifi_profiles (
  id TEXT PRIMARY KEY, device_id TEXT, ssid TEXT NOT NULL, psk TEXT,
  security TEXT DEFAULT 'wpa2', hidden INTEGER DEFAULT 0,
  priority INTEGER DEFAULT 10, auto_connect INTEGER DEFAULT 1, created_at TEXT
);
CREATE TABLE IF NOT EXISTS networks (
  id TEXT PRIMARY KEY, ssid TEXT NOT NULL, bssid TEXT, security TEXT,
  is_open INTEGER DEFAULT 0, is_requested INTEGER DEFAULT 0,
  band TEXT DEFAULT '2.4GHz', password TEXT, tenant_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS telemetry (
  id TEXT PRIMARY KEY, device_id TEXT, ts TEXT,
  rssi INTEGER, latency_ms INTEGER, jitter_ms REAL, loss_pct REAL, mos REAL,
  tx_kbps REAL, rx_kbps REAL, connection_state TEXT, extra TEXT
);
CREATE TABLE IF NOT EXISTS device_telemetry (
  id TEXT PRIMARY KEY, device_id TEXT, tenant_id TEXT, ts TEXT,
  rssi INTEGER, rtt_ms INTEGER, jitter_ms REAL, loss_pct REAL,
  free_heap INTEGER, uptime_sec INTEGER, fw_version TEXT
);
CREATE TABLE IF NOT EXISTS link_events (
  id TEXT PRIMARY KEY, device_id TEXT, ts TEXT, type TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS routes (
  id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, name TEXT,
  priority INTEGER DEFAULT 100, match_json TEXT, dest_type TEXT, dest_uri TEXT,
  options_json TEXT, fallback_json TEXT, enabled INTEGER DEFAULT 1, created_at TEXT
);
CREATE TABLE IF NOT EXISTS bridges (
  id TEXT PRIMARY KEY, tenant_id TEXT, device_id TEXT, project_id TEXT,
  route_id TEXT, direction TEXT, from_number TEXT, to_number TEXT,
  state TEXT DEFAULT 'initiated', codec TEXT, started_at TEXT, answered_at TEXT,
  ended_at TEXT, duration_sec INTEGER DEFAULT 0, packets INTEGER DEFAULT 0,
  bytes INTEGER DEFAULT 0, loss_pct REAL, jitter_ms REAL, mos REAL,
  recording_id TEXT, agent_id TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY, device_id TEXT, direction TEXT, source TEXT,
  incoming_number TEXT, whatsapp_number TEXT, caller_name TEXT,
  state TEXT, route TEXT, started_at TEXT, ended_at TEXT, duration_s INTEGER,
  mos REAL, jitter_ms REAL, loss_pct REAL, erle_db INTEGER, aec_enabled INTEGER,
  confidence REAL
);
CREATE TABLE IF NOT EXISTS call_sessions (
  id TEXT PRIMARY KEY, tenant_id TEXT, device_id TEXT, project_id TEXT,
  route_id TEXT, bridge_id TEXT, state TEXT DEFAULT 'detected',
  source TEXT DEFAULT 'unknown', direction TEXT DEFAULT 'incoming',
  caller_name TEXT, caller_number TEXT, incoming_number TEXT,
  whatsapp_number TEXT,
  detection_sco INTEGER DEFAULT 0, detection_hfp INTEGER DEFAULT 0,
  detection_companion INTEGER DEFAULT 0, detection_confidence REAL DEFAULT 0.4,
  identify_ms INTEGER, route TEXT,
  mos REAL, jitter_ms REAL, loss_pct REAL, erle_db INTEGER, aec_enabled INTEGER,
  started_at TEXT, answered_at TEXT, ended_at TEXT, duration_sec INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS call_events (
  id TEXT PRIMARY KEY, device_id TEXT, tenant_id TEXT, call_session_id TEXT,
  event TEXT, data_json TEXT, device_ts TEXT, server_ts TEXT, seq INTEGER
);
CREATE TABLE IF NOT EXISTS enrichment_events (
  id TEXT PRIMARY KEY, device_id TEXT, call_session_id TEXT, source_app TEXT,
  caller_name TEXT, caller_number TEXT, direction TEXT, confidence REAL,
  phone_ts TEXT, server_ts TEXT
);
CREATE TABLE IF NOT EXISTS phone_devices (
  id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT, device_id TEXT,
  platform TEXT, push_token TEXT, pair_code TEXT, app_version TEXT,
  paired_at TEXT, last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS cdr (
  id TEXT PRIMARY KEY, bridge_id TEXT, tenant_id TEXT, project_id TEXT,
  device_id TEXT, direction TEXT, from_number TEXT, to_number TEXT,
  route_id TEXT, started_at TEXT, answered_at TEXT, ended_at TEXT,
  duration_sec INTEGER DEFAULT 0, codec TEXT, packets INTEGER DEFAULT 0,
  bytes INTEGER DEFAULT 0, loss_pct REAL, jitter_ms REAL, mos REAL,
  cost_cents INTEGER DEFAULT 0, currency TEXT DEFAULT 'USD',
  billing_components_json TEXT, rounded_minutes INTEGER DEFAULT 0,
  recording_url TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS recordings (
  id TEXT PRIMARY KEY, tenant_id TEXT, bridge_id TEXT, object_key TEXT,
  size_bytes INTEGER, duration_sec INTEGER, codec TEXT, encrypted INTEGER DEFAULT 1,
  retention_until TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, name TEXT, base_price_cents INTEGER, currency TEXT DEFAULT 'USD',
  included_devices INTEGER, included_minutes INTEGER, overage_per_min_cents INTEGER,
  features_json TEXT, is_public INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS subscriptions (
  id TEXT PRIMARY KEY, tenant_id TEXT, plan_id TEXT, status TEXT DEFAULT 'active',
  period_start TEXT, period_end TEXT, quantity INTEGER DEFAULT 1, price_cents INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS usage_records (
  id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, device_id TEXT,
  metric TEXT, quantity REAL, unit TEXT, occurred_at TEXT, idempotency_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS usage_rollups (
  tenant_id TEXT, project_id TEXT, metric TEXT, period TEXT,
  bucket TEXT, quantity REAL, computed_at TEXT,
  PRIMARY KEY (tenant_id, project_id, metric, period, bucket)
);
CREATE TABLE IF NOT EXISTS invoices (
  id TEXT PRIMARY KEY, tenant_id TEXT, number TEXT, status TEXT DEFAULT 'draft',
  subtotal_cents INTEGER, tax_cents INTEGER, total_cents INTEGER, currency TEXT DEFAULT 'USD',
  period_start TEXT, period_end TEXT, issued_at TEXT, due_at TEXT, pdf_url TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS invoice_lines (
  id TEXT PRIMARY KEY, invoice_id TEXT, description TEXT, metric TEXT,
  quantity REAL, unit_price_cents INTEGER, amount_cents INTEGER
);
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY, tenant_id TEXT, invoice_id TEXT, provider TEXT,
  provider_ref TEXT, amount_cents INTEGER, currency TEXT DEFAULT 'USD',
  status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, key_hash TEXT, prefix TEXT,
  scopes TEXT, last_used_at TEXT, expires_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS firmware (
  id TEXT PRIMARY KEY, version TEXT, channel TEXT DEFAULT 'stable',
  object_key TEXT, sha256 TEXT, signature TEXT, notes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS firmware_deployments (
  id TEXT PRIMARY KEY, firmware_id TEXT, target TEXT, device_id TEXT,
  status TEXT DEFAULT 'pending', created_at TEXT, applied_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id TEXT PRIMARY KEY, tenant_id TEXT, actor_user_id TEXT, action TEXT,
  target_type TEXT, target_id TEXT, metadata_json TEXT, ip TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS webhooks (
  id TEXT PRIMARY KEY, tenant_id TEXT, url TEXT, secret TEXT, events TEXT,
  enabled INTEGER DEFAULT 1, created_at TEXT, last_status INTEGER, last_attempt_at TEXT
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id TEXT PRIMARY KEY, webhook_id TEXT, tenant_id TEXT, event TEXT,
  status TEXT, attempts INTEGER DEFAULT 0, status_code INTEGER, error TEXT,
  created_at TEXT, delivered_at TEXT, request_body TEXT
);
CREATE INDEX IF NOT EXISTS idx_whd_hook ON webhook_deliveries(webhook_id, created_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS device_forwarding (
  device_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  dest_type TEXT NOT NULL, dest_uri TEXT NOT NULL,
  options_json TEXT, enabled INTEGER DEFAULT 1, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS dids (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, number TEXT NOT NULL UNIQUE,
  country TEXT, label TEXT, device_id TEXT, project_id TEXT, route_id TEXT,
  capabilities TEXT DEFAULT 'voice', enabled INTEGER DEFAULT 1, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ops_tasks (
  id TEXT PRIMARY KEY, tenant_id TEXT, serial TEXT, ref TEXT,
  subject TEXT NOT NULL, body TEXT, attachments_json TEXT,
  status TEXT DEFAULT 'pending', created_at TEXT,
  claimed_at TEXT, done_at TEXT, report TEXT
);
"""

TABLES = [
    "ops_tasks",
    "webhooks", "audit_logs", "firmware_deployments", "firmware", "api_keys",
    "payments", "invoice_lines", "invoices", "usage_rollups", "usage_records",
    "subscriptions", "plans", "recordings", "cdr", "phone_devices",
    "enrichment_events", "call_events", "call_sessions", "calls", "bridges",
    "routes", "link_events", "device_telemetry", "telemetry", "networks",
    "wifi_profiles", "devices", "projects", "users", "tenants", "settings",
    "device_forwarding", "dids",
]


def record_telemetry(device_id: str, fields: dict[str, Any]) -> str:
    """Persist one real telemetry frame from a device.

    This table used to be filled only by the demo seed, with random.randint and a synthetic
    "Office-WiFi" - so the console's history was never anything a bench could contradict. Two
    rules here keep it honest instead:

    * a column we do not measure stays NULL. latency_ms, jitter_ms, loss_pct, mos, tx_kbps and
      rx_kbps have no source yet; NULL says "not measured", which is the truth, and an empty
      table is more useful than a plausible lie.
    * anything without a column goes into `extra` as JSON, so the firmware can add a counter
      without a schema change.
    """
    measured = ("rssi", "latency_ms", "jitter_ms", "loss_pct", "mos", "tx_kbps", "rx_kbps")
    extra = {k: v for k, v in fields.items()
             if k not in measured and k not in ("type", "ts", "seq", "kind")}
    tid = new_id("tl")
    execute("""INSERT INTO telemetry(id,device_id,ts,rssi,latency_ms,jitter_ms,loss_pct,mos,
               tx_kbps,rx_kbps,connection_state,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, device_id, now(), fields.get("rssi"),
             *(fields.get(k) for k in measured[1:]), "online", json.dumps(extra)))
    return tid


def _apply_upgrades() -> None:
    cols = {r["name"] for r in query("PRAGMA table_info(devices)")}
    for name, decl in _DEVICE_UPGRADES.items():
        if name not in cols:
            execute(f"ALTER TABLE devices ADD COLUMN {name} {decl}")
    # call_events.seq: the device holds state events until they are acked and re-sends
    # them after a reconnect, so the receiver needs the device's sequence number to stay
    # idempotent (otherwise a resent call.ended is applied twice).
    cols = {r["name"] for r in query("PRAGMA table_info(call_events)")}
    if "seq" not in cols:
        execute("ALTER TABLE call_events ADD COLUMN seq INTEGER")
    # telemetry.extra: added so a new device counter lands without a migration dance.
    cols = {r["name"] for r in query("PRAGMA table_info(telemetry)")}
    if "extra" not in cols:
        execute("ALTER TABLE telemetry ADD COLUMN extra TEXT")


def demo_enabled() -> bool:
    """Fictional tenants, devices and calls are demo furniture, never production data.

    Without VB_DEMO_DATA the console starts with the operator's tenant, the plans and one
    admin user - and no devices at all, so "which of these are real?" never has to be asked.
    """
    return os.environ.get("VB_DEMO_DATA", "").strip().lower() in ("1", "true", "yes", "on")


def migrate() -> None:
    """Create or upgrade the schema WITHOUT seeding. Safe on every boot.

    Nothing called ``init()`` in production: the tables existed only because they
    had been created by hand, so a fresh deployment would have come up against an
    empty database and any new table (webhook_deliveries) would never appear.
    """
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()
    _apply_upgrades()


def init() -> None:
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()
    _apply_upgrades()
    if demo_enabled():
        seed()
        # Backfill demo users on databases seeded before the users table was populated.
        _ensure_demo_users(now())
    else:
        seed_baseline()


def reset() -> None:
    with _lock:
        for t in TABLES:
            _conn.execute(f"DELETE FROM {t}")
        _conn.commit()
    seed()


def get_setting(key: str, default: str | None = None) -> str | None:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# --------------------------------------------------------------------------- #
# Seed data
# --------------------------------------------------------------------------- #
_DEMO_USERS = [
    ("usr_owner", "ten_1", "owner@acme.example", "Ada Owner", "owner"),
    ("usr_admin", "ten_1", "admin@acme.example", "Alan Admin", "admin"),
    ("usr_op", "ten_1", "operator@acme.example", "Ophelia Operator", "operator"),
    ("usr_view", "ten_1", "viewer@acme.example", "Vic Viewer", "viewer"),
    ("usr_bill", "ten_1", "billing@acme.example", "Bea Billing", "billing"),
    ("usr_globex", "ten_2", "owner@globex.example", "Grace Globex", "owner"),
]


def _ensure_demo_users(ts: str) -> None:
    """Insert any missing demo users (idempotent backfill for pre-existing DBs)."""
    from . import security  # local import to avoid an import cycle at load time
    for uid, tenant_id, email, name, role in _DEMO_USERS:
        if one("SELECT id FROM users WHERE email=?", (email,)):
            continue
        execute("""INSERT INTO users(id,tenant_id,email,password_hash,name,role,
                   mfa_enabled,created_at) VALUES(?,?,?,?,?,?,0,?)""",
                (uid, tenant_id, email, security.hash_password("password123"),
                 name, role, ts))


def _seed_plans() -> None:
    plans = [
        ("plan_starter", "Starter", 2900, 10, 500, 5),
        ("plan_business", "Business", 9900, 50, 3000, 4),
        ("plan_enterprise", "Enterprise", 49900, 500, 25000, 3),
    ]
    for pid, name, base, inc_dev, inc_min, overage in plans:
        execute("""INSERT INTO plans(id,name,base_price_cents,currency,included_devices,
                   included_minutes,overage_per_min_cents,features_json,is_public)
                   VALUES(?,?,?,?,?,?,?,?,1)""",
                (pid, name, base, "USD", inc_dev, inc_min, overage,
                 '{"recording": true, "pstn": true, "webrtc": true}'))


def _ensure_admin_user() -> None:
    """The operator's first login. Password from VB_ADMIN_PASSWORD, else generated once and
    printed to the log so a fresh deployment is usable without shipping a default."""
    from . import security  # local import to avoid an import cycle at load time
    if one("SELECT id FROM users WHERE email=?", (config.ADMIN_EMAIL,)):
        return
    generated = None
    password = os.environ.get("VB_ADMIN_PASSWORD", "")
    if not password:
        import secrets
        password = generated = secrets.token_urlsafe(12)
    execute("""INSERT INTO users(id,tenant_id,email,password_hash,name,role,
               mfa_enabled,created_at) VALUES(?,?,?,?,?,?,0,?)""",
            (new_id("usr"), config.TENANT_ID, config.ADMIN_EMAIL,
             security.hash_password(password), config.ADMIN_NAME, "owner", now()))
    if generated:
        # Not to stdout: a container keeps uvicorn's own lines and drops print() output (this
        # is exactly how the generated password was lost on the gigies host), and a password
        # in a log is a leak. Write it owner-only beside the database, which is already secret.
        path = Path(config.DB_PATH).parent / "admin_password.txt"
        try:
            path.write_text(f"{config.ADMIN_EMAIL} {generated}\n", encoding="utf-8")
            os.chmod(path, 0o600)
            print(f"[voicebridge] created admin {config.ADMIN_EMAIL}; password written to {path}",
                  flush=True)
        except OSError as exc:
            print(f"[voicebridge] created admin {config.ADMIN_EMAIL} but could not write "
                  f"{path} ({exc}); password: {generated}", flush=True)


def seed_baseline() -> None:
    """What a real console needs and nothing fictional: settings, plans, the operator's
    tenant and one admin user. No example devices, no sample calls, no invented telemetry."""
    ts = now()
    if not one("SELECT id FROM tenants LIMIT 1"):
        execute("""INSERT INTO tenants(id,name,slug,status,plan,balance_cents,currency,
                   credit_limit_cents,contact_email,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (config.TENANT_ID, config.TENANT_NAME, config.TENANT_SLUG, "active",
                 "business", 0, "USD", 0, config.ADMIN_EMAIL, ts))
    set_setting("device_id_prefix", "GBX")
    if not one("SELECT id FROM plans LIMIT 1"):
        _seed_plans()
    _ensure_admin_user()


def seed() -> None:
    if one("SELECT id FROM tenants LIMIT 1"):
        return
    ts = now()
    execute("""INSERT INTO tenants(id,name,slug,status,plan,balance_cents,currency,
               credit_limit_cents,contact_email,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("ten_1", "Acme Voice Ltd", "acme", "active", "growth", 1284500, "USD",
             500000, "ops@acme.example", ts))
    execute("""INSERT INTO tenants(id,name,slug,status,plan,balance_cents,currency,
               credit_limit_cents,contact_email,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("ten_2", "Globex Messenger", "globex", "active", "starter", 0, "USD",
             0, "it@globex.example", ts))
    set_setting("device_id_prefix", "GBX")

    _seed_plans()

    _ensure_demo_users(ts)

    execute("""INSERT INTO subscriptions(id,tenant_id,plan_id,status,period_start,
               period_end,quantity,price_cents,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("sub_1", "ten_1", "plan_business", "active", ts, ts, 4, 9900, ts))

    projects = [
        ("prj_1", "ten_1", "Reception", "Front-desk devices"),
        ("prj_2", "ten_1", "Warehouse", "Warehouse bay devices"),
        ("prj_3", "ten_1", "Security", "Gate & perimeter"),
        ("prj_4", "ten_1", "Lab", "Lab / spare units"),
    ]
    for pid, tid, name, desc in projects:
        execute("INSERT INTO projects(id,tenant_id,name,description,created_at) VALUES(?,?,?,?,?)",
                (pid, tid, name, desc, ts))

    devices = [
        ("dev_1", "GBX-A1B2C3", "Reception Desk", "prj_1", "Reception", "online",
         "192.168.1.42", "Office-WiFi", -56, 128, 1),
        ("dev_2", "GBX-7F3C9D", "Warehouse Bay", "prj_2", "Warehouse", "degraded",
         "192.168.1.77", "Office-WiFi", -74, 2, 52),
        ("dev_3", "GBX-2D91E4", "Main Gate", "prj_3", "Security", "online",
         "192.168.1.90", "Office-WiFi", -61, 96, 1),
        ("dev_4", "GBX-5A0B77", "Spare Unit", "prj_4", "Lab", "offline",
         None, None, None, 0, None),
    ]
    for i, (did, serial, name, prid, project, status, ip, ssid, rssi, uptime, fw) in enumerate(devices):
        fwv = f"1.0.{3 + i % 3}"
        execute("""INSERT INTO devices(id,serial,name,tenant_id,project,project_id,
                   bluetooth_name,status,ip,ssid,rssi,uptime_s,free_heap_kb,fw,fw_version,
                   codec,codec_pref,sample_rate,battery_pct,aec_enabled,volume,mic_gain,
                   echo_erle_db,mac,pop_region,last_seen,last_seen_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (did, serial, name, "ten_1", project, prid, f"Gbox-{serial[-4:]}", status,
                 ip, ssid, rssi, uptime, 84 - i * 6, fwv, fwv, "opus", "opus", 16000,
                 100 - i * 5, 1, 70, 60, 52 - i * 2,
                 f"24:6f:28:{i:02x}:11:22", "eu-west-1", ts, ts, ts))
        # 30 telemetry samples per device
        random.seed(100 + i)
        base = datetime.now(timezone.utc)
        for n in range(30):
            t = base - timedelta(minutes=(30 - n) * 2)
            srssi = random.randint(-80, -50) if status != "offline" else -95
            state = "online" if status != "offline" else "offline"
            execute("""INSERT INTO telemetry(id,device_id,ts,rssi,latency_ms,jitter_ms,loss_pct,
                       mos,tx_kbps,rx_kbps,connection_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("tl"), did, t.isoformat(), srssi, random.randint(60, 160),
                     round(random.uniform(5, 28), 1), round(random.uniform(0, 1.5), 2),
                     round(random.uniform(3.6, 4.5), 1), round(random.uniform(18, 26), 1),
                     round(random.uniform(18, 26), 1), state))
            execute("""INSERT INTO device_telemetry(id,device_id,tenant_id,ts,rssi,rtt_ms,
                       jitter_ms,loss_pct,free_heap,uptime_sec,fw_version)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("dt"), did, "ten_1", t.isoformat(), srssi,
                     random.randint(60, 160), round(random.uniform(5, 28), 1),
                     round(random.uniform(0, 1.5), 2), 84 - i * 6, uptime, fwv))
        if status != "offline":
            for mins, typ, detail in [
                (14, "disconnect", "Wi-Fi link lost (beacon timeout)"),
                (13, "reconnect", "Reconnected to Office-WiFi"),
                (40, "connect", "Connected to Office-WiFi"),
            ]:
                execute("INSERT INTO link_events(id,device_id,ts,type,detail) VALUES(?,?,?,?,?)",
                        (new_id("ev"), did, (base - timedelta(minutes=mins)).isoformat(),
                         typ, detail))

    for did, ssid in [("dev_1", "Office-WiFi"), ("dev_2", "Office-WiFi"),
                      ("dev_3", "Office-WiFi"), ("dev_4", "Office-Guest")]:
        execute("""INSERT INTO wifi_profiles(id,device_id,ssid,psk,security,hidden,
                   priority,auto_connect,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("wp"), did, ssid, "AcmeWifi!2024", "wpa2", 0, 10, 1, ts))

    nets = [
        ("Office-WiFi", "wpa2", 0, 1, "2.4GHz", "AcmeWifi!2024"),
        ("Office-Guest", "open", 1, 0, "2.4GHz", None),
        ("Warehouse-AP", "wpa2", 0, 1, "2.4GHz", "Wh!se2024"),
        ("CafeOpenNet", "open", 1, 0, "2.4GHz", None),
        ("Neighbour-5G", "wpa2", 0, 0, "5GHz", None),
    ]
    for ssid, sec, openf, req, band, pw in nets:
        execute("""INSERT INTO networks(id,ssid,bssid,security,is_open,is_requested,band,
                   password,tenant_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (new_id("net"), ssid, ":".join(f"{i:02x}" for i in range(6)), sec,
                 openf, req, band, pw, "ten_1", ts))

    routes = [
        ("rt_1", "prj_1", "Reception to PBX", 100,
         '{"devices":["dev_1"],"direction":"inbound"}', "sip",
         "sip:reception@pbx.acme.com", '{"codec":"opus","record":true}',
         '{"type":"pstn","uri":"tel:+15551234"}'),
        ("rt_2", "prj_3", "Gate to SIP", 90,
         '{"devices":["dev_3"],"direction":"both"}', "sip",
         "sip:gate@pbx.acme.com", '{"codec":"opus","record":false}', None),
        ("rt_3", "prj_2", "Warehouse webhook", 50,
         '{"devices":["dev_2"]}', "webhook",
         "https://vps.acme.example/voice", '{"codec":"opus"}', None),
    ]
    for rid, prid, name, prio, match, dtype, duri, opts, fb in routes:
        execute("""INSERT INTO routes(id,tenant_id,project_id,name,priority,match_json,
                   dest_type,dest_uri,options_json,fallback_json,enabled,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,1,?)""",
                (rid, "ten_1", prid, name, prio, match, dtype, duri, opts, fb, ts))

    dids = [
        ("did_1", "+15551230001", "US", "Reception main line", "dev_1", "prj_1", "rt_1"),
        ("did_2", "+15551230002", "US", "Gate line", "dev_3", "prj_3", "rt_2"),
        ("did_3", "+442079460958", "GB", "London office (unassigned)", None, None, None),
    ]
    for did_id, num, ctry, label, dev, prj, rt in dids:
        execute("""INSERT INTO dids(id,tenant_id,number,country,label,device_id,
                   project_id,route_id,capabilities,enabled,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (did_id, "ten_1", num, ctry, label, dev, prj, rt, "voice", 1, ts))

    base = datetime.now(timezone.utc)
    # Active call session (WhatsApp) on dev_1
    cs = new_id("cs")
    execute("""INSERT INTO call_sessions(id,tenant_id,device_id,project_id,route_id,state,
               source,direction,caller_name,caller_number,incoming_number,whatsapp_number,
               detection_sco,detection_hfp,detection_companion,detection_confidence,identify_ms,
               route,mos,jitter_ms,loss_pct,erle_db,aec_enabled,started_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cs, "ten_1", "dev_1", "prj_1", "rt_1", "bridged", "whatsapp", "incoming",
             "Kamal Perera", "+94771234567", None, "+94771234567",
             1, 1, 1, 0.95, 120, "Reception -> sip:reception@pbx.acme.com",
             4.4, 11.0, 0.1, 52, 1, (base - timedelta(minutes=3)).isoformat(), ts))
    execute("""INSERT INTO calls(id,device_id,direction,source,incoming_number,whatsapp_number,
               caller_name,state,route,started_at,ended_at,duration_s,mos,jitter_ms,loss_pct,
               erle_db,aec_enabled,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("call"), "dev_1", "incoming", "whatsapp", None, "+94771234567",
             "Kamal Perera", "bridged", "Reception -> sip:reception@pbx.acme.com",
             (base - timedelta(minutes=3)).isoformat(), None, 180, 4.4, 11.0, 0.1, 52, 1, 0.95))

    # Ended cellular call (with CDR) on dev_3
    br = new_id("br")
    execute("""INSERT INTO bridges(id,tenant_id,device_id,project_id,route_id,direction,
               from_number,to_number,state,codec,started_at,answered_at,ended_at,duration_sec,
               packets,bytes,loss_pct,jitter_ms,mos,agent_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (br, "ten_1", "dev_3", "prj_3", "rt_2", "incoming", "+94775559876",
             "sip:gate@pbx.acme.com", "ended", "opus",
             (base - timedelta(minutes=55)).isoformat(), (base - timedelta(minutes=55)).isoformat(),
             (base - timedelta(minutes=50)).isoformat(), 300, 15000, 3000000, 0.4, 18.0, 4.1, "agent-1"))
    execute("""INSERT INTO calls(id,device_id,direction,source,incoming_number,whatsapp_number,
               caller_name,state,route,started_at,ended_at,duration_s,mos,jitter_ms,loss_pct,
               erle_db,aec_enabled,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("call"), "dev_3", "incoming", "cellular", "+94775559876", None,
             None, "ended", "Security -> sip:gate@pbx.acme.com",
             (base - timedelta(minutes=55)).isoformat(), (base - timedelta(minutes=50)).isoformat(),
             300, 4.1, 18.0, 0.4, 47, 1, 0.7))
    execute("""INSERT INTO cdr(id,bridge_id,tenant_id,project_id,device_id,direction,from_number,
               to_number,route_id,started_at,answered_at,ended_at,duration_sec,codec,packets,
               bytes,loss_pct,jitter_ms,mos,cost_cents,currency,billing_components_json,
               rounded_minutes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("cdr"), br, "ten_1", "prj_3", "dev_3", "incoming", "+94775559876",
             "sip:gate@pbx.acme.com", "rt_2", (base - timedelta(minutes=55)).isoformat(),
             (base - timedelta(minutes=55)).isoformat(), (base - timedelta(minutes=50)).isoformat(),
             300, "opus", 15000, 3000000, 0.4, 18.0, 4.1, 25, "USD",
             '{"bridge_minutes":5,"rate_cents":5}', 5, ts))
    # The same ended cellular call as a CallSession (call_sessions is the source of
    # truth; the legacy `calls` row above is kept for the bundled SPA).
    execute("""INSERT INTO call_sessions(id,tenant_id,device_id,project_id,route_id,bridge_id,
               state,source,direction,caller_name,caller_number,incoming_number,whatsapp_number,
               detection_sco,detection_hfp,detection_companion,detection_confidence,identify_ms,
               route,mos,jitter_ms,loss_pct,erle_db,aec_enabled,started_at,answered_at,ended_at,
               duration_sec,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("cs"), "ten_1", "dev_3", "prj_3", "rt_2", br, "ended", "cellular",
             "incoming", None, "+94775559876", "+94775559876", None,
             1, 1, 0, 0.70, None, "Security -> sip:gate@pbx.acme.com", 4.1, 18.0, 0.4, 47, 1,
             (base - timedelta(minutes=55)).isoformat(),
             (base - timedelta(minutes=55)).isoformat(),
             (base - timedelta(minutes=50)).isoformat(), 300, ts))

    # One open invoice with a line
    inv = new_id("inv")
    execute("""INSERT INTO invoices(id,tenant_id,number,status,subtotal_cents,tax_cents,
               total_cents,currency,period_start,period_end,issued_at,due_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (inv, "ten_1", "INV-1001", "open", 9900, 1584, 11484, "USD",
             (base - timedelta(days=30)).isoformat(), base.isoformat(), ts,
             (base + timedelta(days=14)).isoformat(), ts))
    execute("""INSERT INTO invoice_lines(id,invoice_id,description,metric,quantity,
               unit_price_cents,amount_cents) VALUES(?,?,?,?,?,?,?)""",
            (new_id("il"), inv, "Business plan - base fee", "subscription", 1, 9900, 9900))

    execute("""INSERT INTO usage_records(id,tenant_id,project_id,device_id,metric,quantity,
               unit,occurred_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)""",
            (new_id("ur"), "ten_1", "prj_3", "dev_3", "bridge_seconds", 300, "seconds",
             ts, new_id("idem")))

    execute("""INSERT INTO firmware(id,version,channel,object_key,sha256,signature,notes,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("fw_1", "1.0.3", "stable", "firmware/gbox-1.0.3.bin",
             "a" * 64, "sig", "Initial stable", ts))

    execute("""INSERT INTO webhooks(id,tenant_id,url,secret,events,enabled,created_at)
               VALUES(?,?,?,?,?,1,?)""",
            ("wh_1", "ten_1", "https://vps.acme.example/vb-hook", "whsec_demo",
             "call.incoming,call.enriched,call.ended,bridge.started,bridge.ended,"
             "device.online,device.offline,invoice.created,invoice.paid,balance.low", ts))
