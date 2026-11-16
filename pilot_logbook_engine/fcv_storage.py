"""Encrypted FC View credential storage and idempotent import staging."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class FCVStorageError(RuntimeError):
    """Safe, user-displayable FC View storage error."""


def _create_key_file_if_missing(key_path: str) -> None:
    """Publish a fully written owner-only key atomically; concurrent starters share one key."""
    directory = os.path.dirname(key_path)
    temporary = os.path.join(
        directory, f".{os.path.basename(key_path)}.{secrets.token_hex(8)}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(Fernet.generate_key())
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A hard link is atomic and never replaces a key another worker won.
            os.link(temporary, key_path)
        except FileExistsError:
            pass
        else:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_key_file(key_path: str) -> str:
    """Read only a regular, non-symlink key file with no group/other permissions."""
    if os.path.islink(key_path):
        raise FCVStorageError("FC View encryption key must not be a symbolic link.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_path, flags)
    except OSError as exc:
        if os.path.islink(key_path):
            raise FCVStorageError("FC View encryption key must not be a symbolic link.") from exc
        raise FCVStorageError("FC View encryption key could not be read from persistent add-on storage.") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FCVStorageError("FC View encryption key must be a regular file.")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise FCVStorageError("FC View encryption key must have owner-only permissions (0600).")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            try:
                return handle.read(256).decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise FCVStorageError("FC View encryption key is not valid ASCII.") from exc
    except OSError as exc:
        raise FCVStorageError("FC View encryption key could not be read from persistent add-on storage.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fernet() -> Fernet:
    """Use an explicit key when configured; otherwise create one securely in persistent app data."""
    key = os.environ.get("FCV_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        data_dir = os.environ.get("DATA_DIR", "/data")
        try:
            os.makedirs(data_dir, exist_ok=True)
            key_path = os.path.join(data_dir, "fcv_token_encryption.key")
            if not os.path.lexists(key_path):
                _create_key_file_if_missing(key_path)
            key = _read_key_file(key_path)
        except FCVStorageError:
            raise
        except OSError as exc:
            raise FCVStorageError("FC View encryption key could not be created or read in persistent add-on storage.") from exc
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise FCVStorageError("FC View token encryption key is invalid.") from exc


def init_fcv_storage(conn: sqlite3.Connection) -> None:
    """Create FC View-only tables; never alter canonical flight tables."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS fcv_credentials (
      id INTEGER PRIMARY KEY CHECK(id=1),
      access_token_enc BLOB NOT NULL,
      refresh_token_enc BLOB NOT NULL,
      access_expires_utc TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS fcv_sync_state (
      id INTEGER PRIMARY KEY CHECK(id=1),
      last_attempt_utc TEXT,
      last_success_utc TEXT,
      last_result TEXT,
      last_error TEXT
    );
    INSERT OR IGNORE INTO fcv_sync_state(id) VALUES(1);
    CREATE TABLE IF NOT EXISTS fcv_import_records (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fcv_flight_id TEXT NOT NULL UNIQUE,
      source_json TEXT NOT NULL,
      source_sha256 TEXT NOT NULL,
      review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(review_status IN ('pending','accepted_for_review','rejected')),
      first_seen_utc TEXT NOT NULL,
      updated_at_utc TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_fcv_import_review
      ON fcv_import_records(review_status, first_seen_utc);
    """)


def save_tokens(conn: sqlite3.Connection, tokens: dict[str, Any]) -> None:
    """Encrypt and atomically replace both rotated OAuth tokens."""
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    expires = tokens.get("expires_in", 3600)
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise FCVStorageError("FC View did not return a complete token pair.")
    try:
        expires = int(expires)
    except (TypeError, ValueError) as exc:
        raise FCVStorageError("FC View returned an invalid token expiry.") from exc
    if not 60 <= expires <= 86400:
        raise FCVStorageError("FC View returned an unexpected token expiry.")
    cipher = _fernet()
    now = datetime.now(timezone.utc)
    expiry = now.timestamp() + expires
    expiry_text = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO fcv_credentials
           (id,access_token_enc,refresh_token_enc,access_expires_utc,updated_at)
           VALUES(1,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             access_token_enc=excluded.access_token_enc,
             refresh_token_enc=excluded.refresh_token_enc,
             access_expires_utc=excluded.access_expires_utc,
             updated_at=excluded.updated_at""",
        (cipher.encrypt(access.encode()), cipher.encrypt(refresh.encode()),
         expiry_text, now.isoformat()),
    )
    conn.commit()


