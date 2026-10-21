"""The Store shares ONE sqlite connection between the web server threads and the poll threads.
Reading a result after another thread had used the connection used to return None / a wrong row
(HTTP 500 on /api/dashboard, random 401s), most visibly on Python 3.13."""
import sqlite3
import threading
import time


def hammer(b, seconds=4.0):
    st = b.store
    st.add_zlan("1.1.1.1", "Z", 15)
    st.add_meter("M1", "1.1.1.1", 1)
    for i in range(200):
        st.enqueue("M1", "kwh", float(i))
    st.set("auth_user", "admin")
    errors = {}
    stop = time.time() + seconds

    def note(e):
        key = f"{type(e).__name__}: {str(e)[:60]}"
        errors[key] = errors.get(key, 0) + 1

    def reader():
        while time.time() < stop:
            try:
                assert st.counts()["total"] >= 0
                assert st.get("auth_user") == "admin"       # the login check reads this on every request
                st.settings_dict(); st.list_meters(); st.queue_count()
            except Exception as e:
                note(e)

    def writer():
        n = 0
        while time.time() < stop:
            try:
                n += 1
                st.mark_published("M1", "kwh"); st.log_skipped("M1", "kwh", 1.0, "x"); st.set("max_attempts", str(5 + n % 3))
            except Exception as e:
                note(e)

    ts = [threading.Thread(target=reader) for _ in range(6)] + [threading.Thread(target=writer) for _ in range(3)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return errors


def test_concurrent_readers_and_writers_never_see_a_broken_result(bridge):
    assert hammer(bridge) == {}


def test_changes_made_by_another_process_are_visible_to_the_running_app(bridge):
    """e.g. clearing the login from the command line must take effect without a restart."""
    st = bridge.store
    st.set("auth_hash", "abc")
    assert st.get("auth_hash") == "abc"
    other = sqlite3.connect("bridge.db")
    other.execute("UPDATE settings SET value='' WHERE key='auth_hash'")
    other.commit(); other.close()
    assert st.get("auth_hash") == ""


def test_result_object_behaves_like_the_cursor_it_replaces(bridge):
    st = bridge.store
    st.add_zlan("2.2.2.2", "A", 15); st.add_zlan("3.3.3.3", "B", 15)
    r = st._q("SELECT zlan_ip FROM zlans ORDER BY zlan_ip")
    assert r.fetchone() == ("2.2.2.2",) and r.fetchall() == [("3.3.3.3",)] and r.fetchone() is None
    assert [x[0] for x in st._q("SELECT zlan_ip FROM zlans ORDER BY zlan_ip")] == ["2.2.2.2", "3.3.3.3"]
    assert st._q("UPDATE zlans SET zlan_label='X' WHERE zlan_ip='2.2.2.2'").rowcount == 1
    assert st._q("SELECT COUNT(*) FROM zlans WHERE zlan_ip='nope'").fetchone() == (0,)
    assert st._q("SELECT * FROM zlans WHERE zlan_ip='nope'").fetchone() is None
