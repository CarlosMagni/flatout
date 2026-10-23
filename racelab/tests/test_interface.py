"""Controller interfaces (racelab/drive/): the bot model, its safety rules and the serial wire protocol
(docs/controller-interface.md), apart from the CU link."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from racelab.config import SafetyConfig
from racelab.cu.unit import RaceControlUnit
from racelab.drive import SerialInterface, bluetooth, rfcomm
from racelab.drive import interface as interface_mod
from racelab.drive.serial import parse_report
from racelab.events import BotSent, InterfaceState

from .test_link import FakeCU, wait_for

PICO = Path(__file__).resolve().parents[2] / "controller_interfaces" / "pico" / "main.py"
_spec = importlib.util.spec_from_file_location("pico_main", PICO)
pico_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pico_main)  # CPython: no rp2, only the pure functions


class FakePort:
    """A serial port as the interface sees it: frames written, report lines to read, failures."""

    def __init__(self):
        self.frames: list[bytes] = []
        self.inbox = bytearray()
        self.broken = False
        self.opens = 0
        self.lock = threading.Lock()

    def open(self, device, baud):
        if self.broken:
            raise OSError("port missing")
        self.opens += 1
        return self

    def write(self, frame):
        if self.broken:
            raise OSError("Bluetooth link dropped")
        with self.lock:
            self.frames.append(bytes(frame))

    def read(self, n):
        with self.lock:
            data, self.inbox = bytes(self.inbox[:n]), self.inbox[n:]
        return data

    def say(self, line: bytes):
        with self.lock:
            self.inbox += line

    def close(self):
        pass

    def slots(self, i=-1):
        """What the reference device (controller_interfaces/pico) makes of a frame."""
        with self.lock:
            return pico_main.parse(self.frames[i])


@pytest.fixture
def make_interface(monkeypatch):
    monkeypatch.setattr(SerialInterface, "RETRY_S", 0.05)
    made = []

    def make(port=None, device="fake-port", **safety):
        port = port or FakePort()
        events = []
        iface = SerialInterface(device, SafetyConfig(**safety), events.append, opener=port.open)
        iface.start()
        made.append(iface)
        return iface, port, events

    yield make
    for iface in made:
        iface.stop()


def states(events):
    return [e.state for e in events if isinstance(e, InterfaceState)]


def test_frames_match_the_reference_device():
    """RaceLab's frame is exactly what the reference device accepts (checksum, P for a held 0, lanes)."""
    cases = [([None] * 6, ()), ([0, 15, None, 7, 1, None], (1, 3)), ([9] * 6, (0, 5))]
    for values, lanes in cases:
        frame = b'"' + RaceControlUnit.ctl_frame(values, lanes) + b"$"
        want = [None if v is None else (v, a in lanes) for a, v in enumerate(values)]
        assert pico_main.parse(frame) == want, frame


def test_reports_are_key_value_lines():
    assert parse_report(b"pico sync=on period=73.5ms late=0") == {
        "name": "pico", "sync": "on", "period": "73.5ms", "late": "0"}
    assert parse_report(b"sync=lost") == {"sync": "lost"}
    assert parse_report(b"hello world") is None             # no key=value: not a report
    assert parse_report(b"") is None


def test_drives_the_slots_at_once(make_interface):
    iface, port, events = make_interface()
    assert wait_for(lambda: iface.state == "connected")
    iface.set_bot(2, 9)
    t0 = time.monotonic()
    assert wait_for(lambda: port.frames and port.slots()[2] == (9, False))
    assert time.monotonic() - t0 < 0.06                      # its own thread: no CU poll in the way
    assert port.slots() == [None, None, (9, False), None, None, None]
    assert iface.bot_applied()[2] == 9
    assert any(isinstance(e, BotSent) and e.slot == 2 and e.value == 9 for e in events)


def test_heartbeat_lane_and_estop(make_interface):
    iface, port, _ = make_interface()
    iface.set_bot(0, 7)
    iface.set_bot(4, 3)
    assert wait_for(lambda: port.frames and port.slots()[0] == (7, False))
    n = len(port.frames)
    time.sleep(0.5)                                          # unchanged: repeated well inside 750 ms
    assert len(port.frames) - n >= 3
    iface.lane(0)
    assert wait_for(lambda: port.slots()[0] == (7, True))
    assert wait_for(lambda: port.slots()[0] == (7, False), timeout=1.0)   # released after the press
    iface.lane(1)                                            # not a bot: ignored
    iface.estop()
    assert wait_for(lambda: port.slots() == [(0, False), None, None, None, (0, False), None])
    iface.set_bot(0, 12)                                     # held at 0 until cleared
    time.sleep(0.05)
    assert port.slots()[0] == (0, False)
    iface.clear_estop()
    iface.set_bot(0, 12)
    assert wait_for(lambda: port.slots()[0] == (12, False))


