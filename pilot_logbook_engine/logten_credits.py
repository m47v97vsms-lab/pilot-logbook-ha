"""Defensive conversion for optional LogTen crew time credits."""
from __future__ import annotations

import math


def sqlite_credit(value, label: str) -> float:
    """Return a finite decimal-hour credit suitable for a NOT NULL SQLite column.

    Missing credit is represented as zero; malformed or non-finite values are
    rejected so the import row is retained for exception review rather than
    silently storing an invalid value or aborting the batch.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} credit is not a valid number.") from exc
    if not math.isfinite(number) or number < 0 or number > 24:
        raise ValueError(f"{label} credit must be a finite value between 0 and 24 hours.")
    return number
