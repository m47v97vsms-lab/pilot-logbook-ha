"""Conservative mapping and validation for staged LogTen rows.

This module never writes to the canonical flights table. It produces a review
assessment only; unresolved dates, airport codes, time zones and DST folds are
reported rather than guessed.
"""
from __future__ import annotations
import math
import re
from datetime import date, datetime, timedelta
from itertools import product
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from time_engine import local_candidates

FIELD_MAP = {
    "flight_date": "Date", "flight_number": "Flight #",
    "aircraft_registration": "Aircraft ID", "aircraft_type": "Aircraft Type",
    "departure": "From", "arrival": "To", "out": "Out", "off": "Off",
    "on": "On", "in": "In", "total_time": "Total Time",
    "night": "Night", "actual_instrument": "Actual Inst", "pic": "PIC",
    "sic": "SIC", "pilot_flying": "Pilot Flying", "pic_crew": "PIC/P1 Crew",
    "sic_crew": "SIC/P2 Crew", "remarks": "Remarks",
}

FIELD_ALIASES = {
    "flight_date": ("Date", "Flight Date"),
    "flight_number": ("Flight #", "Flight Number", "Flight No"),
    "aircraft_registration": ("Aircraft ID", "Aircraft Registration", "Registration", "Tail"),
    "aircraft_type": ("Aircraft Type",),
    "departure": ("From", "Departure", "Departure Airport", "Origin", "Dep"),
    "arrival": ("To", "Arrival", "Arrival Airport", "Destination", "Dest"),
    "out": ("Out", "Block Out"),
    "off": ("Off", "Takeoff"),
    "on": ("On", "Landing"),
    "in": ("In", "Block In"),
    "total_time": ("Total Time", "Flight Time", "Air Time", "Duration"),
    "night": ("Night", "Night Time"),
    "actual_instrument": ("Actual Inst", "Actual Instrument", "Instrument"),
    "pic": ("PIC", "PIC Time", "Pilot in Command"),
    "sic": ("SIC", "SIC Time", "Second in Command"),
    "pilot_flying": ("Pilot Flying", "PF", "PF Time"),
    "pic_crew": ("PIC/P1 Crew", "PIC Crew", "P1 Crew"),
    "sic_crew": ("SIC/P2 Crew", "SIC Crew", "P2 Crew"),
    "remarks": ("Remarks", "Comments", "Notes"),
}


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def default_field_mapping(headers) -> dict[str, str]:
    """Choose unambiguous common LogTen header aliases without altering source names."""
    available = list(headers)
    normalized = {_header_key(header): header for header in available}
    mapping = {}
    for target, aliases in FIELD_ALIASES.items():
        matches = []
        for alias in aliases:
            match = normalized.get(_header_key(alias))
            if match and match not in matches:
                matches.append(match)
        mapping[target] = matches[0] if len(matches) == 1 else ""
    return mapping


def validate_field_mapping(mapping, headers) -> dict[str, str]:
    """Validate a target-to-source mapping and fill omitted targets from safe defaults."""
    if not isinstance(mapping, dict):
        raise ValueError("field_mapping must be an object mapping canonical fields to source columns.")
    available = set(headers)
    result = default_field_mapping(headers)
    for target, source_column in mapping.items():
        if target not in FIELD_MAP:
            raise ValueError(f"Unknown canonical field: {target}")
        if not isinstance(source_column, str):
            raise ValueError(f"Mapping for {target} must be a source-column name or an empty string.")
        if source_column and source_column not in available:
            raise ValueError(f"Unknown source column for {target}: {source_column}")
        result[target] = source_column
    selected = [column for column in result.values() if column]
    if len(selected) != len(set(selected)):
        raise ValueError("A source column can only be mapped to one canonical field.")
    return result


def _parse_date(value: str):
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_hhmm(value: str):
    value = (value or "").strip()
    if not value:
        return None
    # LogTen exports commonly encode clock times as four digits (e.g. 0735).
    if len(value) == 4 and value.isdigit():
        hour, minute = int(value[:2]), int(value[2:])
        if hour < 24 and minute < 60:
            return f"{hour:02d}:{minute:02d}"
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def _resolve_airport(code: str, airports: dict):
    code = (code or "").strip().upper()
    if not code:
        return None
    return airports.get(code)


