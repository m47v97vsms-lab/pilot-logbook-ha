"""Read-only validation and explicit-confirmation promotion helpers for LogTen rows."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime


def _minutes(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    a = datetime.fromisoformat(start.replace("Z", "+00:00"))
    b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    if a.tzinfo is None or a.utcoffset() is None or b.tzinfo is None or b.utcoffset() is None:
        raise ValueError("UTC timestamps must be timezone-aware")
    seconds = (b - a).total_seconds()
    if seconds < 0:
        raise ValueError("Event sequence is not chronological")
    return int(seconds // 60)


def _decimal_hours(value: str | None, label: str, blockers: list[str]) -> float | None:
    """Parse a source time credit expressed as HH:MM or decimal hours."""
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hours, minutes = (int(part) for part in text.split(":"))
        if hours <= 24 and minutes < 60 and not (hours == 24 and minutes):
            return hours + minutes / 60
        blockers.append(f"{label} must be a valid HH:MM duration or decimal-hour value (0–24 hours).")
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        blockers.append(f"{label} must be a decimal-hour value.")
        return None
    if not math.isfinite(number) or number < 0 or number > 24:
        blockers.append(f"{label} must be between 0 and 24 decimal hours.")
        return None
    return number


def build_promotion_preview(assessment: dict) -> dict:
    """Build a candidate for event-timed or duration-only historical records.

    Duration-only records retain the supplied duration as logged flight time.
    They do not receive derived air/block time or fabricated event timestamps.
    """
    blockers = list(assessment.get("errors") or [])
    warnings = list(assessment.get("warnings") or [])
    mapped = assessment.get("mapped") or {}
    airports = assessment.get("resolved_airports") or {}
    times = assessment.get("times") or {}
    duration_minutes = assessment.get("duration_minutes")

    for field, label in (("pic_crew", "PIC/P1 crew"), ("sic_crew", "SIC/P2 crew")):
        if len(str(mapped.get(field) or "").strip()) > 120:
            blockers.append(f"{label} exceeds the 120-character crew-name limit.")
    if assessment.get("classification") not in (None, "flight_candidate"):
        blockers.append("Only route-flight candidates can be previewed for promotion.")
    unmapped_columns = assessment.get("unmapped_columns") or []
    if unmapped_columns and not assessment.get("unmapped_acknowledged", False):
        blockers.append("Unmapped source columns contain data. Map them or explicitly acknowledge source-only retention: " +
                        ", ".join(item.get("source_column", "unknown") for item in unmapped_columns))
    elif unmapped_columns:
        warnings.append("Unmapped source columns remain preserved in the staged source record: " +
                        ", ".join(item.get("source_column", "unknown") for item in unmapped_columns))
    if not assessment.get("eligible_for_manual_promotion_review", False):
        blockers.append("Assessment is not eligible for manual promotion review.")
    for key in ("departure_icao", "arrival_icao", "departure_timezone", "arrival_timezone"):
        if not airports.get(key):
            blockers.append(f"Resolved {key.replace('_', ' ')} is missing.")

    event_fields = ("out", "off", "on", "in")
    present_events = [key for key in event_fields if str(mapped.get(key) or "").strip()]
    duration_only = not present_events
    if duration_only:
        if duration_minutes is None:
            blockers.append("A valid Total Time duration is required when OUT/OFF/ON/IN are absent.")
        warnings.append("Duration-only historical record: supplied duration is retained as logged flight time; block time, air time and event timestamps are unavailable.")
    else:
        for key in event_fields:
            if times.get(key, {}).get("status") != "valid_local_time":
                blockers.append(f"{key.upper()} must have one unambiguous resolved timestamp; partial event-time records cannot be inferred.")

    if blockers:
        return {"ready_for_manual_confirmation": False, "promotion_enabled": False,
                "blockers": list(dict.fromkeys(blockers)), "warnings": warnings, "candidate": None}

    def credits(validation_minutes):
        values = {}
        for key, label in (("pic", "PIC"), ("sic", "SIC"), ("pilot_flying", "Pilot Flying"),
                           ("night", "Night time"), ("actual_instrument", "Actual instrument time")):
            values[key] = _decimal_hours(mapped.get(key), label, blockers)
        for key, label in (("pic", "PIC time"), ("sic", "SIC time"), ("pilot_flying", "Pilot Flying time"),
                           ("night", "Night time"), ("actual_instrument", "Actual instrument time")):
            value = values[key]
            if value is not None and validation_minutes is not None and value * 60 > validation_minutes + 1:
                basis = "logged duration" if duration_only else "calculated OFF–ON air time"
                blockers.append(f"{label} cannot exceed {basis} ({validation_minutes} minutes) by more than one minute.")
        return values

    if duration_only:
        block_minutes, air_minutes, logged_minutes = None, None, duration_minutes
        ordered_utc, local_values = [None] * 4, [None] * 4
        arrival_date = mapped["flight_date"]
    else:
        def utc(key): return times[key]["utc_candidate"]["utc"]
        def local(key): return times[key]["utc_candidate"]["local"]
        ordered_utc = [utc(key) for key in event_fields]
        instants = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in ordered_utc]
        if not all(instants[i] < instants[i + 1] for i in range(3)):
            blockers.append("OUT, OFF, ON and IN must be strictly chronological.")
        try:
            block_minutes = _minutes(ordered_utc[0], ordered_utc[3])
            air_minutes = _minutes(ordered_utc[1], ordered_utc[2])
        except ValueError:
            block_minutes = air_minutes = None
            blockers.append("UTC event sequence is invalid.")
        if block_minutes is None or block_minutes <= 0:
            blockers.append("Calculated block time must be greater than zero.")
        if air_minutes is None or air_minutes <= 0:
            blockers.append("Calculated air time must be greater than zero.")
        if block_minutes is not None and air_minutes is not None and air_minutes > block_minutes:
            blockers.append("Calculated air time exceeds calculated block time.")
        local_values = [local(key) for key in event_fields]
        arrival_date = times["in"].get("local_date")
        logged_minutes = duration_minutes if duration_minutes is not None else air_minutes

    credit_values = credits(logged_minutes if duration_only else air_minutes)
    if blockers:
        return {"ready_for_manual_confirmation": False, "promotion_enabled": False,
                "blockers": list(dict.fromkeys(blockers)), "warnings": warnings, "candidate": None}
    if not duration_only and duration_minutes is not None and abs(duration_minutes - air_minutes) > 5:
        warnings.append("LogTen Total Time differs from the OFF/ON air-time calculation by more than five minutes.")

    source_total_time = next((item.get("original_value") for item in assessment.get("reconciliation", []) if item.get("target") == "total_time"), mapped.get("total_time") or None)

    candidate = {
        "flight_date": mapped["flight_date"], "arrival_date": arrival_date,
        "flight_number": mapped.get("flight_number") or None,
        "aircraft_registration": mapped.get("aircraft_registration") or None,
        "aircraft_type": mapped.get("aircraft_type") or None,
        "departure_icao": airports["departure_icao"], "arrival_icao": airports["arrival_icao"],
        "out_utc": ordered_utc[0], "off_utc": ordered_utc[1], "on_utc": ordered_utc[2], "in_utc": ordered_utc[3],
        "out_local": local_values[0], "off_local": local_values[1], "on_local": local_values[2], "in_local": local_values[3],
        "departure_timezone": airports["departure_timezone"], "arrival_timezone": airports["arrival_timezone"],
        "block_minutes": block_minutes, "air_minutes": air_minutes, "logged_minutes": logged_minutes,
        "pic": credit_values["pic"], "sic": credit_values["sic"], "pilot_flying": credit_values["pilot_flying"],
        "night_minutes": credit_values["night"] * 60 if credit_values["night"] is not None else None,
        "actual_instrument_minutes": credit_values["actual_instrument"] * 60 if credit_values["actual_instrument"] is not None else None,
        "pic_crew": mapped.get("pic_crew") or None, "sic_crew": mapped.get("sic_crew") or None,
        "remarks": mapped.get("remarks") or None,
        "source_only_columns": [item.get("source_column") for item in unmapped_columns],
        "time_recording_mode": "duration_only" if duration_only else "out_off_on_in",
        "source_total_time": source_total_time,
    }
    return {"ready_for_manual_confirmation": True, "promotion_enabled": False, "blockers": [],
            "warnings": list(dict.fromkeys(warnings)), "candidate": candidate,
            "note": "Read-only preview. No live flight has been created or changed."}


def preview_fingerprint(
    candidate: dict,
    source_fingerprint: str,
    corrections: dict,
    field_mapping: dict | None = None,
    acknowledgements: dict | None = None,
    duplicate_review: dict | None = None,
) -> str:
    """Bind confirmation to source, mapping, corrections, acknowledgements, duplicates and candidate."""
    payload = {
        "candidate": candidate,
        "source_fingerprint": source_fingerprint,
        "corrections": corrections,
        "field_mapping": field_mapping or {},
        "acknowledgements": acknowledgements or {},
        "duplicate_review": duplicate_review or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
