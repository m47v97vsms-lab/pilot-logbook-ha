# Pilot Logbook documentation

## Status and safeguards

This is an experimental development app. It is not approved for operational
flight records. Test it on a separate Home Assistant installation or with
synthetic data first.

The app stores its SQLite database and generated backups under `/data`, the
persistent app data directory. Keep regular Home Assistant backups and verify
the app's own backup/restore process before relying on it.

## LogTen import

CSV/TSV files are staged with their original headers, cells and source rows
retained. A batch-level target-to-source field mapping supports alternate
LogTen headers. The reconciliation view shows every mapped field and every
non-empty unmapped source column. Staging does not change canonical flight
totals.

Unmapped values block promotion until the reviewer explicitly acknowledges
that they will remain source-only in the immutable staged record. Exact staged
duplicates, exact canonical duplicates, and possible date/route/flight-number
matches are identified; possible matches require explicit review. Mapping and
correction changes reset review acknowledgements and invalidate prior preview
hashes. A batch mapping is locked after its first promoted record.

Promotion is disabled by default and requires an accepted review, valid
chronological OUT/OFF/ON/IN events, explicit source-only and duplicate review
where applicable, a typed confirmation phrase, a matching preview fingerprint,
and an atomic database transaction. Never enable promotion on the operational
installation until the import has been tested against a copy of the real
export and reconciled against LogTen totals.

## Install from GitHub

The Supervisor must be able to clone the repository URL. This app intentionally omits `image` from config.yaml during development, so Supervisor builds the Dockerfile on the Home Assistant host. The Dockerfile includes the required Home Assistant image metadata labels. In Home Assistant,
open Settings > Apps > Install app, open the top-right menu, choose
Repositories, and add the repository URL. To use a test channel, append
`#branch-name` to the repository URL.

The public deployment repository is https://github.com/m47v97vsms-lab/pilot-logbook-ha. No personal access token or authenticated Git transport is required.

## Home Assistant access

Open the app through the Home Assistant app panel. The app is configured for
Supervisor Ingress and rejects direct non-Ingress requests. Do not publish a
host port or expose the service to the public internet.

## Flight time and reporting

OUT/OFF/ON/IN timestamps are stored internally in UTC and displayed in airport
local time. Verify time-zone and daylight-saving handling for each airport.
Night and actual instrument values are recorded as hours in the UI and stored
as minutes for reporting.

## FC View

FC View synchronization is optional. Credentials and callback-relay secrets
must be configured only in the app options on the test installation. Never
commit credentials or callback secrets to the repository.
