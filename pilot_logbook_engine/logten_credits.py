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



def insert_logten_crew(conn, flight_id: int, candidate: dict) -> int:
    """Insert only named crew members, with finite non-NULL credit values.

    A missing PIC/SIC name means that crew position does not exist; no
    placeholder crew row is created for single-pilot operations.
    """
    inserted = 0
    for sort_order, (field, role, credit_key) in enumerate((
        ("pic_crew", "PIC", "pic"),
        ("sic_crew", "SIC", "sic"),
    )):
        crew_name = candidate.get(field)
        if not crew_name:
            continue
        conn.execute(
            """INSERT INTO flight_crew(
                flight_id,person_name,role,pic_credit,sic_credit,pf_credit,pm_credit,sort_order
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                flight_id, crew_name, role,
                sqlite_credit(candidate.get("pic"), "PIC") if credit_key == "pic" else 0.0,
                sqlite_credit(candidate.get("sic"), "SIC") if credit_key == "sic" else 0.0,
                0.0, 0.0, sort_order,
            ),
        )
        inserted += 1
    return inserted
