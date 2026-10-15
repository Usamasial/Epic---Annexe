# Changelog

## 1.1.0
**Fixed**
- **MD, voltage and current readings were being dropped.** The publish throttle was tracked per meter, so after kWh
  (the first parameter read) was published, every other parameter of that meter was discarded until the next window.
  The throttle is now tracked per meter **and** per parameter.

**Added**
- Per-parameter **Poll (min)** and **Publish (min)** intervals on the *Parameters* tab (front-end only, stored in the
  database). Precedence for publishing: parameter, then meter, then ZLAN default.
  Existing gateways are upgraded automatically and MD is set to poll 30 / publish 30 (once; editable afterwards).
- **CSV backup on the gateway** of everything handed to IDMS: `backup/YYYYMM/{readings,connectivity,acks}_YYYYMMDD.csv`.
  Low load (background thread, batched appends), pauses when free disk is low, optional retention, settings on the
  *Settings* tab.
- Application version shown in the header (`/api/version`).
- `deploy/update.sh`: one-command, safe update of a gateway from git (database backup, health check, automatic rollback).
- Automated tests (`tests/`).

**Upgrade notes**
- The database is upgraded in place on first start (new columns/table; nothing is deleted).
- Rolling back to 1.0 requires restoring the pre-update database copy (`deploy/update.sh --rollback` does this).
