"""Runs the real poll loop against a fake Modbus device."""
import struct
import threading
import time
from datetime import datetime, timedelta


class FakeResponse:
    def __init__(self, regs): self.registers = regs
    def isError(self): return False


class FakeModbus:
    reads = []          # (unit_id, address, count) per Modbus request

    def __init__(self, host, port=502, timeout=3):
        self.connected = False
    def connect(self):
        self.connected = True
        return True
    def close(self):
        self.connected = False
    def read_holding_registers(self, address, count, slave=None, device_id=None):
        FakeModbus.reads.append((slave if slave is not None else device_id, address, count))
        regs = []
        for _ in range(count // 2):
            hi, lo = struct.unpack(">HH", struct.pack(">f", 42.5))
            regs += [hi, lo]
        return FakeResponse(regs)


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.02)
    return False


def test_md_is_polled_every_second_cycle_others_every_cycle(site, rec, monkeypatch):
    FakeModbus.reads = []
    monkeypatch.setattr(site, "ModbusTcpClient", FakeModbus)
    st = site.store
    st.set("modbus_inter_request_delay", "0")
    st.update_param_intervals("md", 30, 30)

    t = threading.Thread(target=site.poll_zlan_forever, args=("192.168.50.155",), daemon=True)
    t.start()
    addrs = lambda: [r[1] for r in FakeModbus.reads]

    # cycle 1: everything is read -- kWh, MD, volt and the three amps (one block)
    assert wait_for(lambda: len(FakeModbus.reads) >= 4)
    assert addrs() == [32784, 36880, 42514, 42530]
    assert FakeModbus.reads[-1][2] == 6                    # amp, amp2, amp3 read in one request
    assert wait_for(lambda: st.list_meters()[0]["last_poll_at"] is not None)
    time.sleep(0.2)

    # cycle 2 (15 min later): MD is not due yet (30 min) -> not read
    FakeModbus.reads.clear()
    site._next_due["M090"] = datetime.now() - timedelta(seconds=1)
    assert wait_for(lambda: len(FakeModbus.reads) >= 3)
    time.sleep(0.3)
    assert addrs() == [32784, 42514, 42530]
    assert 36880 not in addrs()

    # cycle 3 (30 min after cycle 1): MD is due again
    FakeModbus.reads.clear()
    site._param_due[("M090", "md")] = datetime.now() - timedelta(seconds=1)
    site._next_due["M090"] = datetime.now() - timedelta(seconds=1)
    assert wait_for(lambda: len(FakeModbus.reads) >= 4)
    assert 36880 in addrs()

    st.delete_zlan("192.168.50.155")                       # worker exits when its ZLAN is removed
    t.join(timeout=5)
    assert not t.is_alive()


def test_all_readings_reach_history_when_polled(site, rec, monkeypatch):
    FakeModbus.reads = []
    monkeypatch.setattr(site, "ModbusTcpClient", FakeModbus)
    st = site.store
    st.set("modbus_inter_request_delay", "0")
    t = threading.Thread(target=site.poll_zlan_forever, args=("192.168.50.155",), daemon=True)
    t.start()
    readings = lambda: [s for s in rec.sent if s[1] not in ("zlan", "meter")]   # skip connectivity events
    assert wait_for(lambda: len(readings()) >= 6)
    st.delete_zlan("192.168.50.155")
    t.join(timeout=5)
    assert sorted(s[1] for s in readings()[:6]) == sorted(["kwh", "md", "volt", "amp", "amp2", "amp3"])
    assert all(s[2] == 42.5 for s in readings()[:6])


def test_due_params_helper(site):
    st = site.store
    st.update_param_intervals("md", 30, 30)
    params = st.list_params(True)
    now = datetime.now()
    labels = lambda ps: [p["label"] for p in ps]
    assert "md" in labels(site.due_params("M090", params, now))                       # never read -> due
    site._param_due[("M090", "md")] = now + timedelta(minutes=10)
    assert "md" not in labels(site.due_params("M090", params, now))                   # not yet
    assert "kwh" in labels(site.due_params("M090", params, now))                      # no poll_interval -> every cycle
    site._param_due[("M090", "md")] = now - timedelta(seconds=1)
    assert "md" in labels(site.due_params("M090", params, now))                       # due again
