"""Real accounts for Gbox: SQLite schema, password hashing and a FastAPI router.

Drop-in for the deployed API (FastAPI). It EXTENDS the existing `users` table rather than replacing
it, so the operator console's accounts keep working, and it keeps its own state in `gbox_*` tables.
Nothing here deletes anything, and no table is dropped.

  python3 auth_service.py --migrate /opt/voicebridge/data/app.db   # create/extend the schema
  python3 auth_service.py --selftest                              # hashing round-trip

Router endpoints (all JSON, no templates):
  POST /v1/ui/signup   {email, password, name?}   -> 201 {ok, email}         409 if registered
  POST /v1/ui/login    {email, password}          -> {ok, email, token, expires_at} + cookie
  POST /v1/ui/logout   (Bearer or cookie)
  GET  /v1/ui/me       (Bearer or cookie)         -> {email, role, created_at}
  POST /v1/ui/forgot   {email}                    -> 200 always (no account enumeration)
  POST /v1/ui/reset    {token, password}          -> 200, single use, 30 min

Sessions are returned as a bearer token AND set as an httpOnly cookie. Only the SHA-256 of each
token is stored, so a leaked database cannot be replayed. Passwords are scrypt (stdlib), with a
bcrypt fallback so pre-existing bcrypt hashes still verify - and silently upgrade to scrypt.
"""
import base64, hashlib, hmac, os, secrets, sqlite3, time
from datetime import datetime, timedelta, timezone
from typing import Optional

SESSION_TTL_DAYS = 7
RESET_TTL_MIN = 30
MIN_PASSWORD = 8
MAX_PASSWORD = 200
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
COOKIE = "gbox_session"


# ---------- time ----------
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def after(**kw) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat(timespec="seconds")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode())


# ---------- passwords ----------
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, _b64(salt), _b64(dk))


def verify_password(stored: str, pw: str) -> bool:
    try:
        if stored.startswith("scrypt$"):
            _, n, r, p, salt, dk = stored.split("$")
            got = hashlib.scrypt(pw.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                 dklen=len(_unb64(dk)))
            return hmac.compare_digest(got, _unb64(dk))
        # legacy rows created by the control plane itself (pbkdf2_sha256) - delegate to the
        # implementation that wrote them, so the existing owner account keeps working.
        if stored.startswith("pbkdf2_sha256$"):
            try:
                from .security import verify_password as _legacy_verify
                return _legacy_verify(pw, stored)
            except Exception:
                return False
        # pre-existing bcrypt rows: verify, caller may rehash
        import bcrypt  # optional dependency
        return bcrypt.checkpw(pw.encode("utf-8"), stored.encode("utf-8"))
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    return not stored.startswith("scrypt$")


def norm_email(e: str) -> str:
    return (e or "").strip().lower()


def check_password(pw: str) -> Optional[str]:
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        return "password must be at least %d characters" % MIN_PASSWORD
    if len(pw) > MAX_PASSWORD:
        return "password is too long"
    return None


# ---------- tokens ----------
def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


# ---------- schema ----------
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS gbox_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_gbox_sessions_user ON gbox_sessions(user_id);

CREATE TABLE IF NOT EXISTS gbox_reset_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    token_hash TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    used_at    TEXT,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS ix_gbox_reset_email ON gbox_reset_requests(email);

CREATE TABLE IF NOT EXISTS gbox_auth_events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind  TEXT NOT NULL,
    email TEXT,
    ip    TEXT,
    at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gbox_events_at ON gbox_auth_events(at);
