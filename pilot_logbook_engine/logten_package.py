"""Read-only parser and reconciliation for a multi-file LogTen export package.

The package parser keeps source rows intact and produces reference indexes and
link diagnostics only. It does not write to the application database.
"""
from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any


FILE_ROLES = {
    "flights": ("export flights", "flights"),
    "address_book": ("address book export", "address book"),
    "aircraft": ("export aircraft", "aircraft"),
    "aircraft_types": ("export aircraft types", "aircraft types"),
    "places": ("export places (tab)", "places"),
    "places_kml": ("export places (kml)", "places kml"),
}


def _role_for_filename(filename: str) -> str | None:
    normalized = re.sub(r"\s+", " ", filename.casefold()).strip()
    matches = [
        (len(alias), role)
        for role, aliases in FILE_ROLES.items()
        for alias in aliases
        if alias in normalized
    ]
    if not matches:
        return None
    longest = max(length for length, _role in matches)
    best = {role for length, role in matches if length == longest}
    if len(best) != 1:
        raise ValueError(f"Ambiguous LogTen export filename: {filename}")
    role = best.pop()
    if role == "places_kml" and not normalized.endswith(".kml"):
        raise ValueError(f"{filename}: Places KML export must use a .kml filename.")
    return role


def _read_tabular(content: str | bytes, filename: str) -> dict[str, Any]:
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("\ufeff")
    sample = text[:8192]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters="\t,").delimiter
    except csv.Error:
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        original_headers = next(reader)
    except StopIteration:
        raise ValueError(f"{filename}: file is empty.")
    if not original_headers or not any(str(h).strip() for h in original_headers):
        raise ValueError(f"{filename}: no usable header row.")
    headers = []
    seen = Counter()
    for index, raw_header in enumerate(original_headers, 1):
        base = str(raw_header) if str(raw_header).strip() else f"__blank_column_{index}"
        seen[base] += 1
        headers.append(base if seen[base] == 1 else f"{base}__{seen[base]}")
    rows = []
    blank_rows = 0
    for line_number, values in enumerate(reader, 2):
        if not any(str(value).strip() for value in values):
            blank_rows += 1
            continue
        if len(values) > len(headers):
            raise ValueError(
                f"{filename}: row {line_number} has {len(values)} cells for "
                f"{len(headers)} headers; refusing to discard extra source data."
            )
        padded = list(values) + [""] * (len(headers) - len(values))
        rows.append(dict(zip(headers, padded)))
    return {
        "filename": filename,
        "delimiter": "tab" if delimiter == "\t" else "comma",
        "headers": headers,
        "rows": rows,
        "record_count": len(rows),
        "blank_rows": blank_rows,
    }


def _value(row: dict[str, str], *names: str) -> str:
    wanted = {re.sub(r"[^a-z0-9]", "", name.casefold()) for name in names}
    for key, value in row.items():
        normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
        if normalized in wanted:
            return str(value or "").strip()
    return ""


def _index(rows: list[dict[str, str]], *keys: str) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        value = _value(row, *keys)
        if value:
            result.setdefault(value.casefold(), []).append(row)
    return result


def _kml_summary(content: str | bytes, filename: str) -> dict[str, Any]:
    raw = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"{filename}: invalid KML/XML: {exc}") from exc
    placemarks = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "Placemark":
            continue
        item = {}
        for child in node.iter():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag in {"name", "description"} and child.text:
                item[tag] = child.text.strip()
            elif tag == "coordinates" and child.text:
                item["coordinates"] = child.text.strip()
        placemarks.append(item)
    return {"filename": filename, "placemark_count": len(placemarks), "placemarks": placemarks}


