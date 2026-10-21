import time

import pytest
from fastapi.testclient import TestClient

from fake_servers import FakeHttpServer, FakeMqttBroker, free_port

GOOD = {"connection_type": "mqtt", "mqtt_broker": "127.0.0.1", "mqtt_port": "1883",
        "mqtt_topic_template": "ksb/meter{meter_code}/{label}",
        "mqtt_connectivity_topic_template": "ksb/connectivity/{entity_id}",
        "mqtt_response_topic": "ksb/server/response/idms", "mqtt_response_timeout": "15",
        "mqtt_username": "", "mqtt_password": "", "server_enabled": "true"}


def cfg(**kw):
    return dict(GOOD, **kw)


@pytest.fixture
def client(bridge):
    c = TestClient(bridge.app)
    assert c.post("/api/auth/setup", json={"username": "admin", "password": "pw-for-tests"}).status_code == 200
    return c


# ------------------------------------------------------------------ validation rules
def test_valid_mqtt_settings_have_no_errors(bridge):
    assert bridge.validate_output(cfg()) == {}


@pytest.mark.parametrize("field,value,fragment", [
    ("mqtt_broker", "mqtt://103.224.93.124", "no mqtt://"),
    ("mqtt_broker", "103.224.93.124:1883", "port in the Port box"),
    ("mqtt_broker", "bad host name", "no mqtt://"),
    ("mqtt_broker", "-nope-", "valid IP address or host name"),
    ("mqtt_port", "0", "1 to 65535"),
    ("mqtt_port", "70000", "1 to 65535"),
    ("mqtt_port", "18 83", "1 to 65535"),
    ("mqtt_topic_template", "ksb/meter{meter_c", "braces"),
    ("mqtt_topic_template", "ksb/meter{meter_code}/{lable}", "Unknown placeholder {lable}"),
    ("mqtt_topic_template", "ksb/{}/x", "braces"),
    ("mqtt_topic_template", "ksb/#", "cannot contain"),
    ("mqtt_topic_template", " ksb/x", "spaces"),
    ("mqtt_connectivity_topic_template", "ksb/c/{meter_code}", "Unknown placeholder {meter_code}"),
    ("mqtt_response_timeout", "0", "1 to 300"),
    ("mqtt_response_timeout", "abc", "1 to 300"),
])
def test_bad_values_are_explained(bridge, field, value, fragment):
    errors = bridge.validate_output(cfg(**{field: value}))
    assert fragment in errors.get(field, ""), errors


def test_password_without_username(bridge):
    assert "password" in bridge.validate_output(cfg(mqtt_password="x"))["mqtt_username"].lower()
    assert bridge.validate_output(cfg(mqtt_username="u", mqtt_password="")) == {}      # username only is fine


@pytest.mark.parametrize("field", ["mqtt_broker", "mqtt_port", "mqtt_topic_template",
                                   "mqtt_connectivity_topic_template"])
def test_required_fields_only_matter_when_enabled(bridge, field):
    assert bridge.validate_output(cfg(**{field: ""}))[field] == "Required."
    assert field not in bridge.validate_output(cfg(**{field: "", "server_enabled": "false"}))   # draft allowed
    assert field in bridge.validate_output(cfg(**{field: "", "server_enabled": "false"}), need_all=True)


def test_response_topic_is_optional_but_timeout_then_is_required(bridge):
    assert bridge.validate_output(cfg(mqtt_response_topic="", mqtt_response_timeout="")) == {}
    assert "Required" in bridge.validate_output(cfg(mqtt_response_timeout=""))["mqtt_response_timeout"]


def test_http_rules(bridge):
    h = {"connection_type": "http", "server_enabled": "true"}
    assert bridge.validate_output(dict(h, server_url=""))["server_url"] == "Required."
    assert "http://" in bridge.validate_output(dict(h, server_url="idms.local/api"))["server_url"]
    assert bridge.validate_output(dict(h, server_url="https://idms.example.com/api/readings")) == {}
    assert bridge.validate_output(dict(h, server_url="http://10.1.2.3:8080/in")) == {}


# ------------------------------------------------------------------ PUT /api/settings
def test_save_with_errors_is_refused_and_stores_nothing(client, bridge):
    r = client.put("/api/settings", json=cfg(mqtt_broker="", mqtt_port="99999"))
    assert r.status_code == 400
    body = r.json()
    assert body["detail"] and set(body["errors"]) == {"mqtt_broker", "mqtt_port"}
    assert bridge.store.get("mqtt_port") != "99999"


