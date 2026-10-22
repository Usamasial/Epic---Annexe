"""
Modbus Bridge -- polls energy meters behind ZLAN Modbus RTU-to-TCP
gateways, queues readings, forwards them via HTTP or MQTT, with
dead-letter handling, priority, validation, and full audit history.

Built directly against the finalized Scope of Work (12 sections).
"""
import asyncio, base64, csv, hashlib, hmac, io, json, logging, os, re, secrets, signal, sys
import queue as _queue_mod
import ipaddress, shutil, socket, sqlite3, struct, threading, time
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import requests
import paho.mqtt.client as mqtt
import uvicorn
from fastapi import Body, Cookie, FastAPI, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pymodbus.client import ModbusTcpClient
from typing import Optional, Dict, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bridge")

APP_VERSION = "1.2.2"
DB_PATH = Path("bridge.db")
BACKUP_DIR = Path("backup")   # CSV copy of everything sent to IDMS (see BackupWriter)
WEB_PORT = 8000
RETRY_INTERVAL = 2
STALE_TIMEOUT = 20  # seconds an MQTT publish can wait for a response before counted as failed

def now_s(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def now_ms(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

def _parse_ts(s: str) -> datetime:
    """Parses timestamps that may or may not include microseconds."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

def _months_ago(n: int) -> datetime:
    d = datetime.now()
    month = d.month - n
    year = d.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(d.day, 28)  # avoid invalid-day overflow (e.g. Feb 30)
    return d.replace(year=year, month=month, day=day)

def decode_f32(hi, lo): return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
def encode_f32(v): return struct.unpack(">HH", struct.pack(">f", v))

def rr_compat(client, address, count, unit_id):
    try: return client.read_holding_registers(address=address, count=count, slave=unit_id)
    except TypeError: return client.read_holding_registers(address=address, count=count, device_id=unit_id)

# ---------------------------------------------------------------- register grouping (Sec 3.1)
def group_addresses(params: List[dict], max_span: int = 60) -> List[dict]:
    """Merges contiguous parameter addresses into single reads.
    Each item in params: {label, address, count}. Returns groups:
    {start, count, members:[{label, offset, count}]}."""
    items = sorted(params, key=lambda p: p["address"])
    groups, cur = [], None
    for it in items:
        if cur and it["address"] == cur["start"] + cur["count"] and (it["address"] + it["count"] - cur["start"]) <= max_span:
            cur["members"].append({"label": it["label"], "offset": cur["count"], "count": it["count"]})
            cur["count"] += it["count"]
        else:
            cur = {"start": it["address"], "count": it["count"],
                   "members": [{"label": it["label"], "offset": 0, "count": it["count"]}]}
            groups.append(cur)
    return groups


# ==================================================================== SCHEMA
_SCHEMA = """
CREATE TABLE IF NOT EXISTS zlans (
    zlan_ip TEXT PRIMARY KEY, zlan_label TEXT NOT NULL,
    default_polling_interval INTEGER NOT NULL DEFAULT 30, port INTEGER NOT NULL DEFAULT 502
);
CREATE TABLE IF NOT EXISTS meters (
    meter_code TEXT PRIMARY KEY, zlan_ip TEXT NOT NULL, unit_id INTEGER NOT NULL,
    polling_interval INTEGER, active INTEGER NOT NULL DEFAULT 1,
    last_poll_at TEXT, last_status TEXT DEFAULT 'unknown', consecutive_fail INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL, UNIQUE(zlan_ip, unit_id)
);
CREATE TABLE IF NOT EXISTS parameters (
    label TEXT PRIMARY KEY, address INTEGER NOT NULL, wire_code TEXT,
    priority INTEGER NOT NULL DEFAULT 100, min_value REAL, max_value REAL, active INTEGER NOT NULL DEFAULT 1,
    poll_interval INTEGER, publish_interval INTEGER
);
CREATE TABLE IF NOT EXISTS publish_state (
    meter_code TEXT NOT NULL, parameter TEXT NOT NULL, last_published_at TEXT NOT NULL,
    PRIMARY KEY(meter_code, parameter)
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT NOT NULL, parameter TEXT NOT NULL,
    value REAL NOT NULL, date_time TEXT NOT NULL, msg_no TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, awaiting_response TEXT
);
CREATE INDEX IF NOT EXISTS idx_q_created ON queue(created_at);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT NOT NULL, parameter TEXT NOT NULL,
    value REAL NOT NULL, date_time TEXT NOT NULL, msg_no TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
    released_at TEXT, failed_at TEXT, error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_h_lookup ON history(meter_code, parameter, status, created_at);
CREATE TABLE IF NOT EXISTS invalid_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT, parameter TEXT, value REAL,
    reason TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dropped_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT, parameter TEXT, value REAL,
    msg_no TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS msg_counters (
    meter_code TEXT, parameter TEXT, day TEXT, counter INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(meter_code, parameter, day)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connectivity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    status TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conn_lookup ON connectivity_history(entity_type, entity_id, at);
CREATE TABLE IF NOT EXISTS connectivity_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    status TEXT NOT NULL, at TEXT NOT NULL, msg_no TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, awaiting_response TEXT
);
CREATE TABLE IF NOT EXISTS skipped_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT, parameter TEXT, value REAL,
    reason TEXT, at TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "connection_type": "http", "server_url": "", "mqtt_broker": "", "mqtt_port": "1883",
    "mqtt_topic_template": "ksb/meter{meter_code}/{label}", "mqtt_response_topic": "",
    "mqtt_connectivity_topic_template": "ksb/connectivity/{entity_id}",
    "mqtt_response_timeout": "15", "server_enabled": "false",
    "mqtt_username": "", "mqtt_password": "",
    "max_attempts": "5", "max_queue_size": "5000", "queue_retention_days": "7", "retention_days": "90", "connectivity_retention_days": "90",
    "auth_user": "admin", "auth_hash": "", "auth_salt": "",
    "modbus_timeout": "4.5", "modbus_inter_request_delay": "6",
    "publish_throttle_enabled": "false", "log_skipped_events": "false",
    "connectivity_debounce_enabled": "false", "mqtt_connectivity_min_gap_seconds": "60",
    "connectivity_heartbeat_enabled": "false", "connectivity_heartbeat_hours": "24", "connectivity_heartbeat_check_minutes": "15",
    "backup_enabled": "true", "backup_retention_days": "0",
}

# ==================================================================== BACKUP (CSV copy of everything sent to IDMS)
class BackupWriter:
    """Keeps a plain CSV copy of every reading / connectivity event handed to
    IDMS, plus every delivery result (ack), on the gateway's own disk:

        backup/YYYYMM/readings_YYYYMMDD.csv
        backup/YYYYMM/connectivity_YYYYMMDD.csv
        backup/YYYYMM/acks_YYYYMMDD.csv

    It is a safety copy that survives history purges, factory resets and a
    damaged bridge.db. Load on the gateway is negligible: callers only put a
    tuple on an in-memory queue; one low-priority background thread appends
    batches of lines. Writing stops (with a warning) when free disk space is
    low, so a full disk can never take the bridge down."""
    HEADERS = {
        "readings":     ["logged_at", "date_time", "meter_code", "parameter", "value", "msg_no", "status"],
        "connectivity": ["logged_at", "at", "entity_type", "entity_id", "status", "msg_no"],
        "acks":         ["logged_at", "msg_no", "result", "message"],
    }
    MIN_FREE_BYTES = 200 * 1024 * 1024

    def __init__(self, base: Path, enabled_fn):
        self.base = Path(base)
        self.enabled_fn = enabled_fn
        self.batch_delay = 2.0            # seconds to let a burst accumulate into one write
        self.q = _queue_mod.SimpleQueue()
        self.lock = threading.Lock()
        self.thread = None
        self.pending = 0
        self._enabled_cache = (0.0, True)
        self._last_warn = 0.0

    def _enabled(self):
        ts, val = self._enabled_cache
        if time.time() - ts > 30:
            try: val = bool(self.enabled_fn())
            except Exception: val = True
            self._enabled_cache = (time.time(), val)
        return val

    def record(self, kind, *fields):
        """Never raises and never blocks the caller."""
        try:
            if not self._enabled(): return
            with self.lock:
                self.pending += 1
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._run, name="backup-writer", daemon=True)
                    self.thread.start()
            self.q.put((kind, datetime.now(), list(fields)))
        except Exception:
            pass

    def _warn(self, msg, *args):
        if time.time() - self._last_warn > 60:
            self._last_warn = time.time()
            log.warning(msg, *args)

    def _run(self):
        while True:
            items = [self.q.get()]
            if self.batch_delay: time.sleep(self.batch_delay)
            while len(items) < 1000:
                try: items.append(self.q.get_nowait())
                except _queue_mod.Empty: break
            try:
                self._write(items)
            except Exception as e:
                self._warn("backup write failed: %s", e)
            finally:
                with self.lock: self.pending -= len(items)

    def _write(self, items):
        self.base.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.base).free < self.MIN_FREE_BYTES:
            self._warn("backup paused: less than %d MB free on disk", self.MIN_FREE_BYTES // (1024 * 1024))
            return
        by_file = {}
        for kind, ts, fields in items:
            d = self.base / ts.strftime("%Y%m")
            by_file.setdefault((d / f"{kind}_{ts.strftime('%Y%m%d')}.csv", kind), []).append([ts.strftime("%Y-%m-%d %H:%M:%S")] + fields)
        for (path, kind), rows in by_file.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists() or path.stat().st_size == 0
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if new: w.writerow(self.HEADERS[kind])
                w.writerows(rows)

    def flush(self, timeout=10.0):
        """Waits until everything queued so far is on disk (used by tests / shutdown)."""
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if self.pending <= 0: return True
            time.sleep(0.02)
        return False

    def files(self):
        return sorted(self.base.glob("*/*.csv")) if self.base.exists() else []

    def status(self):
        fs = self.files()
        free = shutil.disk_usage(self.base if self.base.exists() else Path(".")).free
        return {"enabled": self._enabled(), "path": str(self.base.resolve()), "files": len(fs),
                "size_mb": round(sum(f.stat().st_size for f in fs) / (1024 * 1024), 2),
                "latest_file": fs[-1].name if fs else None, "free_disk_mb": round(free / (1024 * 1024))}

    def purge(self, days):
        """Deletes backup files older than `days` (0 = keep forever)."""
        if days <= 0 or not self.base.exists(): return 0
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        n = 0
        for f in self.files():
            day = f.stem.rsplit("_", 1)[-1]
            if day.isdigit() and day < cutoff:
                try: f.unlink(); n += 1
                except OSError: pass
        for d in self.base.glob("*"):
            try: d.rmdir()   # only succeeds when empty
            except OSError: pass
        return n

class _Rows:
    """In-memory result of a query: same fetchone / fetchall / rowcount / iteration
    interface the callers already use, but safe to read from any thread."""
    __slots__ = ("_rows", "_i", "rowcount", "lastrowid")
    def __init__(self, rows, rowcount, lastrowid):
        self._rows, self._i, self.rowcount, self.lastrowid = rows, 0, rowcount, lastrowid
    def fetchone(self):
        if self._i >= len(self._rows): return None
        self._i += 1
        return self._rows[self._i - 1]
    def fetchall(self):
        out = self._rows[self._i:]; self._i = len(self._rows)
        return out
    def __iter__(self):
        return iter(self.fetchall())

class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._rc = None                    # separate read-only connection for heavy queries (see _rq)
        self._rlock = threading.Lock()
        self._stats, self._stats_lock = None, threading.Lock()
        self.lock = threading.RLock()  # RLock, not Lock: several methods call other
            # locked methods internally (e.g. mark_meter_result -> log_connectivity),
            # which would deadlock a plain Lock on the same thread. RLock allows safe
            # re-entry so every DB call -- reads included -- can be serialized through
            # the same lock without risk of self-deadlock.
        self.c = sqlite3.connect(str(path), check_same_thread=False)
        self.c.execute("PRAGMA journal_mode=WAL;")
        self.c.executescript(_SCHEMA)
        try:
            self.c.execute("ALTER TABLE zlans ADD COLUMN last_status TEXT DEFAULT 'unknown'")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self.c.execute("ALTER TABLE zlans ADD COLUMN default_publish_interval INTEGER")
        except sqlite3.OperationalError:
            pass
        for col, ddl in [("msg_no", "TEXT"), ("delivery_status", "TEXT DEFAULT 'queued'"),
                          ("released_at", "TEXT"), ("failed_at", "TEXT"), ("error_message", "TEXT"),
                          ("created_at", "TEXT")]:
            try:
                self.c.execute(f"ALTER TABLE connectivity_history ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        try:
            self.c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conn_msgno ON connectivity_history(msg_no)")
        except sqlite3.OperationalError:
            pass
        for col, ddl in [("publish_interval", "INTEGER"), ("last_published_at", "TEXT"), ("last_connectivity_sent_at", "TEXT")]:
            try:
                self.c.execute(f"ALTER TABLE meters ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass
        try:
            self.c.execute("ALTER TABLE zlans ADD COLUMN last_connectivity_sent_at TEXT")
        except sqlite3.OperationalError:
            pass
        # v1.1: per-parameter poll / publish intervals. Gateways that were set up
        # before this version get the columns added; MD is then set to the
        # 30-minute poll + 30-minute publish the site requires (editable on the
        # Parameters tab afterwards -- this runs only once, when the columns are new).
        added = []
        for col in ("poll_interval", "publish_interval"):
            try:
                self.c.execute(f"ALTER TABLE parameters ADD COLUMN {col} INTEGER")
                added.append(col)
            except sqlite3.OperationalError:
                pass  # column already exists
        if added:
            self.c.execute("UPDATE parameters SET poll_interval=30, publish_interval=30 WHERE label='md'")
        for k, v in DEFAULT_SETTINGS.items():
            self.c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        self.c.commit()
        self.backup = BackupWriter(BACKUP_DIR, lambda: self.get("backup_enabled") != "false")

    # -- settings --
    def _q(self, sql, params=()):
        """Thread-safe query execution -- every DB call (read or write) goes
        through here, serialized on self.lock. Necessary because the single
        sqlite3 connection is shared raw (check_same_thread=False) across
        every thread: all ZLAN polling threads, retry_loop, broker_health_loop,
        the MQTT ack callback, and every HTTP request handler. Unsynchronized
        concurrent access -- even just reads happening during a write -- is
        undefined behavior in Python's sqlite3 module and was silently
        corrupting/hanging the shared connection under real production load."""
        with self.lock:
            cur = self.c.execute(sql, params)
            # Read the WHOLE result while still holding the lock, then release the
            # statement. Handing the live cursor back let another thread's query on
            # the same connection invalidate it before the caller read it
            # (fetchone() -> None / wrong row => HTTP 500 on /api/dashboard on
            # Python 3.13), and left read snapshots open so other processes' edits
            # to bridge.db stayed invisible to the running app.
            rows = cur.fetchall() if cur.description is not None else []
            res = _Rows(rows, cur.rowcount, cur.lastrowid)
            cur.close()
            return res

    def _rq(self, sql, params=()):
        """Heavy READ-ONLY queries (dashboard counts, History / Queue / Connectivity pages) run on their own
        connection. With WAL, readers and the writer do not block each other, so a slow query over a large history
        can no longer hold the main lock that every poll thread, MQTT acknowledgement and login check needs.
        Falls back to the main connection if a read-only connection cannot be opened."""
        with self._rlock:
            if self._rc is None:
                try:
                    uri = "file:" + quote(str(self.path.resolve())) + "?mode=ro"
                    self._rc = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10)
                except sqlite3.Error:
                    self._rc = False
            if self._rc:
                cur = self._rc.execute(sql, params)
                rows = cur.fetchall() if cur.description is not None else []
                res = _Rows(rows, cur.rowcount, cur.lastrowid)
                cur.close()
                return res
        return self._q(sql, params)

    def get(self, k):
        r = self._q("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        return r[0] if r else ""
    def set(self, k, v):
        with self.lock:
            self._q("UPDATE settings SET value=? WHERE key=?", (str(v), k)); self.c.commit()
    def settings_dict(self):
        return {r[0]: r[1] for r in self._q("SELECT key,value FROM settings")}

    # -- ZLANs --
    def list_zlans(self):
        rows = self._q("SELECT zlan_ip,zlan_label,default_polling_interval,port,last_status,default_publish_interval FROM zlans ORDER BY zlan_label").fetchall()
        return [{"zlan_ip":r[0],"zlan_label":r[1],"default_polling_interval":r[2],"port":r[3],"last_status":r[4] or "unknown","default_publish_interval":r[5]} for r in rows]

    def update_zlan_interval(self, zlan_ip, minutes):
        with self.lock:
            cur = self._q("UPDATE zlans SET default_polling_interval=? WHERE zlan_ip=?", (minutes, zlan_ip))
            self.c.commit()
        return cur.rowcount > 0

    def update_zlan_publish_interval(self, zlan_ip, minutes):
        with self.lock:
            cur = self._q("UPDATE zlans SET default_publish_interval=? WHERE zlan_ip=?", (minutes, zlan_ip))
            self.c.commit()
        return cur.rowcount > 0

    def add_zlan(self, zlan_ip, zlan_label, default_polling_interval, port=502):
        if self._q("SELECT 1 FROM zlans WHERE zlan_ip=?", (zlan_ip,)).fetchone():
            return "ZLAN with this IP already exists."
        with self.lock:
            self._q("INSERT INTO zlans(zlan_ip,zlan_label,default_polling_interval,port) VALUES(?,?,?,?)", (zlan_ip, zlan_label, default_polling_interval, port))
            self.c.commit()
        return None

    def delete_zlan(self, zlan_ip):
        with self.lock:
            self._q("DELETE FROM meters WHERE zlan_ip=?", (zlan_ip,))
            cur = self._q("DELETE FROM zlans WHERE zlan_ip=?", (zlan_ip,))
            self.c.commit()
        return cur.rowcount > 0

    # -- Meters --
    def list_meters(self, zlan_ip=None, active_only=False):
        q = "SELECT meter_code,zlan_ip,unit_id,polling_interval,active,last_poll_at,last_status,consecutive_fail,registered_at,publish_interval FROM meters"
        cond, params = [], []
        if zlan_ip: cond.append("zlan_ip=?"); params.append(zlan_ip)
        if active_only: cond.append("active=1")
        if cond: q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY zlan_ip, unit_id"
        rows = self._q(q, params).fetchall()
        return [{"meter_code":r[0],"zlan_ip":r[1],"unit_id":r[2],"polling_interval":r[3],
                  "active":bool(r[4]),"last_poll_at":r[5],"last_status":r[6],
                  "consecutive_fail":r[7],"registered_at":r[8],"publish_interval":r[9]} for r in rows]

    def add_meter(self, meter_code, zlan_ip, unit_id, polling_interval=None):
        if self._q("SELECT 1 FROM meters WHERE meter_code=?", (meter_code,)).fetchone():
            return "Meter code already exists."
        if self._q("SELECT 1 FROM meters WHERE zlan_ip=? AND unit_id=?", (zlan_ip, unit_id)).fetchone():
            return f"Unit ID {unit_id} already used on this ZLAN."
        with self.lock:
            self._q(
                "INSERT INTO meters(meter_code,zlan_ip,unit_id,polling_interval,active,registered_at) VALUES(?,?,?,?,1,?)",
                (meter_code, zlan_ip, unit_id, polling_interval, now_s()))
            self.c.commit()
        return None

    def set_meter_active(self, meter_code, active: bool):
        with self.lock:
            cur = self._q("UPDATE meters SET active=? WHERE meter_code=?", (1 if active else 0, meter_code))
            self.c.commit()
        return cur.rowcount > 0

    def rename_meter(self, old_code, new_code):
        if self._q("SELECT 1 FROM meters WHERE meter_code=?", (new_code,)).fetchone():
            return "New meter code already in use."
        with self.lock:
            self._q("UPDATE meters SET meter_code=? WHERE meter_code=?", (new_code, old_code))
            self._q("UPDATE publish_state SET meter_code=? WHERE meter_code=?", (new_code, old_code))
            self.c.commit()
        return None

    def update_meter_interval(self, meter_code, minutes):
        with self.lock:
            cur = self._q("UPDATE meters SET polling_interval=? WHERE meter_code=?", (minutes, meter_code))
            self.c.commit()
        return cur.rowcount > 0

    def update_meter_publish_interval(self, meter_code, minutes):
        with self.lock:
            cur = self._q("UPDATE meters SET publish_interval=? WHERE meter_code=?", (minutes, meter_code))
            self.c.commit()
        return cur.rowcount > 0

    THROTTLE_TOLERANCE_S = 30   # polls drift by a few seconds; don't lose a whole poll cycle to that

    def publish_interval_for(self, meter_code, label):
        """Minutes between publishes for one meter+parameter. Most specific wins:
        parameter -> meter -> ZLAN default -> None (no throttle, every read)."""
        r = self._q("""SELECT p.publish_interval, m.publish_interval, z.default_publish_interval
            FROM meters m LEFT JOIN zlans z ON z.zlan_ip = m.zlan_ip
            LEFT JOIN parameters p ON p.label=?
            WHERE m.meter_code=?""", (label, meter_code)).fetchone()
        if not r: return None
        return r[0] or r[1] or r[2] or None

    def check_publish_throttle(self, meter_code, label):
        """True = OK to publish this parameter now. False = published too
        recently, drop this reading. Tracked per meter AND per parameter, so
        publishing kWh never blocks MD / volt / amp (v1.0 tracked per meter
        only, which silently dropped everything except the first parameter)."""
        interval = self.publish_interval_for(meter_code, label)
        if not interval: return True
        r = self._q("SELECT last_published_at FROM publish_state WHERE meter_code=? AND parameter=?",
                    (meter_code, label)).fetchone()
        if not r: return True
        elapsed = (datetime.now() - _parse_ts(r[0])).total_seconds()
        return elapsed >= interval * 60 - self.THROTTLE_TOLERANCE_S

    def mark_published(self, meter_code, label, dt=None):
        dt = dt or datetime.now()
        with self.lock:
            self._q("INSERT INTO publish_state(meter_code,parameter,last_published_at) VALUES(?,?,?) "
                    "ON CONFLICT(meter_code,parameter) DO UPDATE SET last_published_at=excluded.last_published_at",
                    (meter_code, label, dt.strftime("%Y-%m-%d %H:%M:%S.%f")))
            self.c.commit()

    def log_skipped(self, meter_code, parameter, value, reason):
        with self.lock:
            self._q("INSERT INTO skipped_log(meter_code,parameter,value,reason,at) VALUES(?,?,?,?,?)",
                (meter_code, parameter, value, reason, now_s()))
            self.c.commit()

    def log_connectivity(self, entity_type, entity_id, status):
        """Writes the local audit row and returns (msg_no, at) so the
        caller can trigger the reliable MQTT/HTTP delivery attempt."""
        now = datetime.now()
        msg_no = self.next_msg_no(entity_id, entity_type, now)
        at = now.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self._q("""INSERT INTO connectivity_history(entity_type,entity_id,status,at,msg_no,delivery_status,created_at)
                VALUES(?,?,?,?,?,'queued',?)""", (entity_type, entity_id, status, at, msg_no, now_ms()))
            self.c.commit()
        self.backup.record("connectivity", at, entity_type, entity_id, status, msg_no)
        bus.publish("connectivity", {"entity_type": entity_type, "entity_id": entity_id, "status": status})
        return msg_no, at

    def conn_enqueue(self, msg_no, entity_type, entity_id, status, at, awaiting=False):
        created = now_ms()
        with self.lock:
            self._q("""INSERT INTO connectivity_queue(entity_type,entity_id,status,at,msg_no,created_at,awaiting_response)
                VALUES(?,?,?,?,?,?,?)""", (entity_type, entity_id, status, at, msg_no, created, created if awaiting else None))
            self.c.commit()

    def conn_mark_attempt(self, row_id):
        with self.lock:
            self._q("UPDATE connectivity_queue SET attempts=attempts+1 WHERE id=?", (row_id,)); self.c.commit()

    def conn_mark_awaiting(self, row_id):
        with self.lock:
            self._q("UPDATE connectivity_queue SET awaiting_response=? WHERE id=?", (now_ms(), row_id)); self.c.commit()

    def conn_clear_stale_awaiting(self, timeout_s):
        cutoff = (datetime.now() - timedelta(seconds=timeout_s)).strftime("%Y-%m-%d %H:%M:%S.%f")
        with self.lock:
            rows = self._q("SELECT id,msg_no FROM connectivity_queue WHERE awaiting_response IS NOT NULL AND awaiting_response < ?", (cutoff,)).fetchall()
            for rid, _ in rows:
                self._q("UPDATE connectivity_queue SET attempts=attempts+1, awaiting_response=NULL WHERE id=?", (rid,))
            self.c.commit()
        return rows

    def conn_release(self, msg_no, message=""):
        with self.lock:
            self._q("DELETE FROM connectivity_queue WHERE msg_no=?", (msg_no,))
            self._q("UPDATE connectivity_history SET delivery_status='released', released_at=?, error_message=? WHERE msg_no=?",
                (now_ms(), message or None, msg_no))
            self.c.commit()
        self.backup.record("acks", msg_no, "released", message or "")

    def conn_fail_attempt(self, msg_no, error=""):
        max_a = int(self.get("max_attempts") or 5)
        with self.lock:
            r = self._q("SELECT id,attempts FROM connectivity_queue WHERE msg_no=?", (msg_no,)).fetchone()
            if not r: return "gone"
            rid, attempts = r
            if attempts >= max_a:
                self._q("DELETE FROM connectivity_queue WHERE id=?", (rid,))
                self._q("UPDATE connectivity_history SET delivery_status='failed', failed_at=?, error_message=? WHERE msg_no=?", (now_ms(), error, msg_no))
                self.c.commit()
                self.backup.record("acks", msg_no, "failed", error or "")
                return "failed"
            self._q("UPDATE connectivity_queue SET awaiting_response=NULL WHERE id=?", (rid,)); self.c.commit()
            return "requeued"

    def conn_oldest_queued(self, skip_awaiting=True):
        clause = "AND awaiting_response IS NULL" if skip_awaiting else ""
        r = self._q(f"""SELECT id,entity_type,entity_id,status,at,msg_no,attempts FROM connectivity_queue
            WHERE 1=1 {clause} ORDER BY created_at ASC LIMIT 1""").fetchone()
        if not r: return None
        return {"id":r[0],"entity_type":r[1],"entity_id":r[2],"status":r[3],"at":r[4],"msg_no":r[5],"attempts":r[6]}

    def get_zlan_label(self, zlan_ip):
        r = self._q("SELECT zlan_label FROM zlans WHERE zlan_ip=?", (zlan_ip,)).fetchone()
        return r[0] if r else zlan_ip

    def get_zlan_label_for_meter(self, meter_code):
        r = self._q("""SELECT z.zlan_label FROM meters m JOIN zlans z ON z.zlan_ip = m.zlan_ip
            WHERE m.meter_code=?""", (meter_code,)).fetchone()
        return r[0] if r else meter_code

    def check_connectivity_gap(self, entity_type, entity_id, gap_seconds):
        """True = OK to publish now. False = within debounce gap, skip outbound push."""
        table, id_col = ("zlans", "zlan_ip") if entity_type == "zlan" else ("meters", "meter_code")
        r = self._q(f"SELECT last_connectivity_sent_at FROM {table} WHERE {id_col}=?", (entity_id,)).fetchone()
        if not r or not r[0]: return True
        last_dt = _parse_ts(r[0])
        return (datetime.now() - last_dt).total_seconds() >= gap_seconds

    def mark_connectivity_sent(self, entity_type, entity_id):
        table, id_col = ("zlans", "zlan_ip") if entity_type == "zlan" else ("meters", "meter_code")
        with self.lock:
            self._q(f"UPDATE {table} SET last_connectivity_sent_at=? WHERE {id_col}=?", (now_ms(), entity_id))
            self.c.commit()

    def stale_connectivity_entities(self, hours):
        """Active meters/ZLANs whose last connectivity signal (a real event
        or a previous heartbeat) is older than the given hours -- or that
        have never sent one at all. Used by the heartbeat feature so a
        long-stable connection still produces a recent, visible signal
        instead of going silent for days."""
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S.%f")
        meters = self._q("""SELECT meter_code, last_status FROM meters
            WHERE active=1 AND last_status IN ('ok','fail')
            AND (last_connectivity_sent_at IS NULL OR last_connectivity_sent_at < ?)""", (cutoff,)).fetchall()
        zlans = self._q("""SELECT zlan_ip, last_status FROM zlans
            WHERE last_status IN ('ok','fail')
            AND (last_connectivity_sent_at IS NULL OR last_connectivity_sent_at < ?)""", (cutoff,)).fetchall()
        return ([{"entity_type": "meter", "entity_id": r[0], "status": "connected" if r[1] == "ok" else "disconnected"} for r in meters] +
                [{"entity_type": "zlan", "entity_id": r[0], "status": "connected" if r[1] == "ok" else "disconnected"} for r in zlans])

    def mark_connectivity_skipped(self, msg_no):
        with self.lock:
            self._q("UPDATE connectivity_history SET delivery_status='skipped' WHERE msg_no=?", (msg_no,))
            self.c.commit()

    def resolve_ack(self, msg_no, ok: bool, message=""):
        """MQTT acks are msg_no-keyed but shared between the reading and
        connectivity pipelines -- route to whichever table actually has it."""
        is_reading = self._q("SELECT 1 FROM history WHERE msg_no=?", (msg_no,)).fetchone()
        if is_reading:
            if ok: self.release(msg_no, message=message); return "released"
            return self.fail_attempt(msg_no, error=message)
        if ok: self.conn_release(msg_no, message=message); return "released"
        return self.conn_fail_attempt(msg_no, error=message)

    def connectivity_history(self, entity_type=None, entity_id=None, zlan_ip=None, date_from=None, date_to=None, limit=100, offset=0):
        cl, pr = [], []
        # Resolves the owning ZLAN for every row: for a meter event, look up
        # its ZLAN via the meters table; for a zlan event, the entity_id IS
        # the zlan_ip already. This lets the UI always show which ZLAN a
        # meter belongs to, not just when a ZLAN filter is applied.
        base = """FROM connectivity_history ch
            LEFT JOIN meters m ON m.meter_code = ch.entity_id AND ch.entity_type='meter'
            LEFT JOIN zlans z ON z.zlan_ip = COALESCE(m.zlan_ip, CASE WHEN ch.entity_type='zlan' THEN ch.entity_id END)"""
        if zlan_ip: cl.append("z.zlan_ip = ? COLLATE NOCASE"); pr.append(zlan_ip)
        if entity_type: cl.append("ch.entity_type=?"); pr.append(entity_type)
        if entity_id: cl.append("ch.entity_id LIKE ? COLLATE NOCASE"); pr.append(f"%{entity_id}%")
        if date_from: cl.append("ch.at >= ?"); pr.append(date_from)
        if date_to:
            if len(date_to) == 10: date_to = date_to + " 23:59:59.999999"
            cl.append("ch.at <= ?"); pr.append(date_to)
        where = "WHERE " + " AND ".join(cl) if cl else ""
        total = self._rq(f"SELECT COUNT(*) {base} {where}", pr).fetchone()[0]
        rows = self._rq(f"""SELECT ch.entity_type,ch.entity_id,ch.status,ch.at,ch.msg_no,ch.delivery_status,ch.released_at,ch.failed_at,ch.error_message,
            z.zlan_ip, z.zlan_label
            {base} {where} ORDER BY ch.at DESC, ch.id DESC LIMIT ? OFFSET ?""", pr+[limit,offset]).fetchall()
        items = [{"entity_type":r[0],"entity_id":r[1],"status":r[2],"at":r[3],"msg_no":r[4],
                   "delivery_status":r[5],"released_at":r[6],"failed_at":r[7],"error_message":r[8],
                   "zlan_ip":r[9],"zlan_label":r[10]} for r in rows]
        return items, total

    def dashboard_stats(self):
        """Message counts for the dashboard (overall and per ZLAN) from ONE scan of history, cached.
        A big history (hundreds of thousands of rows) makes this slow, so the recompute interval grows with the
        time it took (about 5% of one core at most); a thread that arrives while another is computing gets the
        previous numbers instead of piling on."""
        c = self._stats
        if c and time.time() < c["until"]:
            return c["value"]
        if not self._stats_lock.acquire(blocking=False):
            if c: return c["value"]
            with self._stats_lock:                      # very first call: wait for the other thread's result
                return self._stats["value"] if self._stats else self._compute_stats()
        try:
            return self._compute_stats()
        finally:
            self._stats_lock.release()

    def _compute_stats(self):
        t0 = time.time()
        hist = self._rq("SELECT meter_code, status, COUNT(*) FROM history GROUP BY meter_code, status").fetchall()
        zlan_of = dict(self._rq("SELECT meter_code, zlan_ip FROM meters").fetchall())
        queued_rows = self._rq("SELECT meter_code, COUNT(*) FROM queue GROUP BY meter_code").fetchall()
        blank = lambda: {"queued": 0, "released": 0, "failed": 0}
        released = failed = total = queued = 0
        per_zlan = {}
        for meter, status, n in hist:
            total += n
            if status == "released": released += n
            elif status == "failed": failed += n
            z = zlan_of.get(meter)
            if z and status in ("released", "failed"):
                per_zlan.setdefault(z, blank())[status] += n
        for meter, n in queued_rows:
            queued += n
            z = zlan_of.get(meter)
            if z: per_zlan.setdefault(z, blank())["queued"] += n
        value = {"counts": {"queued": queued, "released": released, "failed": failed, "total": total}, "per_zlan": per_zlan}
        took = time.time() - t0
        self._stats = {"value": value, "until": time.time() + min(120.0, max(2.0, took * 20))}
        return value

    def counts(self):
        return self.dashboard_stats()["counts"]

    def per_zlan_message_counts(self):
        return self.dashboard_stats()["per_zlan"]

    def mark_meter_result(self, meter_code, ok: bool, cascade: bool = False):
        """cascade=True: a ZLAN-level failure marking a meter that wasn't
        actually due this tick -- update status/log only, don't touch
        consecutive_fail or last_poll_at (those reflect real poll attempts)."""
        prev = self._q("SELECT last_status FROM meters WHERE meter_code=?", (meter_code,)).fetchone()
        prev_status = prev[0] if prev else "unknown"
        new_status = "ok" if ok else "fail"
        with self.lock:
            if cascade:
                self._q("UPDATE meters SET last_status=? WHERE meter_code=?", (new_status, meter_code))
            elif ok:
                self._q("UPDATE meters SET last_poll_at=?, last_status='ok', consecutive_fail=0 WHERE meter_code=?", (now_s(), meter_code))
            else:
                self._q("UPDATE meters SET last_poll_at=?, last_status='fail', consecutive_fail=consecutive_fail+1 WHERE meter_code=?", (now_s(), meter_code))
            self.c.commit()
        if new_status != prev_status:
            msg_no, at = self.log_connectivity("meter", meter_code, "connected" if ok else "disconnected")
            handle_connectivity("meter", meter_code, "connected" if ok else "disconnected", msg_no, at)

    def update_zlan_status(self, zlan_ip, connected: bool):
        prev = self._q("SELECT last_status FROM zlans WHERE zlan_ip=?", (zlan_ip,)).fetchone()
        prev_status = prev[0] if prev else "unknown"
        new_status = "ok" if connected else "fail"
        if new_status == prev_status:
            return
        with self.lock:
            self._q("UPDATE zlans SET last_status=? WHERE zlan_ip=?", (new_status, zlan_ip))
            self.c.commit()
        msg_no, at = self.log_connectivity("zlan", zlan_ip, "connected" if connected else "disconnected")
        handle_connectivity("zlan", zlan_ip, "connected" if connected else "disconnected", msg_no, at)

    # -- Parameters --
    def list_params(self, active_only=True):
        q = "SELECT label,address,wire_code,priority,min_value,max_value,active,poll_interval,publish_interval FROM parameters"
        if active_only: q += " WHERE active=1"
        q += " ORDER BY priority ASC, label"
        rows = self._q(q).fetchall()
        return [{"label":r[0],"address":r[1],"wire_code":r[2] or r[0],"priority":r[3],
                  "min_value":r[4],"max_value":r[5],"active":bool(r[6]),
                  "poll_interval":r[7],"publish_interval":r[8]} for r in rows]

    def add_param(self, label, address, wire_code=None, priority=100, min_value=None, max_value=None,
                  poll_interval=None, publish_interval=None):
        existing = self.list_params(active_only=False)
        if any(p["label"] == label for p in existing): return f"Parameter '{label}' already exists."
        for p in existing:
            if address < p["address"] + 2 and p["address"] < address + 2:
                return f"Address overlaps with '{p['label']}'."
        with self.lock:
            self._q("INSERT INTO parameters(label,address,wire_code,priority,min_value,max_value,active,poll_interval,publish_interval) "
                    "VALUES(?,?,?,?,?,?,1,?,?)",
                (label, address, wire_code, priority, min_value, max_value, poll_interval, publish_interval))
            self.c.commit()
        return None

    def update_param_intervals(self, label, poll_interval, publish_interval):
        """Minutes, or None to fall back (poll: every meter cycle; publish: meter / ZLAN default)."""
        with self.lock:
            cur = self._q("UPDATE parameters SET poll_interval=?, publish_interval=? WHERE label=?",
                          (poll_interval, publish_interval, label))
            self.c.commit()
        return cur.rowcount > 0

    def delete_param(self, label):
        with self.lock:
            cur = self._q("DELETE FROM parameters WHERE label=?", (label,)); self.c.commit()
        return cur.rowcount > 0

    def validate_range(self, label, value):
        p = self._q("SELECT min_value,max_value FROM parameters WHERE label=?", (label,)).fetchone()
        if not p: return True
        mn, mx = p
        if mn is not None and value < mn: return False
        if mx is not None and value > mx: return False
        return True

    def log_invalid(self, meter_code, parameter, value, reason):
        with self.lock:
            self._q("INSERT INTO invalid_log(meter_code,parameter,value,reason,at) VALUES(?,?,?,?,?)",
                (meter_code, parameter, value, reason, now_s()))
            self.c.commit()

    # -- msg_no --
    def next_msg_no(self, meter_code, parameter, dt: datetime):
        day = dt.strftime("%Y%m%d")
        with self.lock:
            r = self._q("SELECT counter FROM msg_counters WHERE meter_code=? AND parameter=? AND day=?",
                (meter_code, parameter, day)).fetchone()
            n = (r[0] if r else 0) + 1
            self._q("INSERT INTO msg_counters VALUES(?,?,?,?) ON CONFLICT(meter_code,parameter,day) DO UPDATE SET counter=excluded.counter",
                (meter_code, parameter, day, n))
            self.c.commit()
        return f"{meter_code}_{parameter}_{dt.strftime('%Y%m%d%H%M%S')}_{n}"

    # -- queue / history (Sec 4) --
    def queue_count(self):
        return self._q("SELECT COUNT(*) FROM queue").fetchone()[0]

    def enqueue(self, meter_code, parameter, value, msg_no=None, dt=None, awaiting=False):
        dropped_msg = None
        dt = dt or datetime.now()
        msg_no = msg_no or self.next_msg_no(meter_code, parameter, dt)
        date_time = dt.strftime("%Y-%m-%d %H:%M:%S")
        created = now_ms()
        with self.lock:
            # Sec 8.1: cap enforcement, drop oldest queued if at max
            max_q = int(self.get("max_queue_size") or 5000)
            if self.queue_count() >= max_q:
                oldest = self._q("SELECT id,meter_code,parameter,value,msg_no FROM queue ORDER BY created_at ASC LIMIT 1").fetchone()
                if oldest:
                    self._q("DELETE FROM queue WHERE id=?", (oldest[0],))
                    self._q("INSERT INTO dropped_log(meter_code,parameter,value,msg_no,at) VALUES(?,?,?,?,?)",
                        (oldest[1], oldest[2], oldest[3], oldest[4], now_s()))
                    self._q("UPDATE history SET status='dropped' WHERE msg_no=?", (oldest[4],))
                    dropped_msg = oldest[4]
            self._q("INSERT INTO queue(meter_code,parameter,value,date_time,msg_no,created_at,awaiting_response) VALUES(?,?,?,?,?,?,?)",
                (meter_code, parameter, value, date_time, msg_no, created, created if awaiting else None))
            self._q("INSERT INTO history(meter_code,parameter,value,date_time,msg_no,status,created_at) VALUES(?,?,?,?,?,'queued',?)",
                (meter_code, parameter, value, date_time, msg_no, created))
            self.c.commit()
        if dropped_msg: self.backup.record("acks", dropped_msg, "dropped", "queue full (max_queue_size)")
        self.backup.record("readings", date_time, meter_code, parameter, value, msg_no, "queued")
        return msg_no

    def log_direct_release(self, meter_code, parameter, value, msg_no=None, dt=None):
        dt = dt or datetime.now()
        msg_no = msg_no or self.next_msg_no(meter_code, parameter, dt)
        date_time = dt.strftime("%Y-%m-%d %H:%M:%S")
        ts = now_ms()
        with self.lock:
            self._q("INSERT INTO history(meter_code,parameter,value,date_time,msg_no,status,created_at,released_at) VALUES(?,?,?,?,?,'released',?,?)",
                (meter_code, parameter, value, date_time, msg_no, ts, ts))
            self.c.commit()
        self.backup.record("readings", date_time, meter_code, parameter, value, msg_no, "released")
        return msg_no

    def oldest_queued(self, skip_awaiting=True):
        clause = "AND q.awaiting_response IS NULL" if skip_awaiting else ""
        r = self._q(f"""SELECT q.id,q.meter_code,q.parameter,q.value,q.msg_no,q.date_time,q.attempts
            FROM queue q LEFT JOIN parameters p ON p.label=q.parameter WHERE 1=1 {clause}
            ORDER BY COALESCE(p.priority,100) ASC, q.created_at ASC LIMIT 1""").fetchone()
        if not r: return None
        return {"id":r[0],"meter_code":r[1],"parameter":r[2],"value":r[3],"msg_no":r[4],"date_time":r[5],"attempts":r[6]}

    def mark_attempt(self, row_id):
        with self.lock:
            self._q("UPDATE queue SET attempts=attempts+1 WHERE id=?", (row_id,)); self.c.commit()

    def mark_awaiting(self, row_id):
        with self.lock:
            self._q("UPDATE queue SET awaiting_response=? WHERE id=?", (now_ms(), row_id)); self.c.commit()

    def clear_stale_awaiting(self, timeout_s):
        cutoff = (datetime.now() - timedelta(seconds=timeout_s)).strftime("%Y-%m-%d %H:%M:%S.%f")
        with self.lock:
            rows = self._q("SELECT id,msg_no FROM queue WHERE awaiting_response IS NOT NULL AND awaiting_response < ?", (cutoff,)).fetchall()
            for rid, _ in rows:
                self._q("UPDATE queue SET attempts=attempts+1, awaiting_response=NULL WHERE id=?", (rid,))
            self.c.commit()
        return rows

    def release(self, msg_no, message=""):
        with self.lock:
            self._q("DELETE FROM queue WHERE msg_no=?", (msg_no,))
            self._q("UPDATE history SET status='released', released_at=?, error_message=? WHERE msg_no=?",
                            (now_ms(), message or None, msg_no))
            self.c.commit()
        self.backup.record("acks", msg_no, "released", message or "")

    def fail_attempt(self, msg_no, error=""):
        max_a = int(self.get("max_attempts") or 5)
        with self.lock:
            r = self._q("SELECT id,attempts FROM queue WHERE msg_no=?", (msg_no,)).fetchone()
            if not r: return "gone"
            rid, attempts = r
            if attempts >= max_a:
                self._q("DELETE FROM queue WHERE id=?", (rid,))
                self._q("UPDATE history SET status='failed', failed_at=?, error_message=? WHERE msg_no=?", (now_ms(), error, msg_no))
                self.c.commit()
                self.backup.record("acks", msg_no, "failed", error or "")
                return "failed"
            self._q("UPDATE queue SET awaiting_response=NULL WHERE id=?", (rid,)); self.c.commit()
            return "requeued"

    def retry_failed(self, msg_no):
        r = self._q("SELECT meter_code,parameter,value,date_time FROM history WHERE msg_no=? AND status='failed'", (msg_no,)).fetchone()
        if not r: return None
        with self.lock:
            self._q("INSERT INTO queue(meter_code,parameter,value,date_time,msg_no,created_at) VALUES(?,?,?,?,?,?)",
                (r[0], r[1], r[2], r[3], msg_no, now_ms()))
            self._q("UPDATE history SET status='queued', failed_at=NULL, error_message=NULL WHERE msg_no=?", (msg_no,))
            self.c.commit()
        return msg_no

    def retry_all_failed(self):
        rows = self._q("SELECT msg_no FROM history WHERE status='failed'").fetchall()
        n = 0
        for (msg_no,) in rows:
            if self.retry_failed(msg_no):
                n += 1
        return n

    def queue_page(self, search=None, limit=50, offset=0):
        cl, pr = [], []
        if search: cl.append("(q.meter_code LIKE ? OR q.parameter LIKE ?)"); pr += [f"%{search}%"]*2
        where = "WHERE " + " AND ".join(cl) if cl else ""
        total = self._rq(f"SELECT COUNT(*) FROM queue q {where}", pr).fetchone()[0]
        rows = self._rq(f"""SELECT q.meter_code,q.parameter,q.value,q.date_time,q.msg_no,q.attempts,
            COALESCE(p.priority,100),q.awaiting_response FROM queue q LEFT JOIN parameters p ON p.label=q.parameter {where}
            ORDER BY COALESCE(p.priority,100) ASC, q.created_at ASC LIMIT ? OFFSET ?""", pr+[limit,offset]).fetchall()
        items = [{"meter_code":r[0],"parameter":r[1],"value":r[2],"date_time":r[3],"msg_no":r[4],
                   "attempts":r[5],"priority":r[6],"awaiting_response":bool(r[7])} for r in rows]
        return items, total

    _HISTORY_SORT_COLUMNS = {"meter_code": "meter_code", "parameter": "parameter", "value": "value",
        "date_time": "date_time", "status": "status", "released_at": "released_at", "created_at": "created_at"}

    def history(self, meter_code=None, parameter=None, status=None, search=None, date_from=None, date_to=None,
                limit=100, offset=0, order_by=None, order_dir="desc"):
        cl, pr = [], []
        if meter_code: cl.append("meter_code = ? COLLATE NOCASE"); pr.append(meter_code)
        if parameter: cl.append("parameter = ? COLLATE NOCASE"); pr.append(parameter)
        if status: cl.append("status=?"); pr.append(status)
        if search: cl.append("(meter_code LIKE ? COLLATE NOCASE OR parameter LIKE ? COLLATE NOCASE)"); pr += [f"%{search}%"]*2
        if date_from: cl.append("date_time >= ?"); pr.append(date_from)
        if date_to:
            # A bare "YYYY-MM-DD" date_to must include the whole day -- otherwise
            # every timestamped row (which is all of them) fails the <= check,
            # since "2026-09-07 06:08:28" is lexicographically > "2026-09-07".
            if len(date_to) == 10: date_to = date_to + " 23:59:59.999999"
            cl.append("date_time <= ?"); pr.append(date_to)
        where = "WHERE " + " AND ".join(cl) if cl else ""
        col = self._HISTORY_SORT_COLUMNS.get(order_by, "created_at")
        direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
        total = self._rq(f"SELECT COUNT(*) FROM history {where}", pr).fetchone()[0]
        rows = self._rq(f"""SELECT meter_code,parameter,value,date_time,msg_no,status,created_at,released_at,failed_at,error_message
            FROM history {where} ORDER BY {col} {direction}, id DESC LIMIT ? OFFSET ?""", pr+[limit,offset]).fetchall()
        items = [{"meter_code":r[0],"parameter":r[1],"value":r[2],"date_time":r[3],"msg_no":r[4],"status":r[5],
                   "created_at":r[6],"released_at":r[7],"failed_at":r[8],"error_message":r[9]} for r in rows]
        return items, total

    def purge_expired(self):
        days = int(self.get("retention_days") or 0)
        if days <= 0: return 0
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            cur = self._q("DELETE FROM history WHERE created_at < ?", (cutoff,))
            self.c.commit()
        return cur.rowcount

    def purge_stale_queue(self):
        """Unlike max_queue_size (a count cap that drops the oldest item
        when full), this is a time-based cleanup: a queue item stuck for
        too long (repeatedly failing, or awaiting a response that never
        comes) is removed and marked 'failed' in history, rather than
        retrying forever."""
        days = int(self.get("queue_retention_days") or 0)
        if days <= 0: return 0
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S.%f")
        with self.lock:
            rows = self._q("SELECT msg_no FROM queue WHERE created_at < ?", (cutoff,)).fetchall()
            for (msg_no,) in rows:
                self._q("DELETE FROM queue WHERE msg_no=?", (msg_no,))
                self._q("UPDATE history SET status='failed', failed_at=?, error_message=? WHERE msg_no=?",
                    (now_ms(), f"Purged: exceeded queue retention ({days} days)", msg_no))
            self.c.commit()
        for (msg_no,) in rows:
            self.backup.record("acks", msg_no, "failed", f"Purged: exceeded queue retention ({days} days)")
        return len(rows)

    def purge_expired_connectivity(self):
        days = int(self.get("connectivity_retention_days") or 0)
        if days <= 0: return 0
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            cur = self._q("DELETE FROM connectivity_history WHERE at < ?", (cutoff,))
            self.c.commit()
        return cur.rowcount

    # -- backup / restore (Sec 2.7) --
    def export_config(self):
        return {
            "zlans": self.list_zlans(), "meters": self.list_meters(),
            "parameters": self.list_params(active_only=False), "settings": self.settings_dict(),
        }

    def import_config(self, data):
        with self.lock:
            for z in data.get("zlans", []):
                self._q("INSERT OR REPLACE INTO zlans(zlan_ip,zlan_label,default_polling_interval,port,last_status) VALUES(?,?,?,?,?)",
                    (z["zlan_ip"], z["zlan_label"], z["default_polling_interval"], z.get("port",502), z.get("last_status","unknown")))
            for m in data.get("meters", []):
                self._q("""INSERT OR REPLACE INTO meters(meter_code,zlan_ip,unit_id,polling_interval,active,registered_at)
                    VALUES(?,?,?,?,?,?)""", (m["meter_code"], m["zlan_ip"], m["unit_id"], m.get("polling_interval"),
                    1 if m.get("active",True) else 0, m.get("registered_at", now_s())))
            for p in data.get("parameters", []):
                self._q("INSERT OR REPLACE INTO parameters(label,address,wire_code,priority,min_value,max_value,active,poll_interval,publish_interval) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                    (p["label"], p["address"], p.get("wire_code"), p.get("priority",100),
                     p.get("min_value"), p.get("max_value"), 1 if p.get("active",True) else 0,
                     p.get("poll_interval"), p.get("publish_interval")))
            for k, v in data.get("settings", {}).items():
                self._q("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
            self.c.commit()

    def reset_settings_only(self):
        """Restores every setting to its default EXCEPT login credentials --
        this is the lightweight reset, not the destructive factory reset."""
        with self.lock:
            for k, v in DEFAULT_SETTINGS.items():
                if k in ("auth_user", "auth_hash", "auth_salt"):
                    continue
                self._q("UPDATE settings SET value=? WHERE key=?", (v, k))
            self.c.commit()

    def factory_reset(self):
        """Wipes every table, including login credentials -- the app will
        show the initial 'set up password' screen again after restart."""
        with self.lock:
            for t in ["zlans", "meters", "parameters", "queue", "history", "invalid_log",
                      "dropped_log", "msg_counters", "connectivity_history", "connectivity_queue", "skipped_log", "publish_state"]:
                self._q(f"DELETE FROM {t}")
            self._q("DELETE FROM settings")
            for k, v in DEFAULT_SETTINGS.items():
                self._q("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))
            self.c.commit()

    def delete_history(self, mode, msg_nos=None, before=None):
        with self.lock:
            if mode == "all":
                cur = self._q("DELETE FROM history")
            elif mode == "selected" and msg_nos:
                q = f"DELETE FROM history WHERE msg_no IN ({','.join(['?']*len(msg_nos))})"
                cur = self._q(q, msg_nos)
            elif mode == "before" and before:
                cur = self._q("DELETE FROM history WHERE created_at < ?", (before,))
            else:
                return 0
            self.c.commit()
        return cur.rowcount

    def delete_connectivity_history(self, mode, msg_nos=None, before=None):
        with self.lock:
            if mode == "all":
                cur = self._q("DELETE FROM connectivity_history")
            elif mode == "selected" and msg_nos:
                q = f"DELETE FROM connectivity_history WHERE msg_no IN ({','.join(['?']*len(msg_nos))})"
                cur = self._q(q, msg_nos)
            elif mode == "before" and before:
                cur = self._q("DELETE FROM connectivity_history WHERE at < ?", (before,))
            else:
                return 0
            self.c.commit()
        return cur.rowcount

store = Store(DB_PATH)

# ==================================================================== EVENTS
import queue as _q
class Bus:
    def __init__(self): self.subs=[]; self.lock=threading.Lock()
    def subscribe(self):
        q=_q.Queue(maxsize=500)
        with self.lock: self.subs.append(q)
        return q
    def unsubscribe(self,q):
        with self.lock:
            if q in self.subs: self.subs.remove(q)
    def publish(self, ch, payload):
        ev={"channel":ch,"payload":payload}
        with self.lock: subs=list(self.subs)
        for q in subs:
            try: q.put_nowait(ev)
            except _q.Full:
                try: q.get_nowait(); q.put_nowait(ev)
                except _q.Empty: pass
bus = Bus()

# ==================================================================== OUTBOUND (Sec 6)
class Outbound:
    """HTTP: synchronous, status code = confirmation.
    MQTT: publish, then genuinely wait for the response topic (Sec 6, 6.2)
    -- matches the real production ack pattern (status_code 200/500),
    not just a broker-accept shortcut."""
    def __init__(self):
        self.lock = threading.Lock()
        self.session = requests.Session()
        self.mqtt_client = None
        self.mqtt_target = None
        self.mqtt_sub_topic = None
        self.pending = {}   # msg_no -> True (awaiting)
        self.pending_lock = threading.Lock()
        self.last_connected = None  # None = unknown yet, True/False once we've actually tried

    def _payload(self, meter_code, parameter, value, msg_no, date_time):
        params = {p["label"]: p["wire_code"] for p in store.list_params(active_only=False)}
        wire = params.get(parameter, parameter)
        return {"data":{"date_time":date_time,"meter_code":meter_code,"parameter":wire,"value":value,"msg_no":msg_no}}

    def send(self, meter_code, parameter, value, msg_no, date_time, payload_override=None, topic_kind="reading", topic_override=None):
        """Returns 'confirmed' | 'failed' | 'pending'."""
        cfg = store.settings_dict()
        if cfg.get("server_enabled") != "true":
            return "failed"
        payload = payload_override if payload_override is not None else self._payload(meter_code, parameter, value, msg_no, date_time)
        if cfg.get("connection_type") == "mqtt":
            return self._send_mqtt(cfg, payload, msg_no, meter_code, parameter, topic_kind=topic_kind, topic_override=topic_override)
        return self._send_http(cfg, payload)

    def _send_http(self, cfg, payload):
        url = cfg.get("server_url")
        if not url: return "failed"
        try:
            r = self.session.post(url, json=payload, timeout=5)
            ok = 200 <= r.status_code < 300
            self.last_connected = ok; bus.publish("server_status", {"connected": ok})
            return "confirmed" if ok else "failed"
        except requests.RequestException:
            self.last_connected = False; bus.publish("server_status", {"connected": False})
            return "failed"

    def _ensure_mqtt(self, cfg):
        broker, port = cfg.get("mqtt_broker",""), int(cfg.get("mqtt_port") or 1883)
        resp_topic = cfg.get("mqtt_response_topic","")
        username, password = cfg.get("mqtt_username",""), cfg.get("mqtt_password","")
        if not broker: return False
        target = (broker, port, username, password)   # a changed login must also trigger a reconnect
        if self.mqtt_client is None or self.mqtt_target != target:
            if self.mqtt_client:
                try: self.mqtt_client.loop_stop(); self.mqtt_client.disconnect()
                except Exception: pass
            cid = f"bridge-{int(time.time())}"
            self.mqtt_client = (mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
                                 if hasattr(mqtt,"CallbackAPIVersion") else mqtt.Client(client_id=cid))
            if username:
                self.mqtt_client.username_pw_set(username, password or None)
            self.mqtt_client.on_message = self._on_response
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_target = target
        if not self.mqtt_client.is_connected():
            try:
                self.mqtt_client.connect(broker, port, keepalive=30)
                self.mqtt_client.loop_start()
                time.sleep(0.5)
            except Exception:
                log.exception("MQTT connect() raised for %s:%s", broker, port)
                return False
            if not self.mqtt_client.is_connected():
                log.warning("MQTT connect to %s:%s did not succeed (check broker address, "
                                "port, and username/password if the broker requires auth)", broker, port)
                return False
        if resp_topic and self.mqtt_sub_topic != resp_topic:
            result = self.mqtt_client.subscribe(resp_topic, qos=1)
            log.info("MQTT subscribe(%s) called, result=%s", resp_topic, result)
            self.mqtt_sub_topic = resp_topic
        return self.mqtt_client.is_connected()

    def reconfigure(self):
        """Called after the Output settings were saved: drop the live MQTT client so the next
        connection uses the saved broker / login / topics, and reconnect right away."""
        cfg = store.settings_dict()
        with self.lock:
            if self.mqtt_client is not None:
                try: self.mqtt_client.loop_stop(); self.mqtt_client.disconnect()
                except Exception: pass
            self.mqtt_client = None; self.mqtt_target = None; self.mqtt_sub_topic = None
            self.last_connected = None
            with self.pending_lock: self.pending.clear()      # unanswered ones time out and are retried
            if cfg.get("server_enabled") == "true" and cfg.get("connection_type") == "mqtt":
                try: self._ensure_mqtt(cfg)
                except Exception: log.exception("MQTT reconnect after settings change failed")
        bus.publish("server_status", {"connected": bool(self.last_connected)})

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0 or str(reason_code) == "Success":
            log.info("MQTT connected successfully")
            self.last_connected = True
            # A fresh connection (first time, or after a drop/reconnect)
            # has no subscriptions yet -- MQTT doesn't carry them over.
            # Resetting here, at the actual moment a connection is
            # confirmed, avoids the race of doing this in the
            # synchronous connect() code, which can run slightly
            # ahead of this async confirmation and skip the reset.
            self.mqtt_sub_topic = None
            bus.publish("server_status", {"connected": True})
        else:
            log.warning("MQTT connection REJECTED by broker -- reason: %s "
                            "(commonly: wrong username/password, or client not authorized)", reason_code)
            self.last_connected = False
            bus.publish("server_status", {"connected": False})

    def _on_mqtt_disconnect(self, client, userdata, *args):
        log.warning("MQTT disconnected: %s", args)
        self.last_connected = False
        bus.publish("server_status", {"connected": False})

    def _on_response(self, client, userdata, msg):
        """Real ack: payload.data.data.status_code / msg_no (matches SS9)."""
        log.info("MQTT message received on %s: %s", msg.topic, msg.payload[:300])
        try:
            body = json.loads(msg.payload)
            d = body.get("data", {}).get("data", body.get("data", {}))
            status_code = str(d.get("status_code",""))
            msg_no = d.get("msg_no")
            message = d.get("message", "")  # Laravel sends a reason even on status_code=200,
                                             # e.g. "Meter Doesn't Exist!" -- surfaced in History either way
        except Exception:
            return
        if not msg_no: return
        with self.pending_lock:
            self.pending.pop(msg_no, None)
        if status_code == "200":
            result = store.resolve_ack(msg_no, True, message=message)
            bus.publish("released", {"msg_no": msg_no})
        else:
            result = store.resolve_ack(msg_no, False, message=message or f"status_code={status_code}")
            bus.publish("attempt_failed", {"msg_no": msg_no, "result": result})

    def _topic_meter_id(self, meter_code: str) -> str:
        """Topic uses the numeric portion only, M stripped, leading
        zeros stripped: M001 -> 1, M010 -> 10, M100 -> 100. The payload
        itself still carries the full meter_code unchanged -- this only
        affects the MQTT topic string."""
        m = re.search(r"(\d+)", meter_code)
        return str(int(m.group(1))) if m else meter_code

    def _send_mqtt(self, cfg, payload, msg_no, meter_code, parameter_label, topic_kind="reading", topic_override=None):
        if not self._ensure_mqtt(cfg):
            self.last_connected = False; bus.publish("server_status", {"connected": False})
            return "failed"
        try:
            if topic_override:
                topic = topic_override
            elif topic_kind == "connectivity":
                topic_tmpl = cfg.get("mqtt_connectivity_topic_template") or "ksb/connectivity/{entity_id}"
                topic = topic_tmpl.format(entity_id=meter_code)
            else:
                topic_tmpl = cfg.get("mqtt_topic_template") or "ksb/meter{meter_code}/{label}"
                topic = topic_tmpl.format(meter_code=self._topic_meter_id(meter_code), label=parameter_label)
        except (KeyError, IndexError, ValueError):
            # a mistyped placeholder must not look like a dead meter -- fail the send, say why
            log.error("Invalid MQTT topic template -- fix it on the Output tab (placeholders: {meter_code} {label} {entity_id})")
            return "failed"
        # Real server (Laravel) reads $msg->data->data->*, one level
        # deeper than HTTP mode uses -- MQTT-only wrapping.
        mqtt_payload = {"data": payload}
        try:
            info = self.mqtt_client.publish(topic, json.dumps(mqtt_payload), qos=1)
            info.wait_for_publish(timeout=5)
            if not info.is_published():
                self.last_connected = False; bus.publish("server_status", {"connected": False})
                return "failed"
            self.last_connected = True; bus.publish("server_status", {"connected": True})
            if cfg.get("mqtt_response_topic"):
                with self.pending_lock: self.pending[msg_no] = True
                return "pending"
            return "confirmed"  # no response topic configured -> broker-ack is the confirmation
        except Exception:
            self.last_connected = False; bus.publish("server_status", {"connected": False})
            return "failed"

outbound = Outbound()

# ==================================================================== INBOUND POLLING (Sec 1, 2, 5)
# This app is a Modbus TCP CLIENT -- actively polls each ZLAN, one meter
# at a time (RS-485 behind the ZLAN is half-duplex), respecting each
# meter's own polling_interval. Different ZLANs poll independently/parallel.

_next_due: Dict[str, datetime] = {}   # meter_code -> next scheduled poll time
_param_due: Dict[tuple, datetime] = {}  # (meter_code, label) -> next time that parameter should be read

def due_params(meter_code, params, now=None):
    """Parameters to read in this poll cycle of a meter. A parameter with its
    own poll_interval (minutes) is read only when that has elapsed since its
    last successful read; one without is read every cycle. The interval acts
    in steps of the meter's own poll cycle (e.g. MD 30 min on a 15-min meter
    -> every second cycle)."""
    now = now or datetime.now()
    out = []
    for p in params:
        if not p.get("poll_interval"):
            out.append(p); continue
        due = _param_due.get((meter_code, p["label"]))
        if due is None or now >= due:
            out.append(p)
    return out
_zlan_threads: Dict[str, threading.Thread] = {}

def handle_reading(meter_code, label, value):
    """A validated reading is ready to send -- direct-send-first,
    fall back to queue/pending on failure (Sec 4). If publish throttling
    is enabled, a reading is dropped (optionally logged) when the same
    meter+parameter was published less than its publish interval ago --
    interval = parameter, else meter, else ZLAN default. Every reading that
    IS published is stored in history and in the CSV backup."""
    if not store.validate_range(label, value):
        store.log_invalid(meter_code, label, value, "out of configured range")
        bus.publish("invalid", {"meter_code": meter_code, "parameter": label, "value": value})
        return
    cfg = store.settings_dict()
    if cfg.get("publish_throttle_enabled") == "true" and not store.check_publish_throttle(meter_code, label):
        if cfg.get("log_skipped_events") == "true":
            store.log_skipped(meter_code, label, value, "publish interval not elapsed")
        bus.publish("skipped", {"meter_code": meter_code, "parameter": label, "value": value})
        return
    now = datetime.now()
    msg_no = store.next_msg_no(meter_code, label, now)
    date_time = now.strftime("%Y-%m-%d %H:%M:%S")
    bus.publish("reading", {"meter_code": meter_code, "parameter": label, "value": value})
    if cfg.get("publish_throttle_enabled") == "true":
        store.mark_published(meter_code, label, now)
    result = outbound.send(meter_code, label, value, msg_no, date_time)
    if result == "confirmed":
        store.log_direct_release(meter_code, label, value, msg_no=msg_no, dt=now)
        bus.publish("released", {"meter_code": meter_code, "parameter": label, "value": value})
    elif result == "pending":
        store.enqueue(meter_code, label, value, msg_no=msg_no, dt=now, awaiting=True)
    else:
        store.enqueue(meter_code, label, value, msg_no=msg_no, dt=now)

def handle_connectivity(entity_type, entity_id, status, msg_no, at):
    """A connectivity change is ready to send -- same direct-send-first,
    fall back to connectivity_queue/retry on failure, as meter readings.
    If debounce is enabled, the local history row (already written by
    log_connectivity) stays as the full audit trail regardless -- only
    the outbound push is skipped when within the minimum gap.

    Topic shows the ZLAN-meter relationship directly:
      zlan event  -> ksb/connectivity/{zlan_label}
      meter event -> ksb/connectivity/{zlan_label}/{meter_code}"""
    cfg = store.settings_dict()
    if cfg.get("connectivity_debounce_enabled") == "true":
        gap = int(cfg.get("mqtt_connectivity_min_gap_seconds") or 60)
        if not store.check_connectivity_gap(entity_type, entity_id, gap):
            store.mark_connectivity_skipped(msg_no)
            return
    # Always record when a signal actually goes out, regardless of whether
    # the debounce feature itself is on -- the heartbeat feature needs a
    # reliable "last sent" timestamp to check staleness against, even when
    # debounce has never been enabled.
    store.mark_connectivity_sent(entity_type, entity_id)
    payload = {"data": {"entity_type": entity_type, "entity_id": entity_id, "status": status, "at": at, "msg_no": msg_no}}
    if entity_type == "zlan":
        topic = f"ksb/connectivity/{store.get_zlan_label(entity_id)}"
    else:
        topic = f"ksb/connectivity/{store.get_zlan_label_for_meter(entity_id)}/{entity_id}"
    result = outbound.send(entity_id, entity_type, None, msg_no, at, payload_override=payload, topic_kind="connectivity", topic_override=topic)
    if result == "confirmed":
        store.conn_release(msg_no)
        bus.publish("released", {"msg_no": msg_no})
    elif result == "pending":
        store.conn_enqueue(msg_no, entity_type, entity_id, status, at, awaiting=True)
    else:
        store.conn_enqueue(msg_no, entity_type, entity_id, status, at)

def poll_zlan_forever(zlan_ip):
    client = None
    while True:
        zlans = {z["zlan_ip"]: z for z in store.list_zlans()}
        if zlan_ip not in zlans:
            if client: client.close()
            return  # ZLAN removed -- this worker exits cleanly
        zlan = zlans[zlan_ip]
        meters = [m for m in store.list_meters(zlan_ip=zlan_ip, active_only=True)]
        params = store.list_params(active_only=True)

        if client is None:
            client = ModbusTcpClient(zlan_ip, port=zlan["port"], timeout=float(store.get("modbus_timeout") or 4.5))

        due_now = []
        for m in meters:
            interval = m["polling_interval"] or zlan["default_polling_interval"]
            due = _next_due.get(m["meter_code"])
            if due is None or datetime.now() >= due:
                due_now.append((m, interval))

        if not due_now:
            time.sleep(1)
            continue

        if not client.connected:
            ok = client.connect()
            if not ok:
                # ZLAN-level failure (Sec 5.1): whole gateway unreachable -- mark ALL its meters
                store.update_zlan_status(zlan_ip, False)
                for m in meters:  # ALL meters on this ZLAN, not just due_now, for connectivity history
                    is_due = any(m["meter_code"] == dm["meter_code"] for dm, _ in due_now)
                    store.mark_meter_result(m["meter_code"], False, cascade=not is_due)
                for m, interval in due_now:
                    _next_due[m["meter_code"]] = datetime.now() + timedelta(minutes=interval)
                bus.publish("zlan_status", {"zlan_ip": zlan_ip, "connected": False})
                time.sleep(2)
                continue
        store.update_zlan_status(zlan_ip, True)
        bus.publish("zlan_status", {"zlan_ip": zlan_ip, "connected": True})

        for m, interval in due_now:
            if client is None:
                # Connection died from another meter's failure earlier this
                # same cycle -- mark this one failed too rather than attempt
                # a doomed read; the next full loop iteration will make a
                # genuine fresh client.connect() attempt for everyone.
                store.mark_meter_result(m["meter_code"], False)
                _next_due[m["meter_code"]] = datetime.now() + timedelta(minutes=interval)
                continue
            m_params = due_params(m["meter_code"], params)
            if not m_params:
                # nothing due for this meter in this cycle (e.g. only MD, which is
                # polled less often) -- no Modbus traffic, no status change
                _next_due[m["meter_code"]] = datetime.now() + timedelta(minutes=interval)
                continue
            groups = group_addresses([{"label": p["label"], "address": p["address"], "count": 2} for p in m_params])
            poll_minutes = {p["label"]: p.get("poll_interval") for p in m_params}
            meter_ok = True
            transport_failure = False
            for g in groups:
                try:
                    r = rr_compat(client, g["start"], g["count"], m["unit_id"])
                    if r.isError():
                        # A well-formed Modbus exception response (wrong unit
                        # id, invalid register address, etc.) -- the TCP
                        # connection itself is still perfectly fine, only
                        # THIS meter's request was rejected. Must not be
                        # confused with a real transport failure below, or
                        # one misconfigured meter poisons every other meter
                        # on the same ZLAN for the rest of this cycle.
                        meter_ok = False
                        break
                    regs = r.registers
                    for mem in g["members"]:
                        hi, lo = regs[mem["offset"]], regs[mem["offset"] + 1]
                        value = round(decode_f32(hi, lo), 2)
                        if poll_minutes.get(mem["label"]):
                            _param_due[(m["meter_code"], mem["label"])] = datetime.now() + timedelta(minutes=poll_minutes[mem["label"]])
                        handle_reading(m["meter_code"], mem["label"], value)
                except Exception:
                    # A genuine transport-level failure (timeout, connection
                    # reset, socket error) -- the connection itself is dead.
                    meter_ok = False
                    transport_failure = True
                    break
            if transport_failure:
                # Close the connection so the NEXT poll cycle is forced
                # through a real client.connect() attempt. Without this, a
                # stale pymodbus client can keep reporting .connected=True
                # forever even after the remote genuinely drops -- silently
                # blocking both disconnect AND reconnect detection.
                try:
                    client.close()
                except Exception:
                    pass
                client = None
            store.mark_meter_result(m["meter_code"], meter_ok)
            _next_due[m["meter_code"]] = datetime.now() + timedelta(minutes=interval)
            time.sleep(float(store.get("modbus_inter_request_delay") or 6))  # inter-request gap on the shared RS-485 bus, configurable

def zlan_manager_loop():
    while True:
        for z in store.list_zlans():
            ip = z["zlan_ip"]
            th = _zlan_threads.get(ip)
            if th is None or not th.is_alive():
                t = threading.Thread(target=poll_zlan_forever, args=(ip,), daemon=True)
                _zlan_threads[ip] = t
                t.start()
        time.sleep(2)

def retry_loop():
    while True:
        stale = store.clear_stale_awaiting(int(store.get("mqtt_response_timeout") or 15))
        for rid, msg_no in stale:
            store.fail_attempt(msg_no, error="response timeout")
        conn_stale = store.conn_clear_stale_awaiting(int(store.get("mqtt_response_timeout") or 15))
        for rid, msg_no in conn_stale:
            store.conn_fail_attempt(msg_no, error="response timeout")

        cfg = store.settings_dict()
        if cfg.get("server_enabled") == "true":
            row = store.oldest_queued()
            if row:
                store.mark_attempt(row["id"])
                result = outbound.send(row["meter_code"], row["parameter"], row["value"], row["msg_no"], row["date_time"])
                if result == "confirmed":
                    store.release(row["msg_no"])
                    bus.publish("released", {"msg_no": row["msg_no"]})
                elif result == "pending":
                    store.mark_awaiting(row["id"])
                else:
                    r = store.fail_attempt(row["msg_no"], error="send failed")
                    bus.publish("attempt_failed", {"msg_no": row["msg_no"], "result": r})

            conn_row = store.conn_oldest_queued()
            if conn_row:
                store.conn_mark_attempt(conn_row["id"])
                payload = {"data": {"entity_type": conn_row["entity_type"], "entity_id": conn_row["entity_id"],
                                     "status": conn_row["status"], "at": conn_row["at"], "msg_no": conn_row["msg_no"]}}
                if conn_row["entity_type"] == "zlan":
                    conn_topic = f"ksb/connectivity/{store.get_zlan_label(conn_row['entity_id'])}"
                else:
                    conn_topic = f"ksb/connectivity/{store.get_zlan_label_for_meter(conn_row['entity_id'])}/{conn_row['entity_id']}"
                result = outbound.send(conn_row["entity_id"], conn_row["entity_type"], None, conn_row["msg_no"], conn_row["at"],
                                        payload_override=payload, topic_kind="connectivity", topic_override=conn_topic)
                if result == "confirmed":
                    store.conn_release(conn_row["msg_no"])
                    bus.publish("released", {"msg_no": conn_row["msg_no"]})
                elif result == "pending":
                    store.conn_mark_awaiting(conn_row["id"])
                else:
                    r = store.conn_fail_attempt(conn_row["msg_no"], error="send failed")
                    bus.publish("attempt_failed", {"msg_no": conn_row["msg_no"], "result": r})
        time.sleep(RETRY_INTERVAL)

def retention_loop():
    while True:
        try:
            store.purge_stale_queue()
            store.purge_expired()
            store.purge_expired_connectivity()
            store.backup.purge(int(store.get("backup_retention_days") or 0))
        except Exception:
            log.exception("retention cycle failed")  # never let a purge error kill the thread
        time.sleep(3600)

def heartbeat_check_once():
    """One heartbeat check cycle -- separated from the sleep loop below so
    it can be invoked directly (e.g. in tests) without waiting through a
    real sleep interval. Returns the check_minutes value to sleep for next."""
    check_minutes = 15
    try:
        cfg = store.settings_dict()
        check_minutes = float(cfg.get("connectivity_heartbeat_check_minutes") or 15)
        if cfg.get("connectivity_heartbeat_enabled") == "true":
            hours = float(cfg.get("connectivity_heartbeat_hours") or 24)
            for entity in store.stale_connectivity_entities(hours):
                msg_no, at = store.log_connectivity(entity["entity_type"], entity["entity_id"], entity["status"])
                handle_connectivity(entity["entity_type"], entity["entity_id"], entity["status"], msg_no, at)
    except Exception:
        pass  # never let a heartbeat-loop error kill this background thread
    return check_minutes

def heartbeat_loop():
    """Periodically re-confirms status for meters/ZLANs that have gone
    quiet for too long -- purely event-based logging means a connection
    that's been stable (or stuck) for days produces no new signal at all,
    making it impossible to tell 'still fine' from 'silently broken'."""
    while True:
        check_minutes = heartbeat_check_once()
        time.sleep(max(60, check_minutes * 60))

def broker_health_loop():
    """Checks the MQTT broker connection independently of data flow --
    previously the status chip only updated when a reading was actually
    sent, so with no ZLAN data yet it stayed 'unknown' forever even if
    the broker itself was reachable. This keeps it accurate regardless."""
    while True:
        cfg = store.settings_dict()
        if cfg.get("server_enabled") == "true" and cfg.get("connection_type") == "mqtt":
            with outbound.lock:
                outbound._ensure_mqtt(cfg)
        time.sleep(10)

# ==================================================================== OUTPUT CONNECTION: validation + connection test
OUTPUT_KEYS = ("connection_type", "server_url", "mqtt_broker", "mqtt_port", "mqtt_username", "mqtt_password",
               "mqtt_topic_template", "mqtt_response_topic", "mqtt_connectivity_topic_template",
               "mqtt_response_timeout", "server_enabled")
TEST_TIMEOUT = 8.0   # seconds the MQTT broker gets to answer a connection check
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)*$")

def _topic_template_error(tmpl, sample, allowed):
    if not tmpl.strip(): return "Required."
    if tmpl != tmpl.strip(): return "Remove the spaces at the start or end."
    try:
        topic = tmpl.format(**sample)
    except KeyError as e:
        return "Unknown placeholder {%s}. Allowed: %s." % (e.args[0], ", ".join("{%s}" % a for a in allowed))
    except (IndexError, ValueError):
        return "The { } braces do not match. Allowed placeholders: %s." % ", ".join("{%s}" % a for a in allowed)
    if "+" in topic or "#" in topic: return "A publish topic cannot contain + or #."
    return None

def validate_output(cfg, need_all=False):
    """Returns {setting_key: message}. Required fields are only enforced when the output is Enabled
    (or need_all=True, used by the connection test); format rules always apply to whatever is filled in."""
    errors = {}
    ctype = (cfg.get("connection_type") or "http").strip()
    need = need_all or cfg.get("server_enabled") == "true"
    if ctype not in ("http", "mqtt"):
        errors["connection_type"] = "Choose HTTP API or MQTT."
        return errors
    if ctype == "http":
        url = (cfg.get("server_url") or "").strip()
        if not url:
            if need: errors["server_url"] = "Required."
        elif not re.match(r"^https?://[^\s/]+\S*$", url):
            errors["server_url"] = "Must start with http:// or https:// followed by the server address."
        return errors
    broker = (cfg.get("mqtt_broker") or "").strip()
    if not broker:
        if need: errors["mqtt_broker"] = "Required."
    elif "://" in broker or "/" in broker or " " in broker:
        errors["mqtt_broker"] = "Enter only the address (for example 103.224.93.124): no mqtt://, no path, no spaces."
    else:
        try:
            ipaddress.ip_address(broker.strip("[]"))
        except ValueError:
            if broker.count(":") == 1:
                errors["mqtt_broker"] = "Put the port in the Port box, not in the address."
            elif not _HOST_RE.match(broker):
                errors["mqtt_broker"] = "Not a valid IP address or host name."
    port = str(cfg.get("mqtt_port") or "").strip()
    if not port:
        if need: errors["mqtt_port"] = "Required."
    elif not port.isdigit() or not (1 <= int(port) <= 65535):
        errors["mqtt_port"] = "Port must be a whole number from 1 to 65535 (usually 1883)."
    if (cfg.get("mqtt_password") or "") and not (cfg.get("mqtt_username") or "").strip():
        errors["mqtt_username"] = "Required when a password is entered."
    t = cfg.get("mqtt_topic_template") or ""
    if t.strip() or need:
        e = _topic_template_error(t, {"meter_code": "1", "label": "kwh"}, ("meter_code", "label"))
        if e: errors["mqtt_topic_template"] = e
    t = cfg.get("mqtt_connectivity_topic_template") or ""
    if t.strip() or need:
        e = _topic_template_error(t, {"entity_id": "x"}, ("entity_id",))
        if e: errors["mqtt_connectivity_topic_template"] = e
    resp = cfg.get("mqtt_response_topic") or ""
    if resp and resp != resp.strip():
        errors["mqtt_response_topic"] = "Remove the spaces at the start or end."
    timeout = str(cfg.get("mqtt_response_timeout") or "").strip()
    if not timeout:
        if need and resp.strip(): errors["mqtt_response_timeout"] = "Required when a response topic is set."
    elif not timeout.isdigit() or not (1 <= int(timeout) <= 300):
        errors["mqtt_response_timeout"] = "Seconds, from 1 to 300."
    return errors

def _net_error_text(e, host, port):
    """(short problem, what to check) for a failed TCP connection."""
    if isinstance(e, socket.gaierror):
        return (f"Cannot find '{host}'.", "Check the address for typos (no mqtt:// prefix) and that this gateway has DNS / internet access.")
    if isinstance(e, ConnectionRefusedError):
        return (f"{host}:{port} refused the connection.", "Nothing is listening on that port: wrong port, or the broker is stopped.")
    if isinstance(e, (socket.timeout, TimeoutError)):
        return (f"No answer from {host}:{port} (timed out).", "Wrong IP address, or a firewall / the network is blocking port %s." % port)
    if isinstance(e, OSError) and getattr(e, "errno", None) in (101, 113, 51, 65):
        return (f"Cannot reach {host}:{port} (no route).", "Check this gateway's network connection and the IP address.")
    return (f"Could not connect to {host}:{port}: {e}", "Check the address and port.")

def test_mqtt_connection(cfg, timeout=None):
    """Connects to the broker the way the bridge does (same login, same response topic) and reports each step.
    Publishes nothing, uses its own client id, and does not touch the live connection."""
    timeout = timeout or TEST_TIMEOUT
    broker, port = cfg["mqtt_broker"].strip().strip("[]"), int(cfg["mqtt_port"])
    user, pw = cfg.get("mqtt_username", ""), cfg.get("mqtt_password", "")
    resp = (cfg.get("mqtt_response_topic") or "").strip()
    checks, t0 = [], time.time()
    def done(ok, message, hint=None):
        return {"ok": ok, "checks": checks, "message": message, "hint": hint,
                "latency_ms": int((time.time() - t0) * 1000), "target": f"{broker}:{port}"}
    try:
        socket.getaddrinfo(broker, port, type=socket.SOCK_STREAM)
        checks.append({"name": "Address", "ok": True, "text": f"{broker} found"})
    except OSError as e:
        problem, hint = _net_error_text(e, broker, port)
        checks.append({"name": "Address", "ok": False, "text": problem, "hint": hint})
        return done(False, problem, hint)
    connack, suback, state = threading.Event(), threading.Event(), {}
    cid = f"bridge-test-{int(time.time() * 1000) % 10**9}"
    client = (mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
              if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client(client_id=cid))
    if user: client.username_pw_set(user, pw or None)
    def on_connect(c, ud, flags, rc, props=None):
        state["rc"], state["t"] = rc, time.time(); connack.set()
    def on_subscribe(c, ud, mid, granted=None, *a):
        state["granted"] = granted; suback.set()
    client.on_connect, client.on_subscribe = on_connect, on_subscribe
    try:
        try:
            client.connect(broker, port, keepalive=10)
        except Exception as e:
            problem, hint = _net_error_text(e, broker, port)
            checks.append({"name": "Network", "ok": False, "text": problem, "hint": hint})
            return done(False, problem, hint)
        client.loop_start()
        checks.append({"name": "Network", "ok": True, "text": f"Port {port} is open"})
        if not connack.wait(timeout):
            hint = "The port is open but it is not answering like an MQTT broker (wrong port?), or the broker is overloaded."
            checks.append({"name": "Login", "ok": False, "text": f"The broker did not answer within {int(timeout)} s.", "hint": hint})
            return done(False, "The broker did not answer.", hint)
        rc = state["rc"]
        failed = rc.is_failure if hasattr(rc, "is_failure") else (rc != 0)
        if failed:
            hint = ("Check the username and password, or ask the broker administrator to allow this user."
                    if user else "This broker needs a username and password. Ask the broker administrator for them.")
            text = f"The broker rejected the connection: {rc}."
            checks.append({"name": "Login", "ok": False, "text": text, "hint": hint})
            return done(False, text, hint)
        checks.append({"name": "Login", "ok": True,
                       "text": f"Logged in as {user}" if user else "Connected without a login (this broker allows anonymous clients)"})
        if resp:
            client.subscribe(resp, qos=1)
            if not suback.wait(min(timeout, 5)):
                checks.append({"name": "Response topic", "ok": False, "text": f"No answer when listening on {resp}.",
                               "hint": "Delivery confirmations may not arrive. Check the topic name."})
                return done(False, "Could not confirm the response topic.", "Check the response topic name.")
            granted = state.get("granted") or []
            denied = any((g.is_failure if hasattr(g, "is_failure") else (g is not None and int(g) >= 128)) for g in granted)
            if denied:
                hint = "IDMS delivery confirmations will not arrive. Ask the broker administrator to allow this user to subscribe to that topic."
                checks.append({"name": "Response topic", "ok": False, "text": f"The broker does not allow listening on {resp}.", "hint": hint})
                return done(False, "Not allowed to listen on the response topic.", hint)
            checks.append({"name": "Response topic", "ok": True, "text": f"Allowed to listen on {resp}"})
        else:
            checks.append({"name": "Response topic", "ok": True,
                           "text": "No response topic set: a message counts as delivered once the broker accepts it"})
        res = done(True, f"Connected to {broker}:{port}")
        res["latency_ms"] = int((state["t"] - t0) * 1000)
        return res
    finally:
        try: client.disconnect()
        except Exception: pass
        try: client.loop_stop()
        except Exception: pass

def test_http_connection(cfg):
    url = cfg["server_url"].strip()
    checks, t0 = [], time.time()
    def done(ok, message, hint=None):
        return {"ok": ok, "checks": checks, "message": message, "hint": hint,
                "latency_ms": int((time.time() - t0) * 1000), "target": url}
    try:
        r = requests.request("HEAD", url, timeout=5, allow_redirects=False)
    except requests.exceptions.SSLError:
        t, h = "The secure (https) connection failed.", "The server's certificate is not trusted by this gateway, or the address should start with http://."
    except requests.exceptions.Timeout:
        t, h = "No answer from the server (timed out).", "Wrong address, or a firewall / the network is blocking it."
    except requests.exceptions.ConnectionError as e:
        txt = str(e)
        if "Name or service not known" in txt or "Temporary failure in name resolution" in txt or "nodename nor servname" in txt:
            t, h = "Cannot find that server name.", "Check the address for typos and that this gateway has DNS / internet access."
        elif "refused" in txt.lower():
            t, h = "The server refused the connection.", "Nothing is listening on that port: wrong port, or the server is stopped."
        else:
            t, h = "Could not connect to the server.", "Check the address and the gateway's network connection."
    except requests.exceptions.RequestException as e:
        t, h = f"The request failed: {e}", "Check the URL."
    else:
        code = r.status_code
        if code == 404:
            checks.append({"name": "Server", "ok": False, "text": "The server answered, but this URL returns 404 (Not Found).",
                           "hint": "Check the path at the end of the URL."})
            return done(False, "URL not found (404).", "Check the path at the end of the URL.")
        if code >= 500:
            checks.append({"name": "Server", "ok": False, "text": f"The server answered with an error (HTTP {code}).",
                           "hint": "The server is reachable but failing; ask the IDMS developer."})
            return done(False, f"Server error (HTTP {code}).", "Ask the IDMS developer.")
        extra = " It asks for authentication." if code in (401, 403) else ""
        checks.append({"name": "Server", "ok": True, "text": f"Server answered (HTTP {code}).{extra}"})
        return done(True, f"Server reachable (HTTP {code}).")
    checks.append({"name": "Server", "ok": False, "text": t, "hint": h})
    return done(False, t, h)

# ==================================================================== AUTH (Sec 10)
def _hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()

def _sign(value: str) -> str:
    secret = store.get("auth_salt") or "bridge"
    sig = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{value}.{sig}"

def _verify_token(token: str) -> bool:
    if not token or "." not in token: return False
    value, sig = token.rsplit(".", 1)
    return hmac.compare_digest(_sign(value), token) and value == store.get("auth_user")

def require_auth(session: Optional[str]):
    return _verify_token(session or "")

# ==================================================================== WEB APP
@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=zlan_manager_loop, daemon=True).start()
    threading.Thread(target=retry_loop, daemon=True).start()
    threading.Thread(target=retention_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=broker_health_loop, daemon=True).start()
    yield

app = FastAPI(title="Modbus Bridge", lifespan=lifespan)
class RevalidatingStatic(StaticFiles):
    """Browsers must re-check the page scripts with the server every time (a cheap 304 when unchanged).
    Without this a browser could keep running an old app.js against a new index.html after an update."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

app.mount("/static", RevalidatingStatic(directory="static"), name="static")

_PUBLIC_PATHS = {"/", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout"}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Enforces the session cookie on every /api/ route except the auth
    endpoints themselves and the index page (which does its own client-side
    login gate). Previously only individual routes checked auth explicitly --
    this closes the gap where any /api/* call could be made directly
    without ever logging in."""
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if path.startswith("/api/"):
        session = request.cookies.get("session")
        if not require_auth(session):
            return JSONResponse(status_code=401, content={"detail": "Not authenticated."})
    return await call_next(request)

def bad(msg, code=400): return JSONResponse(status_code=code, content={"detail": msg})

@app.get("/", response_class=HTMLResponse)
def index():
    html = open("templates/index.html").read()
    for name in ("style.css", "app.js"):        # the URL changes with every release, so old copies are never reused
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={APP_VERSION}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

# -- auth --
@app.get("/api/auth/status")
def auth_status(session: Optional[str] = Cookie(default=None)):
    has_pw = bool(store.get("auth_hash"))
    return {"configured": has_pw, "logged_in": require_auth(session)}

@app.post("/api/auth/setup")
def auth_setup(payload: dict = Body(...), response: Response = None):
    if store.get("auth_hash"): return bad("Already configured.")
    salt = secrets.token_hex(16)
    store.set("auth_salt", salt)
    store.set("auth_hash", _hash_pw(payload.get("password",""), salt))
    store.set("auth_user", payload.get("username","admin"))
    token = _sign(store.get("auth_user"))
    response.set_cookie("session", token, httponly=True, max_age=3650*86400)
    return {"ok": True}

@app.post("/api/auth/login")
def auth_login(payload: dict = Body(...), response: Response = None):
    salt = store.get("auth_salt")
    if _hash_pw(payload.get("password",""), salt) != store.get("auth_hash") or payload.get("username") != store.get("auth_user"):
        return bad("Invalid credentials.", 401)
    token = _sign(store.get("auth_user"))
    response.set_cookie("session", token, httponly=True, max_age=3650*86400)
    return {"ok": True}

@app.post("/api/auth/logout")
def auth_logout(response: Response):
    # the session cookie is HttpOnly, so only the server can clear it (JavaScript cannot)
    response.delete_cookie("session")
    return {"ok": True}

@app.post("/api/auth/change-password")
def auth_change(payload: dict = Body(...), session: Optional[str] = Cookie(default=None)):
    if not require_auth(session): return bad("Not authenticated.", 401)
    salt = store.get("auth_salt")
    if _hash_pw(payload.get("current",""), salt) != store.get("auth_hash"):
        return bad("Current password incorrect.")
    store.set("auth_hash", _hash_pw(payload.get("new",""), salt))
    return {"ok": True}

# -- System (Settings tab: restart / reset) --
def _delayed_exit():
    time.sleep(0.5)  # give the HTTP response time to actually reach the client first
    os._exit(0)

@app.post("/api/system/restart")
def api_system_restart():
    """Restarts just this app process -- systemd's Restart=always brings
    it back up in a couple seconds. Does not touch the OS/machine."""
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"restarting": True}

@app.post("/api/settings/reset")
def api_reset_settings():
    """Lightweight reset -- settings back to defaults, login/data untouched."""
    store.reset_settings_only()
    return {"ok": True}

@app.post("/api/system/factory-reset")
def api_factory_reset(payload: dict = Body(...), session: Optional[str] = Cookie(default=None)):
    """Destructive: wipes every table including login credentials, then
    restarts the process so it comes back up at the initial setup screen.
    Requires re-entering the current password as an extra confirmation."""
    if not require_auth(session): return bad("Not authenticated.", 401)
    salt = store.get("auth_salt")
    if _hash_pw(payload.get("password", ""), salt) != store.get("auth_hash"):
        return bad("Incorrect password.", 401)
    store.factory_reset()
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"reset": True}

