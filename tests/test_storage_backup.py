import csv
import re
from pathlib import Path

TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def rows(b, kind):
    b.store.backup.flush()
    out = []
    for f in sorted(b.BACKUP_DIR.glob(f"*/{kind}_*.csv")):
        out += list(csv.DictReader(open(f)))
    return out


def test_every_published_reading_is_in_history_and_backup(site, rec):
    for label in ["kwh", "md", "volt", "amp", "amp2", "amp3"]:
        site.handle_reading("M090", label, 12.5)
    hist = site.store._q("SELECT parameter,date_time,msg_no,status FROM history ORDER BY id").fetchall()
    assert [h[0] for h in hist] == ["kwh", "md", "volt", "amp", "amp2", "amp3"]
    backup = rows(site, "readings")
    assert sorted(r["msg_no"] for r in backup) == sorted(h[2] for h in hist)
    assert {r["status"] for r in backup} == {"released"}


def test_md_timestamp_format_is_identical_to_kwh_and_volt(site, rec):
    """The question asked on site: MD must carry the same timestamp format as kWh / voltage."""
    for label in ["kwh", "md", "volt"]:
        site.handle_reading("M090", label, 1.0)
    stamps = {s[1]: s[4] for s in rec.sent}
    assert all(TS.match(v) for v in stamps.values()), stamps
    wire = {p["label"]: p["wire_code"] for p in site.store.list_params(False)}
    payload = site.outbound._payload("M090", "md", 1.0, "x", stamps["md"])
    assert payload["data"]["parameter"] == wire["md"] == "P006"
    assert TS.match(payload["data"]["date_time"])


def test_failed_send_is_queued_stored_and_backed_up(site, rec):
    rec.result = "failed"
    site.handle_reading("M090", "md", 3.0)
    assert site.store.queue_count() == 1
    assert site.store._q("SELECT status FROM history").fetchone()[0] == "queued"
    assert [r["status"] for r in rows(site, "readings")] == ["queued"]
    msg_no = site.store._q("SELECT msg_no FROM queue").fetchone()[0]
    site.store.release(msg_no, message="ok")
    acks = rows(site, "acks")
    assert acks[-1]["msg_no"] == msg_no and acks[-1]["result"] == "released"


def test_connectivity_events_are_backed_up(site):
    msg_no, at = site.store.log_connectivity("meter", "M090", "disconnected")
    r = rows(site, "connectivity")
    assert r[0]["msg_no"] == msg_no and r[0]["status"] == "disconnected"


def test_failed_and_dropped_results_are_backed_up(site, rec):
    rec.result = "failed"
    site.store.set("max_attempts", "1")
    site.handle_reading("M090", "kwh", 1.0)
    msg_no = site.store._q("SELECT msg_no FROM queue").fetchone()[0]
    site.store.mark_attempt(site.store._q("SELECT id FROM queue").fetchone()[0])
    assert site.store.fail_attempt(msg_no, "boom") == "failed"
    assert any(a["result"] == "failed" and a["message"] == "boom" for a in rows(site, "acks"))


def test_backup_survives_history_purge_and_reset(site, rec):
    site.handle_reading("M090", "kwh", 1.0)
    site.store.delete_history("all")
    site.store.factory_reset()
    assert len(rows(site, "readings")) == 1


def test_backup_can_be_switched_off(site, rec):
    site.store.set("backup_enabled", "false")
    site.store.backup._enabled_cache = (0.0, True)     # drop the 30 s cache
    site.handle_reading("M090", "kwh", 1.0)
    assert rows(site, "readings") == []


def test_backup_stops_quietly_when_disk_is_nearly_full(site, rec, monkeypatch):
    import collections
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(site.shutil, "disk_usage", lambda p: usage(10**9, 10**9 - 1000, 1000))
    site.handle_reading("M090", "kwh", 1.0)             # must not raise
    assert rows(site, "readings") == []
    assert site.store._q("SELECT COUNT(*) FROM history").fetchone()[0] == 1    # DB still stores it


def test_backup_retention_purges_old_files_only(site):
    base = site.BACKUP_DIR
    (base / "202601").mkdir(parents=True)
    old = base / "202601" / "readings_20260105.csv"; old.write_text("x")
    today = site.datetime.now().strftime("%Y%m%d")
    (base / today[:6]).mkdir(exist_ok=True)
    new = base / today[:6] / f"readings_{today}.csv"; new.write_text("x")
    assert site.store.backup.purge(0) == 0 and old.exists()          # 0 = keep forever
    assert site.store.backup.purge(30) == 1
    assert not old.exists() and new.exists()
    assert not (base / "202601").exists()                             # empty month folder removed


def test_backup_status(site, rec):
    site.handle_reading("M090", "kwh", 1.0)
    site.store.backup.flush()
    st = site.store.backup.status()
    assert st["enabled"] and st["files"] == 1 and st["latest_file"].startswith("readings_")
