# Pilot Logbook documentation

## Status and safeguards

This is an experimental development app. It is not approved for operational
flight records. Test it on a separate Home Assistant installation or with
synthetic data first.

The browser UI includes a keyboard skip link, a main landmark, visible focus
styles, and associated form labels; browser smoke tests verify these controls.
Route reporting retains airport pairs even when an airport-directory record is
missing or coordinates are incomplete; such routes remain explicitly unplotted.
Date filters accept only canonical YYYY-MM-DD values to keep lexical SQLite
date comparisons correct.
The app stores its SQLite database and generated backups under `/data`, the
persistent app data directory. Keep regular Home Assistant backups and verify
the app's own backup/restore process before relying on it. Backup validation
checks SQLite and foreign-key integrity. Restore validates the source before
copying, verifies the temporary database metadata, rechecks the source before
atomic replacement, and creates a pre-restore backup. Restore refuses a
symlinked live database path, including a broken symbolic link.

## FC View credentials

Access and refresh tokens are encrypted before they are stored in SQLite. The
fallback Fernet key is created atomically under `/data` with owner-only
permissions. Symlinked, non-regular, or group/other-readable key files are
rejected. The SQLite backup does not contain this separate key file: retain a
private Home Assistant app-data backup for migration, or reconnect FC View if
only the database is restored without its encryption key. Never commit the key
or provider credentials to the repository. FC View token exchange, refresh,
flight-history, and revocation requests refuse redirects so client secrets,
authorization codes, and bearer tokens are never forwarded to another host.

## LogTen import

CSV/TSV files are staged with their original headers, cells and source rows
retained. A batch-level target-to-source field mapping supports alternate
LogTen headers. The review UI uses dropdowns to map each canonical target
to one exact source column. The reconciliation view shows every mapped field and every
non-empty unmapped source column. Staging does not change canonical flight
totals.

Unmapped values block promotion until the reviewer explicitly acknowledges
that they will remain source-only in the immutable staged record. A correction
that blanks a review value does not erase the original non-empty source value
or bypass this acknowledgement. Duplicate source headers and generated suffixes
are collision-safe; literal suffix-looking headers are not auto-mapped unless
their unsuffixed base header is present. Extra cells first encountered on later
rows remain addressable for batch mapping. Dates must be canonical YYYY-MM-DD values;
alternate ISO forms remain visible in the source but are not eligible for
promotion. Exact staged duplicates, exact canonical duplicates, and possible
date/route/flight-number matches are identified; possible matches require
explicit review. Case- or punctuation-equivalent source headers are not
auto-mapped when ambiguous; choose the intended source column explicitly. PIC,
SIC, Pilot Flying, Night, and actual-instrument credits more than one minute
above calculated OFF–ON air time are blocked as inconsistent; original source
values remain available in the staged record. The reconciliation panel displays
original source, corrected review, and mapped values separately; clearing a
review value never conceals the original source. Mapping and correction changes
reset review acknowledgements and invalidate prior preview hashes. A batch
mapping is locked after its first promoted record.

Promotion is disabled by default and requires an accepted review, valid
chronological OUT/OFF/ON/IN events, explicit source-only and duplicate review
where applicable, a typed confirmation phrase, a matching preview fingerprint,
and an atomic database transaction. Never enable promotion on the operational
installation until the import has been tested against a copy of the real
export and reconciled against LogTen totals.



### Historical duration-only records

Historical flights may have a supplied total duration without OUT/OFF/ON/IN events. The importer accepts explicit `HH:MM` durations (for example `01:35` = 95 minutes) and decimal-hour values (for example `1.6` = 96 minutes). The original source string is retained in the immutable staged source row and in the promoted flight's `source_total_time` field, even if a review correction changes the value used for `logged_minutes`. A duration-only flight stores the supplied duration as `logged_minutes`; `block_minutes` and `air_minutes` remain NULL because neither can be derived from total duration alone. All four event timestamps remain NULL. Reports show Logged separately from Block and Air. Event-timed flights retain the existing OUT-to-IN block and OFF-to-ON air calculations. Mixed-era batches are supported. The app never synthesizes event timestamps. If only some event timestamps are present, promotion remains blocked until the source is corrected; missing events are never inferred. May 2017 is a review hint only, not a hard-coded cutoff. Each row's actual source fields determine its recording mode. Night, instrument and pilot credits remain subject to duration-based consistency checks.

## Install from GitHub

The Supervisor must be able to clone the repository URL. This app intentionally omits `image` from config.yaml during development, so Supervisor builds the Dockerfile on the Home Assistant host. The Dockerfile includes the required Home Assistant image metadata labels. In Home Assistant,
open Settings > Apps > Install app, open the top-right menu, choose
Repositories, and add the repository URL. To use a test channel, append
`#branch-name` to the repository URL.

The public Home Assistant deployment repository is
`https://github.com/m47v97vsms-lab/pilot-logbook-ha`. No personal access token or
authenticated Git transport is required for this repository. The private
development source repository remains separate and is not needed by Home Assistant.

## Home Assistant access

Open the app through the Home Assistant app panel. The app is configured for
Supervisor Ingress and rejects direct non-Ingress requests. Do not publish a
host port or expose the service to the public internet.

## Flight time and reporting

OUT/OFF/ON/IN timestamps are stored internally in UTC and displayed in airport
local time. Verify time-zone and daylight-saving handling for each airport.
Night and actual instrument values are recorded as hours in the UI and stored
as minutes for reporting. Each flight also retains `logged_minutes` separately
from derived block and air time. Duration-only historical records contribute to
Logged totals while Block and Air remain unavailable; no taxi time is inferred.

## FC View

FC View synchronization is optional. Credentials and callback-relay secrets
must be configured only in the app options on the test installation. Never
commit credentials or callback secrets to the repository.

The registered provider redirect URI should be the Home Assistant Cloud
webhook URL stored in the `fcv_redirect_uri` app option. The webhook automation
must forward the query's `state` and either `code` or `error` as a small JSON
POST to the app's internal Supervisor hostname on port 8101 at
`/api/fcv/oauth/callback-relay`, with the `X-FCV-Relay-Secret` header matching
the separate `fcv_callback_relay_secret` app option (minimum 32 characters).
The endpoint accepts only `application/json` bodies up to 4 KiB, consumes
OAuth state once, and returns `Cache-Control: no-store`; it never returns
authorization codes or tokens. Only this exact POST route bypasses Ingress
after the shared-secret check. Every other route and method remains
Ingress-only.

Keep webhook IDs and secrets in Home Assistant secrets/options, do not expose
port 8101 to LAN/WAN, and disable automation trace retention for the callback
relay so one-time codes are not stored in traces. Start with a synthetic
provider account on a separate Home Assistant test installation.
