"""Consistent SQLite backup and restore helpers for the pilot logbook."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BACKUP_RE = re.compile(r"^logbook-(\d{8}T\d{6}Z)-([a-f0-9]{8})(-v2)?\.sqlite3$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
REQUIRED_TABLES = {"airports", "flights", "schema_version", "users", "login_guard"}


class BackupError(ValueError):
    """Raised when a backup cannot be safely validated or restored."""


def _connect(path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path):
    manifest_path = Path(path).with_name(Path(path).name + ".json")
    if manifest_path.is_symlink():
        raise BackupError("Backup manifest must not be a symbolic link.")
    if not manifest_path.exists():
        return None
    if not manifest_path.is_file():
        raise BackupError("Backup manifest is not a regular file.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise BackupError("Backup manifest is not readable JSON.") from exc
    if not isinstance(manifest, dict):
        raise BackupError("Backup manifest must contain a JSON object.")
    return manifest


def _validate_manifest(path, details):
    """Verify a present sidecar and require one for version-2 backups."""
    path = Path(path)
    manifest = _load_manifest(path)
    if manifest is None:
        if path.name.endswith("-v2.sqlite3"):
            raise BackupError("Version 2 backup is missing its required manifest.")
        return None
    if manifest.get("file") != path.name:
        raise BackupError("Backup manifest filename does not match the backup.")
    if path.name.endswith("-v2.sqlite3") and manifest.get("manifest_version") != 2:
        raise BackupError("Version 2 backup manifest has an unsupported version.")
    expected_digest = manifest.get("sha256")
    if not isinstance(expected_digest, str) or not SHA256_RE.fullmatch(expected_digest):
        raise BackupError("Backup manifest has an invalid SHA-256 value.")
    actual_digest = _sha256_file(path)
    if not hmac.compare_digest(expected_digest, actual_digest):
        raise BackupError("Backup SHA-256 does not match its manifest.")
    for field, expected in details.items():
        actual = manifest.get(field)
        if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected:
            raise BackupError(f"Backup manifest {field} does not match the database.")
    created_utc = manifest.get("created_utc")
    if created_utc is not None and (
        not isinstance(created_utc, str) or not created_utc.strip()
    ):
        raise BackupError("Backup manifest has an invalid creation timestamp.")
    return manifest


def validate_database(path, verify_manifest=True):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise BackupError("Backup file is missing, empty, or not a regular file.")
    conn = _connect(path)
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise BackupError("SQLite integrity check failed.")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            raise BackupError(
                "Backup is missing required tables: " + ", ".join(sorted(missing))
            )
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        if not isinstance(version, int) or version < 1:
            raise BackupError("Backup has an invalid schema version.")
        details = {
            "schema_version": version,
            "flight_count": conn.execute(
                "SELECT COUNT(*) FROM flights"
            ).fetchone()[0],
            "airport_count": conn.execute(
                "SELECT COUNT(*) FROM airports"
            ).fetchone()[0],
        }
    except sqlite3.DatabaseError as exc:
        raise BackupError("Backup is not a readable SQLite database.") from exc
    finally:
        conn.close()

    if verify_manifest:
        _validate_manifest(path, details)
    return details


def _remove_reserved_paths(paths):
    """Remove only paths reserved by the current backup/restore operation."""
    for path in reversed(tuple(paths)):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def _reserve_exclusive_path(path):
    """Create a new private file, never following or replacing an existing path."""
    try:
        descriptor = os.open(
            str(path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return False
    try:
        os.close(descriptor)
    except Exception:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True


def _reserve_backup_paths(backup_dir, stamp):
    """Reserve every backup path before SQLite or JSON writes begin."""
    for _ in range(10):
        name = f"logbook-{stamp}-{secrets.token_hex(4)}-v2.sqlite3"
        final_path = backup_dir / name
        temp_path = backup_dir / (name + ".tmp")
        manifest_path = backup_dir / (name + ".json")
        manifest_temp = backup_dir / (name + ".json.tmp")
        reserved = []
        try:
            for candidate in (final_path, temp_path, manifest_path, manifest_temp):
                if not _reserve_exclusive_path(candidate):
                    raise FileExistsError(str(candidate))
                reserved.append(candidate)
        except FileExistsError:
            _remove_reserved_paths(reserved)
            continue
        except Exception:
            _remove_reserved_paths(reserved)
            raise
        return name, final_path, temp_path, manifest_path, manifest_temp, reserved
    raise BackupError(
        "Could not allocate a unique backup filename without overwriting existing files."
    )


def _reserve_restore_temp_path(db_path):
    """Reserve a unique restore file beside the database without following symlinks."""
    for _ in range(10):
        candidate = db_path.with_name(
            f".{db_path.name}.{secrets.token_hex(8)}.restore.tmp"
        )
        if _reserve_exclusive_path(candidate):
            return candidate
    raise BackupError(
        "Could not allocate a unique restore temporary file without following existing paths."
    )


def create_backup(db_path, backup_dir):
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    if not db_path.is_file():
        raise BackupError("The live database does not exist yet.")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (
        name,
        final_path,
        temp_path,
        manifest_path,
        manifest_temp,
        reserved_paths,
    ) = _reserve_backup_paths(backup_dir, stamp)
    source = target = None

    try:
        source = _connect(db_path)
        target = _connect(temp_path)
        source.backup(target)
        target.commit()
        target.close()
        target = None
        source.close()
        source = None

        os.replace(temp_path, final_path)
        details = validate_database(final_path, verify_manifest=False)
        manifest = {
            "manifest_version": 2,
            "file": name,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "sha256": _sha256_file(final_path),
            **details,
        }
        with manifest_temp.open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(manifest_temp, manifest_path)
        validate_database(final_path)
        return manifest
    except Exception:
        _remove_reserved_paths(reserved_paths)
        raise
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()

def list_backups(backup_dir):
    backup_dir = Path(backup_dir)
    items = []
    if not backup_dir.exists():
        return items
    for path in sorted(backup_dir.glob("logbook-*.sqlite3"), reverse=True):
        if not BACKUP_RE.fullmatch(path.name):
            continue
        try:
            details = validate_database(path)
            manifest = _load_manifest(path)
            items.append(
                {
                    "file": path.name,
                    "valid": True,
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                    **details,
                    "created_utc": manifest.get("created_utc") if manifest else None,
                }
            )
        except (BackupError, OSError, ValueError) as exc:
            items.append({"file": path.name, "valid": False, "error": str(exc)})
    return items


def restore_backup(backup_dir, filename, db_path):
    if not isinstance(filename, str) or not BACKUP_RE.fullmatch(filename):
        raise BackupError("Invalid backup filename.")
    backup_dir, db_path = Path(backup_dir), Path(db_path)
    source_path = backup_dir / filename
    # Resolve prevents path traversal even if filename validation changes later.
    if source_path.resolve().parent != backup_dir.resolve():
        raise BackupError("Invalid backup path.")
    details = validate_database(source_path)
    if db_path.exists():
        create_backup(db_path, backup_dir)  # mandatory pre-restore snapshot
    temp_path = _reserve_restore_temp_path(db_path)
    try:
        src = _connect(source_path)
        dst = _connect(temp_path)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        validate_database(temp_path)
        os.replace(temp_path, db_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return details
