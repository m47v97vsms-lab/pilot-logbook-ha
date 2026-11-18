# LogTen reference dataset — migration requirements

This document records the structure of the user's six companion LogTen exports supplied on 2026-11-18. The source files remain conversation attachments and are deliberately **not copied into this repository** because they contain personal flight, crew, aircraft, and contact information.

## Export inventory observed

| Export | Observed records / size | Intended use |
|---|---:|---|
| Export Flights (Tab) | 3,760 data rows; 281 columns; 2,272,495 bytes | Authoritative source for individual flight records and flight-time credits |
| Address Book Export | 555 records; 12 columns; 24,069 bytes | Crew/person identity and contact metadata |
| Export Aircraft | 237 records; 45 columns; 50,571 bytes | Aircraft registration, identifiers, attributes, and aircraft-type relationship |
| Export Aircraft Types | 15 records; 7 columns; 2,700 bytes | Type, make, model, engine, category, and class reference data |
| Export Places (Tab) | 175 rows (170 non-empty); 12 columns; 14,634 bytes | Airport/place identifiers, names, coordinates, elevation, province/country, magnetic variation, beacon |
| Export Places (KML) | 239,842 bytes | Geographic place features for map enrichment / cross-checking |

These are the counts in the newly supplied 2026-11-18 files. They differ from the earlier batch-9 upload described in historical logs (3,779 rows); do not assume those are the same export snapshot.

## Flight-export observations

- Date range: 2007-06-16 through 2026-11-18.
- The flight file is tab-delimited and has 281 columns.
- Many headers have leading spaces. Examples include ` flight_from`, ` flight_to`, ` flight_selectedCrewPIC`, and ` flight_actualDepartureTime`. Header normalization must trim/normalize whitespace for matching while preserving original header names and raw values in the staged source record.
- Every row has a unique non-empty `flight_key` in this export. Preserve it as a source identifier/fingerprint component where appropriate; do not treat it as a canonical flight ID without validating stability across exports.
- Core native fields include:
  - `flight_flightDate`, `flight_flightNumber`, `flight_from`, `flight_to`
  - `flight_actualDepartureTime` (OUT), `flight_takeoffTime` (OFF), `flight_landingTime` (ON), `flight_actualArrivalTime` (IN)
  - `flight_totalTime`, `flight_pic`, `flight_sic`, `flight_night`, `flight_actualInstrument`
  - `flight_selectedCrewPIC`, `flight_selectedCrewSIC`
  - `aircraft_aircraftID`, `aircraftType_type`
- The four event-time fields are populated in 1,713 rows; all four are blank in 1,204 rows; OUT and IN only are populated in 841 rows; two rows have an unusual partial pattern. Do not synthesize missing event timestamps. The importer should accept full event timing, OUT/IN-only timing, and duration-only history; unusual partial combinations should be retained for review.
- `flight_totalTime` is populated in 3,759 of 3,760 rows. It is a source logged-duration value and must remain distinct from calculated block and air time.
- There are 3 rows with a missing departure or arrival location. There are 695 rows whose departure and arrival identifiers are the same; these may be legitimate local circuits/training flights and must not be rejected solely because the route endpoints match.
- The native `flight_type` column is blank in this export. Simulator classification must use reliable simulator/type/category evidence rather than assuming this field is populated.
- The flight export contains many additional operational, landing, approach, duty, weather, fuel, passenger, and custom fields. Unmapped non-empty fields must remain visible in immutable staged source data and be reported in review; never silently discard them.

## Companion-file relationships

The importer should support a migration package containing all six files, not just a flight TSV:

1. Parse the flight file first and preserve its raw values.
2. Parse Address Book as a people/crew reference. Match names conservatively; names are not guaranteed unique identifiers. Keep unresolved or ambiguous references visible for review.
3. Parse Aircraft by registration/secondary ID and link aircraft types using the exported type fields. Do not silently overwrite flight-level aircraft values.
4. Parse Aircraft Types as a separate reference directory.
5. Parse Places (Tab) as the structured airport/place source. Normalize ICAO/IATA identifiers and coordinates, while retaining source values and handling duplicate/blank codes explicitly.
6. Parse Places (KML) as optional geospatial enrichment. Reconcile KML placemarks against the tabular places file by stable identifiers/name plus coordinates; do not let KML silently overwrite the structured source.

The six-file migration should be repeatable and idempotent. It should produce a dry-run reconciliation before any canonical records are written, including counts for flights, people, aircraft, aircraft types, and places, plus unmatched/ambiguous relationships and source-only fields.

## Required regression cases

- Native LogTen tab headers with leading whitespace map correctly after normalization.
- Original header spelling and raw cell values remain recoverable.
- The native OUT/OFF/ON/IN field names map to the correct event semantics.
- Duration-only and OUT/IN-only historical rows remain valid without fabricated events.
- Same-airport route rows remain eligible for review/import.
- PIC-only rows do not create synthetic SIC crew.
- Missing optional credits become finite zero values only for an actual named crew row; malformed/non-finite credits are row-level exceptions.
- A new export can be re-imported without duplicating existing canonical records; source row identity and canonical duplicate checks are distinct safeguards.
- All companion exports are parsed independently and linked with explicit ambiguity reporting.

## Privacy and authority

LogTen remains the authoritative record until the replacement application has passed full reconciliation and backup/restore validation. These exports are development reference material only. They must not be committed to Git, bundled into CI, or used as live production data. A fresh LogTen export can be supplied for a clean migration at any time.
