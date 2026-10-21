# Changelog

## 1.1.2
**Fixed**
- **Random HTTP 500 errors (and random "401 Unauthorized") in the web page**, e.g. `/api/dashboard` failing with
  `'NoneType' object is not subscriptable`. All threads (web requests, ZLAN pollers, retry loop) share one database
  connection; a query was started under a lock but its result was read after the lock was released, so another
  thread could invalidate it first. Results are now read completely while the lock is held. This bug existed in v1.0;
  it shows up much more on Python 3.13 (Debian 13 gateways). Under a 20 s load test the old code gave hundreds of
  errors, the fixed code none.
- A change made to `bridge.db` by another process (for example clearing the login from the command line) is now seen
  by the running app immediately, instead of only after a restart.

## 1.1.1
**Fixed**
- ZLAN / Meter / Parameter tables lost whatever you had typed (and sometimes the value you were about to save):
  they re-draw themselves every few seconds and on every connectivity event, and the re-draw reset all input boxes.
  Typed-but-unsaved values (and the cursor) now survive the refresh; unsaved boxes are highlighted.
- Saving an interval now shows "✓ Saved" (or "✗ <reason>") right next to the Save button.
- The server now rejects ZLAN / meter intervals that are blank, 0, negative, non-numeric or longer than a year
  (a 0-minute poll interval used to be accepted and made the poller run without any delay).

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
