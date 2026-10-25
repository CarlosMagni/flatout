"""A Bluetooth serial module (HC-05 and the like) reached through its RFCOMM channel, on macOS.

macOS gives such a module a /dev/cu.<name> port, but that port is not dependable: its node often
does not come back after the link drops, and opening it does not bring the link up again, so it
only really works right after pairing. Opening the RFCOMM channel through IOBluetooth works every
time, paired once. The module is then named `bt:<address>` (bt:00-22-09-01-82-7a).

IOBluetooth only works on a process's main thread, which in RaceLab runs the asyncio loop, and
macOS kills a process that calls it without the Bluetooth permission, an abort no Python code can
catch. So the channel lives in a small helper process (`python -m racelab.drive.rfcomm`): its main
thread owns the channel and relays bytes both ways over a pipe. `RfcommPort` drives that helper and
behaves like the pyserial port the controller interface expects (`write`, `read`,
`reset_output_buffer`, `close`).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

PREFIX = "bt:"
SPP_UUID16 = 0x1101  # Serial Port Profile
ALLOWED_ALWAYS = 3  # CBManagerAuthorizationAllowedAlways
OPEN_TIMEOUT_S = 15.0  # the helper connects the module and opens its channel (~2-4 s)
LIST_TIMEOUT_S = 10.0


def available() -> bool:
    """True on macOS with pyobjc's IOBluetooth (the bt-serial extra), once whatever launched
    RaceLab holds the Bluetooth permission (asking never touches IOBluetooth itself)."""
    if sys.platform != "darwin":
        return False
    try:
        import CoreBluetooth  # with bleak
        import IOBluetooth  # noqa: F401
    except ImportError:
        return False
    return CoreBluetooth.CBManager.authorization() == ALLOWED_ALWAYS


def address_of(device: str) -> str | None:
    """'bt:00-22-09-01-82-7a' -> '00-22-09-01-82-7a'; None for any other port."""
    return device[len(PREFIX):] if device.startswith(PREFIX) else None


def _helper(*args: str) -> list[str]:
    return [sys.executable, "-m", "racelab.drive.rfcomm", *args]


def modules() -> list[dict[str, str]]:
    """Paired modules that offer a serial port: [{device: 'bt:<address>', label}]; [] when unavailable."""
    if not available():
        return []
    try:
        out = subprocess.run(_helper("--list"), capture_output=True, text=True, timeout=LIST_TIMEOUT_S)
        return json.loads(out.stdout or "[]") if out.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def open_port(device: str, baud: int = 0, cancelled: Callable[[], bool] | None = None) -> RfcommPort:
    """The `opener` of SerialInterface for a bt:<address> device (the rate means nothing over RFCOMM).
    `cancelled` is polled while the module connects: the interface may be stopped meanwhile."""
    address = address_of(device)
    if address is None:
        raise OSError(f"not a Bluetooth module: {device}")
    if not available():
        raise OSError("Bluetooth unavailable to RaceLab: allow it in System Settings → Privacy & Security → "
                      "Bluetooth (for RaceLab, or for Terminal if it was launched from a terminal)")
    return RfcommPort(address, cancelled)


class RfcommPort:
    """The module's RFCOMM channel, through the helper process. Opening connects the module (~2-4 s);
    OSError if it cannot be, and from `read`/`write` once the link is down."""

    def __init__(self, address: str, cancelled: Callable[[], bool] | None = None):
        self.address = address
        self._cancelled = cancelled or (lambda: False)
        self._proc = subprocess.Popen(_helper(address), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, bufsize=0)
        self._out = self._proc.stdout.fileno()
        os.set_blocking(self._out, False)
        os.set_blocking(self._proc.stdin.fileno(), False)  # a stuck helper never blocks the interface
        self._rx = bytearray()
        line = self._first_line()
        if not line.startswith(b"ok"):
            self.close(at_once=True)
            raise OSError(line.decode("utf-8", "replace").removeprefix("error ").strip()
                          or "Bluetooth: the helper stopped")

    def _first_line(self) -> bytes:
        """The helper's verdict on the opening: 'ok' or 'error <why>'. What follows is the module's."""
        end = time.monotonic() + OPEN_TIMEOUT_S
        while b"\n" not in self._rx:
            left = end - time.monotonic()
            if left <= 0:
                return b"error Bluetooth: the module did not answer in time (off, or out of range?)"
            if self._cancelled():
                return b"error cancelled"
            select.select([self._out], [], [], min(left, 0.1))
            try:
                chunk = os.read(self._out, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                return bytes(self._rx)
            self._rx += chunk
        line, _, rest = bytes(self._rx).partition(b"\n")
        self._rx = bytearray(rest)
        return line

    def write(self, data: bytes) -> int:
        try:
            sent = self._proc.stdin.write(data)
        except (BrokenPipeError, ValueError, OSError):
            raise OSError("Bluetooth link down (module off, or out of range?)") from None
        if sent != len(data):  # the pipe is full: the helper no longer forwards anything
            raise OSError("Bluetooth: the module no longer takes the frames")
        return sent

    def read(self, n: int) -> bytes:
        try:
            chunk = os.read(self._out, 4096)
        except BlockingIOError:
            chunk = None
        if chunk == b"":  # the helper ended: the channel closed
            raise OSError("Bluetooth link down (module off, or out of range?)")
        if chunk:
            self._rx += chunk
        data, self._rx = bytes(self._rx[:n]), self._rx[n:]
        return data

    def reset_output_buffer(self) -> None:
        pass  # the helper writes each chunk at once: nothing waits to drain

    def close(self, at_once: bool = False) -> None:
        """End of input: the helper closes the channel and exits. `at_once` (an opening that failed
        or was cancelled, where the helper may still be connecting): stop it now, macOS drops the link."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if at_once:
                proc.kill()
            proc.stdin.close()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
            proc.wait()
        finally:
            proc.stdout.close()


