"""Version-aware helpers for importing Pilot Logbook development snapshots."""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from snapshot_archive import SnapshotArchiveError, validate_snapshot_archive

EXPECTED_ROOT = "pilot_logbook_engine_034"
SNAPSHOT_PATTERN = re.compile(
    r"^pilot_logbook_engine_(?P<version>0\.4\.0-dev(?P<dev>\d+))\.zip$"
)
REQUIRED_FILES = frozenset({"app.py", "config.yaml", "requirements.txt", "Dockerfile"})
PROTECTED_NAMES = frozenset({
    ".git", ".github", "__pycache__", ".pytest_cache", ".venv", "venv",
    "snapshot_archive.py", "snapshot_import.py",
})


def snapshot_candidates(paths):
    """Return matching top-level development ZIPs in numeric version order."""
    versions = {}
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = PurePosixPath(raw.strip())
        if len(path.parts) != 1:
            continue
        name = path.name
        match = SNAPSHOT_PATTERN.fullmatch(name)
        if match:
            versions[name] = int(match.group("dev"))
    return sorted(versions, key=lambda name: (versions[name], name))


def select_snapshot(paths):
    """Return the newest matching top-level development ZIP, if any."""
    candidates = snapshot_candidates(paths)
    return candidates[-1] if candidates else None


def _version_number(version):
    match = re.fullmatch(r"0\.4\.0-dev(\d+)", version)
    if not match:
        raise SnapshotArchiveError(f"Unsupported development version: {version}")
    return int(match.group(1))


def _read_config_version(config_text, source_name):
    match = re.search(
        r"""(?m)^version:\s*["']?(0\.4\.0-dev\d+)["']?\s*$""",
        config_text,
    )
    if not match:
        raise SnapshotArchiveError(
            f"{source_name} does not contain a valid 0.4.0 development version."
        )
    return match.group(1)


def import_snapshot(archive_path, destination):
    """Validate and overlay a newer snapshot while preserving repository tooling."""
    archive_path = Path(archive_path)
    match = SNAPSHOT_PATTERN.fullmatch(archive_path.name)
    if not match:
        raise SnapshotArchiveError(
            "Snapshot filename must be pilot_logbook_engine_0.4.0-devN.zip."
        )
    version = match.group("version")
    details = validate_snapshot_archive(archive_path, expected_root=EXPECTED_ROOT)

    try:
        with zipfile.ZipFile(archive_path) as archive:
            files = set()
            for info in archive.infolist():
                if info.is_dir():
                    continue
                parts = PurePosixPath(info.filename).parts
                if len(parts) > 1 and parts[0] == EXPECTED_ROOT:
                    files.add(PurePosixPath(*parts[1:]).as_posix())
            missing = REQUIRED_FILES - files
            if missing:
                raise SnapshotArchiveError(
                    "Snapshot is missing required add-on files: "
                    + ", ".join(sorted(missing))
                )
            config_text = archive.read(f"{EXPECTED_ROOT}/config.yaml").decode("utf-8")
    except SnapshotArchiveError:
        raise
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise SnapshotArchiveError(
            "Snapshot does not contain a readable project config."
        ) from exc

    archive_version = _read_config_version(config_text, "Snapshot config.yaml")
    if archive_version != version:
        raise SnapshotArchiveError(
            f"Snapshot filename version {version} does not match "
            f"config.yaml version {archive_version}."
        )

    destination = Path(destination)
    current_config = destination / "config.yaml"
    if current_config.is_file():
        current_version = _read_config_version(
            current_config.read_text(encoding="utf-8"), "Current config.yaml"
        )
        if _version_number(version) <= _version_number(current_version):
            raise SnapshotArchiveError(
                f"Snapshot {version} is not newer than current version {current_version}."
            )

    with tempfile.TemporaryDirectory(prefix="pilot-logbook-snapshot-") as temp_dir:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(temp_dir)
        except (OSError, zipfile.BadZipFile) as exc:
            raise SnapshotArchiveError(
                "Snapshot could not be extracted after validation."
            ) from exc

        source_root = Path(temp_dir) / EXPECTED_ROOT
        if not source_root.is_dir():
            raise SnapshotArchiveError(
                f"Snapshot is missing its {EXPECTED_ROOT}/ project root."
            )

        destination.mkdir(parents=True, exist_ok=True)

        def ignore(_directory, names):
            return {
                name for name in names
                if name in PROTECTED_NAMES or name.endswith(".zip") or name.endswith(".pyc")
            }

        shutil.copytree(source_root, destination, dirs_exist_ok=True, ignore=ignore)

    return {"version": version, **details}
