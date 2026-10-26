"""SQLite-backed store for Voice Bridge: schema, helpers and seed data."""
from __future__ import annotations

import os
import random
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("VB_DB", Path(__file__).resolve().parents[1] / "data" / "voicebridge.db"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
_lock = threading.Lock()


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


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, plan TEXT DEFAULT 'starter',
  contact_email TEXT, balance_cents INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, serial TEXT UNIQUE NOT NULL, name TEXT,
  tenant_id TEXT, project TEXT, bluetooth_name TEXT,
  status TEXT DEFAULT 'offline',
  ip TEXT, ssid TEXT, rssi INTEGER, uptime_s INTEGER DEFAULT 0,
  free_heap_kb INTEGER, fw TEXT, codec TEXT DEFAULT 'opus',
  sample_rate INTEGER DEFAULT 16000, battery_pct INTEGER,
  aec_enabled INTEGER DEFAULT 1, volume INTEGER DEFAULT 70, mic_gain INTEGER DEFAULT 60,
  echo_erle_db INTEGER, muted INTEGER DEFAULT 0,
  last_seen TEXT, created_at TEXT
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
  tx_kbps REAL, rx_kbps REAL, connection_state TEXT
);
CREATE TABLE IF NOT EXISTS link_events (
  id TEXT PRIMARY KEY, device_id TEXT, ts TEXT, type TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY, device_id TEXT, direction TEXT, source TEXT,
  incoming_number TEXT, whatsapp_number TEXT, caller_name TEXT,
  state TEXT, route TEXT, started_at TEXT, ended_at TEXT, duration_s INTEGER,
  mos REAL, jitter_ms REAL, loss_pct REAL, erle_db INTEGER, aec_enabled INTEGER,
  confidence REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def init() -> None:
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()
    seed()


def reset() -> None:
    with _lock:
        for t in ("tenants", "devices", "wifi_profiles", "networks", "telemetry",
                  "link_events", "calls", "settings"):
            _conn.execute(f"DELETE FROM {t}")
        _conn.commit()
    seed()


def get_setting(key: str, default: str | None = None) -> str | None:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def seed() -> None:
    if one("SELECT id FROM tenants LIMIT 1"):
        return
    ts = now()
    execute("INSERT INTO tenants(id,name,plan,contact_email,balance_cents,created_at) VALUES(?,?,?,?,?,?)",
            ("ten_1", "Acme Voice Ltd", "growth", "ops@acme.example", 1284500, ts))
    set_setting("device_id_prefix", "GBX")

    devices = [
        ("dev_1", "GBX-A1B2C3", "Reception Desk", "Reception", "online", "192.168.1.42", "Office-WiFi", -56, 128, 1),
        ("dev_2", "GBX-7F3C9D", "Warehouse Bay", "Warehouse", "degraded", "192.168.1.77", "Office-WiFi", -74, 2, 52),
        ("dev_3", "GBX-2D91E4", "Main Gate", "Security", "online", "192.168.1.90", "Office-WiFi", -61, 96, 1),
        ("dev_4", "GBX-5A0B77", "Spare Unit", "Lab", "offline", None, None, None, 0, None),
    ]
    for i, (did, serial, name, project, status, ip, ssid, rssi, uptime, fw) in enumerate(devices):
        execute("""INSERT INTO devices(id,serial,name,tenant_id,project,bluetooth_name,status,ip,ssid,
                   rssi,uptime_s,free_heap_kb,fw,codec,sample_rate,battery_pct,aec_enabled,volume,
                   mic_gain,echo_erle_db,last_seen,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (did, serial, name, "ten_1", project, f"Gbox-{serial[-4:]}", status, ip, ssid,
                 rssi, uptime, 84 - i * 6, f"1.0.{3+i%3}", "opus", 16000, 100 - i * 5,
                 1, 70, 60, 52 - i * 2, ts, ts))

    for did, ssid in [("dev_1", "Office-WiFi"), ("dev_2", "Office-WiFi"),
                      ("dev_3", "Office-WiFi"), ("dev_4", "Office-Guest")]:
        execute("""INSERT INTO wifi_profiles(id,device_id,ssid,psk,security,hidden,priority,
                   auto_connect,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
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

    random.seed(7)
    base = datetime.now(timezone.utc)
    for dev, serial, name, project, status in fixtures():
        for n in range(20):
            t = base - timedelta(minutes=(20 - n) * 2)
            rssi = random.randint(-80, -50) if status != "offline" else -95
            state = "online" if status != "offline" else "offline"
            execute("""INSERT INTO telemetry(id,device_id,ts,rssi,latency_ms,jitter_ms,loss_pct,
                       mos,tx_kbps,rx_kbps,connection_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("tl"), dev, t.isoformat(), rssi, random.randint(60, 160),
                     round(random.uniform(5, 28), 1), round(random.uniform(0, 1.5), 2),
                     round(random.uniform(3.6, 4.5), 1), round(random.uniform(18, 26), 1),
                     round(random.uniform(18, 26), 1), state))
        if status != "offline":
            execute("INSERT INTO link_events(id,device_id,ts,type,detail) VALUES(?,?,?,?,?)",
                    (new_id("ev"), dev, (base - timedelta(minutes=14)).isoformat(),
                     "disconnect", "Wi-Fi link lost (beacon timeout)"))
            execute("INSERT INTO link_events(id,device_id,ts,type,detail) VALUES(?,?,?,?,?)",
                    (new_id("ev"), dev, (base - timedelta(minutes=13)).isoformat(),
                     "reconnect", "Reconnected to Office-WiFi"))
            execute("INSERT INTO link_events(id,device_id,ts,type,detail) VALUES(?,?,?,?,?)",
                    (new_id("ev"), dev, (base - timedelta(minutes=40)).isoformat(),
                     "connect", "Connected to Office-WiFi"))

    execute("""INSERT INTO calls(id,device_id,direction,source,incoming_number,whatsapp_number,
               caller_name,state,route,started_at,ended_at,duration_s,mos,jitter_ms,loss_pct,
               erle_db,aec_enabled,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("call"), "dev_1", "incoming", "whatsapp", None, "+94771234567",
             "Kamal Perera", "bridged", "Reception -> sip:reception@pbx.acme.com",
             (base - timedelta(minutes=3)).isoformat(), None, 180, 4.4, 11.0, 0.1, 52, 1, 0.95))
    execute("""INSERT INTO calls(id,device_id,direction,source,incoming_number,whatsapp_number,
               caller_name,state,route,started_at,ended_at,duration_s,mos,jitter_ms,loss_pct,
               erle_db,aec_enabled,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("call"), "dev_3", "incoming", "cellular", "+94775559876", None,
             None, "ended", "Security -> sip:gate@pbx.acme.com",
             (base - timedelta(minutes=55)).isoformat(), (base - timedelta(minutes=50)).isoformat(),
             300, 4.1, 18.0, 0.4, 47, 1, 0.7))


def fixtures() -> list[tuple]:
    rows = query("SELECT id,serial,name,project,status FROM devices ORDER BY serial")
    return [(r["id"], r["serial"], r["name"], r["project"], r["status"]) for r in rows]
