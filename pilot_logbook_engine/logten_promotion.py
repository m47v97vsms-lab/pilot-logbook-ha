"""Read-only validation and explicit-confirmation promotion helpers for LogTen rows."""
from __future__ import annotations

import hashlib
import json
import math
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
    if value is None or not str(value).strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        blockers.append(f"{label} must be a decimal-hour value.")
        return None
    if not math.isfinite(number) or number < 0 or number > 24:
        blockers.append(f"{label} must be between 0 and 24 decimal hours.")
        return None
    return number


def build_promotion_preview(assessment: dict) -> dict:
    """Build a complete candidate without writing to the canonical flights table.

    All four local events must have one resolved UTC instant. OUT < OFF < ON < IN
    is enforced strictly, and numeric credit fields are validated before a
    candidate can be presented for explicit confirmation.
    """
    blockers = list(assessment.get("errors") or [])
    warnings = list(assessment.get("warnings") or [])
    mapped = assessment.get("mapped") or {}
    airports = assessment.get("resolved_airports") or {}
    times = assessment.get("times") or {}

    for field, label in (("pic_crew", "PIC/P1 crew"), ("sic_crew", "SIC/P2 crew")):
        if len(str(mapped.get(field) or "").strip()) > 120:
            blockers.append(f"{label} exceeds the 120-character crew-name limit.")

    if assessment.get("classification") not in (None, "flight_candidate"):
        blockers.append("Only route-flight candidates can be previewed for promotion.")
    unmapped_columns = assessment.get("unmapped_columns") or []
    if unmapped_columns and not assessment.get("unmapped_acknowledged", False):
        names = ", ".join(item.get("source_column", "unknown") for item in unmapped_columns)
        blockers.append(
            "Unmapped source columns contain data. Map them or explicitly acknowledge source-only retention: "
            + names
        )
    elif unmapped_columns:
        warnings.append(
            "Unmapped source columns remain preserved in the staged source record: "
            + ", ".join(item.get("source_column", "unknown") for item in unmapped_columns)
        )
    if not assessment.get("eligible_for_manual_promotion_review", False):
        blockers.append("Assessment is not eligible for manual promotion review.")
    for key in ("departure_icao", "arrival_icao", "departure_timezone", "arrival_timezone"):
        if not airports.get(key):
            blockers.append(f"Resolved {key.replace('_', ' ')} is missing.")
    for key in ("out", "off", "on", "in"):
        if times.get(key, {}).get("status") != "valid_local_time":
            blockers.append(f"{key.upper()} must have one unambiguous resolved timestamp.")

    if blockers:
        return {
            "ready_for_manual_confirmation": False,
            "promotion_enabled": False,
            "blockers": list(dict.fromkeys(blockers)),
            "warnings": warnings,
            "candidate": None,
        }

    def utc(key: str) -> str:
        return times[key]["utc_candidate"]["utc"]

    def local(key: str) -> str:
        return times[key]["utc_candidate"]["local"]

    ordered_utc = [utc(key) for key in ("out", "off", "on", "in")]
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

    pic = _decimal_hours(mapped.get("pic"), "PIC", blockers)
    sic = _decimal_hours(mapped.get("sic"), "SIC", blockers)
    pilot_flying = _decimal_hours(mapped.get("pilot_flying"), "Pilot Flying", blockers)
    night_hours = _decimal_hours(mapped.get("night"), "Night time", blockers)
    actual_instrument_hours = _decimal_hours(
        mapped.get("actual_instrument"), "Actual instrument time", blockers
    )
    if blockers:
        return {
            "ready_for_manual_confirmation": False,
            "promotion_enabled": False,
            "blockers": list(dict.fromkeys(blockers)),
            "warnings": warnings,
            "candidate": None,
        }

    total_time = mapped.get("total_time")
    if total_time:
        try:
            if air_minutes is not None and abs(float(total_time) * 60 - air_minutes) > 5:
                warnings.append("LogTen Total Time differs from the OFF/ON air-time calculation by more than five minutes.")
        except (TypeError, ValueError):
            # assess_logten_record already reports malformed Total Time as an error.
            pass

    candidate = {
        "flight_date": mapped["flight_date"],
        "arrival_date": times["in"].get("local_date"),
        "flight_number": mapped.get("flight_number") or None,
        "aircraft_registration": mapped.get("aircraft_registration") or None,
        "aircraft_type": mapped.get("aircraft_type") or None,
        "departure_icao": airports["departure_icao"],
        "arrival_icao": airports["arrival_icao"],
        "out_utc": ordered_utc[0],
        "off_utc": ordered_utc[1],
        "on_utc": ordered_utc[2],
        "in_utc": ordered_utc[3],
        "out_local": local("out"),
        "off_local": local("off"),
        "on_local": local("on"),
        "in_local": local("in"),
        "departure_timezone": airports["departure_timezone"],
        "arrival_timezone": airports["arrival_timezone"],
        "block_minutes": block_minutes,
        "air_minutes": air_minutes,
        "pic": pic,
        "sic": sic,
        "pilot_flying": pilot_flying,
        "night_minutes": night_hours * 60 if night_hours is not None else None,
        "actual_instrument_minutes": (
            actual_instrument_hours * 60 if actual_instrument_hours is not None else None
        ),
        "pic_crew": mapped.get("pic_crew") or None,
        "sic_crew": mapped.get("sic_crew") or None,
        "remarks": mapped.get("remarks") or None,
        "source_only_columns": [
            item.get("source_column") for item in unmapped_columns
        ],
    }
    return {
        "ready_for_manual_confirmation": True,
        "promotion_enabled": False,
        "blockers": [],
        "warnings": list(dict.fromkeys(warnings)),
        "candidate": candidate,
        "note": "Read-only preview. No live flight has been created or changed.",
    }


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
