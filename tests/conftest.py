"""Test helpers: every test gets its own copy of app.py running in an empty
temp directory (app.py uses relative paths: bridge.db, static/, backup/)."""
import importlib.util
import itertools
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_counter = itertools.count()


def load_app(workdir: Path, schema_sql: str = None, seed_sql: str = None):
    """Imports a fresh copy of app.py with `workdir` as the working directory.
    Optionally pre-creates bridge.db from an older schema (to test migrations)."""
    os.chdir(workdir)
    for name in ("static", "templates"):
        if not (workdir / name).exists():
            os.symlink(REPO / name, workdir / name)
    if schema_sql is not None:
        con = sqlite3.connect(workdir / "bridge.db")
        con.executescript(schema_sql)
        if seed_sql:
            con.executescript(seed_sql)
        con.commit()
        con.close()
    name = f"bridge_app_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, REPO / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod.store.backup.batch_delay = 0          # write backup files immediately in tests
    mod.store.set("backup_enabled", "true")
    mod.store.backup._enabled_cache = (0.0, True)
    return mod


@pytest.fixture
def bridge(tmp_path):
    cwd = os.getcwd()
    mod = load_app(tmp_path)
    yield mod
    mod.store.c.close()
    os.chdir(cwd)


class Recorder:
    """Stands in for Outbound.send: records what would go to IDMS."""
    def __init__(self, result="confirmed"):
        self.result = result
        self.sent = []

    def __call__(self, meter_code, parameter, value, msg_no, date_time, **kw):
        self.sent.append((meter_code, parameter, value, msg_no, date_time))
        return self.result

    def params(self):
        return [s[1] for s in self.sent]


@pytest.fixture
def rec(bridge):
    r = Recorder()
    bridge.outbound.send = r
    bridge.store.set("server_enabled", "true")
    return r


@pytest.fixture
def site(bridge, rec):
    """One ZLAN (default publish 180 min), one meter, the six real parameters, throttle ON."""
    st = bridge.store
    assert st.add_zlan("192.168.50.155", "SS7", 15) is None
    st.update_zlan_publish_interval("192.168.50.155", 180)
    assert st.add_meter("M090", "192.168.50.155", 6) is None
    for label, addr, wire, prio in [("kwh", 32784, "P005", 1), ("md", 36880, "P006", 2), ("volt", 42514, "P004", 2),
                                     ("amp", 42530, "P001", 2), ("amp2", 42532, "P002", 2), ("amp3", 42534, "P003", 2)]:
        assert st.add_param(label, addr, wire, prio) is None
    st.set("publish_throttle_enabled", "true")
    return bridge
