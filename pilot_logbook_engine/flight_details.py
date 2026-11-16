from __future__ import annotations
from datetime import datetime
import math

ROLES = {"PIC", "SIC", "RELIEF", "INSTRUCTOR", "CHECK_PILOT", "OBSERVER", "OTHER"}
APPROACH_TYPES = {"ILS", "LOC", "RNAV", "RNP", "VOR", "NDB", "VISUAL", "OTHER"}

def _count(value, name, maximum=99):
    if isinstance(value, bool): raise ValueError(f"{name} must be a whole number.")
    try: n=int(value)
    except (ValueError, TypeError): raise ValueError(f"{name} must be a whole number.")
    if str(value).strip() not in {str(n), f"{n}.0"} or n < 0 or n > maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}.")
    return n

def _credit(value, name):
    try: n=float(value or 0)
    except (ValueError, TypeError): raise ValueError(f"{name} must be numeric.")
    if not math.isfinite(n) or n < 0 or n > 24: raise ValueError(f"{name} must be between 0 and 24 hours.")
    return n

def _utc(value, name):
    if value in (None, ""): return None
    if not isinstance(value,str): raise ValueError(f"{name} must be an ISO timestamp.")
    try: dt=datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError: raise ValueError(f"{name} must be an ISO timestamp.")
    if dt.tzinfo is None or dt.utcoffset() is None: raise ValueError(f"{name} must include a UTC offset.")
    return dt.astimezone(__import__('datetime').timezone.utc).isoformat().replace("+00:00", "Z")

def validate_flight_details(data):
    duty=data.get("duty") or {}; ops=data.get("operations") or {}
    report=_utc(duty.get("report_utc"),"Report time")
    release=_utc(duty.get("release_utc"),"Release time")
    duty_minutes=duty.get("duty_minutes")
    if duty_minutes not in (None, ""): duty_minutes=_count(duty_minutes,"Duty minutes", 24*60)
    notes=str(duty.get("notes") or "")
    if len(notes)>2000: raise ValueError("Duty notes are limited to 2,000 characters.")
    clean_ops={
      "day_landings":_count(ops.get("day_landings",0),"Day landings"),
      "night_landings":_count(ops.get("night_landings",0),"Night landings"),
      "approaches":_count(ops.get("approaches",0),"Approaches"),
      "holds":_count(ops.get("holds",0),"Holds"),
      "autoland":_count(ops.get("autoland",0),"Autoland count"),
      "night_minutes":_credit(ops.get("night_minutes",0),"Night time")*60,
      "actual_instrument_minutes":_credit(ops.get("actual_instrument_hours",0),"Actual instrument time")*60,
      "night_method":str(ops.get("night_method") or "manual")}
    if clean_ops["night_method"] not in {"manual","calculated","imported"}: raise ValueError("Invalid night-time method.")
    crew=[]
    raw_crew=data.get("crew") or []
    if not isinstance(raw_crew,list) or len(raw_crew)>30: raise ValueError("Crew must be a list of no more than 30 people.")
    for person in raw_crew:
        name=str(person.get("person_name") or "").strip(); role=str(person.get("role") or "OTHER").upper()
        if not name or len(name)>120: raise ValueError("Each crew member needs a name of 1–120 characters.")
        if role not in ROLES: raise ValueError(f"Invalid crew role: {role}.")
        employee_id=str(person.get("employee_id") or "").strip()
        if len(employee_id)>80: raise ValueError("Employee ID is limited to 80 characters.")
        raw_person_id = person.get("person_id")
        person_id = None
        if raw_person_id not in (None, ""):
            try: person_id = int(raw_person_id)
            except (TypeError, ValueError): raise ValueError("Crew directory reference must be a valid person ID.")
            if person_id < 1: raise ValueError("Crew directory reference must be a valid person ID.")
        crew.append({"person_id":person_id,"person_name":name,"role":role,"employee_id":employee_id or None,
          "pic_credit":_credit(person.get("pic_credit",0),"Crew PIC credit"),
          "sic_credit":_credit(person.get("sic_credit",0),"Crew SIC credit"),
          "pf_credit":_credit(person.get("pf_credit",0),"Crew PF credit"),
          "pm_credit":_credit(person.get("pm_credit",0),"Crew PM credit")})
    approaches=[]; raw_approaches=data.get("approaches") or []
    if not isinstance(raw_approaches,list) or len(raw_approaches)>50: raise ValueError("Approaches must be a list of no more than 50 entries.")
    for a in raw_approaches:
        typ=str(a.get("approach_type") or "OTHER").upper()
        if typ not in APPROACH_TYPES: raise ValueError(f"Invalid approach type: {typ}.")
        icao=str(a.get("airport_icao") or "").strip().upper() or None
        runway=str(a.get("runway") or "").strip().upper() or None
        if icao and (len(icao)!=4 or not icao.isalnum()): raise ValueError("Approach airport must be a four-character ICAO code.")
        if runway and len(runway)>8: raise ValueError("Runway is limited to 8 characters.")
        approaches.append({"approach_type":typ,"airport_icao":icao,"runway":runway,"count":_count(a.get("count",1),"Approach count")})
    return {"duty":{"report_utc":report,"release_utc":release,"duty_minutes":duty_minutes,"notes":notes},"operations":clean_ops,"crew":crew,"approaches":approaches}
