# Modbus Bridge (IDMS gateway)

Runs on each IoT2050 gateway. It polls energy meters over Modbus TCP (meters sit behind ZLAN RS-485 gateways), keeps a
queue and a full history, and forwards readings to IDMS over MQTT (or HTTP) with retries.

* `app.py` – the whole application (FastAPI + background threads, SQLite `bridge.db`)
* `templates/`, `static/` – the web dashboard (port 8000)
* `modbus-bridge.service` – systemd unit (`/opt/modbus_bridge`)
* `deploy/update.sh` – update a gateway from this repository (see [docs/DEPLOY.md](docs/DEPLOY.md))
* `tests/` – automated tests (`pip install -r requirements-dev.txt && pytest`)

## How readings flow
meter → ZLAN → **poll** (per parameter interval) → validate range → **publish throttle** (per meter + parameter) →
send to IDMS → `history` table **and** CSV backup → ack from IDMS marks it released (otherwise retried from the queue).

## Intervals (all on the web front-end)
| Where | Setting | Meaning |
|---|---|---|
| ZLANs & Meters | ZLAN *Interval* / meter *Poll* | Base poll cycle of the meter (e.g. 15 min). |
| Parameters | *Poll (min)* | How often **this parameter** is read. Blank = every cycle. Works in steps of the meter's cycle (30 on a 15-min meter = every 2nd cycle). |
| Parameters | *Publish (min)* | Minimum minutes between sends of this parameter. Blank = meter's, then ZLAN's publish interval. Needs *Publish throttle* enabled in Settings. |
| ZLANs & Meters | ZLAN *Default publish* / meter *Publish* | Fallback publish intervals. |

**Site setting:** MD = Poll 30, Publish 30. Everything else polls every cycle and publishes every 180 min (ZLAN default).

## Data kept on the gateway
* `bridge.db` – history (retention in Settings; `0` = forever).
* `backup/YYYYMM/readings_YYYYMMDD.csv`, `connectivity_*.csv`, `acks_*.csv` – plain CSV copy of everything sent to IDMS and
  every delivery result. Survives history purges and database resets. Copy it off with WinSCP at any time.
* `db-backups/` – database copies taken automatically before each update (last 5 kept).

## Develop / test
```bash
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest tests -q
venv/bin/python app.py          # http://localhost:8000
```
Python 3.7+ (gateways run Debian 11 / Python 3.9).
