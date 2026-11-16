from __future__ import annotations
from datetime import date
import sqlite3


def build_summary(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict:
    """Return deterministic date-filtered totals using stored flight values."""
    for label, value in (("start", start), ("end", end)):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD).") from exc
    if start and end and start > end:
        raise ValueError("Start date must be on or before end date.")
    clauses, params = [], []
    if start:
        clauses.append("f.flight_date >= ?"); params.append(start)
    if end:
        clauses.append("f.flight_date <= ?"); params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    totals = conn.execute(f"""SELECT COUNT(*) flights,
        COALESCE(SUM(f.block_minutes),0) block_minutes,
        COALESCE(SUM(f.air_minutes),0) air_minutes,
        COALESCE(SUM(o.day_landings),0) day_landings,
        COALESCE(SUM(o.night_landings),0) night_landings,
        COALESCE(SUM(o.night_minutes),0) night_minutes,
        COALESCE(SUM(o.actual_instrument_minutes),0) actual_instrument_minutes,
        COALESCE(SUM(o.approaches),0) approaches,
        COALESCE(SUM(o.holds),0) holds,
        COALESCE(SUM(o.autoland),0) autolands
        FROM flights f LEFT JOIN flight_operations o ON o.flight_id=f.id{where}""", params).fetchone()
    aircraft = conn.execute(f"""SELECT COALESCE(NULLIF(f.aircraft_type,''),'Unspecified') aircraft_type,
        COUNT(*) flights, COALESCE(SUM(f.block_minutes),0) block_minutes,
        COALESCE(SUM(f.air_minutes),0) air_minutes
        FROM flights f{where} GROUP BY COALESCE(NULLIF(f.aircraft_type,''),'Unspecified')
        ORDER BY block_minutes DESC, aircraft_type""", params).fetchall()
    return {"start": start, "end": end, "totals": dict(totals), "aircraft": [dict(r) for r in aircraft]}


def build_routes(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict:
    """Aggregate distinct airport pairs for plotting; missing coordinates remain explicit."""
    # Validate date filters with the same rules as the summary endpoint.
    build_summary(conn, start, end)
    clauses, params = [], []
    if start:
        clauses.append("f.flight_date >= ?"); params.append(start)
    if end:
        clauses.append("f.flight_date <= ?"); params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"""SELECT f.departure_icao, f.arrival_icao, COUNT(*) flights,
        COALESCE(SUM(f.block_minutes),0) block_minutes,
        dep.name departure_name, dep.latitude departure_latitude, dep.longitude departure_longitude,
        arr.name arrival_name, arr.latitude arrival_latitude, arr.longitude arrival_longitude
        FROM flights f JOIN airports dep ON dep.icao=f.departure_icao
        JOIN airports arr ON arr.icao=f.arrival_icao{where}
        GROUP BY f.departure_icao,f.arrival_icao ORDER BY flights DESC, block_minutes DESC""", params).fetchall()
    routes = [dict(r) for r in rows]
    plotted = sum(1 for r in routes if r["departure_latitude"] is not None and r["arrival_latitude"] is not None)
    return {"routes": routes, "plotted": plotted, "unplotted": len(routes)-plotted}
