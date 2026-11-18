# Pilot Logbook for Home Assistant

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fm47v97vsms-lab%2Fpilot-logbook-ha)

A personal pilot logbook app for Home Assistant OS. Current synchronized development package: **0.4.0-dev60**.

> **Experimental:** This release is for evaluation and testing. Do not use it as the authoritative flight record until the Home Assistant installation, calculations, imports, backups, and restore process have been independently validated.

## Install

1. Select **Add repository to Home Assistant** above, or add this repository manually in **Settings → Apps → Install app → ⋮ → Repositories**.
2. Install **Pilot Logbook** from the available apps.
3. Start it and open it from the Home Assistant app panel.

The app is configured for Supervisor Ingress and does not publish a host port. The repository intentionally omits a prebuilt image, so Home Assistant builds the Dockerfile locally; the first installation may take several minutes. The app package in this branch is synchronized from the tested private development snapshot.

Dev53 recognizes native LogTen tab-export headers, including the whitespace-padded field names emitted by LogTen. It maps flight date/number, route, aircraft, crew, OUT/OFF/ON/IN events, logged duration, PIC/SIC, night, actual instrument and remarks while retaining all original source values. Simulator-category records are classified separately from route flights. The import review view uses expandable original-source fields and a horizontally scrollable table on narrow screens.

The importer supports historical duration-only, OUT/IN-only, and complete OUT/OFF/ON/IN records without fabricating missing events. Dev56 adds a directory-aware Browse Flights view with person/crew, aircraft, airport, keyword, and date filters, paginated results, and totals calculated across the full filtered result set. Unsupported partial event combinations and records with missing required durations remain in staging for review. Unpromoted import batches can be safely cleared while promoted source records remain protected.

## Data and safeguards

- App database and generated backups are stored in the persistent `/data` directory.
- Keep regular Home Assistant backups and test the app's own backup/restore workflow.
- FC View credentials and callback-relay secrets belong in the app options, never in source files.
- LogTen import promotion is disabled by default. Review staged imports carefully and validate against a copy of your existing logbook before enabling it.
- The uploaded reference export was used only for local validation and UI formatting; it is not included in this repository or its CI artifacts.

See [app documentation](pilot_logbook_engine/DOCS.md) for configuration, import safeguards, and known limitations.

## Compatibility

This repository currently targets **Home Assistant OS on amd64**.


## Logbook navigation and directories (dev57)

The main navigation is grouped into Logbook, Directories, and Data & Tools. On wider screens it uses a persistent left navigation rail; on narrow screens it becomes a horizontally scrollable section bar. People, Aircraft, and Places directories include searchable, count-bearing indexes. Selecting an entry opens Browse Flights with that person, aircraft, or airport filter applied. Crew indexes include both managed directory entries and legacy crew names retained on historical flights. Aircraft filters match registration or type; airport filters match either departures or arrivals. Directory maintenance forms remain available inside expandable sections.

The dev57 navigation groups Logbook, Directories, and Data & Tools. People, Aircraft (separate registration and type entries), and Places have searchable indexes with flight counts; selecting an entry applies that filter in Browse Flights. The navigation becomes a left rail on wider screens and a horizontal section bar on mobile.
