"""CU link: firmware probe, bot throttle, heartbeat, E-STOP, reconnection, shutdown."""

from __future__ import annotations

import threading
import time

import pytest
from carreralib import protocol
from carreralib.connection import Connection, ConnectionError

from racelab.config import ConnectionConfig, SafetyConfig
from racelab.cu.link import CULink
from racelab.cu.unit import RaceControlUnit, parse_message
from racelab.events import LinkState, StatusMsg, TimerMsg


class FakeCU:
    """Minimal CU shared between connections (the firmware state survives outages)."""

    def __init__(self, bots=True, version=b"5339", ctl=True):
        self.bots = bots
        self.version = version
        self.ctl = ctl  # the CU (and its adapter) accepts the C table
        self.frames: list[bytes] = []
        self.timers: list[tuple[int, int, int]] = []
        self.lock = threading.Lock()
        self.broken = False
        self.opens = 0

    def connect(self):
        if self.broken:
            raise ConnectionError("no link")
        self.opens += 1
        return FakeConnection(self)

    def bot_frames(self):
        with self.lock:
            return [f for f in self.frames if f[:1] == b"B"]

    def ctl_frames(self):
        with self.lock:
            return [f for f in self.frames if f[:1] == b"C"]


class FakeConnection(Connection):
    def __init__(self, cu: FakeCU):
        self.cu = cu
        self.reply: bytes | None = None
        self.dead = False

    def send(self, buf, offset=0, size=None):
        if self.dead or self.cu.broken:
            self.dead = True
            raise ConnectionError("link cut")
        buf = bytes(buf)
        with self.cu.lock:
            self.cu.frames.append(buf)
        letter = buf[:1]
        if letter == b"0":
            self.reply = protocol.pack("c4sC", b"0", self.cu.version)
        elif letter == b"?":
            if self.cu.timers:
                adr, ts, sector = self.cu.timers.pop(0)
                self.reply = protocol.pack("cYIYC", b"?", adr + 1, ts, sector)
            else:
                self.reply = protocol.pack("cc8YYYBYC", b"?", b":", *[9] * 8, 0, 5, 0, 6)
        elif letter == b"B":
            self.reply = b"B" if self.cu.bots else b""
        elif letter == b"C":
            self.reply = b"C" if self.cu.ctl else b""
        else:
            self.reply = letter
        time.sleep(0.001)

    def recv(self, maxlength=None):
        if self.dead or self.cu.broken:
            self.dead = True
            raise ConnectionError("link cut")
        reply, self.reply = self.reply, None
        if reply is None:
            from carreralib.connection import TimeoutError

            raise TimeoutError("nothing")
        return reply

    def close(self):
        self.dead = True


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def make_link():
    links = []

    def make(cu, bot_transport="B", **safety):
        """bot_transport "B" by default: most tests here check the bot module's protocol."""
        events = []
        s = SafetyConfig(heartbeat_ms=200, min_bot_interval_ms=40, **safety)
        link = CULink(cu.connect, transport="sim", device=None,
                      conn_cfg=ConnectionConfig(timeout=0.2, reconnect_delay=0.05,
                                                bot_transport=bot_transport),
                      safety=s, emit=events.append)
        link.start()
        links.append(link)
        assert wait_for(lambda: link.state == "connected")
        return link, events

    yield make
    for link in links:
        link.stop()


def test_parse_messages():
    status = parse_message(protocol.pack("cc8YYYBYC", b"?", b":", 1, 2, 3, 4, 5, 6, 7, 8, 3, 5, 0x81, 6))
    assert status.fuel == (1, 2, 3, 4, 5, 6, 7, 8)
    assert status.start == 3 and status.pit[0] and status.pit[7] and not status.pit[1]
    timer = parse_message(protocol.pack("cYIYC", b"?", 3, 226287, 1))
    assert (timer.address, timer.timestamp, timer.sector) == (2, 226287, 1)
    assert parse_message(b"?B") is None
    assert parse_message(b"J") is None


def test_request_routes_spontaneous_messages():
    """A timing message received while waiting for an echo is not lost."""

    class Conn(Connection):
        def __init__(self):
            self.out = [protocol.pack("cYIYC", b"?", 1, 5000, 1), b"T"]

        def send(self, buf, offset=0, size=None):
            pass

        def recv(self, maxlength=None):
            return self.out.pop(0)

    got = []
    cu = RaceControlUnit(Conn(), on_message=got.append)
    assert cu.press(2) == b"T"
    assert got and got[0].timestamp == 5000


