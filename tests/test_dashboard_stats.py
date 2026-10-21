"""A large history made every dashboard request scan the whole table while holding the one database lock,
starving the poll threads, the MQTT acknowledgements and the login check (v1.2.1)."""
import sqlite3
import threading
import time


def naive(st):
    q = lambda s: st._q(s).fetchone()[0]
    return {"released": q("SELECT COUNT(*) FROM history WHERE status='released'"),
            "failed": q("SELECT COUNT(*) FROM history WHERE status='failed'"),
            "total": q("SELECT COUNT(*) FROM history"), "queued": q("SELECT COUNT(*) FROM queue")}


def populate(b):
    st = b.store
    st.add_zlan("1.1.1.1", "A", 15); st.add_zlan("2.2.2.2", "B", 15)
    st.add_meter("M1", "1.1.1.1", 1); st.add_meter("M2", "1.1.1.1", 2); st.add_meter("M3", "2.2.2.2", 1)
    for i, (m, status) in enumerate([("M1", "released")] * 5 + [("M2", "failed")] * 2 + [("M3", "released")] * 3 + [("GONE", "released")]):
        st._q("INSERT INTO history(meter_code,parameter,value,date_time,msg_no,status,created_at) VALUES(?,?,?,?,?,?,?)",
              (m, "kwh", 1.0, "2026-10-01 00:00:00", f"x{i}", status, "2026-10-01 00:00:00.0"))
    st.c.commit()
    for i in range(4): st.enqueue("M3" if i < 3 else "M1", "kwh", float(i))
    st._stats = None                      # enqueue also wrote history rows


def test_counts_match_a_plain_count_of_every_table(bridge):
    populate(bridge)
    st = bridge.store
    got = st.counts()
    assert got == naive(st)
    per = st.per_zlan_message_counts()
    assert per["1.1.1.1"] == {"queued": 1, "released": 5, "failed": 2}
    assert per["2.2.2.2"]["released"] == 3 and per["2.2.2.2"]["queued"] == 3 and per["2.2.2.2"]["failed"] == 0
    assert got["total"] > got["released"] + got["failed"] - 1          # rows of unknown meters still count in the total


def test_result_is_cached_and_refreshed_after_it_expires(bridge):
    populate(bridge)
    st = bridge.store
    first = st.counts()
    st._q("DELETE FROM history WHERE status='failed'"); st.c.commit()
    assert st.counts() == first                                         # still the cached numbers
    st._stats["until"] = 0                                              # time passes
    assert st.counts()["failed"] == 0


def test_recompute_interval_grows_with_the_cost(bridge):
    populate(bridge)
    st = bridge.store
    st.counts()
    assert st._stats["until"] - time.time() >= 1.5                      # never hammers the database
    assert st._stats["until"] - time.time() <= 120.5


def test_other_threads_get_the_old_numbers_instead_of_piling_on(bridge, monkeypatch):
    populate(bridge)
    st = bridge.store
    st.counts(); st._stats["until"] = 0
    release = threading.Event(); started = threading.Event()
    real = st._compute_stats
    def slow():
        started.set(); release.wait(5); return real()
    monkeypatch.setattr(st, "_compute_stats", slow)
    t = threading.Thread(target=st.counts); t.start(); assert started.wait(2)
    t0 = time.time(); stale = st.counts()                               # second caller while the first is computing
    assert time.time() - t0 < 0.5 and stale["total"] > 0
    release.set(); t.join()


def test_dashboard_does_not_need_the_main_database_lock(bridge):
    """The point of the fix: heavy reads run on their own connection."""
    populate(bridge)
    st = bridge.store
    st._stats = None
    done = []
    with st.lock:                                                       # e.g. a poll thread in the middle of a write
        t = threading.Thread(target=lambda: done.append((st.counts(), st.history(limit=5), st.queue_page(limit=5))))
        t.start(); t.join(2)
    assert done, "dashboard / history / queue pages were blocked by the main lock"


def test_a_slow_read_does_not_block_writers_or_the_login_check(bridge):
    st = bridge.store
    st.set("auth_user", "admin")
    with st._rlock:                                                     # a long History query in progress
        t0 = time.time()
        assert st.get("auth_user") == "admin"
        st.mark_published("M1", "kwh")
        assert time.time() - t0 < 0.5


def test_reads_see_what_was_just_committed(bridge):
    populate(bridge)
    st = bridge.store
    st.enqueue("M1", "md", 9.5)
    items, total = st.history(meter_code="M1", parameter="md")
    assert total == 1 and items[0]["value"] == 9.5
    q_items, q_total = st.queue_page(search="md")
    assert q_total == 1


def test_falls_back_to_the_main_connection_if_read_only_open_fails(bridge, monkeypatch):
    populate(bridge)
    st = bridge.store
    st._rc = None
    real = sqlite3.connect
    monkeypatch.setattr(bridge.sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("nope")) if k.get("uri") else real(*a, **k))
    st._stats = None
    assert st.counts()["total"] > 0 and st._rc is False
