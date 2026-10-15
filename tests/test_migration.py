"""An existing v1.0 gateway database (schema captured from a live gateway) must upgrade in place."""
import os
import sqlite3
from pathlib import Path

from conftest import REPO, load_app

V1_SCHEMA = (REPO / "tests" / "fixtures" / "gateway_v1_schema.sql").read_text()
SEED = """
INSERT INTO parameters VALUES('volt',42514,'P004',2,NULL,NULL,1);
INSERT INTO parameters VALUES('md',36880,'P006',2,NULL,NULL,1);
INSERT INTO parameters VALUES('amp',42530,'P001',2,NULL,NULL,1);
INSERT INTO parameters VALUES('kwh',32784,'P005',1,NULL,NULL,1);
INSERT INTO zlans(zlan_ip,zlan_label,default_polling_interval,port,default_publish_interval) VALUES('192.168.50.155','SS7',15,502,180);
INSERT INTO meters(meter_code,zlan_ip,unit_id,registered_at) VALUES('M090','192.168.50.155',6,'2026-09-08 04:16:33');
INSERT INTO history VALUES(1,'M090','kwh',146.96,'2026-10-14 14:51:13','M090_kwh_20261014145113_5','released','2026-10-14 14:51:13.298033','2026-10-14 14:51:13.383211',NULL,NULL);
INSERT INTO settings VALUES('publish_throttle_enabled','true');
INSERT INTO settings VALUES('retention_days','90');
"""


def test_v1_database_is_upgraded_in_place(tmp_path):
    cwd = os.getcwd()
    try:
        b = load_app(tmp_path, V1_SCHEMA, SEED)
        params = {p["label"]: p for p in b.store.list_params(False)}
        # MD gets the site requirement: poll 30 + publish 30. Everything else is untouched.
        assert (params["md"]["poll_interval"], params["md"]["publish_interval"]) == (30, 30)
        for other in ("kwh", "volt", "amp"):
            assert (params[other]["poll_interval"], params[other]["publish_interval"]) == (None, None)
        # existing data and settings survive
        assert b.store._q("SELECT COUNT(*) FROM history").fetchone()[0] == 1
        assert b.store.get("publish_throttle_enabled") == "true"
        assert b.store.get("retention_days") == "90"
        # new settings are added with safe defaults
        assert b.store.get("backup_enabled") == "true" and b.store.get("backup_retention_days") == "0"
        # new table exists
        b.store.mark_published("M090", "md")
        b.store.c.close()
    finally:
        os.chdir(cwd)


def test_md_default_is_applied_once_not_on_every_start(tmp_path):
    cwd = os.getcwd()
    try:
        b = load_app(tmp_path, V1_SCHEMA, SEED)
        b.store.update_param_intervals("md", 45, 60)        # operator changes it on the front-end
        b.store.c.close()
        b2 = load_app(tmp_path)                               # restart (same bridge.db)
        md = [p for p in b2.store.list_params(False) if p["label"] == "md"][0]
        assert (md["poll_interval"], md["publish_interval"]) == (45, 60)
        b2.store.c.close()
    finally:
        os.chdir(cwd)


def test_fresh_install_has_no_default_parameters(tmp_path):
    cwd = os.getcwd()
    try:
        b = load_app(tmp_path)
        assert b.store.list_params(False) == []
        b.store.c.close()
    finally:
        os.chdir(cwd)