def test_forbidden_letters_never_sent():
    cu = RaceControlUnit(FakeConnection(FakeCU()), on_message=lambda m: None)
    for frame in (b"GB", b"E00", b"C1", b"F0"):
        with pytest.raises(ValueError):
            cu.request(frame)


def test_connect_probe_and_release_all(make_link):
    cu = FakeCU()
    link, events = make_link(cu)
    states = [e for e in events if isinstance(e, LinkState)]
    assert states[-1].bots_supported is True and states[-1].version == "5339"
    # B0A probe then reset of the 6 slots, before any poll
    assert cu.bot_frames()[:7] == [b"B0A"] + [b"B%dA" % s for s in range(6)]
    assert wait_for(lambda: any(isinstance(e, StatusMsg) for e in events))


def test_stock_firmware_sends_no_bot_commands(make_link):
    cu = FakeCU(bots=False)
    link, _ = make_link(cu)
    assert link.bots_supported is False
    link.set_bot(0, 7)
    time.sleep(0.15)
    assert cu.bot_frames() == [b"B0A"]  # the probe only


def test_throttle_coalescing_and_heartbeat(make_link):
    cu = FakeCU()
    link, _ = make_link(cu)
    base = len(cu.bot_frames())
    for v in range(1, 11):
        link.set_bot(2, v)
        time.sleep(0.004)
    assert wait_for(lambda: cu.bot_frames()[-1:] == [b"B2:"])
    burst = cu.bot_frames()[base:]
    assert len(burst) < 6, burst  # coalesced (≤ 1 send / 40 ms)
    n = len(cu.bot_frames())
    time.sleep(0.45)
    beats = cu.bot_frames()[n:]
    assert beats and set(beats) == {b"B2:"}  # heartbeat 200 ms


def test_lane_one_shot_only_for_bots(make_link):
    cu = FakeCU()
    link, _ = make_link(cu)
    link.lane(4)  # human slot: ignored
    link.set_bot(1, 5)
    link.lane(1)
    assert wait_for(lambda: b"B1L" in cu.bot_frames())
    time.sleep(0.1)
    assert cu.bot_frames().count(b"B1L") == 1
    assert b"B4L" not in cu.bot_frames()


def test_estop_zeroes_first_and_holds(make_link):
    cu = FakeCU()
    link, _ = make_link(cu)
    link.set_bot(0, 9)
    link.set_bot(3, 12)
    assert wait_for(lambda: b"B3<" in cu.bot_frames())
    n = len(cu.frames)
    link.estop()
    assert wait_for(lambda: b"B30" in cu.frames[n:] and b"B00" in cu.frames[n:])
    # at most the poll already in progress goes out before the throttle 0 frames
    after = cu.frames[n:]
    assert after[: after.index(b"B00")].count(b"?") <= 1
    link.set_bot(3, 12)  # refused while the E-STOP is engaged
    time.sleep(0.1)
    assert link.bot_targets()[3] == 0
    link.clear_estop()
    link.set_bot(3, 6)
    assert wait_for(lambda: cu.bot_frames()[-1] == b"B36")


def test_reconnect_forces_zero_then_holds_until_resume(make_link):
    cu = FakeCU()
    link, events = make_link(cu)
    link.set_bot(2, 11)
    assert wait_for(lambda: b"B2;" in cu.bot_frames())
    cu.broken = True
    assert wait_for(lambda: link.state == "lost")
    link.set_bot(2, 11)  # the app insists during the outage
    n = len(cu.frames)
    cu.broken = False
    assert wait_for(lambda: link.state == "connected")
    after = [f for f in cu.frames[n:] if f[:1] == b"B"]
    assert after[:2] == [b"B0A", b"B20"]  # probe then throttle 0 before any resume
    assert cu.opens == 2
    assert "reconnect" in link.holds
    time.sleep(0.1)
    assert b"B2;" not in cu.frames[n:]
    link.resume_bots()
    link.set_bot(2, 8)
    assert wait_for(lambda: cu.bot_frames()[-1] == b"B28")


def test_stop_zeroes_and_releases(make_link):
    cu = FakeCU()
    link, _ = make_link(cu)
    link.set_bot(5, 7)
    assert wait_for(lambda: b"B57" in cu.bot_frames())
    link.stop()
    tail = cu.bot_frames()[-2:]
    assert tail == [b"B50", b"B5A"]
    assert link.unsafe_slots() == []


def test_timer_events_reach_app(make_link):
    cu = FakeCU()
    cu.timers.append((1, 123456, 1))
    link, events = make_link(cu)
    assert wait_for(lambda: any(isinstance(e, TimerMsg) for e in events))
    t = next(e for e in events if isinstance(e, TimerMsg))
    assert (t.address, t.timestamp, t.sector) == (1, 123456, 1)