def test_draft_can_be_saved_while_disabled(client, bridge):
    assert client.put("/api/settings", json=cfg(mqtt_broker="", server_enabled="false")).status_code == 200


def test_unrelated_settings_are_never_blocked_by_output_rules(client, bridge):
    bridge.store.set("mqtt_port", "garbage")            # an old, invalid value already stored
    bridge.store.set("server_enabled", "true")
    assert client.put("/api/settings", json={"retention_days": "120"}).status_code == 200
    assert bridge.store.get("retention_days") == "120"


def test_valid_save_is_stored(client, bridge):
    assert client.put("/api/settings", json=cfg(server_enabled="false", mqtt_broker="10.1.1.1")).status_code == 200
    assert bridge.store.get("mqtt_broker") == "10.1.1.1"


# ------------------------------------------------------------------ connection check: MQTT
@pytest.fixture
def broker_factory():
    made = []
    def make(**kw):
        b = FakeMqttBroker(**kw); made.append(b); return b
    yield make
    for b in made: b.close()


def check(client, **kw):
    return client.post("/api/output/test", json=cfg(**kw)).json()


def names(res):
    return {c["name"]: c["ok"] for c in res["checks"]}


def test_anonymous_broker_ok(client, broker_factory):
    b = broker_factory()
    res = check(client, mqtt_port=str(b.port))
    assert res["ok"] is True and names(res) == {"Address": True, "Network": True, "Login": True, "Response topic": True}
    assert "anonymous" in [c["text"] for c in res["checks"] if c["name"] == "Login"][0]
    assert res["latency_ms"] >= 0 and res["target"] == f"127.0.0.1:{b.port}"
    assert b.subscribed == ["ksb/server/response/idms"]


def test_login_accepted(client, broker_factory):
    b = broker_factory(users={"epicmqtt": "s3cret"})
    res = check(client, mqtt_port=str(b.port), mqtt_username="epicmqtt", mqtt_password="s3cret")
    assert res["ok"] is True
    assert "Logged in as epicmqtt" in [c["text"] for c in res["checks"] if c["name"] == "Login"][0]
    assert b.connects[-1] == ("epicmqtt", "s3cret")


def test_wrong_password_is_reported_as_a_login_problem(client, broker_factory):
    b = broker_factory(users={"epicmqtt": "s3cret"})
    res = check(client, mqtt_port=str(b.port), mqtt_username="epicmqtt", mqtt_password="nope")
    assert res["ok"] is False and names(res)["Network"] is True and names(res)["Login"] is False
    assert "rejected" in res["message"] and "username and password" in res["hint"]


def test_broker_that_needs_a_login_says_so(client, broker_factory):
    b = broker_factory(users={"epicmqtt": "s3cret"})
    res = check(client, mqtt_port=str(b.port))
    assert res["ok"] is False and "needs a username and password" in res["hint"]


def test_closed_port_is_reported_as_refused(client):
    res = check(client, mqtt_port=str(free_port()))
    assert res["ok"] is False and "refused" in res["message"] and names(res) == {"Address": True, "Network": False}


def test_unknown_host_is_reported_as_not_found(client):
    res = check(client, mqtt_broker="no-such-host.invalid")
    assert res["ok"] is False and "Cannot find" in res["message"] and names(res) == {"Address": False}


def test_silent_broker_times_out(client, broker_factory, bridge, monkeypatch):
    monkeypatch.setattr(bridge, "TEST_TIMEOUT", 1.0)
    b = broker_factory(silent=True)
    t0 = time.time()
    res = check(client, mqtt_port=str(b.port))
    assert res["ok"] is False and "did not answer" in res["message"] and time.time() - t0 < 4


def test_denied_response_topic_is_flagged(client, broker_factory):
    b = broker_factory(deny_subscribe={"ksb/server/response/idms"})
    res = check(client, mqtt_port=str(b.port))
    assert res["ok"] is False and names(res)["Login"] is True and names(res)["Response topic"] is False
    assert "allow this user to subscribe" in res["hint"]


def test_no_response_topic_is_explained_not_failed(client, broker_factory):
    b = broker_factory()
    res = check(client, mqtt_port=str(b.port), mqtt_response_topic="")
    assert res["ok"] is True and b.subscribed == []


