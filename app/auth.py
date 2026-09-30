"""Single-user login: scrypt password hash, signed session cookie, lockout after repeated failures."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from . import db

FAILS: dict[str, list[float]] = {}
MAX_FAILS = 5
LOCK_SECONDS = 300


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def secret_key() -> str:
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    p = db.DATA_DIR / ".secret_key"
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(secrets.token_urlsafe(48))
        os.chmod(p, 0o600)
    return p.read_text().strip()


def locked(ip: str) -> int:
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < LOCK_SECONDS]
    if len(FAILS[ip]) >= MAX_FAILS:
        return int(LOCK_SECONDS - (now - FAILS[ip][0]))
    return 0


def record_fail(ip: str) -> None:
    FAILS.setdefault(ip, []).append(time.time())


def clear_fails(ip: str) -> None:
    FAILS.pop(ip, None)


def user_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def ensure_env_user(conn) -> None:
    """If APP_USER / APP_PASSWORD are set and no user exists yet, create it."""
    u, p = os.environ.get("APP_USER"), os.environ.get("APP_PASSWORD")
    if u and p and user_count(conn) == 0:
        conn.execute("INSERT INTO users(username, pw_hash, created_at) VALUES (?,?,?)", (u, hash_pw(p), db.now()))