def test_commands_queue(make_link):
    cu = FakeCU()
    link, _ = make_link(cu)
    link.press(2)
    link.setpos(3, 1)
    link.setlap(12)
    assert wait_for(lambda: any(f[:1] == b"T" for f in cu.frames))
    assert wait_for(lambda: sum(f[:1] == b"J" for f in cu.frames) >= 3)
    with pytest.raises(ValueError):
        link.send_raw(b"GB")
    with pytest.raises(ValueError):
        link.send_raw(b"B07")


# ------------------------------------------------------------ stock C table (no custom firmware)

def ctl_values(frame: bytes):
    """Decode a C frame into (throttles, lanes) like the stock firmware: 0 = controller drives."""
    vals, lanes = [], set()
    for i in range(6):
        t, lane = frame[1 + 2 * i], frame[2 + 2 * i]
        v = ((t - 0x30) & 0xFF) | (0x10 if lane == ord("1") else 0)
        vals.append(None if v == 0 else v & 0x0F)
        if v & 0x10:
            lanes.add(i)
    return vals, lanes


def test_auto_transport_uses_the_stock_c_table_on_v339(make_link):
    cu = FakeCU(bots=False)                       # stock firmware: no B module
    link, events = make_link(cu, bot_transport="auto")
    assert link.bot_transport == "C" and link.bots_supported is True and link.bot_module is False
    state = [e for e in events if isinstance(e, LinkState)][-1]
    assert state.bot_transport == "C" and state.bots_supported
    link.set_bot(2, 9)
    assert wait_for(lambda: cu.ctl_frames() and ctl_values(cu.ctl_frames()[-1])[0][2] == 9)
    vals, _ = ctl_values(cu.ctl_frames()[-1])
    assert vals == [None, None, 9, None, None, None]   # the other slots stay on their controllers
    assert not cu.bot_frames() or all(f == b"B0A" for f in cu.bot_frames())


def test_c_table_heartbeat_hold_lane_release(make_link):
    cu = FakeCU(bots=False)
    link, _ = make_link(cu, bot_transport="auto")
    link.set_bot(0, 7)
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0][0] == 7)
    n = len(cu.ctl_frames())
    time.sleep(0.75)                              # nothing changes: frames keep coming (< 0.75 s expiry)
    assert len(cu.ctl_frames()) - n >= 2
    link.lane(0)
    assert wait_for(lambda: 0 in ctl_values(cu.ctl_frames()[-1])[1])
    assert wait_for(lambda: 0 not in ctl_values(cu.ctl_frames()[-1])[1], timeout=1.0)  # button released
    link.set_bot(0, 0)                            # host-held stop: P, not "00"
    assert wait_for(lambda: cu.ctl_frames()[-1][1:3] == b"P0")
    link.set_bot(0, None)                         # handed back
    assert wait_for(lambda: cu.ctl_frames()[-1][1:13] == b"0" * 12)
    n = len(cu.ctl_frames())
    time.sleep(0.5)
    assert len(cu.ctl_frames()) == n              # no host value: no more frames


def test_c_table_estop_holds_every_bot(make_link):
    cu = FakeCU(bots=False)
    link, _ = make_link(cu, bot_transport="auto")
    link.set_bot(1, 12)
    link.set_bot(3, 5)
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0][1] == 12)
    link.estop()
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0] == [None, 0, None, 0, None, None])


def test_auto_transport_without_c(make_link):
    """C exists from V338 on; older CUs (V336/V337) fall back to the B module, or no bots."""
    link, _ = make_link(FakeCU(bots=True, version=b"5337"), bot_transport="auto")
    assert link.bot_transport == "B"
    link2, _ = make_link(FakeCU(bots=False, version=b"5337"), bot_transport="auto")
    assert link2.bot_transport is None and link2.bots_supported is False


def test_auto_transport_uses_c_on_v338(make_link):
    link, _ = make_link(FakeCU(bots=False, version=b"5338"), bot_transport="auto")
    assert link.bot_transport == "C" and link.bots_supported is True


def test_c_rejected_falls_back(make_link):
    cu = FakeCU(bots=True, ctl=False)             # e.g. an adapter that filters C
    link, _ = make_link(cu, bot_transport="auto")
    link.set_bot(0, 6)
    assert wait_for(lambda: link.bot_transport == "B")
    assert wait_for(lambda: any(f.startswith(b"B0") and f != b"B0A" for f in cu.bot_frames()))


# ------------------------------------------------------------ hardware test: heartbeat pause, bench

