"""Bluetooth scan for AppConnect units, in a separate process.

On macOS, CoreBluetooth kills the process (SIGABRT) when the responsible
application has not declared Bluetooth usage. The RaceLab server must never
die because of that (bots may be driving): the first Bluetooth access
therefore happens here, in a subprocess.

    python -m racelab.cu.blescan [duration_s]   -> JSON [{"address", "name"}, ...]
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

HELP = ("Bluetooth unavailable to RaceLab: allow it in System Settings → "
        "Privacy & Security → Bluetooth (for RaceLab, or for Terminal "
        "if the app is launched from a terminal), and check that Bluetooth is on")

_available: bool | None = None


def scan(timeout: float = 5.0) -> tuple[list[dict[str, str]], str | None]:
    """Visible AppConnect units (`Control_Unit`), or a readable error."""
    global _available
    try:
        proc = subprocess.run([sys.executable, "-m", "racelab.cu.blescan", str(timeout)],
                              capture_output=True, text=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        return [], "the Bluetooth scan is not responding"
    if proc.returncode != 0:
        _available = False
        detail = (proc.stderr.strip().splitlines() or [""])[-1]
        return [], HELP + (f" ({detail})" if detail and "Abort" not in detail else "")
    _available = True
    try:
        return json.loads(proc.stdout), None
    except ValueError:
        return [], "unreadable response from the Bluetooth scan"


def ensure_available() -> None:
    """Call before any BLE connection in the server process."""
    if _available is None:
        scan(2.0)
    if not _available:
        raise ConnectionError(HELP)


def first_cu(timeout: float = 8.0) -> str:
    devices, error = scan(timeout)
    if error:
        raise ConnectionError(error)
    if not devices:
        raise ConnectionError("no AppConnect \"Control_Unit\" found over Bluetooth "
                              "(CU powered on? Carrera app closed on the phone?)")
    return devices[0]["address"]


def _main() -> None:
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    from bleak import BleakScanner

    async def discover():
        return await BleakScanner.discover(timeout=timeout)

    found = [{"address": d.address, "name": d.name}
             for d in asyncio.run(discover()) if d.name and "control_unit" in d.name.lower()]
    print(json.dumps(found))


if __name__ == "__main__":
    _main()
