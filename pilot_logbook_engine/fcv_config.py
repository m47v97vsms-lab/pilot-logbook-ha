"""Read Home Assistant add-on options without exposing secrets to the UI."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


def _options_path() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data")) / "options.json"


@lru_cache(maxsize=1)
def _read_options_cached(path: str, modified_ns: int) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("Home Assistant add-on options could not be read.")
    if not isinstance(value, dict):
        raise RuntimeError("Home Assistant add-on options have an invalid format.")
    return value


def read_options() -> dict[str, Any]:
    path = _options_path()
    try:
        modified_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        modified_ns = 0
    return _read_options_cached(str(path), modified_ns)


def setting(env_name: str, option_name: str) -> str:
    """Environment overrides are useful for tests; Supervisor options are normal HA config."""
    env_value = os.environ.get(env_name)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    value = read_options().get(option_name, "")
    return value.strip() if isinstance(value, str) else ""