# -- ZLANs (Sec 2) --
@app.get("/api/zlans")
def api_zlans(): return store.list_zlans()

@app.post("/api/zlans")
def api_add_zlan(payload: dict = Body(...)):
    zlan_ip = (payload.get("zlan_ip") or "").strip()
    zlan_label = (payload.get("zlan_label") or "").strip()
    if not zlan_ip:
        return bad("ZLAN IP is required.")
    if not zlan_label:
        zlan_label = zlan_ip
    err = store.add_zlan(zlan_ip, zlan_label,
        int(payload.get("default_polling_interval", 30)), int(payload.get("port", 502)))
    if err: return bad(err)
    return {"added": zlan_ip}

@app.post("/api/zlans/{zlan_ip}/meters")
def api_zlan_meters_bulk(zlan_ip: str, payload: dict = Body(...)):
    """Sec 2.1-2.3: enter meter_count meter_codes at once, one slot per
    unit_id. Only creates NEW unit_ids -- existing ones on this ZLAN are
    left untouched (scaling up prompts only for the new slots)."""
    codes = payload.get("meter_codes", [])  # list, index 0 = unit_id 1, etc.
    existing = {m["unit_id"] for m in store.list_meters(zlan_ip=zlan_ip)}
    created, errors = [], []
    for i, code in enumerate(codes, start=1):
        if i in existing or not code:
            continue
        err = store.add_meter(code, zlan_ip, i)
        if err: errors.append({"unit_id": i, "error": err})
        else: created.append(code)
    return {"created": created, "errors": errors}

@app.post("/api/meters/bulk-upload")
def api_bulk_upload(payload: dict = Body(...)):
    """Sec 2.4: CSV parsed client-side into rows, posted here."""
    rows = payload.get("rows", [])
    created, errors = [], []
    for r in rows:
        try:
            zlan_ip = r["zlan_ip"]
            if not store.list_zlans() or not any(z["zlan_ip"] == zlan_ip for z in store.list_zlans()):
                store.add_zlan(zlan_ip, r.get("zlan_label", zlan_ip), int(r.get("polling_interval_minutes") or 30))
            err = store.add_meter(r["meter_code"], zlan_ip, int(r["unit_id"]),
                int(r["polling_interval_minutes"]) if r.get("polling_interval_minutes") else None)
            if err: errors.append({"row": r, "error": err})
            else: created.append(r["meter_code"])
        except Exception as e:
            errors.append({"row": r, "error": str(e)})
    return {"created": created, "errors": errors}

@app.delete("/api/zlans")
def api_del_zlan(zlan_ip: str = ""):
    """Query-param based (not /zlans/{zlan_ip}) so a blank/corrupted IP --
    e.g. a ZLAN mistakenly added with no IP -- can still be matched and
    deleted. An empty path segment never reaches a {zlan_ip} route at all."""
    if not store.delete_zlan(zlan_ip): return bad("Not found.", 404)
    return {"deleted": zlan_ip}

# -- Meters (Sec 2) --
@app.get("/api/meters")
def api_meters(zlan_ip: str = ""): return store.list_meters(zlan_ip=zlan_ip or None)

@app.post("/api/meters")
def api_add_meter(payload: dict = Body(...)):
    err = store.add_meter(payload["meter_code"], payload["zlan_ip"], int(payload["unit_id"]),
        int(payload["polling_interval"]) if payload.get("polling_interval") else None)
    if err: return bad(err)
    return {"added": payload["meter_code"]}

@app.put("/api/meters/{meter_code}/active")
def api_meter_active(meter_code: str, payload: dict = Body(...)):
    store.set_meter_active(meter_code, bool(payload.get("active", True)))
    return {"ok": True}

@app.put("/api/meters/{meter_code}/rename")
def api_meter_rename(meter_code: str, payload: dict = Body(...)):
    err = store.rename_meter(meter_code, payload["new_code"])
    if err: return bad(err)
    return {"renamed": True}

@app.put("/api/meters/{meter_code}/interval")
def api_meter_interval(meter_code: str, payload: dict = Body(...)):
    minutes, err = _minutes_or_none(payload.get("polling_interval"), "Poll interval")
    if err: return bad(err)
    if not store.update_meter_interval(meter_code, minutes): return bad("Not found.", 404)
    return {"ok": True}

@app.put("/api/meters/{meter_code}/publish-interval")
def api_meter_publish_interval(meter_code: str, payload: dict = Body(...)):
    minutes, err = _minutes_or_none(payload.get("publish_interval"), "Publish interval")
    if err: return bad(err)
    if not store.update_meter_publish_interval(meter_code, minutes): return bad("Not found.", 404)
    return {"ok": True}

@app.put("/api/zlans/{zlan_ip}/interval")
def api_zlan_interval(zlan_ip: str, payload: dict = Body(...)):
    minutes, err = _minutes_or_none(payload.get("default_polling_interval"), "Interval")
    if err: return bad(err)
    if minutes is None: return bad("Interval is required (minutes, 1 or more).")
    if not store.update_zlan_interval(zlan_ip, minutes): return bad("Not found.", 404)
    return {"ok": True}

@app.put("/api/zlans/{zlan_ip}/publish-interval")
def api_zlan_publish_interval(zlan_ip: str, payload: dict = Body(...)):
    minutes, err = _minutes_or_none(payload.get("default_publish_interval"), "Publish interval")
    if err: return bad(err)
    if not store.update_zlan_publish_interval(zlan_ip, minutes): return bad("Not found.", 404)
    return {"ok": True}

# -- Parameters (Sec 3) --
@app.get("/api/parameters")
def api_params(): return store.list_params(active_only=False)

def _minutes_or_none(v, name):
    """'' / None -> None (use the fallback); otherwise a whole number of minutes >= 1."""
    if v in (None, ""): return None, None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None, f"{name} must be a whole number of minutes."
    if n < 1: return None, f"{name} must be at least 1 minute."
    if n > 525600: return None, f"{name} must be at most 525600 minutes (one year)."
    return n, None

@app.post("/api/parameters")
def api_add_param(payload: dict = Body(...)):
    poll, e1 = _minutes_or_none(payload.get("poll_interval"), "Poll interval")
    pub, e2 = _minutes_or_none(payload.get("publish_interval"), "Publish interval")
    if e1 or e2: return bad(e1 or e2)
    err = store.add_param(payload["label"], int(payload["address"]), payload.get("wire_code"),
        int(payload.get("priority", 100)), payload.get("min_value"), payload.get("max_value"),
        poll_interval=poll, publish_interval=pub)
    if err: return bad(err)
    return {"added": payload["label"]}

@app.put("/api/parameters/{label}/intervals")
def api_param_intervals(label: str, payload: dict = Body(...)):
    poll, e1 = _minutes_or_none(payload.get("poll_interval"), "Poll interval")
    pub, e2 = _minutes_or_none(payload.get("publish_interval"), "Publish interval")
    if e1 or e2: return bad(e1 or e2)
    if not store.update_param_intervals(label, poll, pub): return bad("Not found.", 404)
    return {"label": label, "poll_interval": poll, "publish_interval": pub}

