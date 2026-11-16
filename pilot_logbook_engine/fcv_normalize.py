"""Normalize documented FC View flight payloads for review only.

No function in this module writes canonical logbook records.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


UTC_FIELDS = ("actual_out_utc", "actual_off_utc", "actual_on_utc", "actual_in_utc")
SCHEDULED_UTC_FIELDS = ("scheduled_out_utc", "scheduled_in_utc")
LOCAL_FIELDS = (
    "scheduled_out_local", "scheduled_in_local",
    "actual_out_local", "actual_off_local", "actual_on_local", "actual_in_local",
)


def _utc_value(value: Any, field: str, warnings: list[str]) -> str | None:
    if value in (None, ""):
        warnings.append(f"{field} is missing.")
        return None
    if not isinstance(value, str):
        warnings.append(f"{field} is not a timestamp string.")
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        warnings.append(f"{field} does not match YYYY-MM-DD HH:MM:SS.")
        return None
    return parsed.isoformat()


def _crew_list(value: Any, field: str, warnings: list[str]) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        warnings.append(f"{field} is not a list.")
        return []
    result = []
    for index, member in enumerate(value):
        if not isinstance(member, dict):
            warnings.append(f"{field}[{index}] is not an object.")
            continue
        result.append({
            "position": str(member.get("position") or "").strip(),
            "name": str(member.get("name") or "").strip(),
            "employee_id": str(member.get("id") or "").strip(),
        })
    return result


def normalize_fcv_flight(item: dict[str, Any]) -> dict[str, Any]:
    """Return a conservative review projection while preserving raw source separately."""
    if not isinstance(item, dict):
        raise ValueError("FC View flight must be an object.")
    warnings: list[str] = []
    flight_id = item.get("fcv_flight_id")
    if flight_id is None or not str(flight_id).strip():
        warnings.append("fcv_flight_id is missing; this record cannot be reconciled safely.")

    block_raw = item.get("block")
    block_minutes = None
    if block_raw not in (None, ""):
        digits = str(block_raw).strip()
        if len(digits) == 4 and digits.isdigit() and int(digits[2:]) < 60:
            block_minutes = int(digits[:2]) * 60 + int(digits[2:])
        else:
            warnings.append("block is not a valid HHMM duration.")

    actual_utc = {field: _utc_value(item.get(field), field, warnings) for field in UTC_FIELDS}
    scheduled_utc = {
        field: _utc_value(item.get(field), field, warnings)
        for field in SCHEDULED_UTC_FIELDS
    }
    local_times = {}
    for field in LOCAL_FIELDS:
        value = item.get(field)
        if value in (None, ""):
            local_times[field] = None
        elif isinstance(value, str):
            local_times[field] = value
        else:
            local_times[field] = None
            warnings.append(f"{field} is not a local timestamp string.")

    departure_icao = str(item.get("dep_airport_icao") or "").strip().upper() or None
    arrival_icao = str(item.get("arr_airport_icao") or "").strip().upper() or None
    if not departure_icao:
        warnings.append("Departure ICAO is missing; verify the airport before reconciliation.")
    if not arrival_icao:
        warnings.append("Arrival ICAO is missing; verify the airport before reconciliation.")

    tail = item.get("fcv_tail_number") or item.get("tail_info")
    if not item.get("fcv_tail_number") and item.get("tail_info"):
        warnings.append("fcv_tail_number is unavailable; tail_info is a schedule-derived fallback.")

    deadhead_value = item.get("is_deadhead")
    is_deadhead = deadhead_value in (1, "1", True)
    if deadhead_value not in (0, 1, "0", "1", True, False, None):
        warnings.append("is_deadhead has an unrecognized value.")

    return {
        "fcv_flight_id": str(flight_id).strip() if flight_id is not None else None,
        "flight_number": str(item.get("flight_number") or "").strip() or None,
        "trip_number": str(item.get("trip_number") or "").strip() or None,
        "is_deadhead": is_deadhead,
        "departure_icao": departure_icao,
        "departure_iata": str(item.get("dep_airport") or "").strip().upper() or None,
        "arrival_icao": arrival_icao,
        "arrival_iata": str(item.get("arr_airport") or "").strip().upper() or None,
        "aircraft_registration": str(tail).strip() if tail not in (None, "") else None,
        "aircraft_type": str(item.get("fcv_aircraft_type") or "").strip() or None,
        "reported_block_minutes": block_minutes,
        "actual_events_utc": actual_utc,
        "scheduled_events_utc": scheduled_utc,
        "local_times_source": local_times,
        "departure_runway": str(item.get("dep_runway") or "").strip() or None,
        "arrival_runway": str(item.get("arr_runway") or "").strip() or None,
        "flight_crew": _crew_list(item.get("crew_list"), "crew_list", warnings),
        "cabin_crew": _crew_list(item.get("cabin_crew_list"), "cabin_crew_list", warnings),
        "warnings": warnings,
        "requires_manual_pf_pm_assignment": True,
        "canonical_flight_created": False,
    }
