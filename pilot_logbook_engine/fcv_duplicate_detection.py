"""Warning-only duplicate detection between FC View previews and canonical flights."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


EVENTS = ("out_utc", "off_utc", "on_utc", "in_utc")


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed
    except ValueError:
        return None


def _normal(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def assess_duplicate_candidates(candidate: dict[str, Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
    """Score plausible same-flight rows; never merge, update, or suppress records."""
    matches = []
    dep = _normal(candidate.get("departure_icao"))
    arr = _normal(candidate.get("arrival_icao"))
    number = _normal(candidate.get("flight_number"))
    registration = _normal(candidate.get("aircraft_registration"))
    candidate_date = None
    try:
        candidate_date = date.fromisoformat(str(candidate.get("flight_date") or ""))
    except ValueError:
        pass

    for row in existing:
        if _normal(row.get("departure_icao")) != dep or _normal(row.get("arrival_icao")) != arr:
            continue
        score = 2
        reasons = ["Same departure and arrival airports"]
        if number and _normal(row.get("flight_number")) == number:
            score += 3
            reasons.append("Same flight number")
        try:
            row_date = date.fromisoformat(str(row.get("flight_date") or ""))
        except ValueError:
            row_date = None
        if candidate_date and row_date:
            day_delta = abs((candidate_date - row_date).days)
            if day_delta == 0:
                score += 2
                reasons.append("Same departure-local date")
            elif day_delta == 1:
                score += 1
                reasons.append("Departure-local dates are one day apart")
        if registration and _normal(row.get("aircraft_registration")) == registration:
            score += 1
            reasons.append("Same aircraft registration")

        close_events = []
        for event in EVENTS:
            left, right = _instant(candidate.get(event)), _instant(row.get(event))
            if left and right and abs((left - right).total_seconds()) <= 300:
                close_events.append(event)
        if len(close_events) >= 2:
            score += 4
            reasons.append("At least two actual UTC events are within five minutes")
        elif len(close_events) == 1:
            score += 1
            reasons.append("One actual UTC event is within five minutes")

        if score >= 5:
            confidence = "high" if score >= 9 and len(close_events) >= 2 else ("possible" if score >= 6 else "low")
            matches.append({
                "canonical_flight_id": row.get("id"),
                "flight_date": row.get("flight_date"),
                "flight_number": row.get("flight_number"),
                "departure_icao": row.get("departure_icao"),
                "arrival_icao": row.get("arrival_icao"),
                "aircraft_registration": row.get("aircraft_registration"),
                "score": score,
                "confidence": confidence,
                "reasons": reasons,
            })
    matches.sort(key=lambda item: (-item["score"], item["canonical_flight_id"] or 0))
    return {
        "matches": matches[:5],
        "match_count": len(matches),
        "warning_only": True,
        "canonical_flights_changed": False,
        "message": "Possible duplicates are flagged for human comparison only; no records were merged or changed.",
    }
