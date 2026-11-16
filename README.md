# Pilot Logbook for Home Assistant

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fm47v97vsms-lab%2Fpilot-logbook-ha)

A personal pilot logbook app for Home Assistant OS. Current synchronized development package: **0.4.0-dev48**.

> **Experimental:** This release is for evaluation and testing. Do not use it as the authoritative flight record until the Home Assistant installation, calculations, imports, backups, and restore process have been independently validated.

## Install

1. Select **Add repository to Home Assistant** above, or add this repository manually in **Settings → Apps → Install app → ⋮ → Repositories**.
2. Install **Pilot Logbook** from the available apps.
3. Start it and open it from the Home Assistant app panel.

The app is configured for Supervisor Ingress and does not publish a host port. The repository intentionally omits a prebuilt image, so Home Assistant builds the Dockerfile locally; the first installation may take several minutes. The app package in this branch is synchronized from the tested private development snapshot.

## Data and safeguards

- App database and generated backups are stored in the persistent `/data` directory.
- Keep regular Home Assistant backups and test the app's own backup/restore workflow.
- FC View credentials and callback-relay secrets belong in the app options, never in source files.
- LogTen import promotion is disabled by default. Review staged imports carefully and validate against a copy of your existing logbook before enabling it.

See [app documentation](pilot_logbook_engine/DOCS.md) for configuration, import safeguards, and known limitations.

## Compatibility

This repository currently targets **Home Assistant OS on amd64**.
