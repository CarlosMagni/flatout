"""Pico controller interface (cu/pico.py, transport "P"): the C frame goes to the Pico's serial
port instead of the CU, with the same heartbeat, E-STOP, hold and release rules."""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import pytest

from racelab.config import ConnectionConfig, SafetyConfig
from racelab.cu import pico as pico_mod
from racelab.cu.link import CULink
from racelab.cu.unit import RaceControlUnit
from racelab.events import PicoState

from .test_link import FakeCU, wait_for

MAIN = Path(__file__).resolve().parents[2] / "tools" / "pico_controllers" / "main.py"
_spec = importlib.util.spec_from_file_location("pico_main", MAIN)
pico_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pico_main)  # CPython: no rp2, only the pure functions


class FakePort:
    """The HC-05's serial port as the link sees it; `broken` makes open and write fail."""

    def __init__(self):
        self.frames: list[bytes] = []
        self.broken = False
        self.opens = 0
        self.lock = threading.Lock()

    def open(self):
        if self.broken:
            raise OSError("port missing")
        self.opens += 1
        return self

    def write(self, frame):
        if self.broken:
            raise OSError("Bluetooth link dropped")
        with self.lock:
            self.frames.append(bytes(frame))

    def close(self):
        pass

    def slots(self, i=-1):
        """What the Pico's own parser makes of a frame: [(throttle, lane) | None] * 6."""
        with self.lock:
            return pico_main.parse(self.frames[i])


@pytest.fixture
def make_pico_link(monkeypatch):
    monkeypatch.setattr(pico_mod, "RETRY_S", 0.05)
    links = []

    def make(cu=None, port=None):
        cu, port = cu or FakeCU(bots=False, version=b"8338"), port or FakePort()
        events = []
        link = CULink(cu.connect, transport="sim", device=None,
                      conn_cfg=ConnectionConfig(timeout=0.2, reconnect_delay=0.05),
                      safety=SafetyConfig(heartbeat_ms=200, min_bot_interval_ms=40),
                      emit=events.append, pico_opener=port.open, pico_device="/dev/cu.HC-05")
        link.start()
        links.append(link)
        assert wait_for(lambda: link.state == "connected")
        return link, cu, port, events

    yield make
    for link in links:
        link.stop()


def test_frames_match_the_pico_parser():
    """RaceLab's C frame is exactly what the Pico accepts (checksum, P for a held 0, lanes)."""
    cases = [([None] * 6, ()), ([0, 15, None, 7, 1, None], (1, 3)), ([9] * 6, (0, 5))]
    for values, lanes in cases:
        frame = b'"' + RaceControlUnit.ctl_frame(values, lanes) + b"$"
        want = [None if v is None else (v, a in lanes) for a, v in enumerate(values)]
        assert pico_main.parse(frame) == want, frame


def test_any_cu_drives_through_the_pico(make_pico_link):
    link, cu, port, events = make_pico_link()       # hardware 8: no C, no B
    assert link.bot_transport == "P" and link.bots_supported is True
    link.set_bot(2, 9)
    assert wait_for(lambda: port.frames and port.slots()[2] == (9, False))
    assert port.slots() == [None, None, (9, False), None, None, None]
    assert not cu.ctl_frames()                       # nothing for the CU itself
    assert any(isinstance(e, PicoState) and e.state == "connected" for e in events)


def test_heartbeat_lane_and_estop(make_pico_link):
    link, _, port, _ = make_pico_link()
    link.set_bot(0, 7)
    link.set_bot(4, 3)
    assert wait_for(lambda: port.frames and port.slots()[0] == (7, False))
    n = len(port.frames)
    time.sleep(0.75)                                 # nothing changes: frames keep coming (Pico expiry 750 ms)
    assert len(port.frames) - n >= 2
    link.lane(0)
    assert wait_for(lambda: port.slots()[0] == (7, True))
    link.estop()
    assert wait_for(lambda: port.slots() == [(0, False), None, None, None, (0, False), None])


def test_stop_sends_the_release_frame(make_pico_link):
    link, _, port, _ = make_pico_link()
    link.set_bot(5, 7)
    assert wait_for(lambda: port.frames and port.slots()[5] == (7, False))
    link.stop()
    assert port.slots() == [None] * 6               # every slot back to its controller
    assert link.unsafe_slots() == []


def test_port_loss_zeroes_holds_and_never_resumes_the_old_throttle(make_pico_link):
    link, _, port, events = make_pico_link()
    link.set_bot(1, 11)
    assert wait_for(lambda: port.frames and port.slots()[1] == (11, False))
    port.broken = True
    assert wait_for(lambda: any(isinstance(e, PicoState) and e.state == "lost" for e in events))
    assert "reconnect" in link.holds and link.bot_targets()[1] == 0
    link.set_bot(1, 11)                              # the app insists during the outage: held at 0
    n = len(port.frames)
    port.broken = False
    assert wait_for(lambda: len(port.frames) > n)
    time.sleep(0.3)
    assert all(pico_main.parse(f)[1] == (0, False) for f in port.frames[n:])
    assert link.state == "connected"                 # the CU link itself never dropped
    link.resume_bots()
    link.set_bot(1, 8)
    assert wait_for(lambda: port.slots()[1] == (8, False))