def parse_logten_package(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse the six LogTen exports and return a dry-run reconciliation report.

    Each input item is {"filename": str, "content": str|bytes}. Required files
    are flights, address book, aircraft, aircraft types, and tabular places.
    KML is optional enrichment. Unknown or duplicate roles are rejected.
    """
    package: dict[str, Any] = {}
    for item in files:
        filename = str(item.get("filename") or "unnamed")
        role = _role_for_filename(filename)
        if not role:
            raise ValueError(f"Unrecognized LogTen package file: {filename}")
        if role in package:
            raise ValueError(f"Duplicate LogTen package file for role: {role}")
        if role == "places_kml":
            package[role] = _kml_summary(item.get("content", b""), filename)
        else:
            package[role] = _read_tabular(item.get("content", b""), filename)

    required = {"flights", "address_book", "aircraft", "aircraft_types", "places"}
    missing = sorted(required - package.keys())
    if missing:
        raise ValueError("Missing required LogTen package exports: " + ", ".join(missing))

    flight_rows = package["flights"]["rows"]
    crew_rows = package["address_book"]["rows"]
    aircraft_rows = package["aircraft"]["rows"]
    type_rows = package["aircraft_types"]["rows"]
    place_rows = package["places"]["rows"]

    crew_index = _index(crew_rows, "Name", "Full Name")
    aircraft_index = _index(aircraft_rows, "Aircraft ID", "Secondary ID")
    type_index = _index(type_rows, "Type")
    place_index = {}
    for row in place_rows:
        for label in ("ICAO ID", "IATA ID"):
            code = _value(row, label)
            if code:
                place_index.setdefault(code.casefold(), []).append(row)

    diagnostics = Counter()
    unresolved_crew = set()
    unresolved_aircraft = set()
    unresolved_places = set()
    for row in flight_rows:
        for field_names, index, unresolved in (
            (("flight_selectedCrewPIC",), crew_index, unresolved_crew),
            (("flight_selectedCrewSIC",), crew_index, unresolved_crew),
            (("aircraft_aircraftID", "Aircraft ID"), aircraft_index, unresolved_aircraft),
            (("flight_from", "From"), place_index, unresolved_places),
            (("flight_to", "To"), place_index, unresolved_places),
        ):
            value = _value(row, *field_names)
            if not value:
                continue
            diagnostics["flight_reference_checks"] += 1
            matches = index.get(value.casefold(), [])
            if not matches:
                unresolved.add(value)
            elif len(matches) > 1:
                diagnostics["ambiguous_reference_values"] += 1
        aircraft_type = _value(row, "aircraftType_type", "Aircraft Type")
        if aircraft_type and aircraft_type.casefold() not in type_index:
            unresolved_aircraft.add(f"type:{aircraft_type}")

    # Preserve duplicates as explicit diagnostics rather than arbitrarily
    # selecting a matching reference record.
    duplicate_reference_keys = {
        "crew": sorted(key for key, values in crew_index.items() if len(values) > 1),
        "aircraft": sorted(key for key, values in aircraft_index.items() if len(values) > 1),
        "aircraft_types": sorted(key for key, values in type_index.items() if len(values) > 1),
        "places": sorted(key for key, values in place_index.items() if len(values) > 1),
    }
    return {
        "mode": "dry_run_only",
        "canonical_database_writes": False,
        "files": {
            role: {
                key: value for key, value in details.items()
                if key not in {"rows", "placemarks"}
            }
            for role, details in package.items()
        },
        "counts": {
            "flights": len(flight_rows),
            "crew_contacts": len(crew_rows),
            "aircraft": len(aircraft_rows),
            "aircraft_types": len(type_rows),
            "places": len(place_rows),
            "kml_placemarks": package.get("places_kml", {}).get("placemark_count", 0),
        },
        "unmatched": {
            "crew_names": sorted(unresolved_crew),
            "aircraft_or_type_values": sorted(unresolved_aircraft),
            "airport_codes": sorted(unresolved_places),
        },
        "duplicate_reference_keys": duplicate_reference_keys,
        "diagnostics": dict(diagnostics),
        "source_rows_retained": True,
        "note": "No canonical flight, crew, aircraft, or airport records were created or modified.",
    }
