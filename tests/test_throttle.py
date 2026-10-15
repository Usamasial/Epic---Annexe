from datetime import datetime, timedelta

ALL = ["kwh", "md", "volt", "amp", "amp2", "amp3"]


def poll_once(b, meter="M090", values=None):
    """What one poll cycle does: hand every parameter to handle_reading, in register order."""
    for i, label in enumerate(ALL):
        b.handle_reading(meter, label, (values or {}).get(label, 10.0 + i))


def age(b, meter, label, minutes):
    """Pretend this parameter was last published `minutes` ago."""
    b.store.mark_published(meter, label, datetime.now() - timedelta(minutes=minutes))


def test_regression_all_six_parameters_published_in_one_poll(site, rec):
    """v1.0 bug: the throttle was per meter, so after kWh went out MD/volt/amp were dropped."""
    poll_once(site)
    assert rec.params() == ALL


def test_second_poll_inside_interval_is_dropped(site, rec):
    poll_once(site)
    rec.sent.clear()
    poll_once(site)
    assert rec.sent == []


def test_each_parameter_has_its_own_clock(site, rec):
    poll_once(site)
    rec.sent.clear()
    age(site, "M090", "kwh", 181)       # only kWh is past its 180 min window
    poll_once(site)
    assert rec.params() == ["kwh"]


def test_md_30_minute_publish_interval(site, rec):
    site.store.update_param_intervals("md", 30, 30)
    poll_once(site)
    rec.sent.clear()
    age(site, "M090", "md", 29)
    poll_once(site)
    assert "md" not in rec.params()
    age(site, "M090", "md", 31)
    poll_once(site)
    assert rec.params() == ["md"]       # others (180 min) still held back


def test_interval_precedence_parameter_then_meter_then_zlan(site):
    st = site.store
    assert st.publish_interval_for("M090", "volt") == 180           # ZLAN default
    st.update_meter_publish_interval("M090", 60)
    assert st.publish_interval_for("M090", "volt") == 60            # meter beats ZLAN
    st.update_param_intervals("volt", None, 15)
    assert st.publish_interval_for("M090", "volt") == 15            # parameter beats meter
    assert st.publish_interval_for("M090", "kwh") == 60             # other parameters unaffected


def test_throttle_off_publishes_every_reading(site, rec):
    site.store.set("publish_throttle_enabled", "false")
    poll_once(site)
    poll_once(site)
    assert len(rec.sent) == 12


def test_no_interval_anywhere_means_no_throttle(site, rec):
    site.store.update_zlan_publish_interval("192.168.50.155", None)
    poll_once(site)
    poll_once(site)
    assert len(rec.sent) == 12


def test_dropped_readings_can_be_logged(site, rec):
    site.store.set("log_skipped_events", "true")
    poll_once(site)
    poll_once(site)
    n = site.store._q("SELECT COUNT(*) FROM skipped_log").fetchone()[0]
    assert n == 6


def test_small_poll_drift_does_not_cost_a_whole_cycle(site, rec):
    """A poll that arrives a few seconds early must still publish (tolerance)."""
    site.store.update_param_intervals("md", 30, 30)
    age(site, "M090", "md", 29.8)       # 12 s early
    site.handle_reading("M090", "md", 1.0)
    assert rec.params() == ["md"]


def test_rename_meter_keeps_publish_state(site, rec):
    poll_once(site)
    site.store.rename_meter("M090", "M999")
    rec.sent.clear()
    poll_once(site, meter="M999")        # renamed meter must NOT be treated as never-published
    assert rec.sent == []
