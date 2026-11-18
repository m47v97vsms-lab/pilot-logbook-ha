from __future__ import annotations
import os, sqlite3, json, re, urllib.request, csv, io, time
from datetime import datetime, date, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from flask import Flask, request, jsonify, render_template, g, session, redirect, url_for, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from time_engine import local_candidates, resolve_local, utc_to_local, AmbiguousTime
from flight_logic import resolve_flight_events
from flight_validation import clean_flight_metadata
from directory_validation import clean_aircraft, clean_person
from backup_manager import create_backup, list_backups, restore_backup, validate_database, BackupError
from reporting import build_summary, build_routes
from flight_calculations import calculate_route
from ingress_security import is_trusted_ingress_proxy
from logten_import import parse_logten
from logten_validation import assess_logten_record, FIELD_MAP, default_field_mapping, validate_field_mapping
from logten_promotion import build_promotion_preview, preview_fingerprint
import hmac
import hashlib
from operating_airports import ADDITIONAL_AIRPORTS, ADDITIONAL_AIRPORT_COORDS, OPERATING_AIRPORT_TIMEZONES
from fcv_storage import init_fcv_storage
from fcv_oauth_state import init_oauth_state
from fcv_scheduler import start_scheduler
from fcv_sync import sync_flights, FCVSyncError
from fcv_storage import load_tokens, delete_tokens, FCVStorageError
from fcv_client import revoke_access, FCVError, authorization_url
from fcv_oauth_state import issue_oauth_state, OAuthStateError
from fcv_config import setting, read_options
from fcv_normalize import normalize_fcv_flight
from fcv_reconciliation import build_reconciliation_preview
from fcv_duplicate_detection import assess_duplicate_candidates
from fcv_oauth_flow import complete_authorization, FCVOAuthFlowError

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "logbook_engine.sqlite3")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
CURRENT_SCHEMA_VERSION = 14
app = Flask(__name__)
def load_secret_key():
    configured = os.environ.get("SECRET_KEY")
    if configured:
        return configured
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "session_secret.key")
    if not os.path.exists(path):
        import secrets
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(48))
        os.replace(temp, path)
        os.chmod(path, 0o600)
    with open(path, encoding="utf-8") as f:
        return f.read().strip()

app.secret_key = load_secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1", PERMANENT_SESSION_LIFETIME=60*60*12)


def logten_promotion_enabled():
    """Promotion is opt-in and disabled in normal add-on installs."""
    env_value = os.environ.get("ENABLE_LOGTEN_PROMOTION")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    return read_options().get("enable_logten_promotion") is True


