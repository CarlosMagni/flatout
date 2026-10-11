"""Messages exchanged between the CU link (I/O thread) and the app (asyncio loop).

The CU link only transports: it converts CU responses into typed messages,
timestamped on reception (`host_t` = time.monotonic()). All the logic (laps,
standings, bots) lives on the app side.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bits of the status `mode` field (see carreralib ControlUnit.Status)
FUEL_MODE = 0x1
REAL_MODE = 0x2
PIT_LANE_MODE = 0x4
LAP_COUNTER_MODE = 0x8

# CU buttons (T command)
BUTTON_PACE = 1
BUTTON_START = 2
BUTTON_SPEED = 5
BUTTON_BRAKE = 6
BUTTON_FUEL = 7
BUTTON_CODE = 8

BOT_SLOTS = range(6)  # addresses drivable by the B command (firmware v1)
ALL_SLOTS = range(8)  # 0-5 controllers, 6 ghost (autonomous car), 7 pace car


@dataclass(frozen=True)
class StatusMsg:
    """CU status (':' response to the poll)."""

    fuel: tuple[int, ...]  # 8 levels 0..15
    start: int  # raw start phase (see lights_for_phase)
    mode: int  # bits FUEL_MODE | REAL_MODE | PIT_LANE_MODE | LAP_COUNTER_MODE
    pit: tuple[bool, ...]  # 8 booleans "in the pit lane"
    display: int  # 6 or 8 drivers
    host_t: float


@dataclass(frozen=True)
class TimerMsg:
    """Finish-line crossing (sector 1) or Check Lane crossing (sectors 2-3)."""

    address: int  # 0..7
    timestamp: int  # CU clock, ms, 32 bits
    sector: int  # 1..3
    host_t: float


@dataclass(frozen=True)
class LinkState:
    """Link state, emitted on every transition."""

    state: str  # "connecting" | "connected" | "updating" | "lost" | "closed"
    transport: str  # "ble" | "serial" | "sim"
    device: str | None = None
    version: str | None = None  # ex. "5339"
    bots_supported: bool | None = None  # None = not probed yet
    error: str | None = None


@dataclass(frozen=True)
class LinkStats:
    """Periodic link statistics (≈ 1 Hz)."""

    rtt_ms: float | None  # average round trip of a request
    rx_per_s: float  # responses received per second
    tx_per_s: float  # frames sent per second
    bot_tx_per_s: float  # of which B commands
    queue: int  # queued commands


@dataclass(frozen=True)
class BotSent:
    """A B command actually acknowledged by the CU (for the UI/telemetry)."""

    slot: int
    value: int | None  # throttle 0..15, None = handed back to the controller
    lane: bool = False
    host_t: float = 0.0


@dataclass(frozen=True)
class FirmwareProgress:
    """Progress of a CU firmware update (emitted by the link)."""

    stage: str  # "prepare" | "bootloader" | "write" | "reboot" | "failed"
    done: int  # lines written
    total: int
    error: str | None = None
    host_t: float = 0.0


@dataclass(frozen=True)
class RawFrame:
    """Raw trace for the diagnostics console (direction, bytes)."""

    direction: str  # "tx" | "rx"
    data: bytes
    host_t: float
    note: str = field(default="")


def lights_for_phase(phase: int) -> tuple[int, bool]:
    """Lights on (0..5) and blinking for a raw start phase.

    carreralib interpretation (tested on a real CU):
    0 = race / idle (off), 1 = start armed (5 reds),
    2..6 = countdown (1..5 lights), 7 = lights out = GO,
    8..9 = false start (blinking).
    """
    if phase == 1:
        return 5, False
    if 2 <= phase <= 6:
        return phase - 1, False
    if phase >= 8:
        return 5, True
    return 0, False
