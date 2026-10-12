"""Tolerant ControlUnit: robust request/response + routing of spontaneous messages.

`carreralib.ControlUnit.request` waits for a response that starts with the
letter sent and discards the rest. Here, any status or timing message
received while waiting is decoded and handed to the link instead of being
lost, a `#` / empty response is an explicit refusal, and the wait is bounded.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from carreralib import ControlUnit, protocol
from carreralib.connection import Connection, TimeoutError

logger = logging.getLogger(__name__)

# Letters the app must never emit as raw requests (docs/protocol.md §8):
# G = bootloader, E/F = firmware update blocks (allowed in update mode only),
# C = controller table, only through setctl() (strictly built frame).
FORBIDDEN_LETTERS = frozenset(b"GEFC")
FIRMWARE_LETTERS = frozenset(b"GEF")
FIRMWARE_TIMEOUT_S = 5.0  # like carreralib.fw: the bootloader can be slow to answer

Parsed = ControlUnit.Status | ControlUnit.Timer


class CommandRejected(Exception):
    """The CU answered `#` (unknown command, e.g. `B` on stock firmware)."""


def parse_message(res: bytes) -> Parsed | None:
    """Decode a `?:` status or a `?<addr>…` timing event (see ControlUnit.poll)."""
    try:
        if res.startswith(b"?:"):
            try:
                parts = protocol.unpack("2x8YYYBYC", res)
            except protocol.ChecksumError:
                # recent CUs add two unknown bytes
                parts = protocol.unpack("2x8YYYBYxxC", res)
            fuel, (start, mode, pitmask, display) = parts[:8], parts[8:]
            pit = tuple(pitmask & (1 << n) != 0 for n in range(8))
            return ControlUnit.Status(tuple(fuel), start, mode, pit, display)
        if res.startswith(b"?") and len(res) >= 11:
            address, timestamp, sector = protocol.unpack("xYIYC", res)
            if 1 <= address <= 8:
                return ControlUnit.Timer(address - 1, timestamp, sector)
    except (protocol.ChecksumError, IndexError, ValueError):
        logger.debug("unreadable message %r", res)
    return None


class RaceControlUnit(ControlUnit):
    """carreralib ControlUnit + tolerant requests + bot commands."""

    def __init__(self, conn: Connection, *, on_message: Callable[[Parsed], None],
                 on_raw: Callable[[str, bytes], None] | None = None,
                 response_timeout: float = 1.0):
        super().__init__(conn)
        self._conn = conn
        self._on_message = on_message
        self._on_raw = on_raw or (lambda direction, data: None)
        self._response_timeout = response_timeout
        self.last_rtt: float | None = None
        self.firmware_mode = False  # update in progress: G/E/F allowed

    def setctl(self, throttles, lanes=()):
        """`C` frame: host values for controllers 0..5 (carreralib ctl_frame, stock V339)."""
        return self.request(self.ctl_frame(throttles, lanes), _ctl=True)

    def request(self, buf, maxlength=None, *, _ctl=False):
        """Send `buf` and return the response that starts with its letter.

        Raises CommandRejected on `#` / empty response, TimeoutError if nothing
        usable arrives before the deadline.
        """
        allowed = (self.firmware_mode and buf[0] in FIRMWARE_LETTERS) or (_ctl and buf[0:1] == b"C")
        if buf[0] in FORBIDDEN_LETTERS and not allowed:
            raise ValueError(f"command forbidden from the app: {buf[:1]!r}")
        letter = buf[0:1]
        t0 = time.monotonic()
        deadline = t0 + (FIRMWARE_TIMEOUT_S if self.firmware_mode else self._response_timeout)
        self._on_raw("tx", bytes(buf))
        self._conn.send(buf)
        while True:
            try:
                res = bytes(self._conn.recv(maxlength))
            except TimeoutError:
                if self.firmware_mode and time.monotonic() < deadline:
                    continue  # wait longer for the response, resending nothing
                raise
            self._on_raw("rx", res)
            if not res or res == b"#":
                raise CommandRejected(letter.decode(errors="replace"))
            msg = parse_message(res)
            if msg is not None:
                if letter == b"?":
                    self.last_rtt = time.monotonic() - t0
                    return res
                self._on_message(msg)  # spontaneous message: keep it
            elif res.startswith(letter):
                self.last_rtt = time.monotonic() - t0
                return res
            elif res == b"?" + letter:
                # BLE: a "B$" echo is returned as "?B" by BLEConnection.recv
                self.last_rtt = time.monotonic() - t0
                return letter
            else:
                logger.debug("unexpected response to %r: %r", buf, res)
            if time.monotonic() > deadline:
                raise TimeoutError(f"no response to {buf!r}")

    def poll_message(self) -> Parsed | None:
        res = self.request(b"?")
        return parse_message(res)

    def probe_bots(self) -> bool:
        """Does the firmware know the B command? (`B0A` = hand control back to slot 0: no effect.)

        Only an explicit refusal (`#`) means "stock firmware"; no response is
        retried, then treated as a failing link.
        """
        for _ in range(3):
            try:
                self.botrelease(0)
                return True
            except CommandRejected:
                return False
            except TimeoutError:
                continue
        raise TimeoutError("the CU does not answer the bot module probe")