def test_stop_sends_the_release_frame_last(make_interface):
    iface, port, _ = make_interface()
    iface.set_bot(5, 7)
    assert wait_for(lambda: port.frames and port.slots()[5] == (7, False))
    iface.stop()
    assert port.slots() == [None] * 6
    assert iface.unsafe_slots() == []


def test_stop_can_hold_the_bots_at_zero(make_interface):
    iface, port, _ = make_interface(release_on_exit=False)
    iface.set_bot(1, 6)
    assert wait_for(lambda: port.frames and port.slots()[1] == (6, False))
    iface.stop()
    assert port.slots() == [None, (0, False), None, None, None, None]


def test_lost_port_zeroes_holds_and_never_resumes_the_old_throttle(make_interface):
    iface, port, events = make_interface()
    iface.set_bot(1, 11)
    assert wait_for(lambda: port.frames and port.slots()[1] == (11, False))
    port.broken = True
    assert wait_for(lambda: "lost" in states(events))
    assert "reconnect" in iface.holds and iface.bot_targets()[1] == 0
    iface.set_bot(1, 11)                                     # the app insists during the outage: 0
    n = len(port.frames)
    port.broken = False
    assert wait_for(lambda: len(port.frames) > n)
    time.sleep(0.2)
    assert all(pico_main.parse(f)[1] == (0, False) for f in port.frames[n:])
    iface.resume_bots()                                      # what the app does once it zeroed its bots
    iface.set_bot(1, 8)
    assert wait_for(lambda: port.slots()[1] == (8, False))


def test_a_device_that_reported_then_went_silent_is_lost(make_interface, monkeypatch):
    monkeypatch.setattr(SerialInterface, "SILENT_S", 0.2)
    iface, port, events = make_interface()
    assert wait_for(lambda: iface.state == "connected")
    port.say(b"pico sync=on period=73.5ms late=0\n")
    assert wait_for(lambda: iface.info.get("sync") == "on")
    assert any(isinstance(e, InterfaceState) and e.info.get("period") == "73.5ms" for e in events)
    assert wait_for(lambda: "lost" in states(events), timeout=1.5)   # no report since: reopened
    assert wait_for(lambda: port.opens >= 2)


def test_a_device_that_never_reports_is_not_watched(make_interface, monkeypatch):
    monkeypatch.setattr(SerialInterface, "SILENT_S", 0.1)
    iface, port, events = make_interface()
    iface.set_bot(0, 4)
    time.sleep(0.4)
    assert "lost" not in states(events) and port.opens == 1


def test_a_vanished_device_node_is_lost(make_interface):
    """macOS drops a Bluetooth port's /dev node with the link, while the open file still takes writes."""
    iface, port, events = make_interface(device="/dev/racelab-test-no-such-port")
    assert wait_for(lambda: "lost" in states(events))


def test_missing_port_is_retried(make_interface):
    port = FakePort()
    port.broken = True
    iface, _, events = make_interface(port)
    assert wait_for(lambda: any(isinstance(e, InterfaceState) and e.state == "connecting" and e.error
                                for e in events))
    iface.set_bot(0, 5)                                      # nowhere to go yet: kept, nothing crashes
    port.broken = False
    assert wait_for(lambda: iface.state == "connected")
    assert wait_for(lambda: port.frames and port.slots()[0] == (5, False))


def test_cu_link_loss_holds_the_bots(make_interface):
    """The CU link lost, the race is blind: throttle 0 and hold, as the CU link does for its own bots."""
    iface, port, _ = make_interface()
    iface.set_bot(3, 10)
    assert wait_for(lambda: port.frames and port.slots()[3] == (10, False))
    iface.on_cu_link("lost")
    assert wait_for(lambda: port.slots()[3] == (0, False))
    assert "reconnect" in iface.holds


