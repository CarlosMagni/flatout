"""BotModel: the bot rules, once, for every way a throttle reaches a car.

The CU link (its `C` table, the `B` module) and a controller interface decide what the cars get
with these rules; they only differ in how a frame reaches the CU or the device:

- a slot is a bot (throttle 0..15, clamped to `[safety] max_throttle`) or None: handed back to its
  controller;
- E-STOP and the "reconnect" hold force throttle 0 on every bot and refuse any other value until
  they are lifted; an E-STOP also goes out before anything else;
- a lane press waits for a frame like any change, then the button is held `LANE_PRESS_S` from the
  frame that carries it, longer than one rail cycle (~75 ms);
- a `C` frame (6 controller values and the lane buttons) goes out at once when it changes, at least
  `min_gap_s` after the last one, else every `heartbeat_s` while a bot is driven: the CU, and the
  devices that follow its protocol, hand every slot back ~750 ms after the last frame;
- the first frame after a (re)connection, whatever was asked before, has the app's bots at 0.

What differs is given by the owner, not guessed here: how long between two unchanged frames
(`heartbeat_s`), how close two frames may be (`min_gap_s`), and how to wake the owner's thread when
something the app asked for is waiting (`wake`).

The `B` module takes one command per slot, so its per-slot send rule stays in `CULink._service_bots`;
it reads the targets, the E-STOP flag and the lane presses from here (`take`).

Threads: the app's methods can be called from any thread, the transport's from its one I/O thread.
The model has its own lock; an owner may hold its own lock while calling in (that order, never the
other way round: the model calls nothing but `wake` and the safety config).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import NamedTuple

from ..config import SafetyConfig
from ..events import BOT_SLOTS

LANE_PRESS_S = 0.12  # a lane press held at least one rail cycle (~75 ms), counted from its frame
HB_PAUSE_MAX_S = 3.0  # hard ceiling of the hand-back test's heartbeat pause (it expires by itself)


class Frame(NamedTuple):
    """What to put on the wire: the 6 controller values and the lane buttons held."""

    values: tuple[int | None, ...]  # per slot: None = its controller drives, else a throttle 0..15
    lanes: frozenset[int]
    prev: tuple[tuple[int | None, ...], frozenset[int]] | None  # (values, lanes) last sent; None = unknown

    def changes(self) -> list[tuple[int, int | None, bool]]:
        """(slot, value, lane held) of what this frame changes, for the session recording. A slot
        that stays on its controller is left out."""
        prev, out = self.prev, []
        for slot in BOT_SLOTS:
            lane = slot in self.lanes
            if prev is None or prev[0][slot] != self.values[slot] or (slot in prev[1]) != lane:
                if self.values[slot] is not None or (prev is not None and prev[0][slot] is not None):
                    out.append((slot, self.values[slot], lane))
        return out


class BotModel:
    def __init__(self, safety: SafetyConfig, *, heartbeat_s: float, min_gap_s: float,
                 wake: Callable[[], None] | None = None):
        self._safety = safety
        self.heartbeat_s = heartbeat_s
        self.min_gap_s = min_gap_s
        self._wake = wake or (lambda: None)
        self._lock = threading.Lock()
        self._target: list[int | None] = [None] * 6  # None = handed back to the controller
        self._holds: set[str] = set()  # "estop", "reconnect": throttle forced to 0
        self._urgent = False  # an E-STOP is waiting to go out
        self._presses: set[int] = set()  # lane presses waiting for a frame to carry them
        self._lane_until = [0.0] * 6  # lane button held until (monotonic), from the frame that carried it
        self._hb_pause_until = 0.0  # hand-back test: no idle heartbeat until then, 0 = off
        self._last: tuple | None = None  # (values, lanes) of the last frame sent; None = device state unknown
        self._sent_at = 0.0

    # ------------------------------------------------------------ the app's side

    def set_bot(self, slot: int, value: int | None) -> None:
        """Desired throttle of the slot (0..15), or None to hand control back to the controller."""
        if slot not in BOT_SLOTS:
            raise ValueError("invalid bot slot")
        if value is not None:
            value = max(0, min(int(value), self._safety.max_throttle))
        with self._lock:
            if value is not None and self._holds:
                value = 0
            self._target[slot] = value
        self._wake()

    def lane(self, slot: int) -> None:
        if slot not in BOT_SLOTS:
            raise ValueError("invalid bot slot")
        with self._lock:
            if self._target[slot] is not None:
                self._presses.add(slot)
        self._wake()

    def estop(self) -> None:
        """Throttle 0 on every driven bot, sent before any other frame."""
        with self._lock:
            self._holds.add("estop")
            self._zero_targets()
            self._urgent = True
        self._wake()

    def clear_estop(self) -> None:
        with self._lock:
            self._holds.discard("estop")

    def resume(self) -> None:
        """Lift the "reconnect" hold (the app has acknowledged throttle 0)."""
        with self._lock:
            self._holds.discard("reconnect")

    @property
    def holds(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._holds)

    def targets(self) -> list[int | None]:
        with self._lock:
            return list(self._target)

    def hold_at_zero(self) -> None:
        """The cars can no longer be watched (CU link or device lost): throttle 0, and a "reconnect"
        hold if a bot was driven, until the app lifts it."""
        with self._lock:
            self._zero_targets()
            if any(t is not None for t in self._target):
                self._holds.add("reconnect")

    def clear(self) -> None:
        """Nothing driven, nothing pending (firmware update): the "reconnect" hold goes too, no
        bot is left to hold."""
        with self._lock:
            self._presses.clear()
            self._target = [None] * 6
            self._holds.discard("reconnect")

    def pause_heartbeat(self, seconds: float) -> None:
        """Hand-back test: stop repeating an unchanged frame for `seconds` (<= HB_PAUSE_MAX_S), so the
        CU's ~750 ms expiry hands the slots back. A changed frame or an E-STOP still goes out. The
        pause ends by itself, with the bots zeroed (see `frame`), never resumed at their old throttle."""
        with self._lock:
            self._hb_pause_until = time.monotonic() + max(0.0, min(float(seconds), HB_PAUSE_MAX_S))
        self._wake()

    def resume_heartbeat(self) -> None:
        with self._lock:
            self._hb_pause_until = 0.0
        self._wake()

    @property
    def hb_pause_until(self) -> float:
        with self._lock:
            return self._hb_pause_until

    # ------------------------------------------------------------ the transport's side

    def take(self) -> tuple[bool, set[int], list[int | None]]:
        """For a transport that sends one command per slot: (E-STOP waiting, lane presses, targets),
        the first two cleared, all three as of the same instant."""
        with self._lock:
            urgent, self._urgent = self._urgent, False
            presses, self._presses = self._presses, set()
            return urgent, presses, list(self._target)

    def forget(self) -> None:
        """The device was (re)opened: its state is unknown, the next frame has the bots at 0."""
        with self._lock:
            self._last, self._lane_until, self._hb_pause_until = None, [0.0] * 6, 0.0
            self._presses.clear()  # a press from before the outage must not fire after it

    def frame(self, now: float, *, probe: bool = False) -> Frame | None:
        """The frame to send at `now`, or None. `probe`: also send an unchanged frame (the hardware
        test's latency probe), once the gap allows and while no bot is driven. Call `sent` once it
        went out."""
        with self._lock:
            urgent, self._urgent = self._urgent, False
            if self._hb_pause_until and (urgent or now >= self._hb_pause_until):
                if not urgent:
                    self._zero_targets()  # the test is over (or forgotten): never resume at the old throttle
                self._hb_pause_until = 0.0
            paused = self._hb_pause_until > 0.0
            values = tuple(self._target)
            self._presses = {s for s in self._presses if values[s] is not None}  # handed back meanwhile: gone
            lanes = frozenset(s for s in BOT_SLOTS
                              if values[s] is not None and (s in self._presses or self._lane_until[s] > now))
            last, gap = self._last, now - self._sent_at
        if last is None:  # device state unknown: the app's bots start from 0
            return Frame(tuple(None if v is None else 0 for v in values), frozenset(), None)
        active = any(v is not None for v in values)
        if (urgent
                or (probe and not active and gap >= self.min_gap_s)
                or ((values, lanes) != last and gap >= self.min_gap_s)
                or (active and not paused and gap >= self.heartbeat_s)):
            return Frame(values, lanes, last)
        return None

    def sent(self, frame: Frame, now: float) -> None:
        """`frame` reached the CU or the device (`now` is when it was decided)."""
        with self._lock:
            self._last, self._sent_at = (frame.values, frame.lanes), now
            for slot in frame.lanes & self._presses:  # the button is down from this frame
                self._lane_until[slot] = now + LANE_PRESS_S
            self._presses -= frame.lanes

    def final_frame(self) -> Frame:
        """On exit: every slot handed back, or the bots held at 0 (`[safety] release_on_exit`)."""
        with self._lock:
            if self._safety.release_on_exit:
                values = (None,) * 6
            else:
                values = tuple(None if v is None else 0 for v in self._target)
            return Frame(values, frozenset(), self._last)

    def _zero_targets(self) -> None:
        """Lock held by the caller."""
        self._hb_pause_until = 0.0  # E-STOP, link loss, pause expiry: the heartbeat is back
        for slot in BOT_SLOTS:
            if self._target[slot] is not None:
                self._target[slot] = 0
