import re

REGISTRATION_RE = re.compile(r"^[A-Z0-9-]{2,12}$")
EMPLOYEE_RE = re.compile(r"^[A-Z0-9-]{1,24}$")

def clean_aircraft(data):
    if not isinstance(data, dict):
        raise ValueError("Aircraft data must be an object.")
    registration = str(data.get("registration", "")).strip().upper()
    aircraft_type = str(data.get("aircraft_type", "")).strip().upper()
    if not REGISTRATION_RE.fullmatch(registration):
        raise ValueError("Enter a valid aircraft registration (2–12 letters, numbers or hyphens).")
    if not aircraft_type or len(aircraft_type) > 32:
        raise ValueError("Aircraft type is required and must be 32 characters or fewer.")
    result = {
        "registration": registration,
        "aircraft_type": aircraft_type,
        "make_model": str(data.get("make_model", "")).strip()[:80],
        "notes": str(data.get("notes", "")).strip()[:1000],
        "active": 1 if data.get("active", True) else 0,
    }
    return result

def clean_person(data):
    if not isinstance(data, dict):
        raise ValueError("Person data must be an object.")
    name = " ".join(str(data.get("full_name", "")).split())
    if not name or len(name) > 100:
        raise ValueError("Crew member name is required and must be 100 characters or fewer.")
    employee_id = str(data.get("employee_id", "")).strip().upper()
    if employee_id and not EMPLOYEE_RE.fullmatch(employee_id):
        raise ValueError("Employee ID must be 1–24 letters, numbers or hyphens.")
    return {
        "full_name": name,
        "employee_id": employee_id or None,
        "default_role": str(data.get("default_role", "")).strip()[:40],
        "notes": str(data.get("notes", "")).strip()[:1000],
        "active": 1 if data.get("active", True) else 0,
    }
