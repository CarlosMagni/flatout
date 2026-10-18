"""Virtual CU: carreralib protocol, bot firmware, start sequence, timing and connection."""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from collections import deque

from carreralib import ControlUnit, protocol
from carreralib import connection as cn
from racelab.config import SimConfig

from .physics import Car, Human
from .track import default_track, track_for

logger = logging.getLogger(__name__)

TICK_S = 0.005  # period of the physics thread
MAX_SUBSTEP = 0.01  # s, maximum integration step (step() splits longer ones)
MAX_CATCHUP_S = 0.1  # max real time caught up per loop iteration (suspended machine)
VERSION = b"5339"  # hardware 5, firmware 339
EVENT_QUEUE_MAX = 512
NUM_CARS = 6  # drivable slots 0..5 (6 = ghost, 7 = pace car: no simulated car)

BUTTON_PACE, BUTTON_START = 1, 2
GO_DELAY_S = (0.2, 1.2)  # random delay between the 5 lights and lights-out
HOLD_PHASES = frozenset({1, 2, 3, 4, 5, 6, 8})  # cars held by the CU
REBOOT_S = 1.0  # bootloader -> new firmware after the last line


def _args(fmt: str, frame: bytes) -> tuple | None:
    """Decode a frame; checksum is optional on our firmware (wrong or missing: tolerated)."""
    for f in (fmt, fmt.rstrip("C")):
        try:
            return protocol.unpack(f, frame)
        except protocol.ChecksumError:
            logger.debug("wrong checksum ignored: %r", frame)
        except (IndexError, ValueError):
            pass
    return None


BOT_WATCHDOG_S = 13 * 0.075  # firmware watchdog: 13 rail cycles of 75 ms (firmware/bot/bot_mode.c)
CTL_EXPIRE_S = 10 * 0.075  # stock C table: cleared 60 controller words (10 cycles) after the last frame

