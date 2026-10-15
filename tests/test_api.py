import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(site):
    c = TestClient(site.app)          # no `with`: background threads are not started
    r = c.post("/api/auth/setup", json={"username": "admin", "password": "pw-for-tests"})
    assert r.status_code == 200
    return c


def test_new_endpoints_require_login(site):
    anon = TestClient(site.app)
    for method, url in [("get", "/api/version"), ("get", "/api/backup/status"),
                        ("put", "/api/parameters/md/intervals")]:
        r = getattr(anon, method)(url, **({"json": {}} if method == "put" else {}))
        assert r.status_code == 401, url


def test_parameters_list_includes_intervals(client):
    client.put("/api/parameters/md/intervals", json={"poll_interval": 30, "publish_interval": 30})
    md = [p for p in client.get("/api/parameters").json() if p["label"] == "md"][0]
    assert (md["poll_interval"], md["publish_interval"]) == (30, 30)
    kwh = [p for p in client.get("/api/parameters").json() if p["label"] == "kwh"][0]
    assert kwh["poll_interval"] is None and kwh["publish_interval"] is None


def test_set_and_clear_intervals(client):
    r = client.put("/api/parameters/volt/intervals", json={"poll_interval": "15", "publish_interval": 60})
    assert r.status_code == 200 and r.json()["poll_interval"] == 15
    r = client.put("/api/parameters/volt/intervals", json={"poll_interval": "", "publish_interval": None})
    assert r.json() == {"label": "volt", "poll_interval": None, "publish_interval": None}


@pytest.mark.parametrize("payload", [{"poll_interval": 0}, {"poll_interval": -5}, {"publish_interval": "abc"},
                                      {"poll_interval": 10**12}, {"publish_interval": 525601}])
def test_bad_intervals_rejected(client, payload):
    r = client.put("/api/parameters/md/intervals", json=payload)
    assert r.status_code == 400


def test_unknown_parameter_is_404(client):
    assert client.put("/api/parameters/nope/intervals", json={"poll_interval": 5}).status_code == 404


def test_add_parameter_with_intervals(client):
    r = client.post("/api/parameters", json={"label": "pf", "address": 40000, "wire_code": "P007", "priority": 3,
                                              "poll_interval": "30", "publish_interval": "30"})
    assert r.status_code == 200
    pf = [p for p in client.get("/api/parameters").json() if p["label"] == "pf"][0]
    assert (pf["poll_interval"], pf["publish_interval"]) == (30, 30)
    # without intervals still works (old front-end / API users)
    assert client.post("/api/parameters", json={"label": "hz", "address": 40010}).status_code == 200


def test_config_export_import_round_trip_keeps_intervals(client, site):
    client.put("/api/parameters/md/intervals", json={"poll_interval": 30, "publish_interval": 30})
    exported = client.get("/api/config/export").json()
    assert [p for p in exported["parameters"] if p["label"] == "md"][0]["poll_interval"] == 30
    site.store._q("DELETE FROM parameters"); site.store.c.commit()
    assert client.post("/api/config/import", json=exported).status_code == 200
    md = [p for p in client.get("/api/parameters").json() if p["label"] == "md"][0]
    assert (md["poll_interval"], md["publish_interval"]) == (30, 30)


def test_import_of_a_v1_config_without_interval_fields(client, site):
    old = {"parameters": [{"label": "kwh", "address": 32784, "wire_code": "P005", "priority": 1, "active": True}]}
    assert client.post("/api/config/import", json=old).status_code == 200


def test_version_and_dashboard_report_the_version(client, site):
    assert client.get("/api/version").json() == {"version": site.APP_VERSION}
    assert client.get("/api/dashboard").json()["version"] == site.APP_VERSION


def test_backup_settings_and_status(client, site, rec):
    site.handle_reading("M090", "kwh", 1.0)
    st = client.get("/api/backup/status").json()
    assert st["enabled"] is True and st["files"] >= 0 and "free_disk_mb" in st
    assert client.put("/api/settings", json={"backup_enabled": "false", "backup_retention_days": "365"}).status_code == 200
    s = client.get("/api/settings").json()
    assert s["backup_enabled"] == "false" and s["backup_retention_days"] == "365"


def test_index_page_has_the_new_controls(client):
    html = client.get("/").text
    for el in ("new-param-poll", "new-param-publish", "set-backup-enabled", "set-backup-retention", "app-version"):
        assert f'id="{el}"' in html, el
    js = client.get("/static/app.js").text
    assert "saveParamIntervals" in js and "/api/backup/status" in js


# ---- ZLAN / meter interval endpoints: same validation as the parameter ones -------------------
@pytest.mark.parametrize("url,key", [
    ("/api/zlans/192.168.50.155/interval", "default_polling_interval"),
    ("/api/zlans/192.168.50.155/publish-interval", "default_publish_interval"),
    ("/api/meters/M090/interval", "polling_interval"),
    ("/api/meters/M090/publish-interval", "publish_interval"),
])
def test_zlan_and_meter_intervals_are_validated(client, url, key):
    assert client.put(url, json={key: 30}).status_code == 200
    for bad in (0, -1, "abc", 10**12):
        assert client.put(url, json={key: bad}).status_code == 400, (url, bad)


def test_blank_zlan_poll_interval_is_rejected_but_publish_blank_clears_it(client, site):
    # a blank/0 poll interval used to be stored and made the poller spin without any delay
    assert client.put("/api/zlans/192.168.50.155/interval", json={"default_polling_interval": None}).status_code == 400
    assert site.store.list_zlans()[0]["default_polling_interval"] == 15
    assert client.put("/api/zlans/192.168.50.155/publish-interval", json={"default_publish_interval": ""}).status_code == 200
    assert site.store.list_zlans()[0]["default_publish_interval"] is None
    assert client.put("/api/meters/M090/interval", json={"polling_interval": None}).status_code == 200
