"""Pairing a classic Bluetooth serial module (HC-05 and the like) from RaceLab, on macOS.

Such a module only gets a serial port once paired, and macOS shows no Connect button for it:
RaceLab pairs it with blueutil (Homebrew), so nobody needs a shell. It needs the Bluetooth
permission of whatever launched RaceLab (Terminal, RaceLab.app). Some modules forget their
pairing when powered off: pairing again drops the stale one first.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

from .serial import list_ports

PIN = "1234"  # HC-05 default
INQUIRY_S = 8
PORT_WAIT_S = 10.0  # after pairing, until macOS creates /dev/cu.<name>


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


def devices(seconds: int = INQUIRY_S) -> list[dict[str, str]]:
    """Classic Bluetooth devices in range that are not paired yet (a paired module already has a port)."""
    found = json.loads(_blueutil("--format", "json", "--inquiry", str(seconds), timeout=seconds + 20) or "[]")
    return [{"address": d["address"], "name": d.get("name") or d["address"]} for d in found if not d.get("paired")]


def pair(address: str, name: str = "") -> str:
    """Pair a serial module (PIN 1234) and return the serial port macOS creates for it."""
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