class Simulator:
    """The virtual CU and the track; the state (firmware, cars) survives reconnections."""

    def __init__(self, cfg: SimConfig, *, realtime: bool = True) -> None:
        self.cfg = cfg
        self.track = default_track() if cfg.track_seed is None else track_for(cfg.track_seed)
        self._lock = threading.RLock()
        rng = random.Random(cfg.seed)
        self._rng = rng
        self.cars = [
            Car(slot, self.track, random.Random(rng.getrandbits(64)),
                fuel_mode=cfg.fuel_mode, pit_lane=cfg.pit_lane)
            for slot in range(NUM_CARS)
        ]
        self._humans = {slot: Human(self.cars[slot], random.Random(rng.getrandbits(64)))
                        for slot in cfg.humans}
        # "firmware" state (see docs/bot-protocol.md)
        self.bot_mask = 0  # bit addr = slot in bot mode
        self.bot_gas = [0] * 8
        self.lane_pending = 0  # bit addr = lane change armed
        self.bot_fed_at = [0.0] * 8  # simulated time of each slot's last throttle command (watchdog)
        self.bot_watchdog_s: float | None = BOT_WATCHDOG_S  # None: no watchdog (low-level tests)
        # stock C table (firmware V339: word_cycle_step reads it instead of the controller)
        self.ctl_table = [0] * 6  # low nibble throttle, bit 4 lane button, 0 = no host value
        self.press_log: list[int] = []  # every T press, in order (tests)
        self.ctl_expire_at = 0.0
        # CU
        self.t = 0.0  # simulated time (s); the CU clock is its truncation to ms
        self.phase = 0
        self.pace = False
        self.ignore_mask = 0
        self.tower = {"positions": {}, "lap": 0}
        self._lap_nibbles = [0, 0]
        self._phase_left = 0.0
        self._events: deque[tuple[int, int, int]] = deque(maxlen=EVENT_QUEUE_MAX)
        self._repeat: tuple[int, int, int] | None = None  # event to return a second time
        # bootloader (firmware update): `GB`, `E` lines / `F` blocks
        self.bootloader = False
        self.fwu_block_size: int | None = None  # 18 = behave like BLE
        self.bot_images: set[str] = set()  # line digests of the "bot" images
        self.flashed: dict | None = None  # last image received (tests, UI)
        self._fw_lines: list[bytes] = []
        self._fw_block = bytearray()
        self._reboot_at: float | None = None
        # link
        self._conn: SimConnection | None = None
        self._link_up_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if realtime:
            self._thread = threading.Thread(target=self._run, name="racelab-sim", daemon=True)
            self._thread.start()

    # --- public API ---------------------------------------------------------

    def connect(self, timeout: float = 1.0) -> SimConnection:
        """New connection (invalidates the previous one, like BLE)."""
        with self._lock:
            if time.monotonic() < self._link_up_at:
                raise cn.ConnectionError("link cut (simulation)")
            self._conn = SimConnection(self, timeout)
            return self._conn

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2.0)
        with self._lock:
            self._conn = None

    def drop_link(self, seconds: float) -> None:
        """Cut the active connection and refuse `connect()` for `seconds` (real time)."""
        with self._lock:
            self._conn = None
            self._link_up_at = time.monotonic() + seconds

    def step(self, dt: float) -> None:
        """Advance the physics by `dt` simulated seconds."""
        with self._lock:
            while dt > 1e-9:
                h = min(dt, MAX_SUBSTEP)
                self._substep(h)
                dt -= h

    def snapshot(self) -> dict:
        """Ground truth (physical position as a centerline lap fraction)."""
        with self._lock:
            return {
                "t": round(self.t, 3),
                "phase": self.phase,
                "pace": self.pace,
                "cars": [{
                    "slot": c.slot,
                    "s": round(self.track.fraction(c.lane, c.s), 5) % 1.0,
                    "lane": c.lane,
                    "v": round(c.v, 3),
                    "throttle": c.throttle,
                    "off": c.off,
                    "pit": c.in_pit,
                    "fuel": round(c.fuel, 2),
                    "controller": self._controller(c.slot),
                    "laps": c.laps,
                    "armed": c.armed,
                } for c in self.cars],
                "tower": {"positions": dict(self.tower["positions"]), "lap": self.tower["lap"]},
            }

    def track_geometry(self) -> dict:
        return self.track.geometry()

    # --- physics ------------------------------------------------------------

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(TICK_S):
            now = time.monotonic()
            self.step(min(now - last, MAX_CATCHUP_S) * self.cfg.time_scale)
            last = now

    def _substep(self, h: float) -> None:
        self._maybe_reboot()
        self.t += h
        if self.lane_pending:  # the firmware passed on the bit: the car remembers the request
            for car in self.cars:
                car.armed = car.armed or bool(self.lane_pending >> car.slot & 1)
            self.lane_pending = 0
        if any(self.ctl_table):
            if self.t >= self.ctl_expire_at:  # the stock firmware drops the whole table
                self.ctl_table = [0] * 6
            for slot, value in enumerate(self.ctl_table):
                if value & 0x10:  # C lane button held: the car remembers the request
                    self.cars[slot].armed = True
        self._advance_phase(h)

        raw = [self._raw_throttle(car, h) for car in self.cars]
        if 2 <= self.phase <= 6 and any(raw):
            self._set_phase(8)  # false start
        hold = self.phase in HOLD_PHASES or self.bootloader  # bootloader: rails cut
        for car, throttle in zip(self.cars, raw):
            back = car.step(h, 0 if hold else throttle, hold=hold, pace=self.pace)
            if back is not None and not self.ignore_mask >> car.slot & 1:
                self._events.append((car.slot, self._clock_ms(self.t - back), 1))

    def _raw_throttle(self, car: Car, h: float) -> int:
        """Throttle requested before the CU hold: bot, otherwise controller (simulated human or nothing)."""
        ctl = self.ctl_table[car.slot] if car.slot < 6 else 0
        if ctl and not self.bot_mask >> car.slot & 1:
            return ctl & 0x0F  # the C value replaces the controller (B, if any, rewrites the word later)
        if self.bot_mask >> car.slot & 1:
            # firmware watchdog (v339-bot.5): no throttle command for ~13 rail cycles -> 0
            if self.bot_watchdog_s is not None and self.t - self.bot_fed_at[car.slot] > self.bot_watchdog_s:
                return 0
            return self.bot_gas[car.slot]
        human = self._humans.get(car.slot)
        return human.throttle(h, self.phase) if human else 0

    def host_throttle(self, slot: int) -> int | None:
        """Throttle a host (C table or B module) applies to the slot, None when the controller drives."""
        if self.bot_mask >> slot & 1:
            if self.bot_watchdog_s is not None and self.t - self.bot_fed_at[slot] > self.bot_watchdog_s:
                return 0
            return self.bot_gas[slot]
        if slot < 6 and self.ctl_table[slot]:
            return self.ctl_table[slot] & 0x0F
        return None

    def _controller(self, slot: int) -> str:
        if self.bot_mask >> slot & 1 or (slot < 6 and self.ctl_table[slot]):
            return "bot"
        return "human" if slot in self._humans else "none"

    @staticmethod
    def _clock_ms(t: float) -> int:
        return int(t * 1000) & 0xFFFFFFFF

    # --- start sequence -----------------------------------------------------

    def _set_phase(self, phase: int) -> None:
        self.phase = phase
        self._phase_left = (self._rng.uniform(*GO_DELAY_S) if phase == 6
                            else {7: 0.5, 8: 3.0}.get(phase, 1.0))
        if phase == 2:
            for human in self._humans.values():
                human.countdown()
        elif phase == 7:
            for human in self._humans.values():
                human.go()

    def _advance_phase(self, h: float) -> None:
        if self.phase == 0 or self.phase == 1:
            return
        self._phase_left -= h
        if self._phase_left <= 0:
            self._set_phase({7: 0, 8: 1}.get(self.phase, self.phase + 1))

    def _press(self, button: int) -> None:
        self.press_log.append(button)
        if button == BUTTON_START:
            if self.phase in (0, 7):
                self._set_phase(1)
            elif self.phase == 1:
                self._set_phase(2)
        elif button == BUTTON_PACE:
            self.pace = not self.pace

    # --- protocol -----------------------------------------------------------

    def _handle(self, frame: bytes) -> bytes:
        """Response (without `$`) to a request; b"" if the CU would answer `#`."""
        self._maybe_reboot()
        if self.bootloader:
            return self._bootloader_frame(frame)
        letter = frame[:1]
        if letter == b"G":
            if frame[1:2] != b"B":
                return b""
            self._enter_bootloader()
            return b"G"
        if letter == b"0":
            return protocol.pack("c4sC", b"0", ("5" + self.cfg.firmware).encode())
        if letter == b"?":
            return self._poll()
        if letter == b"B":
            return self._cmd_bot(frame)
        if letter == b"C" and self.cfg.firmware in ("338", "339"):  # C appeared in V338
            return self._cmd_ctl(frame)
        ok = {b"J": self._cmd_program, b"T": self._cmd_button,
              b"=": self._cmd_reset, b":": self._cmd_ignore}.get(letter)
        if ok is None:
            logger.debug("unknown command %r", frame)
            return b""
        return letter if ok(frame) else b""

    def _poll(self) -> bytes:
        """Pending timing event (the real CU's duplicates are simulated), otherwise status."""
        if self._repeat is not None:
            event, self._repeat = self._repeat, None
        elif self._events:
            event = self._events.popleft()
            if self.cfg.duplicate_rate and self._rng.random() < self.cfg.duplicate_rate:
                self._repeat = event
        else:
            event = None
        if event is not None:
            slot, ts, sector = event
            return protocol.pack("cYIYC", b"?", slot + 1, ts, sector)

        fuel = [min(15, int(c.fuel)) for c in self.cars] + [15] * (8 - NUM_CARS)
        mode = ((ControlUnit.Status.FUEL_MODE if self.cfg.fuel_mode else 0)
                | (ControlUnit.Status.PIT_LANE_MODE if self.cfg.pit_lane else 0))
        pit = sum(1 << c.slot for c in self.cars if c.in_pit)
        return protocol.pack("cc8YYYBYC", b"?", b":", *fuel, self.phase, mode, pit, 6)

    def _cmd_bot(self, frame: bytes) -> bytes:
        """`B<addr><v>`: semantics of the B command (docs/bot-protocol.md)."""
        if not self.cfg.bot_firmware:
            return b""  # stock firmware: the CU answers `#`
        if len(frame) < 3:
            return b""
        adr, val = (frame[1] - 0x30) & 0xFF, (frame[2] - 0x30) & 0xFF
        bit = 1 << adr
        if adr >= 8:
            pass
        elif val == 17:  # 'A': hand control back to the controller
            self.bot_mask &= ~bit
            self.lane_pending &= ~bit
        elif val == 28:  # 'L': lane change, refused if the slot is not in bot mode
            if self.bot_mask & bit:
                self.lane_pending |= bit
        elif val <= 15:
            self.bot_gas[adr] = val
            self.bot_fed_at[adr] = self.t
            self.bot_mask |= bit
        return b"B"

    def _cmd_ctl(self, frame: bytes) -> bytes:
        """`C` + 6 × (throttle char, lane char): the stock firmware's host controller table.

        Value = (throttle char - '0') | 0x10 if the lane char is '1'; 0 = no host value
        (the controller drives). Only bits 0-4 are used by the firmware (`P` = held at 0)."""
        if len(frame) < 13:
            return b""
        table = []
        for i in range(6):
            t, lane = frame[1 + 2 * i], frame[2 + 2 * i]
            table.append(((t - 0x30) & 0xFF) | (0x10 if lane == ord("1") else 0))
        self.ctl_table = table
        self.ctl_expire_at = self.t + CTL_EXPIRE_S
        return b"C"

    def _cmd_program(self, frame: bytes) -> bool:
        """`J`: programming word (car settings, position tower, lap counter)."""
        args = _args("cBYYC", frame)
        if args is None:
            return False
        word, address, value = args[1] & 0x1F, args[1] >> 5, args[2]
        if word in (0, 1, 2) and address < NUM_CARS:
            setattr(self.cars[address], ("speed", "brake", "tank")[word], value)
        elif word == 6:
            if value == 9:  # clrpos
                self.tower["positions"].clear()
            else:
                self.tower["positions"][address] = value
        elif word in (17, 18) and address == 7:
            self._lap_nibbles[word - 17] = value
            self.tower["lap"] = self._lap_nibbles[0] << 4 | self._lap_nibbles[1]
        return True

    def _cmd_button(self, frame: bytes) -> bool:
        args = _args("cYC", frame)
        if args is None:
            return False
        self._press(args[1])
        return True

    def _cmd_reset(self, frame: bytes) -> bool:
        if _args("cYYC", frame) is None:
            return False
        self._events.clear()
        self._repeat = None
        return True

    def _cmd_ignore(self, frame: bytes) -> bool:
        args = _args("cBC", frame)
        if args is None:
            return False
        self.ignore_mask = args[1]
        return True

    # --- bootloader ---------------------------------------------------------

    def _enter_bootloader(self) -> None:
        self.bootloader = True
        self._fw_lines, self._fw_block, self._reboot_at = [], bytearray(), None
        self.bot_mask, self.lane_pending = 0, 0
        self.bot_gas = [0] * 8
        self.ctl_table = [0] * 6
        self._events.clear()
        self._repeat = None

    def _bootloader_frame(self, frame: bytes) -> bytes:
        letter = frame[:1]
        if letter == b"F" and len(frame) >= 2:  # BLE block: 'F' + raw length + data
            self._fw_block += frame[2:2 + frame[1]]
            return b"F"
        if letter == b"E":
            # serial: 'E' + line + checksum; BLE: 'E' + checksum after the F blocks
            line = bytes(frame[1:-1]) if len(frame) > 2 else bytes(self._fw_block)
            self._fw_block = bytearray()
            if not line or self._reboot_at is not None:
                return b""
            self._fw_lines.append(line)
            # end of image: the final line is shorter (see the images built in firmware/build/)
            if len(self._fw_lines) > 1 and len(line) < len(self._fw_lines[0]):
                self._reboot_at = time.monotonic() + REBOOT_S
            return b"E"
        return b""  # the bootloader does not speak the race protocol

    def _maybe_reboot(self) -> None:
        if self._reboot_at is None or time.monotonic() < self._reboot_at:
            return
        digest = hashlib.sha256(b"\n".join(self._fw_lines)).hexdigest()
        self.flashed = {"lines": len(self._fw_lines), "digest": digest}
        if self.bot_images:
            self.cfg.bot_firmware = digest in self.bot_images
        self.bootloader, self._reboot_at = False, None
        self.phase = 0
        logger.info("Virtual CU: new firmware (%d lines, bots=%s)",
                    len(self._fw_lines), self.cfg.bot_firmware)

    # --- link ---------------------------------------------------------------

    def _check_link(self, conn: SimConnection) -> None:
        if conn is not self._conn or time.monotonic() < self._link_up_at:
            raise cn.ConnectionError("link cut")

    def _exchange(self, conn: SimConnection, frame: bytes) -> bytes:
        with self._lock:
            self._check_link(conn)
            return self._handle(frame)


