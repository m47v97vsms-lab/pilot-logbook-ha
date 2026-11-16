"""Input validation for pilot logbook flight metadata."""
import math

def clean_flight_metadata(data):
    """Validate numeric credits and bound free-text fields before SQLite writes."""
    cleaned = {}
    for key in ("pic", "sic", "pilot_flying"):
        value = data.get(key)
        if value in (None, ""):
            cleaned[key] = None
            continue
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a non-negative number.")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a non-negative number.")
        if not math.isfinite(number) or number < 0 or number > 100:
            raise ValueError(f"{key} must be between 0 and 100 hours.")
        cleaned[key] = number
    limits = {"flight_number": 32, "aircraft_registration": 16,
              "aircraft_type": 32, "remarks": 2000}
    for key, limit in limits.items():
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValueError(f"{key} must be text no longer than {limit} characters.")
        cleaned[key] = value.strip() if isinstance(value, str) else value
    return cleaned