def test_bluetooth_pairing_through_blueutil(monkeypatch):
    """Search lists only unpaired modules; pairing drops a stale pairing first, then waits for the port."""
    calls, ports = [], [{"device": "/dev/cu.debug-x", "label": "x"}]

    def fake_run(cmd, **kw):
        calls.append(cmd[1:])
        if "--inquiry" in cmd:
            out = json.dumps([{"address": "00-22-09-01-82-7a", "name": "racelab-wireless", "paired": False},
                              {"address": "aa-bb-cc-dd-ee-ff", "name": "iPhone", "paired": True}])
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[1] == "--unpair":
            return subprocess.CompletedProcess(cmd, 1, "", "not paired")
        ports.append({"device": "/dev/cu.racelab-wireless", "label": "racelab-wireless"})  # --pair
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bluetooth, "blueutil", lambda: "/fake/blueutil")
    monkeypatch.setattr(bluetooth.subprocess, "run", fake_run)
    monkeypatch.setattr(rfcomm, "available", lambda: False)     # the /dev/cu. port macOS creates
    monkeypatch.setattr(bluetooth, "list_ports", lambda: list(ports))
    assert bluetooth.devices(1) == [{"address": "00-22-09-01-82-7a", "name": "racelab-wireless"}]
    assert bluetooth.pair("00-22-09-01-82-7a", "racelab-wireless") == "/dev/cu.racelab-wireless"
    assert calls[1:] == [["--unpair", "00-22-09-01-82-7a"], ["--pair", "00-22-09-01-82-7a", "1234"]]
    monkeypatch.setattr(rfcomm, "available", lambda: True)      # RaceLab opens the RFCOMM channel itself
    assert bluetooth.pair("00-22-09-01-82-7a", "racelab-wireless") == "bt:00-22-09-01-82-7a"
    monkeypatch.setattr(bluetooth, "blueutil", lambda: None)     # not macOS / not installed
    with pytest.raises(RuntimeError, match="blueutil"):
        bluetooth.devices(1)


NO_WINDOWS_PIPES = pytest.mark.skipif(sys.platform == "win32", reason="non-blocking pipes (macOS only in use)")

# a stand-in for the RFCOMM helper: the opening verdict, then an echo of what RaceLab writes
FAKE_HELPER = """
import os, sys, time
if sys.argv[1] == "slow":      # a module that takes its time to connect
    time.sleep(30)
if sys.argv[1] == "off":
    print("error Bluetooth: cannot open the module's serial channel (off, or out of range?)", flush=True)
    sys.exit(1)
os.write(1, b"ok\\npico sync=on\\n")
while True:
    data = os.read(0, 4096)
    if not data or data.startswith(b"bye"):
        break
    os.write(1, data)
"""


@NO_WINDOWS_PIPES
def test_rfcomm_port_relays_through_its_helper(monkeypatch, tmp_path):
    script = tmp_path / "helper.py"
    script.write_text(FAKE_HELPER)
    monkeypatch.setattr(rfcomm, "_helper", lambda *args: [sys.executable, str(script), *args])
    port = rfcomm.RfcommPort("00-22-09-01-82-7a")             # the 'ok' line is the helper's, not read back
    got = bytearray()
    assert wait_for(lambda: got.extend(port.read(4096)) or bytes(got) == b"pico sync=on\n")
    port.write(b'"C000000000000$')
    assert wait_for(lambda: got.extend(port.read(4096)) or bytes(got).endswith(b'"C000000000000$'))
    port.write(b"bye")                                       # the helper ends: the link is down
    with pytest.raises(OSError, match="link down"):
        for _ in range(200):
            port.read(4096)
            time.sleep(0.01)
    port.close()
    with pytest.raises(OSError, match="out of range"):
        rfcomm.RfcommPort("off")


@NO_WINDOWS_PIPES
def test_rfcomm_opening_stops_with_the_interface(monkeypatch, tmp_path):
    """An interface stopped while its module connects does not wait for it: the helper is killed."""
    script = tmp_path / "helper.py"
    script.write_text(FAKE_HELPER)
    monkeypatch.setattr(rfcomm, "_helper", lambda *args: [sys.executable, str(script), *args])
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    t = time.monotonic()
    with pytest.raises(OSError, match="cancelled"):
        rfcomm.RfcommPort("slow", stop.is_set)
    assert time.monotonic() - t < 2.0


