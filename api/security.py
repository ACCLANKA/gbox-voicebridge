"""Authentication, authorization and signing primitives.

Implemented with the standard library only (no third-party crypto dependency):

* ``hash_password`` / ``verify_password`` - PBKDF2-HMAC-SHA256 with per-user salt.
* ``create_token`` / ``decode_token`` - compact JWT (HS256) for access/refresh.
* ``new_api_key`` / ``hash_api_key`` - scoped, hashed-at-rest API keys.
* RBAC role -> permission matrix (see docs/design/BILLING-MULTITENANCY.md).
* ``sign_webhook`` - HMAC-SHA256 webhook signatures (X-VB-Signature).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from . import config

# --------------------------------------------------------------------------- #
# Password hashing (PBKDF2-HMAC-SHA256)
# --------------------------------------------------------------------------- #
_PBKDF2_ITERATIONS = 240_000


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    if stored.startswith("scrypt$"):
        # accounts created by the Gbox auth service use scrypt (stdlib), format:
        #   scrypt$N$r$p$base64(salt)$base64(dk)
        try:
            _, n, r, pp, salt_b64, dk_b64 = stored.split("$")
            dk = hashlib.scrypt(password.encode("utf-8"),
                                salt=base64.b64decode(salt_b64.encode()),
                                n=int(n), r=int(r), p=int(pp), dklen=32)
            return hmac.compare_digest(base64.b64encode(dk).decode(), dk_b64)
        except (ValueError, TypeError):
            return False
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# JWT (HS256)
# --------------------------------------------------------------------------- #
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def create_token(sub: str, *, tenant_id: str | None = None, role: str = "viewer",
                 token_type: str = "access", scopes: str | None = None,
                 ttl: int | None = None) -> str:
    if ttl is None:
        ttl = config.ACCESS_TTL_SEC if token_type == "access" else config.REFRESH_TTL_SEC
    now = int(time.time())
    header = {"alg": config.JWT_ALG, "typ": "JWT"}
    payload: dict[str, Any] = {
        "sub": sub, "tenant_id": tenant_id, "role": role, "type": token_type,
        "scopes": scopes, "iat": now, "exp": now + ttl,
    }
    seg = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
          f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    sig = hmac.new(config.JWT_SECRET.encode(), seg.encode(), hashlib.sha256).digest()
    return f"{seg}.{_b64url(sig)}"


def decode_token(token: str) -> dict[str, Any] | None:
    """Return the payload if the signature and expiry are valid, else ``None``."""
    try:
        header_seg, payload_seg, sig_seg = token.split(".")
    except ValueError:
        return None
    signing_input = f"{header_seg}.{payload_seg}"
    expected = hmac.new(config.JWT_SECRET.encode(), signing_input.encode(),
                        hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(sig_seg)):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_seg))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
def new_api_key(prefix: str = "vb") -> tuple[str, str, str]:
    """Return ``(plaintext, key_hash, shown_prefix)``. Only the hash is stored."""
    raw = secrets.token_urlsafe(32)
    plaintext = f"{prefix}_{raw}"
    return plaintext, hash_api_key(plaintext), plaintext[:10]


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
ROLES = ["owner", "admin", "operator", "viewer", "billing"]

# Permission matrix from docs/design/BILLING-MULTITENANCY.md#2-roles-rbac plus
# the dashboard matrix in docs/design/DASHBOARD.md#6-permissions.
PERMISSIONS: dict[str, set[str]] = {
    "owner": {"users", "devices", "routes", "calls", "calls.control", "cdr",
              "recordings", "recordings.manage", "billing", "webhooks", "admin"},
    "admin": {"users", "devices", "routes", "calls", "calls.control", "cdr",
              "recordings", "recordings.manage", "webhooks", "admin"},
    "operator": {"devices", "routes", "calls", "calls.control", "cdr", "recordings",
                 "webhooks"},
    "viewer": {"calls", "cdr"},
    "billing": {"billing"},
}


def has_permission(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, set())


def permissions_for(role: str) -> list[str]:
    return sorted(PERMISSIONS.get(role, set()))


# --------------------------------------------------------------------------- #
# Webhook signing
# --------------------------------------------------------------------------- #
def sign_webhook(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_webhook(secret: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_webhook(secret, body), signature)
