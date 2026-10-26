"""ControllerInterface: hardware that plays the hand controllers, outside the CU.

The bot model RaceLab drives through such a device, with its safety rules, is `model.BotModel`, the
CU link's too; a new kind of hardware only says how a frame reaches it (`open`, `send`, `poll`, `close`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from ..config import SafetyConfig
from ..events import BotSent, InterfaceState
from .model import BotModel, Frame

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
    device itself hands every slot back when frames stop for ~750 ms. Both are fixed, whatever
    `[safety]` says: docs/controller-interface.md publishes them to the builders of devices (and they
    are the lowest values `heartbeat_ms` and `min_bot_interval_ms` accept, see config.py).

    Safety, the same as the CU link's, both from `BotModel`: E-STOP before anything else, throttle 0
    and a "reconnect" hold when the device or the CU link is lost (the app lifts it once it has
    zeroed its bots), throttle 0 on the app's bots whenever the device is (re)opened, the release
    frame on exit.

    A subclass implements `open()`, `send(values, lanes)`, `poll()` (read the device, call
    `feedback(info)` on each report it gets) and `close()`; any exception from them means the device
    is lost: it is closed, then opened again RETRY_S later.
    """

    kind = "base"
    PERIOD_S = 0.01  # frames are decided this often
    MIN_GAP_S = 0.02  # two frames at least this far apart (a device keeps only the latest anyway)
    HEARTBEAT_S = 0.1  # an unchanged frame repeated this often: far inside the device's ~750 ms expiry
    RETRY_S = 2.0  # between two attempts to open the device
    SILENT_S = 3.0  # a device that has reported once, then stays silent this long, is lost

    def __init__(self, device: str, safety: SafetyConfig, emit: Callable[[object], None]):
        self.device = device
        self._safety = safety
        self._emit = emit
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._bots = BotModel(safety, heartbeat_s=self.HEARTBEAT_S, min_gap_s=self.MIN_GAP_S)
        self._sent: list[int | None] = [None] * 6  # what the device got; None = nothing driven, or unknown
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
        self._bots.set_bot(slot, value)

    def lane(self, slot: int) -> None:
        self._bots.lane(slot)

    def estop(self) -> None:
        """Throttle 0 on every active bot, sent before any other frame."""
        self._bots.estop()

    def clear_estop(self) -> None:
        self._bots.clear_estop()

    def resume_bots(self) -> None:
        """Lift the hold set by a lost link (the app has acknowledged throttle 0)."""
        self._bots.resume()

    @property
    def holds(self) -> frozenset[str]:
        return self._bots.holds

    def bot_targets(self) -> list[int | None]:
        return self._bots.targets()

    def bot_applied(self) -> list[int | None]:
        with self._lock:
            return list(self._sent)

    def unsafe_slots(self) -> list[int]:
        return []  # the device hands every slot back by itself when the frames stop

    def on_cu_link(self, state: str) -> None:
        """The CU link changed state: lost, the race is blind, so throttle 0 and hold, as the CU link does."""
        if state == "lost":
            self._bots.hold_at_zero()

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
            self._bots.forget()
            with self._lock:
                self._feedback_at = None
            self._set_state("connected")
            try:
                while not self._stop.is_set():
                    self.poll()
                    seen = self._feedback_at
                    if seen is not None and time.monotonic() - seen > self.SILENT_S:
                        raise OSError(f"no report for {self.SILENT_S:g} s")
                    self._service()
                    self._stop.wait(self.PERIOD_S)
                self._send(self._bots.final_frame())  # shutting down: hand back, or hold at 0
            except Exception as e:  # write timeout, port gone, device silent
                logger.warning("%s interface lost: %s", self.kind, e)
                self._bots.hold_at_zero()
                with self._lock:
                    self._sent = [None] * 6  # nothing known to be driven any more
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
        frame = self._bots.frame(now)
        if frame is not None:
            self._send(frame, now)

    def _send(self, frame: Frame, now: float | None = None) -> None:
        self.send(list(frame.values), frame.lanes)
        now = time.monotonic() if now is None else now
        self._bots.sent(frame, now)
        with self._lock:
            self._sent = list(frame.values)
        for slot, value, lane in frame.changes():  # what changed, for the session recording
            self._emit(BotSent(slot, value, lane=lane, host_t=now))

    def _set_state(self, state: str, error: str | None = None) -> None:
        if (state, error) == (self.state, self.error):
            return
        self.state, self.error = state, error
        self._emit(InterfaceState(self.kind, state, self.device, error, dict(self.info)))
