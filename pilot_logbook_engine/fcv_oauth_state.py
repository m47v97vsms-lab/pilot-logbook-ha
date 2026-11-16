"""Persistent, one-time OAuth state storage for the FC View callback."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone


STATE_TTL = timedelta(minutes=10)


class OAuthStateError(RuntimeError):
    """Raised when OAuth state cannot safely be issued or consumed."""


def init_oauth_state(conn: sqlite3.Connection) -> None:
    """Create the isolated OAuth state table; no canonical logbook tables touched."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fcv_oauth_states (
            state_sha256 TEXT PRIMARY KEY,
            created_at_utc TEXT NOT NULL,
            expires_at_utc TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fcv_oauth_states_expiry "
        "ON fcv_oauth_states(expires_at_utc)"
    )


def issue_oauth_state(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> str:
    """Return a high-entropy state value while persisting only its SHA-256 digest."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise OAuthStateError("OAuth state timestamps must be timezone-aware.")
    now = now.astimezone(timezone.utc)
    raw_state = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw_state.encode("ascii")).hexdigest()
    created = now.isoformat()
    expires = (now + STATE_TTL).isoformat()
    conn.execute("DELETE FROM fcv_oauth_states WHERE expires_at_utc <= ?", (created,))
    conn.execute(
        "INSERT INTO fcv_oauth_states(state_sha256,created_at_utc,expires_at_utc) VALUES(?,?,?)",
        (digest, created, expires),
    )
    conn.commit()
    return raw_state


def consume_oauth_state(
    conn: sqlite3.Connection, supplied_state: str, *, now: datetime | None = None
) -> bool:
    """Atomically consume a matching, unexpired state exactly once."""
    if not isinstance(supplied_state, str) or not supplied_state or len(supplied_state) > 256:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise OAuthStateError("OAuth state timestamps must be timezone-aware.")
    now_text = now.astimezone(timezone.utc).isoformat()
    digest = hashlib.sha256(supplied_state.encode("utf-8")).hexdigest()

    # Serialize consumers so two concurrent callbacks cannot both accept one state.
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT expires_at_utc FROM fcv_oauth_states WHERE state_sha256=?",
            (digest,),
        ).fetchone()
        if row is None or row[0] <= now_text:
            conn.execute(
                "DELETE FROM fcv_oauth_states WHERE state_sha256=? OR expires_at_utc <= ?",
                (digest, now_text),
            )
            conn.commit()
            return False
        conn.execute("DELETE FROM fcv_oauth_states WHERE state_sha256=?", (digest,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