class SimConnection(cn.Connection):
    """carreralib connection to the simulator: same responses as `SerialConnection.recv`."""

    @property
    def max_fwu_block_size(self) -> int | None:  # type: ignore[override]
        return self._sim.fwu_block_size

    def __init__(self, sim: Simulator, timeout: float = 1.0) -> None:
        self._sim = sim
        self._timeout = timeout
        self._responses: deque[bytes] = deque()
        self._rng = random.Random()

    def close(self) -> None:
        sim = getattr(self, "_sim", None)
        if sim is not None:
            with sim._lock:
                if sim._conn is self:
                    sim._conn = None

    def send(self, buf, offset=0, size=None) -> None:
        n = len(buf)
        if offset < 0:
            raise ValueError("offset is negative")
        elif n < offset:
            raise ValueError("buffer length < offset")
        elif size is None:
            size = n - offset
        elif size < 0:
            raise ValueError("size is negative")
        elif offset + size > n:
            raise ValueError("buffer length < offset + size")
        self._responses.append(self._sim._exchange(self, bytes(buf[offset:offset + size])))

    def recv(self, maxlength=None) -> bytes:
        sim = self._sim
        with sim._lock:
            sim._check_link(self)
        if not self._responses:
            time.sleep(self._timeout)
            raise cn.TimeoutError("no response from the virtual CU")
        if sim.cfg.latency_ms > 0:
            jitter = sim.cfg.jitter_ms
            time.sleep(max(0.0, sim.cfg.latency_ms + self._rng.uniform(-jitter, jitter)) / 1000)
        with sim._lock:
            sim._check_link(self)  # the link may have dropped while waiting
        res = self._responses.popleft()
        if maxlength is not None and len(res) > maxlength:
            raise cn.BufferTooShort("Buffer too short for data received")
        return res