def test_heartbeat_pause_holds_the_idle_refresh_and_expires_by_itself(make_link):
    cu = FakeCU(bots=False)
    link, _ = make_link(cu, bot_transport="auto")   # heartbeat 200 ms
    link.set_bot(2, 7)
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0][2] == 7)
    link.pause_heartbeat(0.6)
    assert link.hb_paused
    n = len(cu.ctl_frames())
    time.sleep(0.45)                                # > 2 heartbeats: no refresh
    assert len(cu.ctl_frames()) == n and link.bot_targets()[2] == 7
    # the pause ends: the bot is zeroed, never resumed at 7
    assert wait_for(lambda: not link.hb_paused and link.bot_targets()[2] == 0, 1.0)
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0][2] == 0)
    assert all(ctl_values(f)[0][2] in (0, None) for f in cu.ctl_frames()[n:])


def test_heartbeat_pause_is_capped_at_three_seconds(make_link):
    link, _ = make_link(FakeCU(bots=False), bot_transport="auto")
    link.pause_heartbeat(60)
    with link._lock:
        assert link._hb_pause_until - time.monotonic() <= 3.0
    link.resume_heartbeat()
    assert not link.hb_paused


def test_heartbeat_pause_still_lets_changes_and_estop_through(make_link):
    cu = FakeCU(bots=False)
    link, _ = make_link(cu, bot_transport="auto")
    link.set_bot(1, 9)
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0][1] == 9)
    link.pause_heartbeat(3)
    link.set_bot(1, 0)                              # a change goes out during the pause
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0][1] == 0)
    link.set_bot(1, 8)
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0][1] == 8)
    link.estop()                                    # E-STOP: frame at once, and the pause is gone
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0][1] == 0 and not link.hb_paused, 0.5)


def test_heartbeat_pause_cleared_by_link_loss(make_link):
    cu = FakeCU(bots=False)
    link, _ = make_link(cu, bot_transport="auto")
    link.set_bot(1, 4)
    assert wait_for(lambda: ctl_values(cu.ctl_frames()[-1])[0][1] == 4)
    link.pause_heartbeat(3)
    cu.broken = True
    assert wait_for(lambda: link.state != "connected", 2.0)
    assert not link.hb_paused
    cu.broken = False


def test_heartbeat_pause_refused_on_the_b_transport(make_link):
    link, _ = make_link(FakeCU(), bot_transport="B")
    with pytest.raises(ValueError):
        link.pause_heartbeat(1)
    assert not link.hb_paused


def test_bench_idle_adds_only_all_controllers_frames(make_link):
    cu = FakeCU(bots=False)
    link, _ = make_link(cu, bot_transport="auto")
    done = []
    link.bench(25, lambda result, error: done.append((result, error)))
    assert wait_for(lambda: done, 5.0)
    result, error = done[0]
    assert error is None and result["answered"] == 25 and result["c_ack"]["n"] >= 1
    assert cu.ctl_frames() and all(f[1:13] == b"0" * 12 for f in cu.ctl_frames())
    link.bench(5, lambda r, e: None)
    with pytest.raises(RuntimeError):               # one at a time
        link.bench(5, lambda r, e: None)
    link.bench_cancel()


def test_bench_ended_by_a_link_loss(make_link):
    cu = FakeCU(bots=False)
    link, _ = make_link(cu, bot_transport="auto")
    done = []
    link.bench(300, lambda result, error: done.append((result, error)))
    cu.broken = True
    assert wait_for(lambda: done, 3.0)
    assert done[0][1] == "link lost"
    cu.broken = False


def test_bench_refused_on_a_closed_link(make_link):
    link, _ = make_link(FakeCU(bots=False), bot_transport="auto")
    link.stop()
    with pytest.raises(ValueError):
        link.bench(5, lambda r, e: None)


def test_a_cu_that_gives_no_version_is_lost_with_a_reason_code():
    """What a CU left in its bootloader looks like: it connects, but never answers the version request."""
    cu = FakeCU()

    class NoVersion(FakeConnection):
        def send(self, buf, offset=0, size=None):
            super().send(buf, offset, size)
            if bytes(buf)[:1] == b"0":
                self.reply = None

    events = []
    link = CULink(lambda: NoVersion(cu), transport="sim", device=None,
                  conn_cfg=ConnectionConfig(timeout=0.2, reconnect_delay=0.05), safety=SafetyConfig(),
                  emit=events.append)
    link.start()
    try:
        assert wait_for(lambda: any(isinstance(e, LinkState) and e.state == "lost" for e in events), 3.0)
    finally:
        link.stop()
    lost = [e for e in events if isinstance(e, LinkState) and e.state == "lost"]
    assert lost[0].error_code == "no_version" and "version" in lost[0].error