def test_missing_port_keeps_the_cu_link(make_pico_link):
    port = FakePort()
    port.broken = True
    link, _, _, events = make_pico_link(port=port)
    assert link.bot_transport == "P"
    assert wait_for(lambda: any(isinstance(e, PicoState) and e.state == "connecting" and e.error
                                for e in events))
    link.set_bot(0, 5)                               # nowhere to go: dropped, nothing crashes
    time.sleep(0.2)
    assert link.state == "connected" and port.frames == []


def test_pico_choice_ops_and_persistence(tmp_path):
    """pico.set keeps the port apart from the CU choice, saves it, and only a real CU uses it."""
    import json

    from .test_api import make_config, op, run, started, stopped

    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            scan = await op(client, "pico.scan")
            assert scan["ok"] and isinstance(scan["result"]["ports"], list)
            res = await op(client, "pico.set", device="/dev/cu.HC-05")
            assert res["ok"], res
            info = app.link_info
            assert info["pico_device"] == "/dev/cu.HC-05" and info["pico"] is None  # virtual CU: unused
            assert info["transport"] == "sim" and app.link.bot_transport != "P"
            saved = json.loads((tmp_path / "settings.json").read_text())
            assert saved["pico_choice"] == "/dev/cu.HC-05"
            assert (await op(client, "pico.set", device=""))["ok"]
            assert app.link_info["pico_device"] is None
            assert json.loads((tmp_path / "settings.json").read_text())["pico_choice"] == ""
            assert (await op(client, "pico.set", device=42))["ok"] is False
        finally:
            await stopped(app, client)

    run(main())


def test_app_drives_a_real_cu_through_the_pico(tmp_path, monkeypatch):
    """End to end in the app: a real-CU link (hardware 8: no C, no B) plus the Pico's port; the
    UI's fader (bot.take manual + bot.throttle) and STOP reach the port as frames the Pico takes."""
    from racelab import app as app_mod

    from .test_api import make_config, op, run, started, stopped, wait_until

    monkeypatch.setattr(pico_mod, "RETRY_S", 0.05)
    cu, port = FakeCU(bots=False, version=b"8338"), FakePort()
    monkeypatch.setattr(app_mod.RaceLabApp, "_make_opener", lambda self: ("serial", "fake", cu.connect))
    monkeypatch.setattr(app_mod, "open_port", lambda device: port.open())

    async def main():
        cfg = make_config(tmp_path)
        cfg.connection.timeout = 0.2
        app, client = await started(cfg)
        try:
            assert app.link_info["bots_supported"] is False          # this CU alone: no bots
            assert (await op(client, "pico.set", device="/dev/cu.HC-05"))["ok"]
            assert await wait_until(lambda: app.link_info["state"] == "connected"
                                    and app.link_info["pico"] == "connected")
            assert app.link_info["bot_transport"] == "P" and app.mode == "advanced"
            assert (await op(client, "bot.take", slot=3, driver="manual"))["ok"]
            assert (await op(client, "bot.throttle", slot=3, value=6))["ok"]
            assert await wait_until(lambda: port.frames and port.slots()[3] == (6, False))
            assert (await op(client, "estop"))["ok"]
            assert await wait_until(lambda: port.slots()[3] == (0, False))
            assert not cu.ctl_frames()                                   # nothing for the CU itself
        finally:
            await stopped(app, client)
        assert port.slots() == [None] * 6                                # every slot handed back on exit

    run(main())


def test_bluetooth_pairing_through_blueutil(monkeypatch):
    """Search lists only unpaired modules; pairing drops a stale pairing first, then waits for the port."""
    import json
    import subprocess

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

    monkeypatch.setattr(pico_mod, "blueutil", lambda: "/fake/blueutil")
    monkeypatch.setattr(pico_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(pico_mod, "list_ports", lambda: list(ports))
    assert pico_mod.bluetooth_devices(1) == [{"address": "00-22-09-01-82-7a", "name": "racelab-wireless"}]
    assert pico_mod.pair("00-22-09-01-82-7a", "racelab-wireless") == "/dev/cu.racelab-wireless"
    assert calls[1:] == [["--unpair", "00-22-09-01-82-7a"], ["--pair", "00-22-09-01-82-7a", "1234"]]
    monkeypatch.setattr(pico_mod, "blueutil", lambda: None)            # not macOS / not installed
    with pytest.raises(RuntimeError, match="blueutil"):
        pico_mod.bluetooth_devices(1)