"""

USER_COLUMNS = {
    "email": "TEXT",
    "password_hash": "TEXT",
    "role": "TEXT DEFAULT 'user'",
    "name": "TEXT",
    "created_at": "TEXT",
    "last_login": "TEXT",
    "status": "TEXT DEFAULT 'active'",
}


def connect(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c


def migrate(path: str) -> list:
    """Create gbox_* tables and ADD missing columns to `users`. Never drops, never renames."""
    done = []
    c = connect(path)
    try:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if not cols:
            c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL)")
            cols = {"id", "email"}
            done.append("created table users")
        for name, decl in USER_COLUMNS.items():
            if name not in cols:
                c.execute("ALTER TABLE users ADD COLUMN %s %s" % (name, decl))
                done.append("users.%s added" % name)
        # The existing table may store the hash under another name. Copy it across so the
        # accounts that already work (admin@gigies.lk in particular) keep working.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for alias in ("password", "hash", "passwd", "pw_hash", "pwhash", "password_digest"):
            if alias in cols:
                c.execute("UPDATE users SET password_hash=%s "
                          "WHERE (password_hash IS NULL OR password_hash='') "
                          "AND %s IS NOT NULL AND %s<>''" % (alias, alias, alias))
                if c.total_changes:
                    done.append("users.password_hash back-filled from %s (%d rows)"
                                % (alias, c.total_changes))
                break
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)")
        c.commit()
    finally:
        c.close()
    return done


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--migrate", metavar="DB_PATH")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.migrate:
        print("migrated", a.migrate, "->", migrate(a.migrate) or "already up to date")
    if a.selftest:
        h = hash_password("correct horse battery staple")
        assert verify_password(h, "correct horse battery staple")
        assert not verify_password(h, "wrong")
        assert needs_rehash(h) is False and needs_rehash("$2b$12$abc") is True
        t = new_token()
        assert len(t) >= 40 and hash_token(t) != t
        print("selftest ok")
    if not (a.migrate or a.selftest):
        ap.print_help()


# ---------- rate limiting (in-process; note in README re multiple workers) ----------
_rl = {}
import threading
_rl_lock = threading.Lock()


def rate_ok(key: str, limit: int = 8, window: int = 600) -> bool:
    t = time.time()
    with _rl_lock:
        hits = [x for x in _rl.get(key, []) if t - x < window]
        hits.append(t)
        _rl[key] = hits
        return len(hits) <= limit


# ---------- DB helpers ----------
def db_path() -> str:
    return os.environ.get("GBOX_DB") or os.environ.get("VB_DB") or "/app/data/voicebridge.db"


def log_event(kind: str, email: str = "", ip: str = "") -> None:
    try:
        c = connect(db_path())
        c.execute("INSERT INTO gbox_auth_events(kind,email,ip,at) VALUES(?,?,?,?)", (kind, email, ip, now()))
        c.commit(); c.close()
    except Exception:
        pass


def find_user(email: str):
    c = connect(db_path())
    try:
        return c.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
    finally:
        c.close()


def make_session(user_id: int, ip: str = "") -> dict:
    token = new_token()
    exp = after(days=SESSION_TTL_DAYS)
    c = connect(db_path())
    try:
        c.execute("INSERT INTO gbox_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                  (hash_token(token), user_id, now(), exp))
        c.commit()
    finally:
        c.close()
    return {"token": token, "expires_at": exp}


def session_user(token: str):
    if not token:
        return None
    c = connect(db_path())
    try:
        row = c.execute("""SELECT u.* FROM gbox_sessions s JOIN users u ON u.id=s.user_id
                           WHERE s.token_hash=? AND s.revoked=0 AND s.expires_at>?""",
                        (hash_token(token), now())).fetchone()
        return row
    finally:
        c.close()


def revoke_all(user_id: int) -> None:
    c = connect(db_path())
    try:
        c.execute("UPDATE gbox_sessions SET revoked=1 WHERE user_id=?", (user_id,))
        c.commit()
    finally:
        c.close()


def smtp_target() -> tuple[str, int]:
    """Host/port from GBOX_SMTP_HOST, accepting a bare host or a URL.

    himalaya-style values (``smtp://smtp.gmail.com:587``) used to be passed
    straight to smtplib, which then tried to resolve the whole string and failed
    with "Name or service not known". Accept both forms.
    """
    raw = (os.environ.get("GBOX_SMTP_HOST") or "").strip()
    port = int(os.environ.get("GBOX_SMTP_PORT", "587"))
    if "://" in raw:
        from urllib.parse import urlsplit
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if parts.port:
            port = parts.port
    else:
        host = raw.split("/")[0]
        if ":" in host:
            host, _, p = host.partition(":")
            try:
                port = int(p)
            except ValueError:
                pass
    return host, port


def send_reset(email: str, token: str) -> str:
    """Email the link when SMTP is configured; otherwise record it and say so.

    Never returns the token to the caller - that would be an account takeover.
    """
    host, port = smtp_target()
    link = "https://gigies.lk/reset.html?token=" + token
    if not host:
        return "recorded"
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = "Reset your Gbox password"
    msg["From"] = os.environ.get("GBOX_SMTP_FROM", "info@gigies.lk")
    msg["To"] = email
    msg.set_content("Open this link within 30 minutes to choose a new password:\n\n" + link +
                    "\n\nIf you did not ask for this, ignore it - nothing has changed.\n")
    try:
        with smtplib.SMTP(host, port, timeout=10) as s:
            if os.environ.get("GBOX_SMTP_USER"):
                s.starttls(); s.login(os.environ["GBOX_SMTP_USER"], os.environ.get("GBOX_SMTP_PASS", ""))
            s.send_message(msg)
        return "sent"
    except Exception as e:
        log_event("reset_send_failed", email, str(e)[:200])
        return "failed"


# ---------- FastAPI ----------
from fastapi import APIRouter, FastAPI, Request, Response, HTTPException
from pydantic import BaseModel, Field

_SCHEMA_READY = False


def _ensure_schema() -> None:
    """Run migrate() once per process before any write.

    The app carries two `users` schemas: api/db.py creates the table without `status`,
    while this module inserts it. migrate() adds missing columns, but it used to run only
    from a CLI entry point, so a fresh database failed signup with
    "table users has no column named status". Doing it lazily here removes the dependency
    on a startup hook firing.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    try:
        migrate(db_path())
        _SCHEMA_READY = True
    except Exception as exc:  # noqa: BLE001
        print("auth schema ensure failed:", type(exc).__name__, str(exc)[:120])


router = APIRouter(prefix="/v1/ui", tags=["auth"])


class SignupIn(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=MAX_PASSWORD)
    name: str = Field("", max_length=120)


class LoginIn(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=MAX_PASSWORD)


class ForgotIn(BaseModel):
    email: str = Field(..., max_length=254)


class ResetIn(BaseModel):
    token: str = Field(..., max_length=200)
    password: str = Field(..., max_length=MAX_PASSWORD)


def _set_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(COOKIE, token, max_age=SESSION_TTL_DAYS * 86400, httponly=True, secure=True,
                    samesite="none", path="/")


def _bearer(req: Request) -> str:
    h = req.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return req.cookies.get(COOKIE, "")


import re
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ip(req: Request) -> str:
    return (req.client.host if req.client else "?") or "?"


@router.post("/signup", status_code=201)
def signup(body: SignupIn, req: Request, resp: Response):
    _ensure_schema()
    email = norm_email(body.email)
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "that does not look like an email address")
    err = check_password(body.password)
    if err:
        raise HTTPException(422, err)
    if not rate_ok("signup:" + _ip(req), 5, 3600):
        raise HTTPException(429, "too many sign-ups from here - try later")
    if find_user(email):
        raise HTTPException(409, "that email is already registered")
    c = connect(db_path())
    try:
        # users.id is a TEXT key generated by the application, not a rowid: relying on
        # cur.lastrowid here produced NULL and broke the session insert (NOT NULL constraint).
        uid = "usr_" + secrets.token_hex(6)
        # One tenant per customer: the console scopes every query by tenant, so an account without
        # one gets 403 "no tenant bound to principal". This is also what keeps customers' devices
        # invisible to each other - isolation by construction rather than by filter.
        tid = "ten_" + secrets.token_hex(6)
        slug = "gbox-" + secrets.token_hex(4)
        display = (body.name.strip() or email.split("@")[0])[:80]
        c.execute("""INSERT INTO tenants(id,name,slug,status,plan,balance_cents,currency,
                                        credit_limit_cents,contact_email,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (tid, display, slug, "active", "plan_starter", 0, "USD", 0, email, now()))
        c.execute("""INSERT INTO users(id,email,password_hash,role,name,created_at,status,tenant_id)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (uid, email, hash_password(body.password), "owner", display, now(), "active", tid))
        c.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "that email is already registered")
    finally:
        c.close()
    s = make_session(uid, _ip(req))
    _set_cookie(resp, s["token"])
    log_event("signup", email, _ip(req))
    return {"ok": True, "email": email, "token": s["token"], "expires_at": s["expires_at"]}


