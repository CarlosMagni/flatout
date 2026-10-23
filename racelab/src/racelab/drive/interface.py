"""ControllerInterface: hardware that plays the hand controllers, outside the CU.

The bot model RaceLab drives through such a device lives here, with its safety rules, so that a new
kind of hardware only says how a frame reaches it (`open`, `send`, `poll`, `close`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from ..config import SafetyConfig
from ..events import BOT_SLOTS, BotSent, InterfaceState

logger = logging.getLogger(__name__)


def _describe(e: BaseException) -> str:
    return str(e) or type(e).__name__


class ControllerInterface:
    """A device on a controller socket that plays the hand controllers (docs/controller-interface.md).

    It answers the same bot calls as the CU link (`set_bot`, `lane`, `estop`, `clear_estop`,
    `resume_bots`, `holds`, `bot_targets`, `bot_applied`, `unsafe_slots`): the bot manager does not
    know which one drives the cars. It runs its own thread and never waits on the CU, whose polls
    take ~50 ms each over the AppConnect.

    What the device gets: the CU's `C` frame, 6 controller values, each None (not driven: the
    slot's real controller drives) or a throttle 0..15 with the lane button. A change goes out at
    once (MIN_GAP_S apart at most), an unchanged frame every HEARTBEAT_S while a bot is active; the
    device itself hands every slot back when frames stop for ~750 ms.

    Safety, the same as the CU link's: E-STOP before anything else, throttle 0 and a "reconnect"
    hold when the device or the CU link is lost (the app lifts it once it has zeroed its bots),
    throttle 0 on the app's bots whenever the device is (re)opened, the release frame on exit.

    A subclass implements `open()`, `send(values, lanes)`, `poll()` (read the device, call
    `feedback(info)` on each report it gets) and `close()`; any exception from them means the device
    is lost: it is closed, then opened again RETRY_S later.
    """

    kind = "base"
    PERIOD_S = 0.01  # frames are decided this often
    MIN_GAP_S = 0.02  # two frames at least this far apart (a device keeps only the latest anyway)
    HEARTBEAT_S = 0.1  # an unchanged frame repeated this often: far inside the device's ~750 ms expiry
    LANE_PRESS_S = 0.12  # a lane press held at least one rail cycle (~75 ms)
    RETRY_S = 2.0  # between two attempts to open the device
    SILENT_S = 3.0  # a device that has reported once, then stays silent this long, is lost

    def __init__(self, device: str, safety: SafetyConfig, emit: Callable[[object], None]):
        self.device = device
        self._safety = safety
        self._emit = emit
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._target: list[int | None] = [None] * 6  # None = slot on its real controller
        self._sent: list[int | None] = [None] * 6
        self._lane: set[int] = set()  # presses not yet turned into a held button
        self._lane_until = [0.0] * 6
        self._holds: set[str] = set()  # "estop", "reconnect": throttle forced to 0
        self._urgent = False
        self._last: tuple | None = None  # last frame sent (values, lanes); None = device state unknown
        self._sent_at = 0.0
        self._feedback_at: float | None = None  # None = no report yet (a device may never report)
        self.state = "connecting"
        self.error: str | None = None
        self.info: dict[str, str] = {}

    # ------------------------------------------------------------ to implement

    def open(self) -> None:
        raise NotImplementedError

    def send(self, values: list[int | None], lanes: frozenset[int]) -> None:
        raise NotImplementedError

    def poll(self) -> None:
        """Read whatever the device said; call `feedback(info)` for each report."""

    def close(self) -> None:
        pass

    # ------------------------------------------------------------ bot API (the CU link's)

    def set_bot(self, slot: int, value: int | None) -> None:
        """Desired throttle of the slot (0..15), or None to hand it back to its controller."""
        if slot not in BOT_SLOTS:
            raise ValueError("invalid bot slot")
        if value is not None:
            value = max(0, min(int(value), self._safety.max_throttle))
        with self._lock:
            if value is not None and self._holds:
                value = 0
            self._target[slot] = value

    def lane(self, slot: int) -> None:
        if slot not in BOT_SLOTS:
            raise ValueError("invalid bot slot")
        with self._lock:
            if self._target[slot] is not None:
                self._lane.add(slot)

    def estop(self) -> None:
        """Throttle 0 on every active bot, sent before any other frame."""
        with self._lock:
            self._holds.add("estop")
            self._zero_targets()
            self._urgent = True

    def clear_estop(self) -> None:
        with self._lock:
            self._holds.discard("estop")

    def resume_bots(self) -> None:
        """Lift the hold set by a lost link (the app has acknowledged throttle 0)."""
        with self._lock:
            self._holds.discard("reconnect")

    @property
    def holds(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._holds)

    def bot_targets(self) -> list[int | None]:
        with self._lock:
            return list(self._target)

    def bot_applied(self) -> list[int | None]:
        with self._lock:
            return list(self._sent)

    def unsafe_slots(self) -> list[int]:
        return []  # the device hands every slot back by itself when the frames stop

    def on_cu_link(self, state: str) -> None:
        """The CU link changed state: lost, the race is blind, so throttle 0 and hold, as the CU link does."""
        if state == "lost":
            with self._lock:
                self._hold_at_zero()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"interface-{self.kind}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """The release frame (or throttle 0 on the bots, see SafetyConfig.release_on_exit), then close."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.error("the %s interface thread did not stop in time", self.kind)
        self._set_state("closed")

    def feedback(self, info: dict[str, str]) -> None:
        """A report from the device: it is alive, and here is what it says about itself."""
        with self._lock:
            self._feedback_at = time.monotonic()
            self.info = dict(info)
        self._emit(InterfaceState(self.kind, self.state, self.device, self.error, dict(info)))

    def _run(self) -> None:
        opened = False
        while not self._stop.is_set():
            try:
                self.open()
            except Exception as e:  # port missing, module off or unpaired
                self._set_state("lost" if opened else "connecting", _describe(e))
                self._stop.wait(self.RETRY_S)
                continue
            opened = True
            with self._lock:
                self._last, self._feedback_at = None, None
            self._set_state("connected")
            try:
                while not self._stop.is_set():
                    self.poll()
                    seen = self._feedback_at
                    if seen is not None and time.monotonic() - seen > self.SILENT_S:
                        raise OSError(f"no report for {self.SILENT_S:g} s")
                    self._service()
                    self._stop.wait(self.PERIOD_S)
                self._send(self._final_values())  # shutting down: hand back, or hold at 0
            except Exception as e:  # write timeout, port gone, device silent
                logger.warning("%s interface lost: %s", self.kind, e)
                with self._lock:
                    self._hold_at_zero()
                    self._last = None
                self._set_state("lost", _describe(e))
            finally:
                try:
                    self.close()
                except Exception:
                    pass
            if not self._stop.is_set():
                self._stop.wait(self.RETRY_S)

    # ------------------------------------------------------------ frames

    def _service(self) -> None:
        now = time.monotonic()
        with self._lock:
            urgent, self._urgent = self._urgent, False
            for slot in self._lane:
                if self._target[slot] is not None:
                    self._lane_until[slot] = now + self.LANE_PRESS_S
            self._lane = set()
            values = tuple(self._target)
            lanes = frozenset(s for s in BOT_SLOTS if self._lane_until[s] > now and values[s] is not None)
            last, sent_at = self._last, self._sent_at
        if last is None:  # just opened, the device's state is unknown: the app's bots start from 0
            values, lanes = tuple(None if v is None else 0 for v in values), frozenset()
        elif not (urgent or ((values, lanes) != last and now - sent_at >= self.MIN_GAP_S)
                  or (any(v is not None for v in values) and now - sent_at >= self.HEARTBEAT_S)):
            return
        self._send(values, lanes, now)

    def _send(self, values, lanes=frozenset(), now: float | None = None) -> None:
        self.send(list(values), frozenset(lanes))
        now = time.monotonic() if now is None else now
        with self._lock:
            prev, self._last, self._sent_at = self._last, (tuple(values), frozenset(lanes)), now
            self._sent = list(values)
        for slot in BOT_SLOTS:  # what changed, for the session recording
            pressed = slot in lanes
            if prev is None or prev[0][slot] != values[slot] or (slot in prev[1]) != pressed:
                if values[slot] is not None or (prev is not None and prev[0][slot] is not None):
                    self._emit(BotSent(slot, values[slot], lane=pressed, host_t=now))

    def _final_values(self) -> list[int | None]:
        if self._safety.release_on_exit:
            return [None] * 6
        with self._lock:
            return [None if v is None else 0 for v in self._target]

    def _zero_targets(self) -> None:
        for slot in BOT_SLOTS:
            if self._target[slot] is not None:
                self._target[slot] = 0

    def _hold_at_zero(self) -> None:
        """Lock held by the caller."""
        self._zero_targets()
        if any(t is not None for t in self._target):
            self._holds.add("reconnect")

    def _set_state(self, state: str, error: str | None = None) -> None:
        if (state, error) == (self.state, self.error):
            return
        self.state, self.error = state, error
        self._emit(InterfaceState(self.kind, state, self.device, error, dict(self.info)))
