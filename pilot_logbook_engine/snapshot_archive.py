"""Validation for versioned development snapshot ZIP archives."""
from __future__ import annotations

import stat
import zipfile
from pathlib import Path


class SnapshotArchiveError(ValueError):
    """Raised when a development snapshot archive is unsafe or incomplete."""


def validate_snapshot_archive(
    archive_path: str | Path,
    expected_root: str = "pilot_logbook_engine_034",
    max_uncompressed_bytes: int = 100 * 1024 * 1024,
    max_entries: int = 10_000,
) -> dict:
    """Validate ZIP structure, paths, entry types, size and CRC before extraction."""
    if not expected_root or "/" in expected_root or chr(92) in expected_root or expected_root in {".", ".."}:
        raise ValueError("expected_root must be a single directory name.")
    if max_uncompressed_bytes < 1 or max_entries < 1:
        raise ValueError("Archive limits must be positive.")

    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if not infos:
                raise SnapshotArchiveError("Snapshot archive is empty.")
            if len(infos) > max_entries:
                raise SnapshotArchiveError("Snapshot archive contains too many entries.")

            seen = set()
            file_count = 0
            uncompressed_bytes = 0

            for info in infos:
                raw_name = info.filename
                if not raw_name or chr(92) in raw_name or raw_name.startswith("/"):
                    raise SnapshotArchiveError("Snapshot contains an invalid or absolute path.")

                name = raw_name[:-1] if info.is_dir() and raw_name.endswith("/") else raw_name
                parts = name.split("/")
                if not name or any(part in {"", ".", ".."} for part in parts):
                    raise SnapshotArchiveError("Snapshot contains an unsafe path component.")
                if any(":" in part for part in parts):
                    raise SnapshotArchiveError("Snapshot contains a drive-qualified path.")
                if len(parts) == 1 and not info.is_dir():
                    raise SnapshotArchiveError("The project root entry must be a directory.")
                if parts[0] != expected_root:
                    raise SnapshotArchiveError(
                        f"Every archive entry must be inside {expected_root}/."
                    )

                normalized = "/".join(parts)
                if normalized in seen:
                    raise SnapshotArchiveError("Snapshot contains duplicate paths.")
                seen.add(normalized)

                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type == stat.S_IFLNK:
                    raise SnapshotArchiveError("Snapshot archives may not contain symbolic links.")
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise SnapshotArchiveError("Snapshot contains a special filesystem entry.")
                if info.is_dir() and file_type not in (0, stat.S_IFDIR):
                    raise SnapshotArchiveError("Snapshot directory entry has an invalid file type.")
                if not info.is_dir() and file_type == stat.S_IFDIR:
                    raise SnapshotArchiveError("Snapshot file entry has a directory file type.")
                if info.flag_bits & 0x1:
                    raise SnapshotArchiveError("Encrypted snapshot archives are not supported.")

                if not info.is_dir():
                    file_count += 1
                    uncompressed_bytes += info.file_size
                    if uncompressed_bytes > max_uncompressed_bytes:
                        raise SnapshotArchiveError("Snapshot exceeds the uncompressed size limit.")

            if file_count == 0:
                raise SnapshotArchiveError("Snapshot archive contains no project files.")

            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise SnapshotArchiveError(f"Snapshot member failed its CRC check: {corrupt_member}")

            return {
                "file_count": file_count,
                "uncompressed_bytes": uncompressed_bytes,
            }
    except SnapshotArchiveError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SnapshotArchiveError("Snapshot is not a readable ZIP archive.") from exc
