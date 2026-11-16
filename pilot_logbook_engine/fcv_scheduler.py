"""Single-process overnight FC View scheduler for the Home Assistant add-on."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fcv_sync import FCVSyncError, sync_flights

LOGGER = logging.getLogger(__name__)
LOCAL_ZONE = ZoneInfo("America/Edmonton")
WINDOW_START_HOUR = 2
WINDOW_END_HOUR = 5
POLL_SECONDS = 300
_scheduler_thread: threading.Thread | None = None
_scheduler_lock = threading.Lock()


def in_sync_window(now: datetime) -> bool:
    """The overnight window is 02:00–04:59 Calgary local time."""
    local = now.astimezone(LOCAL_ZONE)
    return WINDOW_START_HOUR <= local.hour < WINDOW_END_HOUR


def seconds_until_window_end(now: datetime) -> float:
    """Return remaining wall-clock seconds in today's local sync window."""
    local = now.astimezone(LOCAL_ZONE)
    end = local.replace(
        hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    return max(0.0, (end - local).total_seconds())


def run_scheduled_sync(db_path: str | None = None) -> dict[str, int] | None:
    """Run only when connected; sync_flights provides the cross-invocation 24h guard."""
    path = db_path or os.path.join(os.environ.get("DATA_DIR", "/data"), "logbook_engine.sqlite3")
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        connected = conn.execute(
            "SELECT 1 FROM fcv_credentials WHERE id=1"
        ).fetchone()
        if connected is None:
            LOGGER.info("FC View overnight sync skipped: account is not connected.")
            return None
        return sync_flights(conn)
    except FCVSyncError as exc:
        LOGGER.warning("FC View overnight sync did not complete: %s", exc)
        return None
    except Exception:
        LOGGER.exception("FC View overnight sync failed unexpectedly.")
        return None
    finally:
        conn.close()


def scheduler_loop(db_path: str | None = None) -> None:
    """Poll in a lightweight daemon thread; persistent sync guard prevents duplicates."""
    while True:
        try:
            now = datetime.now(LOCAL_ZONE)
            if in_sync_window(now):
                run_scheduled_sync(db_path)
                # Avoid repeated calls during this window. Re-read the clock
                # after network I/O in case the sync crossed the window end.
                # The persistent 24h reservation remains authoritative across restarts.
                remaining = seconds_until_window_end(datetime.now(LOCAL_ZONE))
                if remaining > 0:
                    time.sleep(remaining)
            else:
                time.sleep(POLL_SECONDS)
        except Exception:
            LOGGER.exception("FC View scheduler loop recovered from an error.")
            time.sleep(POLL_SECONDS)


def start_scheduler(db_path: str | None = None) -> bool:
    """Start at most one scheduler thread in this Python process."""
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _scheduler_thread = threading.Thread(
            target=scheduler_loop, args=(db_path,), name="fcv-overnight-sync", daemon=True
        )
        _scheduler_thread.start()
        return True
