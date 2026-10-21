# Changelog

## 1.2.0
**Output tab (connection settings) reworked**
- Required fields are marked `*` and checked before saving, with a message under each wrong box (address typed as
  `mqtt://...`, port out of range, a topic template with an unknown / unbalanced `{placeholder}`, a password without a
  username, ...). Required fields are only enforced while **Enabled** is on, so a half-filled draft can still be saved.
- **Save & check connection**: saves, then really connects and shows a step-by-step result: *Address* -> *Network* ->
  *Login* -> *Response topic*, with timing and, when something fails, what to check (wrong password, broker needs a
  login, port closed, no answer, topic not allowed ...). Nothing is published during the check.
- **Test connection** button checks the values in the form without saving them. HTTP outputs are checked too.
- The same rules are enforced on the server (`PUT /api/settings` answers 400 with the list of wrong fields);
  new endpoint `POST /api/output/test`.
- **Fixed: changing the MQTT username/password did not reconnect** (only a changed address or port did), so the broker
  chip stayed red until a restart. Saving now reconnects immediately with the new settings.
- **Fixed: a mistyped topic template** (e.g. `ksb/meter{meter_c`) raised an error while sending, which the poller
  treated as a failing meter. The send now fails cleanly and logs why; the Output tab refuses such a template.

**App-wide**
- Loader: a slim progress bar at the top for every request you trigger, a blocking "working..." overlay for long
  actions (saving/checking the connection, import, restart, resets, bulk upload) and while the page first loads.
  Background refreshes stay silent. After a restart the overlay waits until the app answers and reloads the page.
- If the login expires the page returns to the sign-in screen instead of filling with errors.
- **Fixed: Sign out did not sign out** (the session cookie is HttpOnly, JavaScript cannot delete it). New
  `POST /api/auth/logout`.

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
