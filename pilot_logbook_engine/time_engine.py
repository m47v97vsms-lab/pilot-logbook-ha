from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class AmbiguousTime(Exception):
    def __init__(self, payload):
        self.payload = payload


def _zone(zone_name):
    """Return a normalized IANA zone name and ZoneInfo instance."""
    if not isinstance(zone_name, str) or not zone_name.strip():
        raise ValueError("Enter a valid IANA time zone.")
    name = zone_name.strip()
    try:
        return name, ZoneInfo(name)
    except TypeError:
        raise ValueError("Enter a valid IANA time zone.") from None


def local_candidates(local_text, zone_name):
    """Return valid UTC instants for a local wall-clock time (0, 1 or 2 candidates)."""
    if not isinstance(local_text, str) or not local_text.strip():
        raise ValueError("Enter a valid local date and time.")
    try:
        naive = datetime.fromisoformat(local_text.strip())
    except (TypeError, ValueError):
        raise ValueError("Enter a valid local date and time.") from None
    if naive.tzinfo is not None:
        raise ValueError("Enter a local date and time without a UTC offset.")

    _, zone = _zone(zone_name)
    found = {}
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            found.setdefault(utc.isoformat(), {
                "utc": utc.isoformat().replace("+00:00", "Z"),
                "fold": fold,
                "offset": aware.strftime("%z"),
                "local": naive.isoformat(timespec="minutes"),
            })
    return list(found.values())


def resolve_local(local_text, zone_name, fold=None):
    """Resolve a local time without coercing malformed DST-fold values."""
    if fold is not None and (type(fold) is not int or fold not in (0, 1)):
        raise ValueError("Daylight-saving fold must be the integer 0 or 1.")

    candidates = local_candidates(local_text, zone_name)
    if not candidates:
        raise ValueError(
            f"{local_text} does not exist in {zone_name} because the clocks move forward. "
            "Choose a valid local time."
        )
    if len(candidates) > 1 and fold is None:
        raise AmbiguousTime({
            "message": f"{local_text} occurs twice in {zone_name}. Select the correct occurrence.",
            "candidates": candidates,
        })
    if fold is not None:
        for candidate in candidates:
            if candidate["fold"] == fold:
                return candidate
        raise ValueError("Selected daylight-saving occurrence is not valid.")
    return candidates[0]


def utc_to_local(utc_text, zone_name):
    """Convert an offset-aware instant to an ISO local wall time and offset."""
    if not isinstance(utc_text, str) or not utc_text.strip():
        raise ValueError("Enter a valid UTC timestamp.")
    try:
        instant = datetime.fromisoformat(utc_text.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("Enter a valid UTC timestamp.") from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("UTC value must include Z or a UTC offset.")

    name, zone = _zone(zone_name)
    local = instant.astimezone(zone)
    return {
        "utc": instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "local": local.replace(tzinfo=None).isoformat(timespec="minutes"),
        "offset": local.strftime("%z"),
        "timezone": name,
    }
