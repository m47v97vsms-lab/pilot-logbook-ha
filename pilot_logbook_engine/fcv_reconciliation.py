"""Build a non-writing canonical-flight reconciliation preview from FC View data."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fcv_normalize import normalize_fcv_flight


EVENT_MAP = {
    "out_utc": "actual_out_utc",
    "off_utc": "actual_off_utc",
    "on_utc": "actual_on_utc",
    "in_utc": "actual_in_utc",
}


def build_reconciliation_preview(
    source: dict[str, Any],
    airport_timezones: dict[str, str],
) -> dict[str, Any]:
    """Map verified actual FCV events to a candidate; never writes to SQLite."""
    normalized = normalize_fcv_flight(source)
    warnings = list(normalized["warnings"])
    departure = normalized["departure_icao"]
    arrival = normalized["arrival_icao"]
    dep_zone_name = airport_timezones.get(departure) if departure else None
    arr_zone_name = airport_timezones.get(arrival) if arrival else None
    dep_zone = arr_zone = None

    if not dep_zone_name:
        warnings.append("Departure airport has no configured IANA time zone.")
    else:
        try:
            dep_zone = ZoneInfo(dep_zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            warnings.append("Departure airport time zone is invalid.")
    if not arr_zone_name:
        warnings.append("Arrival airport has no configured IANA time zone.")
    else:
        try:
            arr_zone = ZoneInfo(arr_zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            warnings.append("Arrival airport time zone is invalid.")

    utc_events: dict[str, datetime | None] = {}
    for canonical_name, source_name in EVENT_MAP.items():
        value = normalized["actual_events_utc"].get(source_name)
        try:
            utc_events[canonical_name] = datetime.fromisoformat(value).astimezone(timezone.utc) if value else None
        except (TypeError, ValueError):
            utc_events[canonical_name] = None

    ordered = [utc_events[name] for name in ("out_utc", "off_utc", "on_utc", "in_utc")]
    if all(ordered):
        if not (ordered[0] < ordered[1] < ordered[2] < ordered[3]):
            warnings.append("Actual OUT/OFF/ON/IN events are not in chronological order.")
    else:
        warnings.append("All four actual UTC events are required for a complete candidate.")

    local_events: dict[str, str | None] = {
        "out_local": None, "off_local": None, "on_local": None, "in_local": None
    }
    for event_name in ("out_utc", "off_utc"):
        value = utc_events[event_name]
        if value and dep_zone:
            local_events[event_name.replace("_utc", "_local")] = value.astimezone(dep_zone).replace(tzinfo=None).isoformat(sep=" ")
    for event_name in ("on_utc", "in_utc"):
        value = utc_events[event_name]
        if value and arr_zone:
            local_events[event_name.replace("_utc", "_local")] = value.astimezone(arr_zone).replace(tzinfo=None).isoformat(sep=" ")

    block_minutes = None
    air_minutes = None
    if utc_events["out_utc"] and utc_events["in_utc"]:
        block_minutes = int((utc_events["in_utc"] - utc_events["out_utc"]).total_seconds() // 60)
        if block_minutes < 0:
            warnings.append("Calculated block duration is negative.")
            block_minutes = None
    if utc_events["off_utc"] and utc_events["on_utc"]:
        air_minutes = int((utc_events["on_utc"] - utc_events["off_utc"]).total_seconds() // 60)
        if air_minutes < 0:
            warnings.append("Calculated air duration is negative.")
            air_minutes = None

    if air_minutes is not None and block_minutes is not None and air_minutes > block_minutes:
        warnings.append("Calculated air duration exceeds calculated block duration.")

    reported_block = normalized["reported_block_minutes"]
    if reported_block is not None and block_minutes is not None and abs(reported_block - block_minutes) > 5:
        warnings.append("FCV reported block duration differs from OUT/IN calculation by more than five minutes.")
    if normalized["is_deadhead"]:
        warnings.append("FCV marks this as a deadhead; do not promote it as pilot-operated flight time.")

    required = (
        bool(normalized["fcv_flight_id"]), bool(normalized["flight_number"]),
        bool(departure), bool(arrival), bool(dep_zone), bool(arr_zone),
        all(ordered), block_minutes is not None, air_minutes is not None,
        not normalized["is_deadhead"],
        air_minutes is not None and block_minutes is not None and air_minutes <= block_minutes,
    )
    ready = all(required) and not any("not in chronological order" in w for w in warnings)

    candidate = {
        "flight_date": local_events["out_local"][:10] if local_events["out_local"] else None,
        "arrival_date": local_events["in_local"][:10] if local_events["in_local"] else None,
        "flight_number": normalized["flight_number"],
        "aircraft_registration": normalized["aircraft_registration"],
        "aircraft_type": normalized["aircraft_type"],
        "departure_icao": departure,
        "arrival_icao": arrival,
        "out_utc": utc_events["out_utc"].isoformat() if utc_events["out_utc"] else None,
        "off_utc": utc_events["off_utc"].isoformat() if utc_events["off_utc"] else None,
        "on_utc": utc_events["on_utc"].isoformat() if utc_events["on_utc"] else None,
        "in_utc": utc_events["in_utc"].isoformat() if utc_events["in_utc"] else None,
        **local_events,
        "departure_timezone": dep_zone_name if dep_zone else None,
        "arrival_timezone": arr_zone_name if arr_zone else None,
        "block_minutes": block_minutes,
        "air_minutes": air_minutes,
        "pic": None,
        "sic": None,
        "pilot_flying": None,
    }
    return {
        "fcv_flight_id": normalized["fcv_flight_id"],
        "candidate": candidate,
        "ready_for_manual_review": ready,
        "warnings": warnings,
        "crew": normalized["flight_crew"],
        "cabin_crew": normalized["cabin_crew"],
        "reported_block_minutes": reported_block,
        "pf_pm_assignment_required": True,
        "canonical_flight_written": False,
    }