def load_tokens(conn: sqlite3.Connection) -> dict[str, str] | None:
    row = conn.execute(
        "SELECT access_token_enc,refresh_token_enc,access_expires_utc FROM fcv_credentials WHERE id=1"
    ).fetchone()
    if row is None:
        return None
    try:
        cipher = _fernet()
        return {
            "access_token": cipher.decrypt(row[0]).decode(),
            "refresh_token": cipher.decrypt(row[1]).decode(),
            "access_expires_utc": row[2],
        }
    except (InvalidToken, UnicodeDecodeError, TypeError) as exc:
        raise FCVStorageError("Stored FC View credentials could not be decrypted; reconnect FC View.") from exc


def delete_tokens(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM fcv_credentials WHERE id=1")
    conn.commit()


def stage_flights(conn: sqlite3.Connection, flights: list[dict[str, Any]]) -> dict[str, int]:
    """Stage source records by stable FCV ID without touching canonical flights."""
    if not isinstance(flights, list):
        raise FCVStorageError("FC View flight payload must be a list.")
    validated = []
    seen_ids: set[str] = set()
    for item in flights:
        if not isinstance(item, dict):
            raise FCVStorageError("FC View returned a malformed flight record.")
        raw_id = item.get("fcv_flight_id")
        if raw_id is None or not str(raw_id).strip() or len(str(raw_id)) > 200:
            raise FCVStorageError("FC View returned a flight without a valid fcv_flight_id.")
        flight_id = str(raw_id).strip()
        if flight_id in seen_ids:
            raise FCVStorageError("FC View returned duplicate fcv_flight_id values in one response.")
        seen_ids.add(flight_id)
        try:
            source_json = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise FCVStorageError("FC View returned a flight containing unsupported data.") from exc
        validated.append((flight_id, source_json, hashlib.sha256(source_json.encode("utf-8")).hexdigest()))

    now = datetime.now(timezone.utc).isoformat()
    inserted = updated = unchanged = 0
    # A database error halfway through the batch must not leave a partial import
    # that a later status update could accidentally commit.
    conn.execute("SAVEPOINT fcv_stage_batch")
    try:
        for flight_id, source_json, digest in validated:
            existing = conn.execute(
                "SELECT source_sha256,review_status FROM fcv_import_records WHERE fcv_flight_id=?",
                (flight_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO fcv_import_records
                       (fcv_flight_id,source_json,source_sha256,first_seen_utc,updated_at_utc)
                       VALUES(?,?,?,?,?)""",
                    (flight_id, source_json, digest, now, now),
                )
                inserted += 1
            elif existing[0] == digest or existing[1] != "pending":
                unchanged += 1
            else:
                conn.execute(
                    """UPDATE fcv_import_records SET source_json=?,source_sha256=?,updated_at_utc=?
                       WHERE fcv_flight_id=? AND review_status='pending'""",
                    (source_json, digest, now, flight_id),
                )
                updated += 1
        conn.execute("RELEASE SAVEPOINT fcv_stage_batch")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT fcv_stage_batch")
        conn.execute("RELEASE SAVEPOINT fcv_stage_batch")
        raise
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged}


def mark_sync_attempt(conn: sqlite3.Connection, *, success: bool, error: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE fcv_sync_state SET last_attempt_utc=?,last_result=?,last_error=?,
           last_success_utc=CASE WHEN ? THEN ? ELSE last_success_utc END WHERE id=1""",
        (now, "success" if success else "error", (error or "")[:500] or None,
         1 if success else 0, now),
    )
    conn.commit()