@router.post("/login")
def login(body: LoginIn, req: Request, resp: Response):
    email = norm_email(body.email)
    if not (rate_ok("login-ip:" + _ip(req), 20, 900) and rate_ok("login:" + email, 8, 900)):
        raise HTTPException(429, "too many attempts - wait a few minutes")
    user = find_user(email)
    if user is None:
        hash_password("timing equaliser")  # keep the failure path roughly as slow
        raise HTTPException(401, "invalid credentials")
    if (user["status"] or "active") != "active":
        raise HTTPException(403, "this account is disabled")
    if not verify_password(user["password_hash"] or "", body.password):
        log_event("login_failed", email, _ip(req))
        raise HTTPException(401, "invalid credentials")
    if needs_rehash(user["password_hash"] or ""):
        c = connect(db_path())
        try:
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), user["id"]))
            c.execute("UPDATE users SET last_login=? WHERE id=?", (now(), user["id"]))
            c.commit()
        finally:
            c.close()
    else:
        c = connect(db_path())
        try:
            c.execute("UPDATE users SET last_login=? WHERE id=?", (now(), user["id"]))
            c.commit()
        finally:
            c.close()
    s = make_session(user["id"], _ip(req))
    _set_cookie(resp, s["token"])
    log_event("login", email, _ip(req))
    return {"ok": True, "email": user["email"], "role": user["role"] or "user",
            "token": s["token"], "expires_at": s["expires_at"]}