def assess_logten_record(
    raw: dict,
    corrections: dict,
    airports: dict,
    field_mapping: dict | None = None,
    unmapped_acknowledged: bool = False,
) -> dict:
    """Return a mapped assessment while retaining every original source column.

    field_mapping maps canonical target names to exact source header names.
    airports maps both ICAO and IATA codes to rows containing icao/timezone.
    """
    source = dict(raw)
    source.update(corrections or {})
    mapping = (
        validate_field_mapping(field_mapping, raw.keys())
        if field_mapping is not None
        else default_field_mapping(raw.keys())
    )
    mapped = {
        target: (source.get(column, "") or "").strip() if column else ""
        for target, column in mapping.items()
    }
    used_columns = {column for column in mapping.values() if column}
    reconciliation = []
    for target, column in mapping.items():
        original_value = raw.get(column, "") if column else ""
        review_value = source.get(column, "") if column else ""
        reconciliation.append({
            "target": target,
            "label": target.replace("_", " ").title(),
            "source_column": column or None,
            "original_value": original_value,
            "review_value": review_value,
            "mapped_value": mapped.get(target, ""),
            "status": "unmapped" if not column else ("blank" if not str(review_value).strip() else "mapped"),
        })
    unmapped_columns = [
        {
            "source_column": column,
            "original_value": raw.get(column, ""),
            "review_value": source.get(column, ""),
        }
        for column in raw
        if column not in used_columns and str(source.get(column, "") or "").strip()
    ]
    errors, warnings, times = [], [], {}

    flight_date = _parse_date(mapped["flight_date"])
    if not flight_date:
        errors.append("Date is missing or is not YYYY-MM-DD.")

    dep = _resolve_airport(mapped["departure"], airports)
    arr = _resolve_airport(mapped["arrival"], airports)
    if not dep:
        errors.append(f"Departure airport '{mapped['departure']}' is not in the airport directory.")
    if not arr:
        errors.append(f"Arrival airport '{mapped['arrival']}' is not in the airport directory.")
    for label, airport in (("Departure", dep), ("Arrival", arr)):
        if airport and not (airport.get("timezone") or "").strip():
            errors.append(f"{label} airport {airport['icao']} has no verified time zone.")
        elif airport:
            try:
                ZoneInfo(airport["timezone"])
            except ZoneInfoNotFoundError:
                errors.append(f"{label} airport {airport['icao']} has an invalid IANA time zone.")

    if mapped["total_time"]:
        try:
            total = float(mapped["total_time"])
            if not math.isfinite(total) or total < 0 or total > 24:
                raise ValueError
        except ValueError:
            errors.append("Total Time is not a valid decimal-hour value.")
    else:
        warnings.append("Total Time is blank.")

    for field in ("out", "off", "on", "in"):
        original = mapped[field]
        if not original:
            times[field] = {"source": original, "status": "missing"}
            continue
        parsed = _parse_hhmm(original)
        if not parsed:
            errors.append(f"{field.upper()} time '{original}' is not a recognized clock time.")
            times[field] = {"source": original, "status": "invalid"}
            continue
        times[field] = {"source": original, "local_time": parsed, "status": "pending"}

    # Resolve a single local date for departure events and a single local date
    # for arrival events. Arrival may be the previous/next calendar day due to
    # date-line crossings or overnight operations. Never choose among multiple
    # valid UTC timelines automatically.
    present_events = [(field, airport) for field, airport in
                      (("out", dep), ("off", dep), ("on", arr), ("in", arr))
                      if times.get(field, {}).get("status") == "pending" and airport
                      and airport.get("timezone") and flight_date]
    if present_events:
        groups = []
        dep_events = [(f, a) for f, a in present_events if f in ("out", "off")]
        arr_events = [(f, a) for f, a in present_events if f in ("on", "in")]
        if dep_events:
            groups.append((dep_events, (0,)))
        if arr_events:
            groups.append((arr_events, (-1, 0, 1)))
        options_by_event = []
        for events, offsets in groups:
            for field, airport in events:
                options = []
                for day_offset in offsets:
                    event_date = flight_date + timedelta(days=day_offset)
                    local_text = f"{event_date.isoformat()}T{times[field]['local_time']}"
                    try:
                        candidates = local_candidates(local_text, airport["timezone"])
                    except (ValueError, ZoneInfoNotFoundError):
                        candidates = []
                    options.extend((field, airport, day_offset, candidate) for candidate in candidates)
                if not options:
                    stated_date = flight_date
                    local_text = f"{stated_date.isoformat()}T{times[field]['local_time']}"
                    try:
                        stated_candidates = local_candidates(local_text, airport["timezone"])
                    except (ValueError, ZoneInfoNotFoundError):
                        stated_candidates = []
                    if not stated_candidates:
                        errors.append(f"{field.upper()} local time does not exist on the flight date in {airport['timezone']} (daylight-saving gap or invalid local time).")
                        times[field]["status"] = "nonexistent_local_time"
                options_by_event.append(options)
        if all(options_by_event):
            sequences = []
            for sequence in product(*options_by_event):
                # Preserve the operational order OUT -> OFF -> ON -> IN, even
                # when one or more optional source timestamps are absent.
                utc_values = [entry[3]["utc"] for entry in sequence]
                if all(utc_values[i] <= utc_values[i + 1] for i in range(len(utc_values) - 1)):
                    signature = tuple((entry[0], entry[2], entry[3]["utc"]) for entry in sequence)
                    if signature not in sequences:
                        sequences.append(signature)
            # Total Time is airborne time, so OUT-to-IN should be somewhat
            # longer. Use it only to reject date offsets that are clearly a
            # full day too long; never treat it as an exact timestamp source.
            if len(sequences) > 1 and mapped.get("total_time"):
                try:
                    airborne_hours = float(mapped["total_time"])
                    out_entries = [e for e in present_events if e[0] == "out"]
                    in_entries = [e for e in present_events if e[0] == "in"]
                    if out_entries and in_entries:
                        out_idx = next(i for i, opts in enumerate(options_by_event)
                                       if opts and opts[0][0] == "out")
                        in_idx = next(i for i, opts in enumerate(options_by_event)
                                      if opts and opts[0][0] == "in")
                        filtered = []
                        for seq in sequences:
                            start_utc = datetime.fromisoformat(seq[out_idx][2].replace("Z", "+00:00"))
                            end_utc = datetime.fromisoformat(seq[in_idx][2].replace("Z", "+00:00"))
                            elapsed = (end_utc - start_utc).total_seconds() / 3600
                            if airborne_hours <= elapsed <= airborne_hours + 2.0:
                                filtered.append(seq)
                        if filtered:
                            sequences = filtered
                except (ValueError, StopIteration):
                    pass
            if len(sequences) == 1:
                selected = sequences[0]
                for field, day_offset, utc_value in selected:
                    event_date = flight_date + timedelta(days=day_offset)
                    airport = dict(dep if field in ("out", "off") else arr)
                    local_text = f"{event_date.isoformat()}T{times[field]['local_time']}"
                    candidates = local_candidates(local_text, airport["timezone"])
                    match = next(c for c in candidates if c["utc"] == utc_value)
                    times[field].update({"status":"valid_local_time", "local_date":event_date.isoformat(),
                                         "date_offset_from_flight":day_offset, "utc_candidate":match})
            elif len(sequences) > 1:
                # A single ambiguous wall-clock event is specifically a DST fold.
                if len(present_events) == 1 and len(options_by_event[0]) == 2:
                    field = present_events[0][0]
                    times[field]["status"] = "ambiguous_dst"
                    times[field]["utc_candidates"] = [x[3] for x in options_by_event[0]]
                    warnings.append(f"{field.upper()} local time occurs twice because of the DST change; manual fold selection is required.")
                else:
                    warnings.append("More than one chronological UTC timeline is possible; manual arrival-date or DST-fold review is required.")
                    for field, _airport in present_events:
                        times[field]["status"] = "ambiguous_date_or_dst"
            elif not errors:
                errors.append("OUT/OFF/ON/IN times cannot be arranged in chronological order; verify the source times and flight date.")
                for field, _airport in present_events:
                    times[field]["status"] = "non_chronological"

    return {
        "mapped": mapped,
        "resolved_airports": {
            "departure_icao": dep.get("icao") if dep else None,
            "arrival_icao": arr.get("icao") if arr else None,
            "departure_timezone": dep.get("timezone") if dep else None,
            "arrival_timezone": arr.get("timezone") if arr else None,
        },
        "times": times,
        "field_mapping": mapping,
        "reconciliation": reconciliation,
        "unmapped_columns": unmapped_columns,
        "unmapped_acknowledged": bool(unmapped_acknowledged),
        "errors": errors,
        "warnings": warnings,
        "eligible_for_manual_promotion_review": not errors,
        "promotion_enabled": False,
        "note": "Assessment only. No live flight has been created or changed.",
    }
