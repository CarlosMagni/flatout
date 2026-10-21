"""The Pico controller interface (tools/pico_controllers/): a Raspberry Pi Pico on the CU's
controller socket 1 that plays the 6 hand controllers, reached through an HC-05 Bluetooth
serial module. It takes the CU's own `C` frame (docs/bot-protocol.md) and, like the CU's C
table, hands every slot back to its controller ~750 ms after the last frame.

The serial port lives in its own thread: a Bluetooth SPP port can block on open and on write
(module off, RFCOMM link dropped), and the CU link thread (polls, E-STOP) must never wait on it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

BAUD = 9600  # a Bluetooth SPP port ignores it; the Pico finds the module's own UART rate itself
RETRY_S = 2.0  # between two attempts to open the port
WRITE_TIMEOUT_S = 0.5
# serial ports that are never a Pico (macOS system ports)
NOT_A_PICO = ("Bluetooth-Incoming-Port", "debug-console", "wlan-debug")
PIN = "1234"  # HC-05 default
INQUIRY_S = 8
PORT_WAIT_S = 10.0  # after pairing, until macOS creates /dev/cu.<name>


def open_port(device: str) -> Any:
    import serial  # pyserial, already a carreralib dependency

    return serial.Serial(device, BAUD, timeout=0, write_timeout=WRITE_TIMEOUT_S)


def list_ports() -> list[dict[str, str]]:
    """Serial ports a Pico could be on, Bluetooth ones included (the CU scan leaves them out)."""
    from serial.tools.list_ports import comports

    return [{"device": p.device, "label": p.description if p.description not in (None, "n/a") else p.device}
            for p in comports() if not any(s in p.device for s in NOT_A_PICO)]


# ------------------------------------------------------------ pairing (macOS)
# A classic Bluetooth serial module (HC-05) only gets a serial port once paired, and macOS shows
# no Connect button for it: RaceLab pairs it with blueutil (Homebrew), so nobody needs a shell.
# Needs the Bluetooth permission of whatever launched RaceLab (Terminal, RaceLab.app).

def blueutil() -> str | None:
    """Path of blueutil, None when pairing from RaceLab is not possible (not macOS, not installed)."""
    if sys.platform != "darwin":
        return None
    return shutil.which("blueutil") or next(
        (p for p in ("/opt/homebrew/bin/blueutil", "/usr/local/bin/blueutil") if os.path.exists(p)), None)


def _blueutil(*args: str, timeout: float) -> str:
    tool = blueutil()
    if tool is None:
        raise RuntimeError("pairing from RaceLab needs blueutil on macOS (brew install blueutil)")
    out = subprocess.run([tool, *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode:
        raise RuntimeError((out.stderr or out.stdout).strip() or f"blueutil {args[0]} failed")
    return out.stdout


def bluetooth_devices(seconds: int = INQUIRY_S) -> list[dict[str, str]]:
    """Classic Bluetooth devices in range that are not paired yet (a paired module already has a port)."""
    found = json.loads(_blueutil("--format", "json", "--inquiry", str(seconds), timeout=seconds + 20) or "[]")
    return [{"address": d["address"], "name": d.get("name") or d["address"]} for d in found if not d.get("paired")]


def pair(address: str, name: str = "") -> str:
    """Pair a serial module (PIN 1234) and return the serial port macOS creates for it.

    A stale pairing (the module forgot it, macOS did not) is dropped first: it fails every connection."""
    before = {p["device"] for p in list_ports()}
    try:
        _blueutil("--unpair", address, timeout=15)
    except RuntimeError:
        pass  # was not paired
    _blueutil("--pair", address, PIN, timeout=40)
    key = name.replace(" ", "-").lower()
    end = time.monotonic() + PORT_WAIT_S
    while time.monotonic() < end:
        for p in list_ports():
            if p["device"] not in before or (key and key in p["device"].lower()):
                return p["device"]
        time.sleep(0.5)
    raise RuntimeError("paired, but no serial port appeared for it (is it a serial module?)")


class PicoWriter:
    """Writes the latest frame to the Pico's port; older frames still waiting are dropped (a C
    frame is the whole state). `on_change(state, error)` on every transition: "connecting"
    (never opened yet), "connected", "lost" (was open, failed)."""

    def __init__(self, opener: Callable[[], Any], on_change: Callable[[str, str | None], None],
                 retry_s: float | None = None):
        self._opener = opener
        self._on_change = on_change
        self._retry_s = RETRY_S if retry_s is None else retry_s
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._stopping = False
        self.state = "connecting"
        self.error: str | None = None
        self._thread = threading.Thread(target=self._run, name="pico", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def send(self, frame: bytes) -> None:
        with self._cond:
            self._frame = frame
            self._cond.notify()

    def stop(self, timeout: float = 2.0) -> None:
        """Write the frame still waiting (the release frame on shutdown), then close."""
        with self._cond:
            self._stopping = True
            self._cond.notify()
        self._thread.join(timeout)

    def _set(self, state: str, error: str | None = None) -> None:
        if (state, error) != (self.state, self.error):
            self.state, self.error = state, error
            self._on_change(state, error)

    def _run(self) -> None:
        opened = False
        while True:
            with self._cond:
                if self._stopping:
                    return
            try:
                port = self._opener()
            except Exception as e:  # port missing, module off or not paired
                self._set("lost" if opened else "connecting", str(e) or type(e).__name__)
                with self._cond:
                    self._cond.wait_for(lambda: self._stopping, self._retry_s)
                continue
            with self._cond:
                self._frame = None  # nothing queued while the port was closed goes out late
            opened = True
            self._set("connected")
            try:
                while True:
                    with self._cond:
                        self._cond.wait_for(lambda: self._frame is not None or self._stopping)
                        frame, self._frame = self._frame, None
                        stopping = self._stopping
                    if frame is not None:
                        port.write(frame)
                    if stopping:
                        return
            except Exception as e:  # SerialException, write timeout: the Bluetooth link dropped
                self._set("lost", str(e) or type(e).__name__)
            finally:
                try:
                    port.close()
                except Exception:
                    pass
            with self._cond:
                self._cond.wait_for(lambda: self._stopping, self._retry_s)