@router.post("/logout")
def logout(req: Request):
    tok = _bearer(req)
    if tok:
        c = connect(db_path())
        try:
            c.execute("UPDATE gbox_sessions SET revoked=1 WHERE token_hash=?", (hash_token(tok),))
            c.commit()
        finally:
            c.close()
    return {"ok": True}


@router.get("/me")
def me(req: Request):
    u = session_user(_bearer(req))
    if u is None:
        raise HTTPException(401, "not signed in")
    return {"email": u["email"], "role": u["role"] or "user", "name": u["name"],
            "created_at": u["created_at"]}


@router.post("/forgot")
def forgot(body: ForgotIn, req: Request):
    """Always 200 - whether or not the address exists. No enumeration."""
    _ensure_schema()
    email = norm_email(body.email)
    if not rate_ok("forgot:" + _ip(req), 5, 3600) or not rate_ok("forgot:" + email, 3, 3600):
        return {"ok": True}
    user = find_user(email)
    c = connect(db_path())
    try:
        token = new_token() if user else None
        c.execute("""INSERT INTO gbox_reset_requests(email,token_hash,created_at,expires_at)
                     VALUES(?,?,?,?)""",
                  (email, hash_token(token) if token else None, now(),
                   after(minutes=RESET_TTL_MIN) if token else None))
        c.commit()
    finally:
        c.close()
    if user:
        send_reset(email, token)
        log_event("reset_requested", email, _ip(req))
    return {"ok": True}


@router.post("/reset")
def reset(body: ResetIn, req: Request):
    if not rate_ok("reset:" + _ip(req), 10, 3600):
        raise HTTPException(429, "too many attempts")
    err = check_password(body.password)
    if err:
        raise HTTPException(422, err)
    th = hash_token(body.token)
    c = connect(db_path())
    try:
        row = c.execute("""SELECT * FROM gbox_reset_requests
                           WHERE token_hash=? AND used_at IS NULL AND expires_at>?""",
                        (th, now())).fetchone()
        if row is None:
            raise HTTPException(400, "this reset link is invalid or has expired")
        user = find_user(norm_email(row["email"]))
        if user is None:
            raise HTTPException(400, "this reset link is invalid or has expired")
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), user["id"]))
        c.execute("UPDATE gbox_reset_requests SET used_at=? WHERE id=?", (now(), row["id"]))
        c.commit()
    finally:
        c.close()
    revoke_all(user["id"])
    log_event("password_reset", user["email"], _ip(req))
    return {"ok": True}


def create_app() -> "FastAPI":
    app = FastAPI(title="Gbox auth", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    return app
