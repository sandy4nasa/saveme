#!/usr/bin/env python3
"""
auth.py

Minimal, dependency-free username/password auth for SaveMe's multi-user
support (IMPLEMENTATION_PLAN.md Section 15). No third-party packages --
uses stdlib hashlib.pbkdf2_hmac for password hashing and a DB-backed
session table (random opaque tokens, not JWT) for login state.

Schema this module expects (created via ensure_auth_schema()):
  users.password_hash   VARCHAR  -- "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>"
  users.display_name    VARCHAR
  sessions(token VARCHAR PRIMARY KEY, user_id VARCHAR, created_at TIMESTAMP, expires_at TIMESTAMP)
"""

import hashlib
import os
import secrets
from datetime import datetime, timedelta

PBKDF2_ITERATIONS = 260_000
SESSION_COOKIE_NAME = "saveme_session"
SESSION_TTL_DAYS = 30


def ensure_auth_schema(con):
    """Idempotently add the auth-related columns/tables to an existing
    saveme.duckdb. Safe to call on every server startup."""
    # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk) in DuckDB -- name is index 1
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info('users')").fetchall()}
    if "password_hash" not in existing_cols:
        con.execute("ALTER TABLE users ADD COLUMN password_hash VARCHAR")
    if "display_name" not in existing_cols:
        con.execute("ALTER TABLE users ADD COLUMN display_name VARCHAR")

    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT current_timestamp,
            expires_at TIMESTAMP NOT NULL
        )
    """)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    try:
        algo, iterations, salt, hex_digest = stored_hash.split("$")
        iterations = int(iterations)
    except (ValueError, AttributeError):
        return False
    if algo != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return secrets.compare_digest(candidate.hex(), hex_digest)


def create_user(con, user_id: str, password: str, display_name: str = None):
    """Raises duckdb.ConstraintException if user_id already exists."""
    con.execute(
        "INSERT INTO users (id, auth_provider, password_hash, display_name) VALUES (?, 'password', ?, ?)",
        [user_id, hash_password(password), display_name or user_id],
    )


def authenticate(con, user_id: str, password: str) -> bool:
    row = con.execute("SELECT password_hash FROM users WHERE id = ?", [user_id]).fetchone()
    if not row:
        return False
    return verify_password(password, row[0])


def create_session(con, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(days=SESSION_TTL_DAYS)
    con.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)", [token, user_id, expires_at])
    return token


def get_user_from_session(con, token: str):
    if not token:
        return None
    row = con.execute(
        "SELECT user_id FROM sessions WHERE token = ? AND expires_at > current_timestamp", [token]
    ).fetchone()
    return row[0] if row else None


def destroy_session(con, token: str):
    if token:
        con.execute("DELETE FROM sessions WHERE token = ?", [token])


def parse_cookies(cookie_header: str) -> dict:
    cookies = {}
    if not cookie_header:
        return cookies
    for part in cookie_header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies
