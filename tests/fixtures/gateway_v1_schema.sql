CREATE TABLE zlans (
    zlan_ip TEXT PRIMARY KEY, zlan_label TEXT NOT NULL,
    default_polling_interval INTEGER NOT NULL DEFAULT 30, port INTEGER NOT NULL DEFAULT 502
, last_status TEXT DEFAULT 'unknown', default_publish_interval INTEGER, last_connectivity_sent_at TEXT);
CREATE TABLE meters (
    meter_code TEXT PRIMARY KEY, zlan_ip TEXT NOT NULL, unit_id INTEGER NOT NULL,
    polling_interval INTEGER, active INTEGER NOT NULL DEFAULT 1,
    last_poll_at TEXT, last_status TEXT DEFAULT 'unknown', consecutive_fail INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL, publish_interval INTEGER, last_published_at TEXT, last_connectivity_sent_at TEXT, UNIQUE(zlan_ip, unit_id)
);
CREATE TABLE parameters (
    label TEXT PRIMARY KEY, address INTEGER NOT NULL, wire_code TEXT,
    priority INTEGER NOT NULL DEFAULT 100, min_value REAL, max_value REAL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT NOT NULL, parameter TEXT NOT NULL,
    value REAL NOT NULL, date_time TEXT NOT NULL, msg_no TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, awaiting_response TEXT
);
CREATE INDEX idx_q_created ON queue(created_at);
CREATE TABLE history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT NOT NULL, parameter TEXT NOT NULL,
    value REAL NOT NULL, date_time TEXT NOT NULL, msg_no TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
    released_at TEXT, failed_at TEXT, error_message TEXT
);
CREATE INDEX idx_h_lookup ON history(meter_code, parameter, status, created_at);
CREATE TABLE invalid_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT, parameter TEXT, value REAL,
    reason TEXT, at TEXT NOT NULL
);
CREATE TABLE dropped_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT, parameter TEXT, value REAL,
    msg_no TEXT, at TEXT NOT NULL
);
CREATE TABLE msg_counters (
    meter_code TEXT, parameter TEXT, day TEXT, counter INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(meter_code, parameter, day)
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE connectivity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    status TEXT NOT NULL, at TEXT NOT NULL
, msg_no TEXT, delivery_status TEXT DEFAULT 'queued', released_at TEXT, failed_at TEXT, error_message TEXT, created_at TEXT);
CREATE INDEX idx_conn_lookup ON connectivity_history(entity_type, entity_id, at);
CREATE TABLE connectivity_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    status TEXT NOT NULL, at TEXT NOT NULL, msg_no TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, awaiting_response TEXT
);
CREATE TABLE skipped_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meter_code TEXT, parameter TEXT, value REAL,
    reason TEXT, at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_conn_msgno ON connectivity_history(msg_no);