def test_test_publishes_nothing_and_does_not_need_enabled(client, broker_factory):
    b = broker_factory()
    assert check(client, mqtt_port=str(b.port), server_enabled="false")["ok"] is True


def test_test_uses_saved_values_for_anything_not_sent(client, broker_factory, bridge):
    b = broker_factory()
    bridge.store.set("mqtt_broker", "127.0.0.1"); bridge.store.set("mqtt_port", str(b.port))
    bridge.store.set("connection_type", "mqtt")
    assert client.post("/api/output/test", json={}).json()["ok"] is True


def test_test_validates_first(client):
    r = client.post("/api/output/test", json=cfg(mqtt_broker="", mqtt_port="x"))
    assert r.status_code == 400 and {"mqtt_broker", "mqtt_port"} <= set(r.json()["errors"])


def test_test_needs_login(bridge):
    assert TestClient(bridge.app).post("/api/output/test", json={}).status_code == 401


# ------------------------------------------------------------------ connection check: HTTP
@pytest.mark.parametrize("status,ok,fragment", [(200, True, "reachable"), (405, True, "reachable"),
                                                (401, True, "authentication"), (404, False, "404"),
                                                (500, False, "error")])
def test_http_check(client, status, ok, fragment):
    srv = FakeHttpServer(status)
    try:
        res = client.post("/api/output/test", json={"connection_type": "http", "server_url": f"http://127.0.0.1:{srv.port}/in"}).json()
    finally:
        srv.close()
    assert res["ok"] is ok and fragment in (res["message"] + res["checks"][-1]["text"]), res


def test_http_refused(client):
    res = client.post("/api/output/test", json={"connection_type": "http", "server_url": f"http://127.0.0.1:{free_port()}/"}).json()
    assert res["ok"] is False and "refused" in res["message"]


# ------------------------------------------------------------------ the live connection follows the saved settings
def wait_for(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False


def test_changed_login_makes_the_live_client_reconnect(bridge, broker_factory):
    """Before 1.2.0 only a changed broker address/port reconnected, so a new password was ignored."""
    b = broker_factory(users={"a": "1", "b": "2"})
    base = {"connection_type": "mqtt", "mqtt_broker": "127.0.0.1", "mqtt_port": str(b.port), "mqtt_response_topic": ""}
    with bridge.outbound.lock:
        assert bridge.outbound._ensure_mqtt(dict(base, mqtt_username="a", mqtt_password="1")) is True
        assert b.connects[-1] == ("a", "1")
        assert bridge.outbound._ensure_mqtt(dict(base, mqtt_username="b", mqtt_password="2")) is True
    assert ("b", "2") in b.connects


def test_saving_new_credentials_reconnects_without_a_restart(client, bridge, broker_factory):
    b = broker_factory(users={"epicmqtt": "s3cret"})
    base = cfg(mqtt_port=str(b.port), mqtt_username="epicmqtt", mqtt_password="wrong")
    assert client.put("/api/settings", json=base).status_code == 200
    assert wait_for(lambda: ("epicmqtt", "wrong") in b.connects)
    assert wait_for(lambda: bridge.outbound.last_connected is False)
    assert client.put("/api/settings", json=dict(base, mqtt_password="s3cret")).status_code == 200
    assert wait_for(lambda: ("epicmqtt", "s3cret") in b.connects)
    assert wait_for(lambda: bridge.outbound.last_connected is True)


def test_bad_topic_template_fails_the_send_instead_of_raising(bridge, broker_factory):
    b = broker_factory()
    cfg_ = {"connection_type": "mqtt", "mqtt_broker": "127.0.0.1", "mqtt_port": str(b.port),
            "mqtt_response_topic": "", "mqtt_topic_template": "ksb/meter{meter_c", "server_enabled": "true"}
    assert bridge.outbound._send_mqtt(cfg_, {"data": {}}, "m1", "M1", "kwh") == "failed"      # no exception


# ------------------------------------------------------------------ sign out
def test_logout_clears_the_cookie_on_the_server(client):
    assert client.get("/api/auth/status").json()["logged_in"] is True
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/status").json()["logged_in"] is False
    assert client.get("/api/zlans").status_code == 401


def test_logout_works_even_when_not_logged_in(bridge):
    assert TestClient(bridge.app).post("/api/auth/logout").status_code == 200
