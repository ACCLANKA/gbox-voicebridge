#!/usr/bin/env python3
"""Take a console that was running on demo fixtures to a clean production one.

Demo data exists to make the dashboard look alive without hardware: fictional tenants
("Acme Voice Ltd", "Globex Messenger"), four example devices, invented DIDs, calls and
telemetry, plus the bench board used during bring-up. None of that belongs in a console
that is showing a customer their own site, because "which of these devices is real?" then
has no good answer.

This script removes all of it and leaves exactly one clean device:

    python3 scripts/clean-console.py                 # report only - changes nothing
    python3 scripts/clean-console.py --yes           # back up the DB, purge, create
    python3 scripts/clean-console.py --yes --serial GBX-0001 --name "Front Desk"

The purge is safe to re-run: it is written as "delete everything operational, then create
the one device if it is missing", and it copies the database to <db>.bak-<timestamp> before
touching anything. Run it with the API stopped if the host is live.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Configuration survives a cleanup: the operator's tenant and users, the plans, the settings.
KEEP_TABLES = {"plans", "users", "tenants", "settings"}
# The bridge task log (ops_tasks) is the bench coordination record, not customer data, and
# api_keys are CREDENTIALS: the purge takes out the operator key and every device's key, and
# the only way back is the pre-purge backup (this happened on the gigies host - the ops API
# answered "authentication required" until the key was restored from the snapshot). Both are
# left alone unless asked for explicitly.
KEEP_TABLES_OPT_IN = {"ops_tasks", "api_keys"}


def _backup(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    shutil.copy2(db_path, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(dest) + suffix))
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("VB_DB", str(REPO_ROOT / "data" / "voicebridge.db")),
                    help="database file (default: data/voicebridge.db)")
    ap.add_argument("--yes", action="store_true", help="actually make the changes")
    ap.add_argument("--serial", default="GBX-0001", help="serial for the clean device")
    ap.add_argument("--name", default="Office Line", help="name for the clean device")
    ap.add_argument("--purge-ops-tasks", action="store_true",
                    help="also clear ops_tasks (the bench/bridge coordination log)")
    ap.add_argument("--purge-api-keys", action="store_true",
                    help="also clear api_keys (takes out the operator and device keys - "
                         "restorable only from the pre-purge backup)")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"no database at {db_path}")
        return 1
    os.environ["VB_DB"] = str(db_path)

    from api import config, db  # noqa: E402  (import after VB_DB is set)

    db.init()   # schema + baseline tenant/admin; never demo fixtures unless VB_DEMO_DATA=1

    keep = set(KEEP_TABLES)
    if not args.purge_ops_tasks:
        keep.add("ops_tasks")
    if not args.purge_api_keys:
        keep.add("api_keys")
    operational = [t for t in db.TABLES if t not in keep]

    tenant = db.one("SELECT * FROM tenants WHERE id=?", (config.TENANT_ID,))
    print(f"database         : {db_path}")
    print(f"operator tenant  : {config.TENANT_ID} ({config.TENANT_NAME})"
          if tenant else f"operator tenant  : {config.TENANT_ID} MISSING - will be created")
    print(f"demo mode (env)  : {'ON - fixtures would be seeded, so re-run with VB_DEMO_DATA unset' if db.demo_enabled() else 'off'}")

    devices = db.query("SELECT id,serial,name,tenant_id,status FROM devices ORDER BY serial")
    tenants = db.query("SELECT id,name FROM tenants ORDER BY id")
    others = [t for t in tenants if t["id"] != config.TENANT_ID]

    print(f"\ndevices to remove : {len(devices)}")
    for d in devices:
        print(f"   - {d['serial']:<12} {d['name']:<16} tenant={d['tenant_id']} "
              f"({d['status']})")
    print(f"extra tenants     : {[t['name'] for t in others] or 'none'}")
    for table in operational:
        try:
            n = db.one(f"SELECT COUNT(*) c FROM {table}")["c"]
        except Exception:
            continue
        if n:
            print(f"   rows in {table:<20} {n}")
    for table in sorted(keep - KEEP_TABLES):
        n = db.one(f"SELECT COUNT(*) c FROM {table}")["c"] if table in db.TABLES else 0
        if n:
            flag = {"ops_tasks": "--purge-ops-tasks", "api_keys": "--purge-api-keys"}[table]
            note = "bridge/bench log" if table == "ops_tasks" else "credentials - keep these"
            print(f"   keeping {table:<12} {n:<5} ({note}; {flag} to clear)")

    if not args.yes:
        print("\nreport only - nothing was changed. Re-run with --yes to apply.")
        return 0

    backup = _backup(db_path)
    print(f"\nbackup           : {backup}")

    for table in operational:
        try:
            db.execute(f"DELETE FROM {table}")
        except Exception:
            pass    # table absent in an older schema
    db.execute("DELETE FROM users WHERE tenant_id<>?", (config.TENANT_ID,))
    # The demo logins live in the operator tenant, so the tenant filter above kept them: five
    # accounts whose password is the demo default. Those must not survive on a real console.
    demo_emails = {u[2] for u in getattr(db, "_DEMO_USERS", ())}
    removed = 0
    for u in db.query("SELECT id,email FROM users WHERE email<>?", (config.ADMIN_EMAIL,)):
        if u["email"] in demo_emails or u["email"].endswith("@acme.example"):
            db.execute("DELETE FROM users WHERE id=?", (u["id"],))
            removed += 1
    if removed:
        print(f"demo logins      : removed {removed} (demo accounts with the default password)")
    db.execute("DELETE FROM tenants WHERE id<>?", (config.TENANT_ID,))
    if not tenant:
        db.execute("""INSERT INTO tenants(id,name,slug,status,plan,balance_cents,currency,
                      credit_limit_cents,contact_email,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (config.TENANT_ID, config.TENANT_NAME, config.TENANT_SLUG, "active",
                    "business", 0, "USD", 0, config.ADMIN_EMAIL, db.now()))
    print(f"purged           : devices, DIDs, calls, telemetry, and "
          f"{len(others)} other tenant(s)")

    # The operator tenant keeps its id but not a demo name: ten_1 survived the purge as
    # "Acme Voice Ltd", and a console header showing a fictional company is exactly the
    # confusion this script exists to remove.
    current = db.one("SELECT name,slug,contact_email FROM tenants WHERE id=?", (config.TENANT_ID,))
    if current and (current["name"] != config.TENANT_NAME or current["slug"] != config.TENANT_SLUG):
        db.execute("UPDATE tenants SET name=?, slug=?, contact_email=? WHERE id=?",
                   (config.TENANT_NAME, config.TENANT_SLUG, config.ADMIN_EMAIL, config.TENANT_ID))
        print(f"tenant renamed   : {current['name']!r} -> {config.TENANT_NAME!r}")

    existing = db.one("SELECT id FROM devices WHERE serial=?", (args.serial,))
    if existing:
        print(f"clean device     : {args.serial} already present")
    else:
        ts = db.now()
        db.insert("devices", {
            "id": db.new_id("dev"), "tenant_id": config.TENANT_ID, "serial": args.serial,
            "name": args.name, "status": "offline", "codec": config.DEFAULT_CODEC,
            "sample_rate": config.DEFAULT_SAMPLE_RATE, "project": "", "created_at": ts,
            "last_seen": ts,
        })
        print(f"clean device     : created {args.serial} ({args.name}) under "
              f"{config.TENANT_ID} - it goes online when the board connects")

    print(f"final devices    : {[d['serial'] for d in db.query('SELECT serial FROM devices')]}")
    print(f"final tenants    : {[t['id'] for t in db.query('SELECT id FROM tenants')]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