@app.delete("/api/parameters/{label}")
def api_del_param(label: str):
    if not store.delete_param(label): return bad("Not found.", 404)
    return {"deleted": label}

# -- Queue / History (Sec 4, 7) --
@app.get("/api/queue")
def api_queue(search: str = "", limit: int = 50, offset: int = 0):
    items, total = store.queue_page(search or None, limit, offset)
    return {"items": items, "total": total}

@app.get("/api/history")
def api_history(meter_code: str="", parameter: str="", status: str="", search: str="",
                 date_from: str="", date_to: str="", limit: int=100, offset: int=0,
                 order_by: str="", order_dir: str="desc"):
    items, total = store.history(meter_code or None, parameter or None, status or None,
        search or None, date_from or None, date_to or None, limit, offset, order_by or None, order_dir)
    return {"items": items, "total": total}

@app.post("/api/history/{msg_no}/retry")
def api_retry(msg_no: str):
    r = store.retry_failed(msg_no)
    if not r: return bad("Not found or not failed.", 404)
    return {"requeued": r}

@app.post("/api/history/retry-all")
def api_retry_all():
    n = store.retry_all_failed()
    return {"requeued": n}

@app.delete("/api/history")
def api_delete_history(payload: dict = Body(...)):
    mode = payload.get("mode")
    if mode == "all":
        n = store.delete_history("all")
    elif mode == "selected":
        n = store.delete_history("selected", msg_nos=payload.get("msg_nos", []))
    elif mode == "months":
        cutoff = _months_ago(int(payload["months"])).strftime("%Y-%m-%d %H:%M:%S")
        n = store.delete_history("before", before=cutoff)
    elif mode == "custom":
        n = store.delete_history("before", before=payload["before"])
    else:
        return bad("Invalid mode.")
    return {"deleted": n}

