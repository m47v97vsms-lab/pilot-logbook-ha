"""Pure flight-time resolution logic shared by the API and automated tests."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from time_engine import resolve_local, AmbiguousTime


def _parse_date(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a valid ISO date.")
    try:
        return date.fromisoformat(value.strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a valid ISO date.") from None


def parse_event(payload, key, event_date, zone):
    """Return (UTC ISO-Z, airport-local ISO) for an event.

    UTC input takes precedence when both representations are supplied. Local
    inputs may be HH:MM (combined with the explicitly supplied event date) or
    a full local ISO datetime. Naive local values are never guessed in UTC.
    """
    if not isinstance(payload, dict):
        raise ValueError("Flight event data must be a JSON object.")
    if not isinstance(key, str) or key.upper() not in {"OUT", "OFF", "ON", "IN"}:
        raise ValueError("Unknown flight event.")
    local_key = key.lower() + "_local"
    utc_key = key.lower() + "_utc"
    if not isinstance(zone, str) or not zone.strip():
        raise ValueError("A valid IANA airport time zone is required.")
    zone = zone.strip()
    try:
        zone_info = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, TypeError):
        raise ValueError(f"Unknown IANA time zone: {zone}") from None

    utc_value = payload.get(utc_key)
    if utc_value is not None and utc_value != "":
        if not isinstance(utc_value, str) or not utc_value.strip():
            raise ValueError(f"{utc_key} must be text containing a UTC timestamp.")
        try:
            value = datetime.fromisoformat(utc_value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise ValueError(f"{utc_key} must be a valid timestamp with UTC offset or Z.") from None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{utc_key} must include a UTC offset or Z.")
        instant = value.astimezone(timezone.utc)
        # UTC is the authoritative event instant. Local time is a derived display
        # value and is deliberately stored without an offset; the airport timezone
        # is stored separately for reproducible display/audit context.
        local = instant.astimezone(zone_info)
        return (
            instant.isoformat().replace("+00:00", "Z"),
            local.replace(tzinfo=None).isoformat(timespec="minutes"),
        )

    raw = payload.get(local_key)
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        raise ValueError(f"{local_key} must be text.")
    raw = raw.strip()
    if not raw:
        return None, None
    if len(raw) <= 5:
        raw = f"{event_date}T{raw}"
    result = resolve_local(raw, zone, payload.get(key.lower() + "_fold"))
    return result["utc"], result["local"]


def minutes_between(start, end, label):
    if start is None or start == "" or end is None or end == "":
        return None
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError(f"{label} values must be text timestamps.")
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"{label} values must be valid ISO timestamps.") from None
    if a.tzinfo is None or a.utcoffset() is None or b.tzinfo is None or b.utcoffset() is None:
        raise ValueError(f"{label} values must be timezone-aware UTC instants.")
    seconds = (b.astimezone(timezone.utc) - a.astimezone(timezone.utc)).total_seconds()
    if seconds < 0:
        raise ValueError(f"{label} end precedes start in UTC. Check the dates and times.")
    return round(seconds / 60)


def resolve_flight_events(payload, departure_timezone, arrival_timezone):
    """Resolve all four events using explicit departure/arrival dates."""
    if not isinstance(payload, dict):
        raise ValueError("Flight data must be a JSON object.")
    flight_date = _parse_date(payload.get("flight_date"), "Flight date")
    arrival_date_value = payload.get("arrival_date")
    if arrival_date_value is None or arrival_date_value == "":
        arrival_date = flight_date
    else:
        arrival_date = _parse_date(arrival_date_value, "Arrival date")

    events = {}
    for key, day, zone in (
        ("OUT", flight_date.isoformat(), departure_timezone),
        ("OFF", flight_date.isoformat(), departure_timezone),
        ("ON", arrival_date.isoformat(), arrival_timezone),
        ("IN", arrival_date.isoformat(), arrival_timezone),
    ):
        events[key] = parse_event(payload, key, day, zone)
    return {
        "flight_date": flight_date.isoformat(),
        "arrival_date": arrival_date.isoformat(),
        "events": events,
        "block_minutes": minutes_between(events["OUT"][0], events["IN"][0], "Block time"),
        "air_minutes": minutes_between(events["OFF"][0], events["ON"][0], "Air time"),
    }
