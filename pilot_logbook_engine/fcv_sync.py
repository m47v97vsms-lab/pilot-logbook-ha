"""Rate-limited FC View token refresh and flight-history synchronization."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fcv_client import FCVError, fetch_flights, refresh_tokens
from fcv_storage import (
    FCVStorageError, load_tokens, mark_sync_attempt, save_tokens, stage_flights,
)


class FCVSyncError(RuntimeError):
    """Safe synchronization status error."""


DAILY_LIMIT = timedelta(hours=24)
HISTORY_WINDOW = timedelta(days=60)
REFRESH_MARGIN = timedelta(seconds=120)


def sync_flights(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    fetcher: Callable[[str, datetime, datetime], list[dict[str, Any]]] = fetch_flights,
    refresher: Callable[[str], dict[str, Any]] = refresh_tokens,
) -> dict[str, int]:
    """Perform one guarded sync, refreshing/rotating tokens when needed.

    The rolling 24-hour reservation is committed before network I/O, preventing
    parallel manual/scheduled invocations from exceeding FCV's daily limit.
    A failed attempt still consumes the window; the caller must not retry early.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise FCVSyncError("FC View synchronization requires a timezone-aware clock.")
    now = now.astimezone(timezone.utc)
    now_text = now.isoformat()

    # A disconnected account is not a provider poll and must not reserve the
    # user's 24-hour FCV request window.
    if conn.execute("SELECT 1 FROM fcv_credentials WHERE id=1").fetchone() is None:
        raise FCVSyncError("FC View is not connected. Complete authorization before syncing.")

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT last_attempt_utc FROM fcv_sync_state WHERE id=1").fetchone()
    previous = row[0] if row else None
    if previous:
        try:
            previous_at = datetime.fromisoformat(previous)
            if previous_at.tzinfo is None or previous_at.utcoffset() is None:
                raise ValueError("naive timestamp")
        except (TypeError, ValueError):
            conn.rollback()
            raise FCVSyncError("Stored FC View sync timestamp is invalid; check sync status before retrying.")
        if now - previous_at.astimezone(timezone.utc) < DAILY_LIMIT:
            conn.rollback()
            raise FCVSyncError("FC View has already been checked within the last 24 hours.")
    conn.execute(
        "UPDATE fcv_sync_state SET last_attempt_utc=?,last_result='running',last_error=NULL WHERE id=1",
        (now_text,),
    )
    conn.commit()

    try:
        tokens = load_tokens(conn)
        if not tokens:
            raise FCVSyncError("FC View is not connected. Complete authorization before syncing.")

        try:
            expires_at = datetime.fromisoformat(tokens["access_expires_utc"])
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("naive token expiry")
        except (KeyError, TypeError, ValueError):
            raise FCVSyncError("Stored FC View token expiry is invalid; reconnect FC View.")

        if expires_at.astimezone(timezone.utc) <= now + REFRESH_MARGIN:
            rotated = refresher(tokens["refresh_token"])
            save_tokens(conn, rotated)
            tokens = load_tokens(conn)
            if not tokens:
                raise FCVSyncError("FC View token refresh could not be saved.")

        start = now - HISTORY_WINDOW
        flights = fetcher(tokens["access_token"], start, now)
        counts = stage_flights(conn, flights)
        mark_sync_attempt(conn, success=True)
        return counts
    except Exception as exc:
        safe_error = str(exc) if isinstance(exc, (FCVSyncError, FCVError, FCVStorageError)) else "FC View synchronization failed."
        mark_sync_attempt(conn, success=False, error=safe_error)
        if isinstance(exc, FCVSyncError):
            raise
        raise FCVSyncError(safe_error) from exc
