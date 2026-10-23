"""SerialInterface: the controller-interface wire protocol over a serial port.

Any device that speaks docs/controller-interface.md on a serial port (USB, USB-serial, Bluetooth
serial) works with it, with no Python to write: controller_interfaces/pico/ is one.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any

from carreralib import ControlUnit

from ..config import SafetyConfig
from . import rfcomm
from .interface import ControllerInterface

WRITE_TIMEOUT_S = 0.5
NOT_A_CONTROLLER = ("Bluetooth-Incoming-Port", "debug-console", "wlan-debug")  # macOS system ports
MAX_LINE = 512


def open_port(device: str, baud: int) -> Any:
    import serial  # pyserial, already a carreralib dependency

    return serial.Serial(device, baud, timeout=0, write_timeout=WRITE_TIMEOUT_S)


def list_ports() -> list[dict[str, str]]:
    """Serial ports an interface could be on, Bluetooth ones included (the CU scan leaves them out)."""
    from serial.tools.list_ports import comports

    return [{"device": p.device, "label": p.description if p.description not in (None, "n/a") else p.device}
            for p in comports() if not any(s in p.device for s in NOT_A_CONTROLLER)]


def interface_ports() -> list[dict[str, str]]:
    """What the interface can be on: the paired Bluetooth modules reached through their RFCOMM
    channel (bt:<address>, macOS), then the serial ports."""
    return rfcomm.modules() + list_ports()


def parse_report(line: bytes) -> dict[str, str] | None:
    """b'pico sync=on period=73.5ms' -> {'name': 'pico', 'sync': 'on', 'period': '73.5ms'}; None if no key=value."""
    info: dict[str, str] = {}
    for word in line.decode("ascii", "replace").split():
        key, eq, value = word.partition("=")
        if eq:
            info[key] = value
        elif "name" not in info and not info:
            info["name"] = key
    return info if len(info) > ("name" in info) else None


class SerialInterface(ControllerInterface):
    """Frames out as `"C…$`, reports in as `name key=value …` lines (docs/controller-interface.md)."""

    kind = "serial"

    def __init__(self, device: str, safety: SafetyConfig, emit: Callable[[object], None],
                 baud: int = 115200, opener: Callable[[str, int], Any] | None = None):
        super().__init__(device, safety, emit)
        self.baud = baud
        if opener is None and device.startswith(rfcomm.PREFIX):  # stoppable while the module connects
            opener = functools.partial(rfcomm.open_port, cancelled=self._stop.is_set)
        self._opener = opener or open_port
        self._port: Any = None
        self._rx = bytearray()

    def open(self) -> None:
        self._port = self._opener(self.device, self.baud)
        self._rx = bytearray()

    def send(self, values: list[int | None], lanes: frozenset[int]) -> None:
        self._port.write(b'"' + ControlUnit.ctl_frame(values, lanes) + b"$")

    def poll(self) -> None:
        # macOS removes a Bluetooth port's node when the link goes, but the open file keeps
        # accepting writes into the void: the node is the only sign
        if self.device.startswith("/dev/") and not os.path.exists(self.device):
            raise OSError("the port went away (module off, or no longer paired?)")
        data = self._port.read(4096)
        if not data:
            return
        self._rx += data
        while True:
            end = self._rx.find(b"\n")
            if end < 0:
                break
            line, self._rx = bytes(self._rx[:end]), self._rx[end + 1:]
            info = parse_report(line.strip())
            if info:
                self.feedback(info)
        if len(self._rx) > MAX_LINE:  # no newline in sight: not a report
            self._rx.clear()

    def close(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            try:  # frames still queued for a dead link: closing would wait for them to drain
                port.reset_output_buffer()
            except Exception:
                pass
            port.close()