def test_bt_devices_go_through_rfcomm(monkeypatch):
    """bt:<address> is opened through RFCOMM, and never without the Bluetooth permission (macOS would kill RaceLab)."""
    iface = SerialInterface("bt:00-22-09-01-82-7a", SafetyConfig(), lambda e: None)
    assert iface._opener.func is rfcomm.open_port and iface._opener.keywords["cancelled"] == iface._stop.is_set
    assert SerialInterface("/dev/cu.usbmodem1", SafetyConfig(), lambda e: None)._opener is not rfcomm.open_port
    assert rfcomm.address_of("bt:00-22-09-01-82-7a") == "00-22-09-01-82-7a" and rfcomm.address_of("COM3") is None
    monkeypatch.setattr(rfcomm, "available", lambda: False)
    with pytest.raises(OSError, match="Bluetooth unavailable"):
        rfcomm.open_port("bt:00-22-09-01-82-7a")
    assert rfcomm.modules() == []


# ------------------------------------------------------------ in the app

def test_app_drives_a_real_cu_through_the_interface(tmp_path, monkeypatch):
    """A CU that takes no bots itself (hardware 8: no C, no B) plus an interface: the UI's fader and
    STOP reach the interface; the CU gets no C frame; the interface stops with the release frame."""
    from racelab import app as app_mod
    from racelab.drive import serial as serial_mod

    from .test_api import make_config, op, run, started, stopped, wait_until

    monkeypatch.setattr(SerialInterface, "RETRY_S", 0.05)
    cu, port = FakeCU(bots=False, version=b"8338"), FakePort()
    monkeypatch.setattr(app_mod.RaceLabApp, "_make_opener", lambda self: ("serial", "fake", cu.connect))
    monkeypatch.setattr(serial_mod, "open_port", port.open)

    async def main():
        cfg = make_config(tmp_path)
        cfg.connection.timeout = 0.2
        app, client = await started(cfg)
        try:
            assert app.link_info["bots_supported"] is False          # this CU alone: no bots
            # not a /dev path: the interface would find no such node there and call the port lost
            assert (await op(client, "interface.set", port="fake-port"))["ok"]
            assert await wait_until(lambda: app.link_info["interface"] == "connected")
            assert app.link_info["bot_transport"] == "I" and app.mode == "advanced"
            assert app.link_info["state"] == "connected"               # the CU link carried on
            assert (await op(client, "bot.take", slot=3, driver="manual"))["ok"]
            assert (await op(client, "bot.throttle", slot=3, value=6))["ok"]
            assert await wait_until(lambda: port.frames and port.slots()[3] == (6, False))
            assert (await op(client, "estop"))["ok"]
            assert await wait_until(lambda: port.slots()[3] == (0, False))
            assert not cu.ctl_frames()                                   # nothing for the CU itself
            port.say(b"pico sync=on period=73.52ms late=0\n")
            assert await wait_until(lambda: app.link_info["interface_info"].get("period") == "73.52ms")
        finally:
            await stopped(app, client)
        assert port.slots() == [None] * 6                                # every slot handed back on exit

    run(main())


def test_interface_choice_ops_and_persistence(tmp_path):
    """The port is kept apart from the CU choice and saved; next to the virtual CU it stays unused."""
    from .test_api import make_config, op, run, started, stopped

    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            scan = await op(client, "interface.scan")
            assert scan["ok"] and isinstance(scan["result"]["ports"], list)
            assert (await op(client, "interface.set", port="/dev/cu.HC-05"))["ok"]
            info = app.link_info
            assert info["interface_device"] == "/dev/cu.HC-05" and info["interface"] is None
            assert info["transport"] == "sim" and app.interface is None
            saved = json.loads((tmp_path / "settings.json").read_text())
            assert saved["interface_choice"] == "/dev/cu.HC-05"
            assert (await op(client, "interface.set", port=""))["ok"]
            assert app.link_info["interface_device"] is None
            assert (await op(client, "interface.set", port=42))["ok"] is False
        finally:
            await stopped(app, client)

    run(main())


def test_legacy_pico_choice_is_read():
    from racelab.config import Config, _merge

    cfg = Config()
    _merge(cfg, {"pico_choice": "/dev/cu.racelab-wireless"})
    assert cfg.interface.port == cfg.interface_choice == "/dev/cu.racelab-wireless"


def test_interface_base_needs_open_and_send():
    base = interface_mod.ControllerInterface("x", SafetyConfig(), lambda m: None)
    with pytest.raises(NotImplementedError):
        base.open()