def _logten_batch_mapping(conn, batch_id, raw=None):
    batch = conn.execute("SELECT field_mapping_json FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        raise ValueError("Import batch not found.")
    if raw is None:
        first = conn.execute(
            "SELECT raw_json FROM import_records WHERE batch_id=? ORDER BY source_row LIMIT 1",
            (batch_id,),
        ).fetchone()
        raw = json.loads(first["raw_json"]) if first else {}
    saved = json.loads(batch["field_mapping_json"] or "{}")
    # Older staged batches may contain a complete mapping object whose values
    # are all blank (the pre-autodetection format). Treat that legacy state as
    # unset so current safe aliases are applied. Preserve any non-empty saved
    # mapping exactly, including deliberate blank selections made by users.
    if saved and any(value for value in saved.values()):
        return validate_field_mapping(saved, raw.keys())
    return default_field_mapping(raw.keys())


def _logten_classification(original, assessment):
    aircraft_type = str((assessment.get("mapped") or {}).get("aircraft_type") or "").upper()
    if original == "simulator" or "SIM" in aircraft_type or "SIMULATOR" in aircraft_type:
        return "simulator"
    mapped = assessment.get("mapped") or {}
    if mapped.get("departure") and mapped.get("arrival"):
        return "flight_candidate"
    return original


def _logten_duplicate_review(conn, batch_id, record_id, fingerprint, candidate,
                             mapped=None, resolved_airports=None):
    current = conn.execute(
        "SELECT source_row FROM import_records WHERE id=? AND batch_id=?", (record_id, batch_id)
    ).fetchone()
    current_source_row = current["source_row"] if current else None
    staged_rows = conn.execute(
        """SELECT r.id,r.batch_id,r.source_row,r.review_status,r.promoted_flight_id,b.filename
           FROM import_records r JOIN import_batches b ON b.id=r.batch_id
           WHERE r.fingerprint=? AND r.id<>? AND r.promoted_flight_id IS NULL ORDER BY r.batch_id,r.source_row,r.id""",
        (fingerprint, record_id),
    ).fetchall()
    staged_matches = [{
        "record_id": row["id"], "batch_id": row["batch_id"], "source_row": row["source_row"],
        "review_status": row["review_status"], "promoted_flight_id": row["promoted_flight_id"],
        "filename": row["filename"],
    } for row in staged_rows]
    primary_staged_duplicates = [
        item for item in staged_matches
        if item["batch_id"] == batch_id and current_source_row is not None
        and item["source_row"] < current_source_row and item["review_status"] != "rejected"
    ]
    cross_batch_staged_duplicates = [
        item for item in staged_matches
        if item["batch_id"] != batch_id and item["review_status"] != "rejected"
    ]
    candidate = candidate or {}
    mapped = mapped or {}
    resolved_airports = resolved_airports or {}
    departure = candidate.get("departure_icao") or resolved_airports.get("departure_icao")
    arrival = candidate.get("arrival_icao") or resolved_airports.get("arrival_icao")
    flight_date = candidate.get("flight_date") or mapped.get("flight_date")
    flight_number = candidate.get("flight_number") or mapped.get("flight_number")
    exact_matches, possible_matches = [], []
    if candidate.get("out_utc") and candidate.get("in_utc") and departure and arrival:
        exact_rows = conn.execute(
            """SELECT id,flight_date,flight_number,departure_icao,arrival_icao,out_utc,in_utc,aircraft_registration
               FROM flights WHERE out_utc=? AND in_utc=? AND departure_icao=? AND arrival_icao=?
               ORDER BY id LIMIT 20""",
            (candidate["out_utc"], candidate["in_utc"], departure, arrival),
        ).fetchall()
        exact_matches = [dict(row) for row in exact_rows]
    if flight_date and departure and arrival and flight_number:
        possible_rows = conn.execute(
            """SELECT id,flight_date,flight_number,departure_icao,arrival_icao,out_utc,in_utc,aircraft_registration
               FROM flights WHERE flight_date=? AND departure_icao=? AND arrival_icao=?
                 AND flight_number=? ORDER BY id LIMIT 20""",
            (flight_date, departure, arrival, flight_number),
        ).fetchall()
        exact_ids = {item["id"] for item in exact_matches}
        possible_matches = [dict(row) for row in possible_rows if row["id"] not in exact_ids]
    if (candidate.get("time_recording_mode") == "duration_only"
            and candidate.get("logged_minutes") is not None
            and flight_date and departure and arrival):
        duration_rows = conn.execute(
            """SELECT id,flight_date,flight_number,departure_icao,arrival_icao,
                      out_utc,in_utc,aircraft_registration
               FROM flights
               WHERE flight_date=? AND departure_icao=? AND arrival_icao=?
                 AND time_recording_mode='duration_only' AND logged_minutes=?
               ORDER BY id LIMIT 20""",
            (flight_date, departure, arrival, candidate["logged_minutes"]),
        ).fetchall()
        known_ids = {item["id"] for item in exact_matches + possible_matches}
        registration = candidate.get("aircraft_registration")
        for row in duration_rows:
            item = dict(row)
            if item["id"] in known_ids:
                continue
            identifier_matches = bool(
                (flight_number and item.get("flight_number") == flight_number)
                or (registration and item.get("aircraft_registration") == registration)
            )
            (exact_matches if identifier_matches else possible_matches).append(item)
    return {
        "identical_staged_records": staged_matches,
        "earlier_staged_duplicates": primary_staged_duplicates,
        "cross_batch_staged_duplicates": cross_batch_staged_duplicates,
        "canonical_exact_matches": exact_matches,
        "canonical_possible_matches": possible_matches,
        "has_exact_duplicate": bool(exact_matches),
        "has_staged_duplicate": bool(primary_staged_duplicates),
        "has_cross_batch_staged_duplicate": bool(cross_batch_staged_duplicates),
        "has_possible_duplicate": bool(possible_matches),
    }


def _apply_logten_duplicate_guard(preview, duplicate_review, duplicate_acknowledged):
    blockers = preview.setdefault("blockers", [])
    warnings = preview.setdefault("warnings", [])
    if duplicate_review["has_exact_duplicate"]:
        blockers.append("An exact canonical flight duplicate already exists.")
    if duplicate_review["has_staged_duplicate"]:
        blockers.append("An earlier identical staged source row has not been rejected. Review the duplicate rows before promotion.")
    if duplicate_review["has_cross_batch_staged_duplicate"] and not duplicate_acknowledged:
        blockers.append("An identical source row exists in another staged batch. Review and acknowledge the cross-batch duplicate before promotion.")
    elif duplicate_review["has_cross_batch_staged_duplicate"]:
        warnings.append("Identical source rows in other staged batches were explicitly reviewed.")
    if duplicate_review["has_possible_duplicate"] and not duplicate_acknowledged:
        blockers.append("A canonical flight with the same date, route and flight number may already exist. Review and acknowledge the possible duplicate before promotion.")
    elif duplicate_review["has_possible_duplicate"]:
        warnings.append("Possible canonical duplicates were explicitly reviewed; verify the selected record against the original source.")
    preview["ready_for_manual_confirmation"] = bool(preview.get("candidate") is not None and not blockers)
    return preview


# Starter reference set. The airport table is editable through the API.
AIRPORTS = [
 ("CYYC","YYC","Calgary International","America/Edmonton"),
 ("CYVR","YVR","Vancouver International","America/Vancouver"),
 ("CYYZ","YYZ","Toronto Pearson","America/Toronto"),
 ("CYUL","YUL","Montréal-Trudeau","America/Toronto"),
 ("CYEG","YEG","Edmonton International","America/Edmonton"),
 ("CYWG","YWG","Winnipeg Richardson","America/Winnipeg"),
 ("CYQR","YQR","Regina International","America/Regina"),
 ("CYXE","YXE","Saskatoon John G. Diefenbaker","America/Regina"),
 ("CYOW","YOW","Ottawa International","America/Toronto"),
 ("CYHZ","YHZ","Halifax Stanfield","America/Halifax"),
 ("CYQB","YQB","Québec City Jean Lesage","America/Toronto"),
 ("CYYJ","YYJ","Victoria International","America/Vancouver"),
 ("CYCD","YCD","Nanaimo Airport","America/Vancouver"),
 ("KLAX","LAX","Los Angeles International","America/Los_Angeles"),
 ("KSFO","SFO","San Francisco International","America/Los_Angeles"),
 ("KSEA","SEA","Seattle-Tacoma International","America/Los_Angeles"),
 ("KLAS","LAS","Harry Reid International","America/Los_Angeles"),
 ("KPHX","PHX","Phoenix Sky Harbor","America/Phoenix"),
 ("KDEN","DEN","Denver International","America/Denver"),
 ("KDFW","DFW","Dallas Fort Worth","America/Chicago"),
 ("KORD","ORD","Chicago O'Hare","America/Chicago"),
 ("KIAH","IAH","Houston Intercontinental","America/Chicago"),
 ("KATL","ATL","Hartsfield-Jackson Atlanta","America/New_York"),
 ("KJFK","JFK","John F. Kennedy International","America/New_York"),
 ("KEWR","EWR","Newark Liberty","America/New_York"),
 ("KBOS","BOS","Boston Logan","America/New_York"),
 ("KMIA","MIA","Miami International","America/New_York"),
 ("KMCO","MCO","Orlando International","America/New_York"),
 ("PHNL","HNL","Daniel K. Inouye International","Pacific/Honolulu"),
 ("PANC","ANC","Ted Stevens Anchorage","America/Anchorage"),
 ("EGLL","LHR","London Heathrow","Europe/London"),
 ("EDDF","FRA","Frankfurt Airport","Europe/Berlin"),
 ("LFPG","CDG","Paris Charles de Gaulle","Europe/Paris"),
 ("EHAM","AMS","Amsterdam Schiphol","Europe/Amsterdam"),
 ("RJTT","HND","Tokyo Haneda","Asia/Tokyo"),
 ("RJAA","NRT","Tokyo Narita","Asia/Tokyo"),
 ("RKSI","ICN","Seoul Incheon","Asia/Seoul"),
 ("VHHH","HKG","Hong Kong International","Asia/Hong_Kong"),
 ("YSSY","SYD","Sydney Kingsford Smith","Australia/Sydney"),
 ]
AIRPORTS.extend(ADDITIONAL_AIRPORTS)

# Reference coordinates are for plotting routes only; they are not navigation data.
AIRPORT_COORDS = {
 "CYYC":(51.1139,-114.0203),"CYVR":(49.1947,-123.1792),"CYYZ":(43.6777,-79.6248),
 "CYUL":(45.4706,-73.7408),"CYEG":(53.3097,-113.5797),"CYWG":(49.9100,-97.2399),
 "CYQR":(50.4319,-104.6658),"CYXE":(52.1708,-106.6997),"CYOW":(45.3225,-75.6692),
 "CYHZ":(44.8808,-63.5086),"CYQB":(46.7911,-71.3933),"CYYJ":(48.6469,-123.4258),
 "CYCD":(49.0523,-123.8702),"KLAX":(33.9416,-118.4085),"KSFO":(37.6213,-122.3790),
 "KSEA":(47.4502,-122.3088),"KLAS":(36.0840,-115.1537),"KPHX":(33.4342,-112.0116),
 "KDEN":(39.8561,-104.6737),"KDFW":(32.8998,-97.0403),"KORD":(41.9742,-87.9073),
 "KIAH":(29.9902,-95.3368),"KATL":(33.6407,-84.4277),"KJFK":(40.6413,-73.7781),
 "KEWR":(40.6895,-74.1745),"KBOS":(42.3656,-71.0096),"KMIA":(25.7959,-80.2870),
 "KMCO":(28.4312,-81.3081),"PHNL":(21.3187,-157.9225),"PANC":(61.1744,-149.9964),
 "EGLL":(51.4700,-0.4543),"EDDF":(50.0379,8.5622),"LFPG":(49.0097,2.5479),
 "EHAM":(52.3105,4.7683),"RJTT":(35.5494,139.7798),"RJAA":(35.7720,140.3929),
 "RKSI":(37.4602,126.4407),"VHHH":(22.3080,113.9185),"YSSY":(-33.9399,151.1753)
}
AIRPORT_COORDS.update(ADDITIONAL_AIRPORT_COORDS)


def db():
    if "db" not in g:
        os.makedirs(DATA_DIR, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn is not None: conn.close()

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS airports(
      icao TEXT PRIMARY KEY, iata TEXT UNIQUE, name TEXT NOT NULL,
      timezone TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS flights(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      flight_date TEXT NOT NULL,
      arrival_date TEXT NOT NULL,
      flight_number TEXT, aircraft_registration TEXT, aircraft_type TEXT,
      departure_icao TEXT NOT NULL, arrival_icao TEXT NOT NULL,
      out_utc TEXT, off_utc TEXT, on_utc TEXT, in_utc TEXT,
      out_local TEXT, off_local TEXT, on_local TEXT, in_local TEXT,
      departure_timezone TEXT NOT NULL, arrival_timezone TEXT NOT NULL,
      block_minutes INTEGER, air_minutes INTEGER, logged_minutes INTEGER,
      time_recording_mode TEXT NOT NULL DEFAULT 'out_off_on_in', source_total_time TEXT,
      pic REAL, sic REAL, pilot_flying REAL, remarks TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(departure_icao) REFERENCES airports(icao),
      FOREIGN KEY(arrival_icao) REFERENCES airports(icao)
    );
    CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS migration_history(
      version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS login_guard(
      id INTEGER PRIMARY KEY CHECK(id=1), failures INTEGER NOT NULL DEFAULT 0,
      locked_until TEXT
    );
    INSERT OR IGNORE INTO login_guard(id,failures,locked_until) VALUES(1,0,NULL);
    """)
    current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    current = current or 1
    if current > CURRENT_SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"Database schema {current} is newer than this app supports.")
    # Migration 3 introduces explicit migration tracking. It is additive and
    # preserves all existing flight rows and user credentials.
    if current < 3:
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (3)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (3,))
    else:
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (3)")
    if current < 4:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS flight_duty (
          flight_id INTEGER PRIMARY KEY, report_utc TEXT, release_utc TEXT,
          duty_minutes INTEGER, notes TEXT,
          FOREIGN KEY(flight_id) REFERENCES flights(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS flight_crew (
          id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER NOT NULL,
          person_name TEXT NOT NULL, role TEXT NOT NULL, employee_id TEXT,
          pic_credit REAL NOT NULL DEFAULT 0, sic_credit REAL NOT NULL DEFAULT 0,
          pf_credit REAL NOT NULL DEFAULT 0, pm_credit REAL NOT NULL DEFAULT 0,
          sort_order INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(flight_id) REFERENCES flights(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS flight_operations (
          flight_id INTEGER PRIMARY KEY, day_landings INTEGER NOT NULL DEFAULT 0,
          night_landings INTEGER NOT NULL DEFAULT 0, approaches INTEGER NOT NULL DEFAULT 0,
          holds INTEGER NOT NULL DEFAULT 0, autoland INTEGER NOT NULL DEFAULT 0,
          night_minutes REAL NOT NULL DEFAULT 0, night_method TEXT NOT NULL DEFAULT 'manual',
          FOREIGN KEY(flight_id) REFERENCES flights(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS flight_approaches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER NOT NULL,
          approach_type TEXT NOT NULL, airport_icao TEXT, runway TEXT, count INTEGER NOT NULL DEFAULT 1,
          FOREIGN KEY(flight_id) REFERENCES flights(id) ON DELETE CASCADE
        );
        """)
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (4)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (4)")
    if current < 5:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS aircraft (
          registration TEXT PRIMARY KEY COLLATE NOCASE,
          aircraft_type TEXT NOT NULL, make_model TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS people (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          full_name TEXT NOT NULL COLLATE NOCASE,
          employee_id TEXT UNIQUE COLLATE NOCASE,
          default_role TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_people_name ON people(full_name);
        """)
        crew_cols = {r[1] for r in conn.execute("PRAGMA table_info(flight_crew)")}
        if "person_id" not in crew_cols:
            conn.execute("ALTER TABLE flight_crew ADD COLUMN person_id INTEGER REFERENCES people(id) ON DELETE SET NULL")
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (5)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (5)")
    if current < 6:
        airport_cols = {r[1] for r in conn.execute("PRAGMA table_info(airports)")}
        if "latitude" not in airport_cols:
            conn.execute("ALTER TABLE airports ADD COLUMN latitude REAL")
        if "longitude" not in airport_cols:
            conn.execute("ALTER TABLE airports ADD COLUMN longitude REAL")
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (6)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (6)")
    if current < 7:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(airports)")}
        additions = {
            "city": "TEXT", "province": "TEXT", "country": "TEXT", "notes": "TEXT",
            "elevation_ft": "REAL", "magnetic_variation": "TEXT", "beacon": "TEXT",
            "data_source": "TEXT NOT NULL DEFAULT 'manual'",
            "data_status": "TEXT NOT NULL DEFAULT 'incomplete'",
            "updated_at": "TEXT"
        }
        for col, decl in additions.items():
            if col not in cols: conn.execute(f"ALTER TABLE airports ADD COLUMN {col} {decl}")
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (7)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (7)")
    if current < 8:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS import_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
          file_sha256 TEXT NOT NULL UNIQUE, delimiter TEXT NOT NULL,
          record_count INTEGER NOT NULL, blank_rows_skipped INTEGER NOT NULL DEFAULT 0,
          classification_json TEXT NOT NULL, warning_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS import_records(
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
          source_row INTEGER NOT NULL, fingerprint TEXT NOT NULL,
          classification TEXT NOT NULL, warnings_json TEXT NOT NULL, raw_json TEXT NOT NULL,
          review_status TEXT NOT NULL DEFAULT 'pending',
          FOREIGN KEY(batch_id) REFERENCES import_batches(id) ON DELETE CASCADE,
          UNIQUE(batch_id, source_row)
        );
        CREATE INDEX IF NOT EXISTS idx_import_records_batch ON import_records(batch_id, source_row);
        """)
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (8)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (8)")
    if current < 9:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(import_records)")}
        if "corrections_json" not in cols:
            conn.execute("ALTER TABLE import_records ADD COLUMN corrections_json TEXT NOT NULL DEFAULT '{}'")
        if "corrected_at" not in cols:
            conn.execute("ALTER TABLE import_records ADD COLUMN corrected_at TEXT")
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (9)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (9)")
    if current < 10:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(import_records)")}
        if "promoted_flight_id" not in cols:
            conn.execute("ALTER TABLE import_records ADD COLUMN promoted_flight_id INTEGER")
        if "promoted_at" not in cols:
            conn.execute("ALTER TABLE import_records ADD COLUMN promoted_at TEXT")
        if "promotion_sha256" not in cols:
            conn.execute("ALTER TABLE import_records ADD COLUMN promotion_sha256 TEXT")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_import_records_promoted_flight
                        ON import_records(promoted_flight_id)
                        WHERE promoted_flight_id IS NOT NULL""")
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (10)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (10)")
    if current < 11:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(flight_operations)")}
        if "actual_instrument_minutes" not in cols:
            conn.execute(
                "ALTER TABLE flight_operations ADD COLUMN actual_instrument_minutes REAL NOT NULL DEFAULT 0"
            )
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (11)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (11)")
    if current < 12:
        batch_cols = {r[1] for r in conn.execute("PRAGMA table_info(import_batches)")}
        if "field_mapping_json" not in batch_cols:
            conn.execute("ALTER TABLE import_batches ADD COLUMN field_mapping_json TEXT NOT NULL DEFAULT '{}'")
        record_cols = {r[1] for r in conn.execute("PRAGMA table_info(import_records)")}
        if "unmapped_acknowledged" not in record_cols:
            conn.execute("ALTER TABLE import_records ADD COLUMN unmapped_acknowledged INTEGER NOT NULL DEFAULT 0")
        if "duplicate_acknowledged" not in record_cols:
            conn.execute("ALTER TABLE import_records ADD COLUMN duplicate_acknowledged INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (12)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (12)")
    if current < 13:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(flights)")}
        additions = {
            "logged_minutes": "INTEGER",
            "time_recording_mode": "TEXT NOT NULL DEFAULT 'out_off_on_in'",
            "source_total_time": "TEXT",
        }
        for col, decl in additions.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE flights ADD COLUMN {col} {decl}")
        conn.execute(
            "UPDATE flights SET logged_minutes=air_minutes "
            "WHERE logged_minutes IS NULL AND air_minutes IS NOT NULL"
        )
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (13)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (13)")
    if current < 14:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        has_staged_imports = {"import_records", "import_batches"} <= tables
        legacy_rows = conn.execute("""
            SELECT id, air_minutes FROM flights
            WHERE out_utc IS NULL AND off_utc IS NULL AND on_utc IS NULL AND in_utc IS NULL
              AND block_minutes IS NULL AND air_minutes IS NOT NULL
        """).fetchall()
        for legacy in legacy_rows:
            flight_id, air_minutes = legacy[0], legacy[1]
            source_total_time = None
            if has_staged_imports:
                staged = conn.execute("""
                    SELECT ir.raw_json, ib.field_mapping_json
                    FROM import_records ir
                    JOIN import_batches ib ON ib.id=ir.batch_id
                    WHERE ir.promoted_flight_id=?
                    ORDER BY ir.id DESC LIMIT 1
                """, (flight_id,)).fetchone()
                if staged:
                    try:
                        raw = json.loads(staged[0] or "{}")
                        mapping = json.loads(staged[1] or "{}")
                        source_column = mapping.get("total_time") or "Total Time"
                        original = raw.get(source_column)
                        source_total_time = None if original is None else str(original)
                    except (TypeError, ValueError, AttributeError):
                        source_total_time = None
            conn.execute("""
                UPDATE flights
                SET logged_minutes=COALESCE(logged_minutes, ?),
                    air_minutes=NULL,
                    time_recording_mode='duration_only',
                    source_total_time=COALESCE(source_total_time, ?)
                WHERE id=?
            """, (air_minutes, source_total_time, flight_id))
        conn.execute("INSERT OR IGNORE INTO migration_history(version) VALUES (14)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (14)")
    for row in AIRPORTS:
        conn.execute("INSERT OR IGNORE INTO airports(icao,iata,name,timezone) VALUES(?,?,?,?)", row)
    # Import the user's LogTen Places export once per ICAO, preserving any existing airport record.
    places_path = os.path.join(os.path.dirname(__file__), "logten_airports.json")
    if os.path.exists(places_path):
        with open(places_path, encoding="utf-8") as f: imported = json.load(f)
        for a in imported:
            conn.execute("""INSERT OR IGNORE INTO airports
              (icao,iata,name,timezone,latitude,longitude,city,province,country,notes,elevation_ft,magnetic_variation,beacon,data_source,data_status)
              VALUES(?,?,?,'',?,?,?,?,?,?,?,?,?,'LogTen import',?)""",
              (a['icao'],a['iata'],a['name'],a['latitude'],a['longitude'],a['city'],a['province'],a['country'],a['notes'],a['elevation_ft'],a['magnetic_variation'],a['beacon'],
               'coordinates_present_unverified' if a['latitude'] is not None and a['longitude'] is not None else 'incomplete'))
    # Fill missing time zones for the explicitly supported Alberta/BC operating set.
    # Do not overwrite an existing airport record's verified/manual time zone.
    for code, zone in OPERATING_AIRPORT_TIMEZONES.items():
        conn.execute(
            "UPDATE airports SET timezone=? WHERE icao=? AND (timezone IS NULL OR timezone='')",
            (zone, code),
        )
    for code, (lat, lon) in AIRPORT_COORDS.items():
        conn.execute("UPDATE airports SET latitude=?,longitude=? WHERE icao=? AND (latitude IS NULL OR longitude IS NULL)", (lat, lon, code))
    # Seeded manual airports receive time zones and coordinates in separate passes.
    # Reconcile their directory status after both reference sets have been applied.
    conn.execute("""
        UPDATE airports
        SET data_status='manual_complete'
        WHERE data_source='manual'
          AND timezone IS NOT NULL AND trim(timezone) <> ''
          AND latitude IS NOT NULL AND longitude IS NOT NULL
    """)
    init_fcv_storage(conn)
    init_oauth_state(conn)
    conn.commit(); conn.close()

def airport(code):
    code = (code or "").strip().upper()
    row = db().execute("SELECT * FROM airports WHERE enabled=1 AND (icao=? OR iata=?)", (code,code)).fetchone()
    if not row: raise ValueError(f"Airport '{code}' is not in the airport directory. Add it before using it.")
    try:
        if not row["timezone"]: raise ZoneInfoNotFoundError
        ZoneInfo(row["timezone"])
    except ZoneInfoNotFoundError: raise ValueError(f"{code} is in the airport directory but has no verified IANA time zone yet. Add its time zone before using it for local-time conversion.")
    return row


def route_coordinates(departure, arrival):
    """Return route endpoints only when both airport coordinate records are complete."""
    coordinates = []
    for item in (departure, arrival):
        if (
            item["data_status"] != "manual_complete"
            or item["latitude"] is None
            or item["longitude"] is None
        ):
            raise ValueError(
                f"{item['icao']} needs complete airport coordinates before night-time "
                "calculation. Verify the coordinates and time zone, or enter night time manually."
            )
        coordinates.append((float(item["latitude"]), float(item["longitude"])))
    return tuple(coordinates)


@app.before_request
def protect_application():
    # A dedicated internal relay endpoint accepts only a configured shared secret.
    # All other paths remain strictly behind the Home Assistant Ingress proxy.
    if request.path == "/api/fcv/oauth/callback-relay" and request.method == "POST":
        import hmac
        relay_secret = setting("FCV_CALLBACK_RELAY_SECRET", "fcv_callback_relay_secret")
        supplied = request.headers.get("X-FCV-Relay-Secret", "")
        if (
            len(relay_secret) >= 32
            and supplied
            and hmac.compare_digest(supplied.encode("utf-8"), relay_secret.encode("utf-8"))
        ):
            return None
        response = jsonify(error="callback_relay_forbidden")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response, 403

    # Home Assistant authenticates the user before forwarding an Ingress request.
    # Never expose this app directly to LAN/WAN clients.
    if not is_trusted_ingress_proxy(request.remote_addr):
        return jsonify(error="ingress_proxy_only"), 403

    # Establish a short-lived app session inside the already-authenticated HA
    # Ingress session. This avoids asking the user to sign in twice.
    if not session.get("authenticated"):
        import secrets
        session.clear()
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        import hmac
        if not supplied or not expected or not hmac.compare_digest(supplied, expected):
            return jsonify(error="csrf_validation_failed"), 403
    return None

@app.get("/health")
def health(): return jsonify(ok=True, schema_version=CURRENT_SCHEMA_VERSION)

@app.post("/api/fcv/connect")
def fcv_connect():
    relay_secret = setting("FCV_CALLBACK_RELAY_SECRET", "fcv_callback_relay_secret")
    if len(relay_secret) < 32:
        return jsonify(error="Configure a callback relay secret of at least 32 characters before connecting FC View."), 400
    try:
        state = issue_oauth_state(db())
        url, _ = authorization_url(state)
        return jsonify(ok=True, authorization_url=url)
    except (FCVError, OAuthStateError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/fcv/oauth/callback-relay")
def fcv_oauth_callback_relay():
    # Called only by the user's private Home Assistant webhook automation.
    # This route bypasses Ingress only after the separate shared-secret check
    # in protect_application(); keep its body small and its schema strict.
    max_body_bytes = 4096
    if request.mimetype != "application/json":
        response = jsonify(ok=False, error="Callback relay requires application/json.")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response, 415
    if request.content_length is not None and request.content_length > max_body_bytes:
        response = jsonify(ok=False, error="Callback relay payload is too large.")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response, 413
    try:
        raw_body = request.stream.read(max_body_bytes + 1)
        if len(raw_body) > max_body_bytes:
            response = jsonify(ok=False, error="Callback relay payload is too large.")
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return response, 413
        data = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        response = jsonify(ok=False, error="Callback relay payload must be a JSON object.")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response, 400
    # Bound every provider-controlled field before it reaches SQLite, URL
    # exchange code, or error handling. Reject non-string values explicitly.
    state = data.get("state")
    code = data.get("code")
    provider_error = data.get("error")
    if (not isinstance(state, str) or not state or len(state) > 256
            or (code is not None and (not isinstance(code, str) or len(code) > 512))
            or (provider_error is not None and
                (not isinstance(provider_error, str) or len(provider_error) > 256))):
        response = jsonify(ok=False, error="Callback relay fields are invalid.")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response, 400
    try:
        complete_authorization(
            db(),
            state=state,
            code=code,
            provider_error=provider_error,
        )
        response = jsonify(ok=True, message="FC View authorization completed. Return to the logbook to verify connection status.")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response
    except FCVOAuthFlowError as exc:
        response = jsonify(ok=False, error=str(exc))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response, 400


@app.get("/api/fcv/status")
def fcv_status():
    conn = db()
    row = conn.execute("""SELECT last_attempt_utc,last_success_utc,last_result,last_error
                          FROM fcv_sync_state WHERE id=1""").fetchone()
    connected = conn.execute("SELECT 1 FROM fcv_credentials WHERE id=1").fetchone() is not None
    pending = conn.execute("SELECT COUNT(*) FROM fcv_import_records WHERE review_status='pending'").fetchone()[0]
    configured = all([
        setting("FCV_CLIENT_ID", "fcv_client_id"),
        setting("FCV_CLIENT_SECRET", "fcv_client_secret"),
        setting("FCV_REDIRECT_URI", "fcv_redirect_uri"),
        len(setting("FCV_CALLBACK_RELAY_SECRET", "fcv_callback_relay_secret")) >= 32,
    ])
    return jsonify(connected=connected, configured=configured, pending_review=pending,
                   last_attempt_utc=row["last_attempt_utc"] if row else None,
                   last_success_utc=row["last_success_utc"] if row else None,
                   last_result=row["last_result"] if row else None,
                   last_error=row["last_error"] if row else None)


@app.get("/api/fcv/imports")
def fcv_imports():
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 100))
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        return jsonify(error="Invalid pagination values."), 400
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM fcv_import_records").fetchone()[0]
    rows = conn.execute("""SELECT id,fcv_flight_id,source_json,review_status,
                                  first_seen_utc,updated_at_utc
                           FROM fcv_import_records
                           ORDER BY first_seen_utc DESC,id DESC LIMIT ? OFFSET ?""",
                        (limit, offset)).fetchall()
    return jsonify(total=total,limit=limit,offset=offset,records=[
        {"id":r["id"],"fcv_flight_id":r["fcv_flight_id"],
         "source":json.loads(r["source_json"]),
         "normalized":normalize_fcv_flight(json.loads(r["source_json"])),
         "review_status":r["review_status"],
         "first_seen_utc":r["first_seen_utc"],"updated_at_utc":r["updated_at_utc"]}
        for r in rows])


@app.get("/api/fcv/imports/<int:record_id>/reconciliation")
def fcv_import_reconciliation(record_id):
    row = db().execute(
        "SELECT source_json FROM fcv_import_records WHERE id=?", (record_id,)
    ).fetchone()
    if row is None:
        return jsonify(error="Staged FC View record was not found."), 404
    timezones = {
        r["icao"]: r["timezone"] for r in db().execute(
            "SELECT icao,timezone FROM airports WHERE enabled=1"
        ).fetchall() if r["timezone"]
    }
    preview = build_reconciliation_preview(json.loads(row["source_json"]), timezones)
    return jsonify(preview)


@app.get("/api/fcv/imports/<int:record_id>/duplicate-check")
def fcv_import_duplicate_check(record_id):
    row = db().execute(
        "SELECT source_json FROM fcv_import_records WHERE id=?", (record_id,)
    ).fetchone()
    if row is None:
        return jsonify(error="Staged FC View record was not found."), 404
    source = json.loads(row["source_json"])
    timezones = {
        r["icao"]: r["timezone"] for r in db().execute(
            "SELECT icao,timezone FROM airports WHERE enabled=1"
        ).fetchall() if r["timezone"]
    }
    preview = build_reconciliation_preview(source, timezones)
    candidate = preview["candidate"]
    flight_date = candidate.get("flight_date")
    try:
        from datetime import date, timedelta
        center = date.fromisoformat(flight_date)
    except (TypeError, ValueError):
        return jsonify(matches=[], match_count=0, warning_only=True,
                       canonical_flights_changed=False,
                       message="Duplicate check requires a valid actual departure-local date.")
    rows = db().execute(
        """SELECT id,flight_date,flight_number,aircraft_registration,
                  departure_icao,arrival_icao,out_utc,off_utc,on_utc,in_utc
           FROM flights
           WHERE departure_icao=? AND arrival_icao=?
             AND flight_date BETWEEN ? AND ?
           ORDER BY flight_date DESC,id DESC LIMIT 100""",
        (candidate.get("departure_icao"), candidate.get("arrival_icao"),
         (center - timedelta(days=1)).isoformat(),
         (center + timedelta(days=1)).isoformat()),
    ).fetchall()
    result = assess_duplicate_candidates(candidate, [dict(item) for item in rows])
    return jsonify(result)


@app.patch("/api/fcv/imports/<int:record_id>")
def fcv_review_import(record_id):
    data = request.get_json(silent=True) or {}
    status = data.get("review_status")
    if status not in {"pending", "accepted_for_review", "rejected"}:
        return jsonify(error="Review status must be pending, accepted_for_review, or rejected."), 400
    cur = db().execute(
        "UPDATE fcv_import_records SET review_status=? WHERE id=?",
        (status, record_id),
    )
    if cur.rowcount != 1:
        return jsonify(error="Staged FC View record was not found."), 404
    db().commit()
    return jsonify(ok=True, id=record_id, review_status=status,
                   message="Review annotation saved. Canonical flights and logbook totals are unchanged.")


@app.post("/api/fcv/sync")
def fcv_manual_sync():
    try:
        result = sync_flights(db())
        return jsonify(ok=True,counts=result,message="FC View sync completed. Imported records remain staged for review.")
    except FCVSyncError as exc:
        return jsonify(ok=False,error=str(exc)), 409


@app.post("/api/fcv/disconnect")
def fcv_disconnect():
    try:
        tokens = load_tokens(db())
        if tokens:
            revoke_access(tokens["refresh_token"])
            delete_tokens(db())
        return jsonify(ok=True,message="FC View access was revoked and local tokens were deleted.")
    except (FCVError, FCVSyncError, FCVStorageError) as exc:
        return jsonify(ok=False,error=str(exc)), 502

@app.get("/api/setup-status")
def setup_status():
    exists = db().execute("SELECT 1 FROM users WHERE id=1").fetchone() is not None
    return jsonify(setup_required=not exists)

@app.post("/api/setup")
def setup():
    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    if not isinstance(password, str) or len(password) < 12:
        return jsonify(error="Choose a password at least 12 characters long."), 400
    if db().execute("SELECT 1 FROM users WHERE id=1").fetchone():
        return jsonify(error="Initial setup has already been completed."), 409
    db().execute("INSERT INTO users(id,password_hash) VALUES(1,?)", (generate_password_hash(password),))
    db().commit()
    establish_session()
    return jsonify(ok=True)

def establish_session():
    import secrets
    session.clear()
    session["authenticated"] = True
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    row = db().execute("SELECT password_hash FROM users WHERE id=1").fetchone()
    if not row:
        return jsonify(error="Initial setup is required."), 409
    guard = db().execute("SELECT failures,locked_until FROM login_guard WHERE id=1").fetchone()
    now = datetime.now(timezone.utc)
    if guard["locked_until"] and datetime.fromisoformat(guard["locked_until"]) > now:
        return jsonify(error="Too many failed attempts. Try again in 15 minutes."), 429
    password = data.get("password", "")
    if not isinstance(password, str) or not check_password_hash(row["password_hash"], password):
        failures = guard["failures"] + 1
        locked = None
        if failures >= 5:
            from datetime import timedelta
            locked = (now + timedelta(minutes=15)).replace(microsecond=0).isoformat()
        db().execute("UPDATE login_guard SET failures=?,locked_until=? WHERE id=1", (0 if locked else failures, locked))
        db().commit()
        return jsonify(error="Too many failed attempts. Try again in 15 minutes." if locked else "Incorrect password."), 429 if locked else 401
    db().execute("UPDATE login_guard SET failures=0,locked_until=NULL WHERE id=1")
    db().commit()
    establish_session()
    return jsonify(ok=True)

@app.get("/api/session")
def session_info():
    return jsonify(authenticated=bool(session.get("authenticated")), csrf_token=session.get("csrf_token"))

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/")
def home(): return render_template("index.html")

@app.get("/api/airports")
def airports():
    q = (request.args.get("q") or "").strip().upper()
    rows = db().execute("""SELECT icao,iata,name,timezone,city,province,country,latitude,longitude,
      elevation_ft,magnetic_variation,beacon,data_source,data_status FROM airports
      WHERE enabled=1 AND (icao LIKE ? OR iata LIKE ? OR upper(name) LIKE ? OR upper(coalesce(city,'')) LIKE ?)
      ORDER BY CASE WHEN icao=? OR iata=? THEN 0 ELSE 1 END, name LIMIT 30""",
      (f"%{q}%",f"%{q}%",f"%{q}%",f"%{q}%",q,q)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.get("/api/airports/lookup")
def lookup_airport_online():
    code=(request.args.get("code") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,4}",code): return jsonify(error="Enter a 3- or 4-character airport code."),400
    cache_path=os.path.join(DATA_DIR,"ourairports.csv")
    try:
        # OurAirports publishes a public CSV of airport reference data. Cache it locally for 24 hours.
        if not os.path.exists(cache_path) or time.time()-os.path.getmtime(cache_path)>86400:
            req=urllib.request.Request("https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv",headers={"User-Agent":"PersonalPilotLogbook/0.3.6"})
            with urllib.request.urlopen(req,timeout=12) as response: payload=response.read()
            tmp=cache_path+".tmp"
            with open(tmp,"wb") as f:f.write(payload)
            os.replace(tmp,cache_path)
        with open(cache_path,encoding="utf-8-sig",newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("icao_code") or "").upper()==code or (row.get("iata_code") or "").upper()==code:
                    return jsonify(found=True,source="OurAirports",icao=row.get("icao_code") or "",iata=row.get("iata_code") or "",name=row.get("name") or "",city=row.get("municipality") or "",country=row.get("iso_country") or "",latitude=row.get("latitude_deg") or "",longitude=row.get("longitude_deg") or "",elevation_ft=row.get("elevation_ft") or "",timezone="",notice="OurAirports does not provide an IANA time zone. Verify and enter the time zone before using local-time conversion." )
        return jsonify(found=False,source="OurAirports",message="No matching airport found in the online reference. You can still enter it manually.")
    except Exception as e:
        return jsonify(error="Online airport lookup is unavailable right now. You can still enter the airport manually.",detail=str(e)),503

@app.post("/api/airports")
def add_airport():
    data=request.get_json(force=True)
    try:
        icao=(data.get("icao") or "").strip().upper(); iata=(data.get("iata") or "").strip().upper() or None
        name=(data.get("name") or "").strip(); tz=(data.get("timezone") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]{4}",icao): raise ValueError("Enter a four-character ICAO code.")
        if not name: raise ValueError("Airport name is required.")
        if tz:
            try: ZoneInfo(tz)
            except ZoneInfoNotFoundError: raise ValueError("Enter a valid IANA time zone, such as America/Edmonton.")
        lat=data.get("latitude"); lon=data.get("longitude")
        lat=float(lat) if lat not in (None,"") else None; lon=float(lon) if lon not in (None,"") else None
        if (lat is None) != (lon is None): raise ValueError("Enter both latitude and longitude, or leave both blank.")
        if lat is not None and not (-90<=lat<=90 and -180<=lon<=180): raise ValueError("Coordinates are outside valid ranges.")
        status="manual_complete" if tz and lat is not None else "incomplete"
        db().execute("""INSERT INTO airports(icao,iata,name,timezone,latitude,longitude,city,province,country,notes,elevation_ft,magnetic_variation,beacon,data_source,data_status)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'manual',?)""",(icao,iata,name,tz,lat,lon,(data.get('city') or '').strip() or None,(data.get('province') or '').strip() or None,(data.get('country') or '').strip() or None,(data.get('notes') or '').strip() or None,float(data['elevation_ft']) if data.get('elevation_ft') not in (None,'') else None,(data.get('magnetic_variation') or '').strip() or None,(data.get('beacon') or '').strip() or None,status))
        db().commit()
        return jsonify(ok=True,icao=icao,data_status=status),201
    except (ValueError,sqlite3.IntegrityError) as e: return jsonify(error=str(e)),400

@app.post("/api/time/from-utc")
def time_from_utc():
    data=request.get_json(force=True)
    try:
        return jsonify(utc_to_local(data["utc"], data["timezone"]))
    except (ValueError,KeyError,ZoneInfoNotFoundError) as e:
        return jsonify(error=str(e)),400

@app.post("/api/time/resolve")
def time_resolve():
    data=request.get_json(force=True)
    try:
        result=resolve_local(data["local"],data["timezone"],data.get("fold"))
        return jsonify(result)
    except AmbiguousTime as e: return jsonify(error="ambiguous_local_time",**e.payload),409
    except (ValueError,KeyError,ZoneInfoNotFoundError) as e: return jsonify(error=str(e)),400

@app.post("/api/flights")
def create_flight():
    data=request.get_json(force=True)
    try:
        meta=clean_flight_metadata(data)
        dep=airport(data["departure_icao"]); arr=airport(data["arrival_icao"])
        resolved=resolve_flight_events(data,dep["timezone"],arr["timezone"])
        flight_date=date.fromisoformat(resolved["flight_date"])
        arrival_date=date.fromisoformat(resolved["arrival_date"])
        events=resolved["events"]
        block=resolved["block_minutes"]
        air=resolved["air_minutes"]
        cur=db().execute("""INSERT INTO flights(
          flight_date,arrival_date,flight_number,aircraft_registration,aircraft_type,
          departure_icao,arrival_icao,out_utc,off_utc,on_utc,in_utc,
          out_local,off_local,on_local,in_local,departure_timezone,arrival_timezone,
          block_minutes,air_minutes,logged_minutes,time_recording_mode,source_total_time,
          pic,sic,pilot_flying,remarks)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (flight_date.isoformat(),arrival_date.isoformat(),meta.get("flight_number"),
           meta.get("aircraft_registration"),meta.get("aircraft_type"),dep["icao"],arr["icao"],
           events["OUT"][0],events["OFF"][0],events["ON"][0],events["IN"][0],
           events["OUT"][1],events["OFF"][1],events["ON"][1],events["IN"][1],
           dep["timezone"],arr["timezone"],block,air,air,"out_off_on_in",None,meta["pic"],meta["sic"],
           meta["pilot_flying"],meta.get("remarks")))
        db().commit()
        return jsonify(id=cur.lastrowid,block_minutes=block,air_minutes=air,logged_minutes=air,events=events),201
    except AmbiguousTime as e: return jsonify(error="ambiguous_local_time",**e.payload),409
    except (ValueError,KeyError,ZoneInfoNotFoundError,sqlite3.IntegrityError) as e: return jsonify(error=str(e)),400

@app.put("/api/flights/<int:flight_id>")
def update_flight(flight_id):
    data = request.get_json(silent=True) or {}
    try:
        existing = db().execute("SELECT id,time_recording_mode FROM flights WHERE id=?", (flight_id,)).fetchone()
        if not existing:
            return jsonify(error="Flight not found"), 404
        if existing["time_recording_mode"] != "out_off_on_in":
            return jsonify(error="This imported flight has incomplete source event times and cannot be edited with the full OUT/OFF/ON/IN editor. Correct its staged LogTen source before promotion; the canonical record is preserved unchanged."), 409
        meta = clean_flight_metadata(data)
        dep = airport(data["departure_icao"])
        arr = airport(data["arrival_icao"])
        resolved = resolve_flight_events(data, dep["timezone"], arr["timezone"])
        events = resolved["events"]
        values = (
            resolved["flight_date"], resolved["arrival_date"], meta.get("flight_number"),
            meta.get("aircraft_registration"), meta.get("aircraft_type"), dep["icao"], arr["icao"],
            events["OUT"][0], events["OFF"][0], events["ON"][0], events["IN"][0],
            events["OUT"][1], events["OFF"][1], events["ON"][1], events["IN"][1],
            dep["timezone"], arr["timezone"], resolved["block_minutes"], resolved["air_minutes"],
            resolved["air_minutes"], "out_off_on_in", None,
            meta["pic"], meta["sic"], meta["pilot_flying"], meta.get("remarks"), flight_id)
        db().execute("""UPDATE flights SET flight_date=?,arrival_date=?,flight_number=?,
          aircraft_registration=?,aircraft_type=?,departure_icao=?,arrival_icao=?,
          out_utc=?,off_utc=?,on_utc=?,in_utc=?,out_local=?,off_local=?,on_local=?,in_local=?,
          departure_timezone=?,arrival_timezone=?,block_minutes=?,air_minutes=?,logged_minutes=?,
          time_recording_mode=?,source_total_time=?,pic=?,sic=?,pilot_flying=?,remarks=?,
          updated_at=CURRENT_TIMESTAMP WHERE id=?""", values)
        db().commit()
        return jsonify(ok=True, id=flight_id, block_minutes=resolved["block_minutes"],
                       air_minutes=resolved["air_minutes"], logged_minutes=resolved["air_minutes"], events=events)
    except AmbiguousTime as e:
        return jsonify(error="ambiguous_local_time", **e.payload), 409
    except (ValueError, KeyError, ZoneInfoNotFoundError, sqlite3.IntegrityError) as e:
        return jsonify(error=str(e)), 400

@app.get("/api/flights")
def list_flights():
    rows=db().execute("SELECT * FROM flights ORDER BY flight_date DESC,id DESC LIMIT 200").fetchall()
    return jsonify([dict(r) for r in rows])

@app.get("/api/flights/search")
def search_flights():
    """Search the canonical logbook with directory-aware filters and aggregate totals."""
    args = request.args
    clauses, params = [], []
    start, end = (args.get("start") or "").strip(), (args.get("end") or "").strip()
    for label, value in (("start", start), ("end", end)):
        if value:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return jsonify(error=f"{label} must be an ISO date (YYYY-MM-DD)."), 400
            try:
                date.fromisoformat(value)
            except ValueError:
                return jsonify(error=f"{label} must be a valid ISO date (YYYY-MM-DD)."), 400
    if start and end and start > end:
        return jsonify(error="Start date must be on or before end date."), 400
    if start:
        clauses.append("f.flight_date >= ?"); params.append(start)
    if end:
        clauses.append("f.flight_date <= ?"); params.append(end)
    person_name = (args.get("person_name") or "").strip()
    if person_name:
        if len(person_name) > 120:
            return jsonify(error="Crew member name is too long."), 400
        clauses.append("EXISTS (SELECT 1 FROM flight_crew fc WHERE fc.flight_id=f.id AND lower(fc.person_name)=lower(?))")
        params.append(person_name)
    person_id = (args.get("person_id") or "").strip()
    if person_id:
        try:
            person_id = int(person_id)
            if person_id < 1: raise ValueError
        except (TypeError, ValueError):
            return jsonify(error="person_id must be a positive integer."), 400
        person = db().execute("SELECT full_name FROM people WHERE id=?", (person_id,)).fetchone()
        if not person:
            return jsonify(error="Crew member not found."), 404
        clauses.append("""EXISTS (SELECT 1 FROM flight_crew fc WHERE fc.flight_id=f.id
            AND (fc.person_id=? OR (fc.person_id IS NULL AND lower(fc.person_name)=lower(?))))""")
        params.extend((person_id, person["full_name"]))
    aircraft = (args.get("aircraft") or "").strip()
    if aircraft:
        clauses.append("(upper(coalesce(f.aircraft_registration,''))=upper(?) OR upper(coalesce(f.aircraft_type,''))=upper(?))")
        params.extend((aircraft, aircraft))
    airport_code = (args.get("airport") or "").strip().upper()
    if airport_code:
        airport_row = db().execute("SELECT icao FROM airports WHERE upper(icao)=? OR upper(coalesce(iata,''))=?", (airport_code, airport_code)).fetchone()
        if not airport_row:
            return jsonify(error="Airport not found in the directory."), 404
        clauses.append("(f.departure_icao=? OR f.arrival_icao=?)")
        params.extend((airport_row["icao"], airport_row["icao"]))
    query = (args.get("q") or "").strip()
    if len(query) > 100:
        return jsonify(error="Search text is limited to 100 characters."), 400
    if query:
        like = f"%{query}%"
        clauses.append("""(coalesce(f.flight_number,'') LIKE ? OR coalesce(f.aircraft_registration,'') LIKE ?
            OR coalesce(f.aircraft_type,'') LIKE ? OR f.departure_icao LIKE ? OR f.arrival_icao LIKE ?
            OR coalesce(f.remarks,'') LIKE ? OR EXISTS (
                SELECT 1 FROM flight_crew fc WHERE fc.flight_id=f.id AND fc.person_name LIKE ?))""")
        params.extend([like] * 7)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    try:
        limit = max(1, min(int(args.get("limit", 50)), 100))
        offset = max(0, int(args.get("offset", 0)))
    except ValueError:
        return jsonify(error="limit and offset must be integers."), 400
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM flights f" + where, params).fetchone()[0]
    totals = conn.execute("""SELECT COUNT(*) flights,
        COALESCE(SUM(f.block_minutes),0) block_minutes,
        COALESCE(SUM(f.air_minutes),0) air_minutes,
        COALESCE(SUM(f.logged_minutes),0) logged_minutes
        FROM flights f""" + where, params).fetchone()
    rows = conn.execute("""SELECT f.*,
        (SELECT group_concat(person_name, ', ') FROM (
            SELECT DISTINCT person_name FROM flight_crew WHERE flight_id=f.id ORDER BY person_name
        )) crew_names
        FROM flights f""" + where + " ORDER BY f.flight_date DESC,f.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    return jsonify(flights=[dict(row) for row in rows], total=total, offset=offset,
        limit=limit, has_more=offset + len(rows) < total, totals=dict(totals))


@app.get("/api/flights/filter-options")
def flight_filter_options():
    conn = db()
    people_rows = conn.execute("""SELECT p.id,p.full_name,p.employee_id,COUNT(DISTINCT fc.flight_id) flights
        FROM people p LEFT JOIN flight_crew fc ON fc.person_id=p.id
        GROUP BY p.id,p.full_name,p.employee_id""").fetchall()
    people = [dict(row) for row in people_rows]
    known_names = {str(row["full_name"]).casefold() for row in people}
    legacy_rows = conn.execute("""SELECT person_name full_name,COUNT(DISTINCT flight_id) flights
        FROM flight_crew WHERE person_id IS NULL AND trim(person_name)<>''
        GROUP BY person_name ORDER BY person_name COLLATE NOCASE""").fetchall()
    for row in legacy_rows:
        if row["full_name"].casefold() not in known_names:
            people.append({"id": None, "full_name": row["full_name"], "employee_id": "", "flights": row["flights"]})
    people.sort(key=lambda item: item["full_name"].casefold())
    aircraft = conn.execute("""SELECT value,kind,COUNT(*) flights FROM (
        SELECT trim(aircraft_registration) value,'Registration' kind FROM flights
        WHERE trim(coalesce(aircraft_registration,''))<>''
        UNION ALL
        SELECT trim(aircraft_type) value,'Type' kind FROM flights
        WHERE trim(coalesce(aircraft_type,''))<>''
    ) GROUP BY value,kind ORDER BY kind,value COLLATE NOCASE""").fetchall()
    airports = conn.execute("""SELECT a.icao,a.iata,a.name,a.city,a.country,COUNT(*) flights FROM (
        SELECT departure_icao icao FROM flights UNION ALL SELECT arrival_icao icao FROM flights
    ) used JOIN airports a ON a.icao=used.icao
        GROUP BY a.icao,a.iata,a.name,a.city,a.country ORDER BY a.icao""").fetchall()
    return jsonify(people=[dict(row) for row in people],
        aircraft=[dict(row) for row in aircraft], airports=[dict(row) for row in airports])


@app.get("/api/flights/<int:flight_id>")
def get_flight(flight_id):
    row=db().execute("SELECT * FROM flights WHERE id=?",(flight_id,)).fetchone()
    if not row: return jsonify(error="Flight not found"),404
    return jsonify(dict(row))

@app.get("/api/flights/<int:flight_id>/details")
def flight_details(flight_id):
    exists = db().execute("SELECT id FROM flights WHERE id=?", (flight_id,)).fetchone()
    if not exists: return jsonify(error="Flight not found"), 404
    duty = db().execute("SELECT * FROM flight_duty WHERE flight_id=?", (flight_id,)).fetchone()
    ops = db().execute("SELECT * FROM flight_operations WHERE flight_id=?", (flight_id,)).fetchone()
    crew = db().execute("SELECT * FROM flight_crew WHERE flight_id=? ORDER BY sort_order,id", (flight_id,)).fetchall()
    approaches = db().execute("SELECT * FROM flight_approaches WHERE flight_id=? ORDER BY id", (flight_id,)).fetchall()
    return jsonify(duty=dict(duty) if duty else {}, operations=dict(ops) if ops else {},
                   crew=[dict(x) for x in crew], approaches=[dict(x) for x in approaches])

@app.put("/api/flights/<int:flight_id>/details")
def save_flight_details(flight_id):
    data = request.get_json(silent=True) or {}
    if not db().execute("SELECT id FROM flights WHERE id=?", (flight_id,)).fetchone():
        return jsonify(error="Flight not found"), 404
    try:
        from flight_details import validate_flight_details
        clean = validate_flight_details(data)
        flight = db().execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
        route_calculation = None
        if clean["operations"]["night_method"] == "calculated":
            dep = airport(flight["departure_icao"]); arr = airport(flight["arrival_icao"])
            dep_coordinates, arr_coordinates = route_coordinates(dep, arr)
            if not flight["off_utc"] or not flight["on_utc"]:
                raise ValueError(
                    "Night-time calculation requires both OFF and ON UTC timestamps. "
                    "Enter both airborne events or use manual night time."
                )
            route_calculation = calculate_route(
                flight["off_utc"], flight["on_utc"], dep_coordinates, arr_coordinates
            )
            clean["operations"]["night_minutes"] = route_calculation["night_minutes"]
        conn = db()
        with conn:
            d = clean["duty"]
            conn.execute("""INSERT INTO flight_duty(flight_id,report_utc,release_utc,duty_minutes,notes)
              VALUES(?,?,?,?,?) ON CONFLICT(flight_id) DO UPDATE SET
              report_utc=excluded.report_utc,release_utc=excluded.release_utc,
              duty_minutes=excluded.duty_minutes,notes=excluded.notes""",
              (flight_id,d["report_utc"],d["release_utc"],d["duty_minutes"],d["notes"]))
            o = clean["operations"]
            conn.execute("""INSERT INTO flight_operations(flight_id,day_landings,night_landings,approaches,holds,autoland,night_minutes,actual_instrument_minutes,night_method)
              VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(flight_id) DO UPDATE SET
              day_landings=excluded.day_landings,night_landings=excluded.night_landings,
              approaches=excluded.approaches,holds=excluded.holds,autoland=excluded.autoland,
              night_minutes=excluded.night_minutes,actual_instrument_minutes=excluded.actual_instrument_minutes,
              night_method=excluded.night_method""",
              (flight_id,o["day_landings"],o["night_landings"],o["approaches"],o["holds"],o["autoland"],
               o["night_minutes"],o["actual_instrument_minutes"],o["night_method"]))
            conn.execute("DELETE FROM flight_crew WHERE flight_id=?", (flight_id,))
            for i,c in enumerate(clean["crew"]):
                conn.execute("""INSERT INTO flight_crew(flight_id,person_id,person_name,role,employee_id,pic_credit,sic_credit,pf_credit,pm_credit,sort_order)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""", (flight_id,c["person_id"],c["person_name"],c["role"],c["employee_id"],c["pic_credit"],c["sic_credit"],c["pf_credit"],c["pm_credit"],i))
            conn.execute("DELETE FROM flight_approaches WHERE flight_id=?", (flight_id,))
            for a in clean["approaches"]:
                conn.execute("INSERT INTO flight_approaches(flight_id,approach_type,airport_icao,runway,count) VALUES(?,?,?,?,?)",
                  (flight_id,a["approach_type"],a["airport_icao"],a["runway"],a["count"]))
        return jsonify(ok=True, flight_id=flight_id, route_calculation=route_calculation)
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify(error=str(exc)), 400

@app.get("/api/aircraft")
def aircraft_directory():
    q = (request.args.get("q") or "").strip().upper()
    rows = db().execute("SELECT * FROM aircraft WHERE registration LIKE ? OR aircraft_type LIKE ? OR make_model LIKE ? ORDER BY active DESC, registration LIMIT 100",
                        (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    return jsonify(aircraft=[dict(r) for r in rows])

@app.post("/api/aircraft")
def aircraft_add():
    try:
        item = clean_aircraft(request.get_json(silent=True) or {})
        db().execute("INSERT INTO aircraft(registration,aircraft_type,make_model,notes,active) VALUES(?,?,?,?,?)",
                     (item["registration"],item["aircraft_type"],item["make_model"],item["notes"],item["active"]))
        db().commit()
        return jsonify(ok=True, aircraft=item), 201
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except sqlite3.IntegrityError:
        return jsonify(error="That aircraft registration is already in the directory."), 409

@app.put("/api/aircraft/<path:registration>")
def aircraft_update(registration):
    try:
        item = clean_aircraft(request.get_json(silent=True) or {})
        if item["registration"] != registration.strip().upper():
            return jsonify(error="Registration cannot be changed. Add a new aircraft record instead."), 400
        cur = db().execute("UPDATE aircraft SET aircraft_type=?,make_model=?,notes=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE registration=?",
                           (item["aircraft_type"],item["make_model"],item["notes"],item["active"],registration.upper()))
        if cur.rowcount == 0: return jsonify(error="Aircraft not found."), 404
        db().commit(); return jsonify(ok=True, aircraft=item)
    except ValueError as exc: return jsonify(error=str(exc)), 400

@app.get("/api/people")
def people_directory():
    q = (request.args.get("q") or "").strip()
    rows = db().execute("SELECT * FROM people WHERE full_name LIKE ? OR COALESCE(employee_id,'') LIKE ? ORDER BY active DESC, full_name COLLATE NOCASE LIMIT 100",
                        (f"%{q}%", f"%{q}%")).fetchall()
    return jsonify(people=[dict(r) for r in rows])

@app.post("/api/people")
def people_add():
    try:
        item = clean_person(request.get_json(silent=True) or {})
        cur = db().execute("INSERT INTO people(full_name,employee_id,default_role,notes,active) VALUES(?,?,?,?,?)",
                           (item["full_name"],item["employee_id"],item["default_role"],item["notes"],item["active"]))
        db().commit(); item["id"] = cur.lastrowid
        return jsonify(ok=True, person=item), 201
    except ValueError as exc: return jsonify(error=str(exc)), 400
    except sqlite3.IntegrityError:
        return jsonify(error="That employee ID is already in the directory."), 409

@app.put("/api/people/<int:person_id>")
def people_update(person_id):
    try:
        item = clean_person(request.get_json(silent=True) or {})
        cur = db().execute("UPDATE people SET full_name=?,employee_id=?,default_role=?,notes=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (item["full_name"],item["employee_id"],item["default_role"],item["notes"],item["active"],person_id))
        if cur.rowcount == 0: return jsonify(error="Crew member not found."), 404
        db().commit(); item["id"] = person_id
        return jsonify(ok=True, person=item)
    except ValueError as exc: return jsonify(error=str(exc)), 400
    except sqlite3.IntegrityError:
        return jsonify(error="That employee ID is already assigned to another person."), 409

@app.get("/api/reports/summary")
def report_summary():
    try:
        return jsonify(build_summary(db(), request.args.get("start") or None,
                                     request.args.get("end") or None))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

@app.get("/api/reports/routes")
def report_routes():
    try:
        return jsonify(build_routes(db(), request.args.get("start") or None,
                                    request.args.get("end") or None))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

@app.post("/api/calculations/route")
def calculate_route_api():
    data = request.get_json(silent=True) or {}
    try:
        dep = airport(data["departure_icao"])
        arr = airport(data["arrival_icao"])
        dep_coordinates, arr_coordinates = route_coordinates(dep, arr)
        if not data.get("off_utc") or not data.get("on_utc"):
            raise ValueError("Provide both OFF and ON UTC timestamps for airborne route/night calculations.")
        result = calculate_route(
            data["off_utc"], data["on_utc"], dep_coordinates, arr_coordinates,
            data.get("solar_threshold_degrees", -6.0)
        )
        return jsonify(result)
    except (ValueError, KeyError, TypeError) as exc:
        return jsonify(error=str(exc)), 400

@app.post("/api/imports/logten")
def logten_upload():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify(error="Choose a LogTen CSV or TSV file first."), 400
    content = uploaded.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        return jsonify(error="File exceeds the 10 MB upload limit."), 413
    try:
        parsed = parse_logten(content, uploaded.filename)
    except (ValueError, UnicodeDecodeError, csv.Error) as exc:
        return jsonify(error=f"Could not read this LogTen export: {exc}"), 400
    digest = hashlib.sha256(content).hexdigest()
    existing = db().execute("SELECT id,record_count,created_at FROM import_batches WHERE file_sha256=?", (digest,)).fetchone()
    if existing:
        return jsonify(duplicate=True, batch_id=existing["id"], record_count=existing["record_count"],
                       message="This exact file has already been staged. No duplicate records were added."), 200
    conn = db()
    try:
        initial_mapping = default_field_mapping(parsed["headers"])
        cur = conn.execute("""INSERT INTO import_batches
          (filename,file_sha256,delimiter,record_count,blank_rows_skipped,classification_json,warning_count,field_mapping_json)
          VALUES(?,?,?,?,?,?,?,?)""",
          (uploaded.filename[:255],digest,parsed["delimiter"],parsed["nonempty_records"],
           parsed["blank_rows_skipped"],json.dumps(parsed["classification_counts"]),parsed["warning_count"],
           json.dumps(initial_mapping, ensure_ascii=False)))
        batch_id = cur.lastrowid
        conn.executemany("""INSERT INTO import_records
          (batch_id,source_row,fingerprint,classification,warnings_json,raw_json)
          VALUES(?,?,?,?,?,?)""",
          [(batch_id,r["source_row"],r["fingerprint"],r["classification"],
            json.dumps(r["warnings"],ensure_ascii=False),json.dumps(r["raw"],ensure_ascii=False))
           for r in parsed["rows"]])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify(ok=True,batch_id=batch_id,filename=uploaded.filename,
      record_count=parsed["nonempty_records"],blank_rows_skipped=parsed["blank_rows_skipped"],
      delimiter=parsed["delimiter"],classification_counts=parsed["classification_counts"],
      warning_count=parsed["warning_count"],message="Staged for review only; canonical flight totals were not changed."),201



def _logten_auto_import_batch(conn, batch_id):
    """Save every unambiguous, non-duplicate route flight; leave only exceptions staged."""
    batch = conn.execute("SELECT id,field_mapping_json FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        raise ValueError("Import batch not found.")
    rows = conn.execute(
        """SELECT id,source_row,fingerprint,classification,raw_json,corrections_json,
                  promoted_flight_id,promoted_at
           FROM import_records WHERE batch_id=? ORDER BY source_row,id""",
        (batch_id,),
    ).fetchall()
    airports = {}
    for airport_row in conn.execute("SELECT icao,iata,name,timezone FROM airports WHERE enabled=1"):
        item = dict(airport_row)
        airports[item["icao"].upper()] = item
        if item.get("iata"):
            airports[item["iata"].upper()] = item
    counts = {"saved": 0, "duplicates": 0, "incomplete": 0, "non_flight": 0, "already_saved": 0}
    exceptions = []
    seen_fingerprints = set()
    for row in rows:
        if row["promoted_flight_id"] is not None or row["promoted_at"]:
            counts["already_saved"] += 1
            continue
        raw = json.loads(row["raw_json"])
        fingerprint = row["fingerprint"]
        if fingerprint in seen_fingerprints:
            counts["duplicates"] += 1
            exceptions.append({"source_row": row["source_row"], "reason": "Duplicate source row within this LogTen export."})
            continue
        seen_fingerprints.add(fingerprint)
        mapping = _logten_batch_mapping(conn, batch_id, raw)
        assessment = assess_logten_record(
            raw, json.loads(row["corrections_json"] or "{}"), airports, mapping, True
        )
        classification = _logten_classification(row["classification"], assessment)
        if classification != "flight_candidate":
            counts["non_flight"] += 1
            continue
        assessment["classification"] = classification
        preview = build_promotion_preview(assessment)
        candidate = preview.get("candidate")
        duplicate_review = _logten_duplicate_review(
            conn, batch_id, row["id"], fingerprint, candidate,
            assessment.get("mapped"), assessment.get("resolved_airports")
        )
        # A possible match is deliberately held for a quick exception review;
        # exact matches and repeated source rows are never imported twice.
        if duplicate_review.get("has_exact_duplicate") or duplicate_review.get("has_staged_duplicate") \
                or duplicate_review.get("has_cross_batch_staged_duplicate"):
            counts["duplicates"] += 1
            exceptions.append({"source_row": row["source_row"], "reason": "Duplicate flight/source record; not added."})
            continue
        if duplicate_review.get("has_possible_duplicate"):
            counts["incomplete"] += 1
            exceptions.append({"source_row": row["source_row"], "reason": "Possible existing flight match; verify before adding."})
            continue
        if not preview.get("ready_for_manual_confirmation") or not candidate:
            counts["incomplete"] += 1
            blockers = preview.get("blockers") or assessment.get("errors") or ["Required flight information is incomplete."]
            exceptions.append({"source_row": row["source_row"], "reason": "; ".join(blockers[:3])})
            continue

        cur = conn.execute("""INSERT INTO flights (
          flight_date,arrival_date,flight_number,aircraft_registration,aircraft_type,
          departure_icao,arrival_icao,out_utc,off_utc,on_utc,in_utc,
          out_local,off_local,on_local,in_local,departure_timezone,arrival_timezone,
          block_minutes,air_minutes,logged_minutes,time_recording_mode,source_total_time,
          pic,sic,pilot_flying,remarks
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
          candidate["flight_date"],candidate["arrival_date"],candidate["flight_number"],
          candidate["aircraft_registration"],candidate["aircraft_type"],
          candidate["departure_icao"],candidate["arrival_icao"],candidate["out_utc"],
          candidate["off_utc"],candidate["on_utc"],candidate["in_utc"],
          candidate["out_local"],candidate["off_local"],candidate["on_local"],candidate["in_local"],
          candidate["departure_timezone"],candidate["arrival_timezone"],
          candidate["block_minutes"],candidate["air_minutes"],candidate["logged_minutes"],
          candidate["time_recording_mode"],candidate["source_total_time"],candidate["pic"],candidate["sic"],
          candidate["pilot_flying"],candidate["remarks"]))
        flight_id = cur.lastrowid
        if candidate.get("night_minutes") is not None or candidate.get("actual_instrument_minutes") is not None:
            conn.execute(
                """INSERT INTO flight_operations(flight_id,night_minutes,actual_instrument_minutes,night_method)
                   VALUES(?,?,?,?)""",
                (flight_id, candidate.get("night_minutes") or 0,
                 candidate.get("actual_instrument_minutes") or 0,
                 "imported" if candidate.get("night_minutes") is not None else "manual"),
            )
        for sort_order, (field, role, credit_key) in enumerate((
            ("pic_crew", "PIC", "pic"), ("sic_crew", "SIC", "sic")
        )):
            crew_name = candidate.get(field)
            if crew_name:
                conn.execute(
                    """INSERT INTO flight_crew(
                        flight_id,person_name,role,pic_credit,sic_credit,pf_credit,pm_credit,sort_order
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (flight_id, crew_name, role,
                     candidate.get("pic") if credit_key == "pic" else 0,
                     candidate.get("sic") if credit_key == "sic" else 0, 0, 0, sort_order),
                )
        # Source row remains intact and linked to the saved canonical flight.
        conn.execute(
            """UPDATE import_records SET promoted_flight_id=?,promoted_at=?,
               review_status='accepted_for_review',unmapped_acknowledged=1
               WHERE id=? AND batch_id=? AND promoted_flight_id IS NULL""",
            (flight_id, datetime.now(timezone.utc).isoformat(), row["id"], batch_id),
        )
        counts["saved"] += 1
    return {"batch_id": batch_id, "counts": counts, "exceptions": exceptions,
            "exception_count": len(exceptions)}

@app.post("/api/imports/<int:batch_id>/auto-import")
def logten_auto_import(batch_id):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _logten_auto_import_batch(conn, batch_id)
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        return jsonify(error=str(exc)), 404
    except Exception:
        conn.rollback()
        raise
    return jsonify(ok=True, **result,
        message=f"Saved {result['counts']['saved']} flights. "
                f"{result['exception_count']} records were retained for exception review; "
                "duplicate and non-flight records were not added."), 200


@app.delete("/api/imports/<int:batch_id>")
def logten_delete_batch(batch_id):
    conn = db()
    batch = conn.execute("SELECT id,filename FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        return jsonify(error="Import batch not found."), 404
    promoted = conn.execute(
        "SELECT COUNT(*) FROM import_records WHERE batch_id=? AND promoted_flight_id IS NOT NULL",
        (batch_id,),
    ).fetchone()[0]
    if promoted:
        return jsonify(error="This batch contains promoted flights and cannot be deleted. Clear only its unpromoted staged records instead.",
                       promoted_records=promoted), 409
    records = conn.execute("SELECT COUNT(*) FROM import_records WHERE batch_id=?", (batch_id,)).fetchone()[0]
    conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    conn.commit()
    return jsonify(ok=True,batch_id=batch_id,deleted_records=records,
                   message="Import batch and its staged records deleted. Confirmed flights were not changed.")


@app.post("/api/imports/clear-unpromoted")
def logten_clear_unpromoted():
    conn = db()
    promoted = conn.execute(
        "SELECT COUNT(*) FROM import_records WHERE promoted_flight_id IS NOT NULL"
    ).fetchone()[0]
    staged = conn.execute(
        "SELECT COUNT(*) FROM import_records WHERE promoted_flight_id IS NULL"
    ).fetchone()[0]
    batches = conn.execute(
        """SELECT COUNT(*) FROM import_batches b
           WHERE EXISTS (SELECT 1 FROM import_records r WHERE r.batch_id=b.id
                         AND r.promoted_flight_id IS NULL)"""
    ).fetchone()[0]
    conn.execute("DELETE FROM import_records WHERE promoted_flight_id IS NULL")
    conn.execute("DELETE FROM import_batches WHERE NOT EXISTS "
                 "(SELECT 1 FROM import_records r WHERE r.batch_id=import_batches.id)")
    conn.commit()
    return jsonify(ok=True,deleted_batches=batches,deleted_records=staged,
                   preserved_promoted_records=promoted,
                   message="All unpromoted staged import records were cleared. Promoted flights and their source records were preserved.")


@app.get("/api/imports")
def logten_imports_list():
    rows=db().execute("SELECT * FROM import_batches ORDER BY id DESC").fetchall()
    return jsonify([dict(r,classification_counts=json.loads(r["classification_json"])) for r in rows])

@app.get("/api/imports/<int:batch_id>")
def logten_import_detail(batch_id):
    batch=db().execute("SELECT * FROM import_batches WHERE id=?",(batch_id,)).fetchone()
    if not batch: return jsonify(error="Import batch not found."),404
    try:
        limit=max(1,min(int(request.args.get("limit",100)),200))
        offset=max(0,int(request.args.get("offset",0)))
    except ValueError:
        return jsonify(error="limit and offset must be integers."),400
    rows=db().execute("""SELECT id,source_row,classification,warnings_json,raw_json,review_status,
      corrections_json,corrected_at,promoted_flight_id,promoted_at,
      unmapped_acknowledged,duplicate_acknowledged FROM import_records
      WHERE batch_id=? ORDER BY source_row LIMIT ? OFFSET ?""",
      (batch_id,limit,offset)).fetchall()
    total=db().execute("SELECT COUNT(*) FROM import_records WHERE batch_id=?",(batch_id,)).fetchone()[0]
    first=db().execute("SELECT raw_json FROM import_records WHERE batch_id=? ORDER BY source_row LIMIT 1",(batch_id,)).fetchone()
    first_raw=json.loads(first["raw_json"]) if first else {}
    source_headers=list(first_raw.keys())
    field_mapping=_logten_batch_mapping(db(),batch_id,first_raw)
    return jsonify(batch=dict(batch,classification_counts=json.loads(batch["classification_json"])),
      field_mapping=field_mapping,field_targets=FIELD_MAP,source_headers=source_headers,
      preview=[dict(r,warnings=json.loads(r["warnings_json"]),raw=json.loads(r["raw_json"]),
                    corrections=json.loads(r["corrections_json"]),
                    unmapped_acknowledged=bool(r["unmapped_acknowledged"]),
                    duplicate_acknowledged=bool(r["duplicate_acknowledged"])) for r in rows],
      total_records=total,limit=limit,offset=offset,has_more=offset+len(rows)<total)


@app.patch("/api/imports/<int:batch_id>/mapping")
def logten_update_mapping(batch_id):
    data=request.get_json(silent=True)
    if not isinstance(data,dict) or not isinstance(data.get("field_mapping"),dict):
        return jsonify(error="Send field_mapping as an object of canonical fields to source columns."),400
    conn=db()
    batch=conn.execute("SELECT id FROM import_batches WHERE id=?",(batch_id,)).fetchone()
    if batch is None:
        return jsonify(error="Import batch not found."),404
    promoted=conn.execute(
        "SELECT COUNT(*) FROM import_records WHERE batch_id=? AND promoted_flight_id IS NOT NULL",
        (batch_id,),
    ).fetchone()[0]
    if promoted:
        return jsonify(error="Field mapping is locked after any record in the batch is promoted."),409
    first=conn.execute("SELECT raw_json FROM import_records WHERE batch_id=? ORDER BY source_row LIMIT 1",(batch_id,)).fetchone()
    headers=list(json.loads(first["raw_json"]).keys()) if first else []
    try:
        mapping=validate_field_mapping(data["field_mapping"],headers)
    except ValueError as exc:
        return jsonify(error=str(exc)),400
    conn.execute("UPDATE import_batches SET field_mapping_json=? WHERE id=?",(json.dumps(mapping,ensure_ascii=False),batch_id))
    conn.execute("UPDATE import_records SET unmapped_acknowledged=0,duplicate_acknowledged=0 WHERE batch_id=?",(batch_id,))
    conn.commit()
    return jsonify(ok=True,field_mapping=mapping,field_targets=FIELD_MAP,
      message="Field mapping saved. Review acknowledgements were reset because the mapping changed; original source values remain unchanged.")

@app.get("/api/imports/<int:batch_id>/records/<int:record_id>/assessment")
def logten_record_assessment(batch_id, record_id):
    row=db().execute("""SELECT classification,raw_json,corrections_json,
      unmapped_acknowledged,fingerprint FROM import_records WHERE id=? AND batch_id=?""",
      (record_id,batch_id)).fetchone()
    if row is None:
        return jsonify(error="Import record not found."),404
    raw=json.loads(row["raw_json"])
    mapping=_logten_batch_mapping(db(),batch_id,raw)
    airports={}
    for airport in db().execute("SELECT icao,iata,name,timezone FROM airports WHERE enabled=1"):
        item=dict(airport); airports[item["icao"].upper()]=item
        if item.get("iata"): airports[item["iata"].upper()]=item
    result=assess_logten_record(raw,json.loads(row["corrections_json"]),airports,mapping,bool(row["unmapped_acknowledged"]))
    classification=_logten_classification(row["classification"],result)
    if classification != "flight_candidate":
        result["errors"].insert(0,f"This record is classified as {classification} and is not a route-flight candidate.")
        result["eligible_for_manual_promotion_review"]=False
    result["classification"]=classification
    candidate_preview=build_promotion_preview(result)
    result["duplicate_review"]=_logten_duplicate_review(
        db(),batch_id,record_id,row["fingerprint"],candidate_preview.get("candidate"),
        result.get("mapped"),result.get("resolved_airports"))
    return jsonify(result)


@app.get("/api/imports/<int:batch_id>/records/<int:record_id>/duplicates")
def logten_record_duplicates(batch_id, record_id):
    row=db().execute("""SELECT fingerprint,raw_json,corrections_json,classification,unmapped_acknowledged
      FROM import_records WHERE id=? AND batch_id=?""",(record_id,batch_id)).fetchone()
    if row is None:
        return jsonify(error="Import record not found."),404
    raw=json.loads(row["raw_json"])
    mapping=_logten_batch_mapping(db(),batch_id,raw)
    airports={}
    for airport in db().execute("SELECT icao,iata,name,timezone FROM airports WHERE enabled=1"):
        item=dict(airport); airports[item["icao"].upper()]=item
        if item.get("iata"): airports[item["iata"].upper()]=item
    assessment=assess_logten_record(raw,json.loads(row["corrections_json"]),airports,mapping,bool(row["unmapped_acknowledged"]))
    assessment["classification"]=_logten_classification(row["classification"],assessment)
    candidate_preview=build_promotion_preview(assessment)
    duplicates=_logten_duplicate_review(
        db(),batch_id,record_id,row["fingerprint"],candidate_preview.get("candidate"),
        assessment.get("mapped"),assessment.get("resolved_airports"))
    return jsonify(duplicates)


@app.get("/api/imports/<int:batch_id>/records/<int:record_id>/promotion-preview")
def logten_promotion_preview(batch_id, record_id):
    row=db().execute("""SELECT classification,review_status,raw_json,corrections_json,
      fingerprint,promoted_flight_id,promoted_at,unmapped_acknowledged,duplicate_acknowledged
      FROM import_records WHERE id=? AND batch_id=?""",(record_id,batch_id)).fetchone()
    if row is None:
        return jsonify(error="Import record not found."),404
    if row["promoted_flight_id"] is not None or row["promoted_at"]:
        return jsonify(error="This staged record has already been promoted.",
          already_promoted=True,promoted_flight_id=row["promoted_flight_id"],
          promoted_at=row["promoted_at"],promotion_enabled=False),409
    if row["review_status"] != "accepted_for_review":
        return jsonify(ready_for_manual_confirmation=False,promotion_enabled=False,
          blockers=["Set review status to Accepted for review before requesting a promotion preview."],
          candidate=None),409
    raw=json.loads(row["raw_json"])
    corrections=json.loads(row["corrections_json"])
    mapping=_logten_batch_mapping(db(),batch_id,raw)
    airports={}
    for airport in db().execute("SELECT icao,iata,name,timezone FROM airports WHERE enabled=1"):
        item=dict(airport); airports[item["icao"].upper()]=item
        if item.get("iata"): airports[item["iata"].upper()]=item
    assessment=assess_logten_record(raw,corrections,airports,mapping,bool(row["unmapped_acknowledged"]))
    assessment["classification"]=_logten_classification(row["classification"],assessment)
    result=build_promotion_preview(assessment)
    duplicate_review=_logten_duplicate_review(db(),batch_id,record_id,row["fingerprint"],result.get("candidate"),
        assessment.get("mapped"),assessment.get("resolved_airports"))
    _apply_logten_duplicate_guard(result,duplicate_review,bool(row["duplicate_acknowledged"]))
    result["duplicate_review"]=duplicate_review
    result["unmapped_acknowledged"]=bool(row["unmapped_acknowledged"])
    result["duplicate_acknowledged"]=bool(row["duplicate_acknowledged"])
    result["batch_id"]=batch_id
    result["record_id"]=record_id
    result["promotion_enabled"]=bool(result["ready_for_manual_confirmation"] and logten_promotion_enabled())
    result["manual_confirmation_required"]=True
    if result["ready_for_manual_confirmation"]:
        result["preview_sha256"]=preview_fingerprint(result["candidate"],row["fingerprint"],corrections,mapping,
            {"unmapped_acknowledged":bool(row["unmapped_acknowledged"]),
             "duplicate_acknowledged":bool(row["duplicate_acknowledged"])},duplicate_review)
        result["confirmation_phrase"]=f"PROMOTE {batch_id}/{record_id}"
    if not result["promotion_enabled"]:
        result["note"]="Promotion is disabled by default. Enable the LogTen promotion option only on a separate test installation after review."
    return jsonify(result)


@app.post("/api/imports/<int:batch_id>/records/<int:record_id>/promote")
def logten_promote_record(batch_id, record_id):
    if not logten_promotion_enabled():
        return jsonify(ok=False,error="LogTen promotion is disabled. Enable it only on a separate test installation after review."),409
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):
        return jsonify(error="Promotion confirmation must be a JSON object."),400
    conn=db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("""SELECT classification,review_status,raw_json,corrections_json,
          fingerprint,promoted_flight_id,promoted_at,unmapped_acknowledged,duplicate_acknowledged
          FROM import_records WHERE id=? AND batch_id=?""",(record_id,batch_id)).fetchone()
        if row is None:
            conn.rollback()
            return jsonify(error="Import record not found."),404
        if row["promoted_flight_id"] is not None or row["promoted_at"]:
            conn.rollback()
            return jsonify(error="This staged record has already been promoted.",
              already_promoted=True,promoted_flight_id=row["promoted_flight_id"],
              promoted_at=row["promoted_at"]),409
        if row["review_status"] != "accepted_for_review":
            conn.rollback()
            return jsonify(error="Set review status to Accepted for review before promotion."),409

        raw=json.loads(row["raw_json"])
        corrections=json.loads(row["corrections_json"])
        mapping=_logten_batch_mapping(conn,batch_id,raw)
        airports={}
        for airport in conn.execute("SELECT icao,iata,name,timezone FROM airports WHERE enabled=1"):
            item=dict(airport); airports[item["icao"].upper()]=item
            if item.get("iata"): airports[item["iata"].upper()]=item
        assessment=assess_logten_record(raw,corrections,airports,mapping,bool(row["unmapped_acknowledged"]))
        assessment["classification"]=_logten_classification(row["classification"],assessment)
        preview=build_promotion_preview(assessment)
        duplicate_review=_logten_duplicate_review(conn,batch_id,record_id,row["fingerprint"],preview.get("candidate"),
            assessment.get("mapped"),assessment.get("resolved_airports"))
        _apply_logten_duplicate_guard(preview,duplicate_review,bool(row["duplicate_acknowledged"]))
        if not preview["ready_for_manual_confirmation"]:
            conn.rollback()
            return jsonify(ok=False,error="The staged record no longer passes promotion validation.",
              blockers=preview["blockers"],warnings=preview["warnings"],duplicate_review=duplicate_review,
              duplicate_flight_id=(duplicate_review["canonical_exact_matches"][0]["id"]
                                   if duplicate_review["canonical_exact_matches"] else None)),409

        expected_phrase=f"PROMOTE {batch_id}/{record_id}"
        if data.get("confirmation_phrase") != expected_phrase:
            conn.rollback()
            return jsonify(error=f"Explicit confirmation is required. Send confirmation_phrase='{expected_phrase}'."),400
        expected_hash=preview_fingerprint(preview["candidate"],row["fingerprint"],corrections,mapping,
            {"unmapped_acknowledged":bool(row["unmapped_acknowledged"]),
             "duplicate_acknowledged":bool(row["duplicate_acknowledged"])},duplicate_review)
        supplied_hash=data.get("preview_sha256")
        if not isinstance(supplied_hash,str) or not hmac.compare_digest(supplied_hash,expected_hash):
            conn.rollback()
            return jsonify(error="The preview is stale or does not match this staged record. Request a new preview."),409

        candidate=preview["candidate"]
        cur=conn.execute("""INSERT INTO flights (
          flight_date,arrival_date,flight_number,aircraft_registration,aircraft_type,
          departure_icao,arrival_icao,out_utc,off_utc,on_utc,in_utc,
          out_local,off_local,on_local,in_local,departure_timezone,arrival_timezone,
          block_minutes,air_minutes,logged_minutes,time_recording_mode,source_total_time,
          pic,sic,pilot_flying,remarks
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
          candidate["flight_date"],candidate["arrival_date"],candidate["flight_number"],
          candidate["aircraft_registration"],candidate["aircraft_type"],
          candidate["departure_icao"],candidate["arrival_icao"],candidate["out_utc"],
          candidate["off_utc"],candidate["on_utc"],candidate["in_utc"],
          candidate["out_local"],candidate["off_local"],candidate["on_local"],candidate["in_local"],
          candidate["departure_timezone"],candidate["arrival_timezone"],
          candidate["block_minutes"],candidate["air_minutes"],candidate["logged_minutes"],
          candidate["time_recording_mode"],candidate["source_total_time"],candidate["pic"],candidate["sic"],
          candidate["pilot_flying"],candidate["remarks"]))
        flight_id=cur.lastrowid
        if candidate.get("night_minutes") is not None or candidate.get("actual_instrument_minutes") is not None:
            conn.execute(
                """INSERT INTO flight_operations(
                    flight_id,night_minutes,actual_instrument_minutes,night_method
                ) VALUES(?,?,?,?)""",
                (
                    flight_id,
                    candidate.get("night_minutes") or 0,
                    candidate.get("actual_instrument_minutes") or 0,
                    "imported" if candidate.get("night_minutes") is not None else "manual",
                ),
            )
        for sort_order, (field, role, credit_key) in enumerate((
            ("pic_crew", "PIC", "pic"),
            ("sic_crew", "SIC", "sic"),
        )):
            crew_name = candidate.get(field)
            if crew_name:
                conn.execute(
                    """INSERT INTO flight_crew(
                        flight_id,person_name,role,pic_credit,sic_credit,pf_credit,pm_credit,sort_order
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        flight_id, crew_name, role,
                        candidate.get("pic") if credit_key == "pic" else 0,
                        candidate.get("sic") if credit_key == "sic" else 0,
                        0, 0, sort_order,
                    ),
                )
        promoted_at=datetime.now(timezone.utc).isoformat()
        updated=conn.execute("""UPDATE import_records
          SET promoted_flight_id=?,promoted_at=?,promotion_sha256=?
          WHERE id=? AND batch_id=? AND promoted_flight_id IS NULL AND promoted_at IS NULL""",
          (flight_id,promoted_at,expected_hash,record_id,batch_id))
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError("staged record promotion state changed")
        conn.commit()
        return jsonify(ok=True,batch_id=batch_id,record_id=record_id,
          flight_id=flight_id,promoted_at=promoted_at,
          message="One canonical flight was created from the reviewed LogTen row. The staged source is locked; no FC View data was used."),201
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify(error="Promotion was not saved because a database integrity constraint rejected the candidate."),409
    except Exception:
        conn.rollback()
        raise


@app.patch("/api/imports/<int:batch_id>/records/<int:record_id>")
def logten_review_record(batch_id, record_id):
    data=request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify(error="Review update must be a JSON object."),400
    status=data.get("review_status")
    corrections=data.get("corrections", None)
    acknowledge_unmapped=data.get("acknowledge_unmapped", None)
    acknowledge_duplicates=data.get("acknowledge_duplicates", None)
    allowed={"pending","accepted_for_review","rejected","needs_correction"}
    if status is not None and status not in allowed:
        return jsonify(error="Invalid review status."),400
    if acknowledge_unmapped is not None and type(acknowledge_unmapped) is not bool:
        return jsonify(error="acknowledge_unmapped must be a boolean."),400
    if acknowledge_duplicates is not None and type(acknowledge_duplicates) is not bool:
        return jsonify(error="acknowledge_duplicates must be a boolean."),400
    row=db().execute("""SELECT raw_json,corrections_json,promoted_flight_id,promoted_at
      FROM import_records WHERE id=? AND batch_id=?""",(record_id,batch_id)).fetchone()
    if row is None:
        return jsonify(error="Import record not found."),404
    if row["promoted_flight_id"] is not None or row["promoted_at"]:
        return jsonify(error="Promoted source records are immutable.",already_promoted=True,
          promoted_flight_id=row["promoted_flight_id"],promoted_at=row["promoted_at"]),409
    updates=[]; params=[]
    if status is not None:
        updates.append("review_status=?"); params.append(status)
    if "corrections" in data:
        if not isinstance(corrections,dict):
            return jsonify(error="corrections must be an object mapping original column names to corrected text values."),400
        original=json.loads(row["raw_json"])
        clean={}
        for key,value in corrections.items():
            if key not in original:
                return jsonify(error=f"Unknown source column: {key}"),400
            if not isinstance(value,str) or len(value)>5000:
                return jsonify(error=f"Correction for {key} must be text no longer than 5,000 characters."),400
            if value != original[key]: clean[key]=value
        updates.extend(["corrections_json=?","corrected_at=CURRENT_TIMESTAMP","unmapped_acknowledged=0","duplicate_acknowledged=0"])
        params.append(json.dumps(clean,ensure_ascii=False))
    if acknowledge_unmapped is not None:
        updates.append("unmapped_acknowledged=?"); params.append(int(acknowledge_unmapped))
    if acknowledge_duplicates is not None:
        updates.append("duplicate_acknowledged=?"); params.append(int(acknowledge_duplicates))
    if not updates:
        return jsonify(error="Provide a review_status or corrections value."),400
    params.extend([record_id,batch_id])
    db().execute(f"UPDATE import_records SET {', '.join(updates)} WHERE id=? AND batch_id=?",params)
    db().commit()
    saved=db().execute("""SELECT review_status,corrections_json,corrected_at,unmapped_acknowledged,duplicate_acknowledged
      FROM import_records WHERE id=?""",(record_id,)).fetchone()
    return jsonify(ok=True,batch_id=batch_id,record_id=record_id,review_status=saved["review_status"],
      corrections=json.loads(saved["corrections_json"]),corrected_at=saved["corrected_at"],
      unmapped_acknowledged=bool(saved["unmapped_acknowledged"]),
      duplicate_acknowledged=bool(saved["duplicate_acknowledged"]),
      message="Review annotations saved separately. Original LogTen data and live flights are unchanged.")
@app.get("/api/backups")
def backups_list():
    return jsonify(list_backups(BACKUP_DIR))

@app.post("/api/backups")
def backups_create():
    try:
        result = create_backup(DB_PATH, BACKUP_DIR)
        return jsonify(result), 201
    except (BackupError, OSError, sqlite3.DatabaseError) as exc:
        return jsonify(error=str(exc)), 400

@app.get("/api/backups/<path:filename>")
def backups_download(filename):
    try:
        # Validate both the filename and the database before serving it.
        import re
        if not re.fullmatch(r"logbook-\d{8}T\d{6}Z-[a-f0-9]{8}(-v2)?\.sqlite3", filename):
            return jsonify(error="Invalid backup filename."), 400
        path = os.path.join(BACKUP_DIR, filename)
        validate_database(path)
        return send_file(path, as_attachment=True, download_name=filename,
                         mimetype="application/vnd.sqlite3")
    except (BackupError, OSError) as exc:
        return jsonify(error=str(exc)), 404

@app.post("/api/backups/restore")
def backups_restore():
    data = request.get_json(silent=True) or {}
    try:
        # Close the request's live connection before atomic replacement.
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()
        details = restore_backup(BACKUP_DIR, data.get("filename"), DB_PATH)
        return jsonify(ok=True, restored=details,
                       message="Backup restored. A pre-restore backup was created.")
    except (BackupError, OSError, sqlite3.DatabaseError) as exc:
        return jsonify(error=str(exc)), 400

if __name__ == "__main__":
    init_db()
    start_scheduler(DB_PATH)
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8100")))