# ------------------------------------------------------------ the helper process (main thread)

def _pump(seconds: float) -> None:
    from Foundation import NSDate, NSRunLoop

    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def _spp_channel(dev: Any) -> int | None:
    import IOBluetooth

    rec = dev.getServiceRecordForUUID_(IOBluetooth.IOBluetoothSDPUUID.uuid16_(SPP_UUID16))
    if rec is None:
        return None
    status, channel = rec.getRFCOMMChannelID_(None)
    return channel if status == 0 else None


def _list() -> int:
    import IOBluetooth

    out = []
    for dev in IOBluetooth.IOBluetoothDevice.pairedDevices() or []:
        try:
            if _spp_channel(dev) is None:
                continue
            address = str(dev.addressString()).lower().replace(":", "-")
            name = str(dev.name() or "") or address  # macOS may not know the name of a module paired by blueutil
            out.append({"device": PREFIX + address, "label": f"{name} (Bluetooth)"})
        except Exception:  # a device macOS cannot describe: not ours
            continue
    print(json.dumps(out))
    return 0


def _bridge(address: str) -> int:
    """Open the channel, say 'ok', then relay: stdin -> module, module -> stdout, until either ends."""
    import IOBluetooth
    import objc
    from Foundation import NSObject

    stdout = sys.stdout.buffer

    class Delegate(NSObject):
        def init(self):
            self = objc.super(Delegate, self).init()
            self.closed = False
            return self

        def rfcommChannelData_data_length_(self, channel, data, length):
            stdout.write(bytes(data[:length]) if not isinstance(data, (bytes, bytearray)) else bytes(data))
            stdout.flush()

        def rfcommChannelClosed_(self, channel):
            self.closed = True

    def fail(why: str) -> int:
        stdout.write(f"error {why}\n".encode())
        stdout.flush()
        return 1

    dev = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(address)
    if dev is None:
        return fail(f"Bluetooth: no such module {address}")
    if not dev.isPaired():
        return fail("Bluetooth: the module is not paired (pair it again from RaceLab)")
    delegate = Delegate.alloc().init()
    status, channel = dev.openRFCOMMChannelSync_withChannelID_delegate_(None, _spp_channel(dev) or 1, delegate)
    if status != 0 or channel is None:
        dev.closeConnection()
        return fail(f"Bluetooth: cannot open the module's serial channel (off, or out of range?) "
                    f"[{status & 0xFFFFFFFF:#x}]")
    stdout.write(b"ok\n")
    stdout.flush()
    stdin = sys.stdin.buffer.fileno()
    try:
        while not delegate.closed and channel.isOpen():
            _pump(0.002)
            if select.select([stdin], [], [], 0.003)[0]:
                data = os.read(stdin, 4096)
                if not data:  # RaceLab closed the pipe (or died)
                    break
                if channel.writeSync_length_(data, len(data)) != 0:
                    break
    finally:
        channel.closeChannel()
        _pump(0.2)
        dev.closeConnection()
    return 0


def main(argv: list[str]) -> int:
    if not available():
        print("error Bluetooth unavailable (no permission, or not macOS)", flush=True)
        return 1
    if argv[:1] == ["--list"]:
        return _list()
    if len(argv) != 1:
        print("usage: python -m racelab.drive.rfcomm (--list | <address>)", file=sys.stderr)
        return 2
    return _bridge(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