@app.get("/api/connectivity-history")
def api_connectivity_history(entity_type: str = "", entity_id: str = "", zlan_ip: str = "", date_from: str = "", date_to: str = "",
                              limit: int = 100, offset: int = 0):
    items, total = store.connectivity_history(entity_type or None, entity_id or None, zlan_ip or None,
        date_from or None, date_to or None, limit, offset)
    return {"items": items, "total": total}

@app.delete("/api/connectivity-history")
def api_delete_connectivity_history(payload: dict = Body(...)):
    mode = payload.get("mode")
    if mode == "all":
        n = store.delete_connectivity_history("all")
    elif mode == "selected":
        n = store.delete_connectivity_history("selected", msg_nos=payload.get("msg_nos", []))
    elif mode == "months":
        cutoff = _months_ago(int(payload["months"])).strftime("%Y-%m-%d %H:%M:%S")
        n = store.delete_connectivity_history("before", before=cutoff)
    elif mode == "custom":
        n = store.delete_connectivity_history("before", before=payload["before"])
    else:
        return bad("Invalid mode.")
    return {"deleted": n}

@app.get("/api/export/connectivity-history")
def export_connectivity_history(entity_type: str = "", entity_id: str = "", zlan_ip: str = "", date_from: str = "", date_to: str = ""):
    items, _ = store.connectivity_history(entity_type or None, entity_id or None, zlan_ip or None, date_from or None, date_to or None, limit=1000000)
    rows = [[i["entity_type"], i["entity_id"], i["zlan_label"] or "-", i["status"], i["at"], i["delivery_status"], i["released_at"], i["failed_at"], i["error_message"]] for i in items]
    return _csv_response(["entity_type","entity_id","zlan","status","at","delivery_status","released_at","failed_at","error_message"], rows, "connectivity_history.csv")

@app.get("/api/dashboard")
def api_dashboard():
    counts = store.counts()
    zlans = store.list_zlans()
    meters = store.list_meters()
    per_zlan_counts = store.per_zlan_message_counts()
    zlan_status = {}
    for z in zlans:
        zm = [m for m in meters if m["zlan_ip"] == z["zlan_ip"]]
        connected = sum(1 for m in zm if m["last_status"] == "ok")
        c = per_zlan_counts.get(z["zlan_ip"], {"queued":0,"released":0,"failed":0})
        zlan_status[z["zlan_ip"]] = {"label": z["zlan_label"], "connected": connected, "total": len(zm),
                                       "status": z["last_status"], **c}
    meters_connected = sum(1 for m in meters if m["last_status"] == "ok")
    db_size_mb = round(DB_PATH.stat().st_size / (1024*1024), 2) if DB_PATH.exists() else 0
    return {**counts, "zlans_total": len(zlans),
            "zlans_connected": sum(1 for v in zlan_status.values() if v["connected"] == v["total"] and v["total"] > 0),
            "meters_total": len(meters), "meters_connected": meters_connected,
            "per_zlan": zlan_status, "max_queue_size": int(store.get("max_queue_size") or 5000),
            "db_size_mb": db_size_mb, "broker_connected": outbound.last_connected,
            "version": APP_VERSION}

# -- CSV export (Sec 4.2) --
def _csv_response(headers, rows, filename):
    buf = io.StringIO()
    w = csv.writer(buf); w.writerow(headers); w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.get("/api/export/meters")
def export_meters():
    rows = [[m["meter_code"],m["zlan_ip"],m["unit_id"],m["polling_interval"],m["active"],m["last_status"]] for m in store.list_meters()]
    return _csv_response(["meter_code","zlan_ip","unit_id","polling_interval","active","last_status"], rows, "meters.csv")

@app.get("/api/export/parameters")
def export_params():
    rows = [[p["label"],p["address"],p["wire_code"],p["priority"],p["min_value"],p["max_value"]] for p in store.list_params(False)]
    return _csv_response(["label","address","wire_code","priority","min_value","max_value"], rows, "parameters.csv")

@app.get("/api/export/queue")
def export_queue():
    items,_ = store.queue_page(limit=100000)
    rows = [[i["meter_code"],i["parameter"],i["value"],i["date_time"],i["msg_no"],i["attempts"]] for i in items]
    return _csv_response(["meter_code","parameter","value","date_time","msg_no","attempts"], rows, "queue.csv")

@app.get("/api/export/history")
def export_history():
    items,_ = store.history(limit=1000000)
    rows = [[i["meter_code"],i["parameter"],i["value"],i["date_time"],i["msg_no"],i["status"],i["released_at"],i["failed_at"]] for i in items]
    return _csv_response(["meter_code","parameter","value","date_time","msg_no","status","released_at","failed_at"], rows, "history.csv")

# -- Settings / backup (Sec 4.2, 6.2, 2.7, 8.1) --
@app.get("/api/settings")
def api_settings():
    d = store.settings_dict(); d.pop("auth_hash", None); d.pop("auth_salt", None)
    return d

@app.put("/api/settings")
def api_set_settings(payload: dict = Body(...)):
    accepted = {k: v for k, v in payload.items() if k in DEFAULT_SETTINGS and k not in ("auth_hash", "auth_salt")}
    touches_output = any(k in OUTPUT_KEYS for k in accepted)
    if touches_output:
        merged = store.settings_dict()
        merged.update({k: ("" if v is None else str(v)) for k, v in accepted.items()})
        errors = validate_output(merged)
        if errors:
            return JSONResponse(status_code=400, content={"detail": "Please fix the highlighted fields.", "errors": errors})
    for k, v in accepted.items():
        store.set(k, v)
    if touches_output:
        threading.Thread(target=outbound.reconfigure, daemon=True).start()   # reconnect with the new settings now
    return {"ok": True}

@app.post("/api/output/test")
def api_output_test(payload: dict = Body(default={})):
    """Checks the output connection with the values in the form (saved or not) without sending any data."""
    cfg = store.settings_dict()
    for k in OUTPUT_KEYS:
        if k in payload: cfg[k] = "" if payload[k] is None else str(payload[k])
    errors = validate_output(cfg, need_all=True)
    if errors:
        return JSONResponse(status_code=400, content={"detail": "Please fix the highlighted fields.", "errors": errors})
    res = test_http_connection(cfg) if (cfg.get("connection_type") or "http") == "http" else test_mqtt_connection(cfg)
    res["type"] = cfg.get("connection_type") or "http"
    return res

@app.get("/api/version")
def api_version(): return {"version": APP_VERSION}

@app.get("/api/backup/status")
def api_backup_status(): return store.backup.status()

@app.get("/api/config/export")
def config_export(): return store.export_config()

@app.post("/api/config/import")
def config_import(payload: dict = Body(...)):
    store.import_config(payload)
    return {"ok": True}

@app.post("/api/test/inject")
def test_inject(payload: dict = Body(...)):
    """Runs through the real, live outbound connection -- not a
    disposable one-off, so async MQTT confirmation actually works."""
    handle_reading(payload["meter_code"], payload["parameter"], float(payload["value"]))
    return {"injected": True}

@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    if not require_auth(ws.cookies.get("session")):
        await ws.close(code=4001)
        return
    await ws.accept()
    sub = bus.subscribe()
    try:
        while True:
            try:
                ev = await asyncio.get_event_loop().run_in_executor(None, sub.get, True, 1.0)
            except _q.Empty:
                continue
            await ws.send_json(ev)
    except Exception:
        pass
    finally:
        bus.unsubscribe(sub)

if __name__ == "__main__":
    # background threads are daemon threads, but systemd was still
    # waiting out its full stop timeout before force-killing -- this
    # makes a stop/restart request exit immediately and cleanly instead.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT)
