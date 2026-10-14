"""Driving the discovery lap safely (pure standard library: tested without a camera).

`map` makes one car run a few laps at a low constant throttle while the camera records. This is
the only place of trackloc that can move a car, so the rules are strict:

- the car is ALWAYS handed back at throttle 0 in a `finally`: normal end, exception, Ctrl-C
  (KeyboardInterrupt), stall, E-STOP, lost link, timeout;
- the throttle is refreshed every 0.25 s (RaceLab's dead-man switch cuts it after 0.6 s of silence);
  nothing slow runs in the driving loop (vision runs in other threads);
- a STALL (no detected progress for `stall_s`, default 1.5 s) bumps the throttle by 1, up to
  `throttle_cap`; stalled again at the cap: E-STOP and abort;
- if the car is taken from us (E-STOP, agent dropped, `act` refused), the run aborts and never
  claims the slot again;
- the session is started only if none is running, and stopped at the end only if we started it.

`control` is any object with `ensure_session() -> bool`, `stop_session()`, `claim(slot)`,
`act(slot, throttle=None, lane=False)`, `unclaim(slot)`, `estop()` (the RaceLab link has them).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("trackloc.drive")

REFRESH_S = 0.25  # period of the throttle refresh (dead-man switch of RaceLab: 0.6 s)


class MapAborted(RuntimeError):
    """The discovery lap was stopped for safety (the message says why and what happened to the car)."""


class Control(Protocol):
    def ensure_session(self) -> bool: ...
    def stop_session(self) -> None: ...
    def claim(self, slot: int) -> None: ...
    def act(self, slot: int, throttle: int | None = None, lane: bool = False) -> None: ...
    def unclaim(self, slot: int) -> None: ...
    def estop(self) -> None: ...


@dataclass
class DriveConfig:
    throttle: int = 4  # 0..15
    throttle_cap: int = 8  # a stall never pushes it above this
    laps: int = 4  # laps to record (crossings needed: laps + 1)
    lanes: int = 2  # 2: a lane change is armed after crossings 2 and 4, to map the other lane too
    arm_after: tuple[int, ...] = (2, 4)  # crossing counts after which the lane change is armed
    stall_s: float = 1.5  # no progress for this long = stalled
    progress_eps: float = 1.0  # what counts as progress (units of the progress meter)
    timeout_s: float = 300.0
    tick_s: float = 0.05
    first_crossing_s: float = 60.0  # no crossing at all this long after the start: something is wrong


class DiscoveryDriver:
    """Runs the car. `progress()` is a monotonically increasing measure of motion seen by the camera;
    `crossings()` the number of finish-line crossings of this car seen so far."""

    def __init__(self, control: Control, slot: int, cfg: DriveConfig, *, progress: Callable[[], float],
                 crossings: Callable[[], int], clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.control, self.slot, self.cfg = control, slot, cfg
        self.progress, self.crossings = progress, crossings
        self.clock, self.sleep = clock, sleep
        self.throttle = cfg.throttle
        self.bumps = 0
        self.armed: set[int] = set()
        self.log: list[str] = []
        self.claimed = False
        self.session_started = False

    def _note(self, text: str) -> None:
        self.log.append(text)
        log.warning(text)

    def run(self, stop: threading.Event | None = None) -> dict[str, Any]:
        cfg = self.cfg
        outcome = "unknown"
        try:
            self.session_started = self.control.ensure_session()
            self.control.claim(self.slot)
            self.claimed = True
            self.control.act(self.slot, throttle=self.throttle)
            t0 = last_refresh = last_move = self.clock()
            last_progress = self.progress()
            while True:
                if stop is not None and stop.is_set():
                    outcome = "stopped"
                    raise MapAborted("stopped by the user: the car is handed back")
                now = self.clock()
                n = self.crossings()
                if n >= cfg.laps + 1:
                    outcome = "done"
                    break
                if now - t0 > cfg.timeout_s:
                    raise MapAborted(f"timeout after {cfg.timeout_s:.0f} s with {n} crossings: the car is handed back")
                if n == 0 and now - t0 > cfg.first_crossing_s:
                    raise MapAborted("the car never crossed the finish line: is the session running and the "
                                     "right slot driven? The car is handed back")
                if cfg.lanes == 2:
                    for k in cfg.arm_after:
                        if n >= k and k not in self.armed:
                            self.armed.add(k)
                            self._act(lane=True)
                p = self.progress()
                if p - last_progress >= cfg.progress_eps:
                    last_progress, last_move = p, now
                elif now - last_move >= cfg.stall_s:
                    self._stalled(now)
                    last_move = now
                if now - last_refresh >= REFRESH_S:
                    self._act(throttle=self.throttle)
                    last_refresh = now
                self.sleep(cfg.tick_s)
        except MapAborted:
            raise
        except KeyboardInterrupt:
            outcome = "interrupted"
            raise
        except Exception as e:
            outcome = "error"
            raise MapAborted(f"{type(e).__name__}: {e}. The car is handed back") from e
        finally:
            self._release(outcome)
        return {"outcome": outcome, "throttle": self.throttle, "bumps": self.bumps, "crossings": self.crossings(),
                "notes": self.log}

    def _act(self, **kw: Any) -> None:
        try:
            self.control.act(self.slot, **kw)
        except Exception as e:  # refused: E-STOP, agent dropped, link lost... abort (and never claim again)
            raise MapAborted(f"the car was taken from us ({type(e).__name__}: {e}): E-STOP or lost link? "
                             "Not claiming it again") from e

    def _stalled(self, now: float) -> None:
        if self.throttle >= self.cfg.throttle_cap:
            self._note(f"stalled at the throttle cap ({self.cfg.throttle_cap}): E-STOP")
            try:
                self.control.estop()
            except Exception as e:
                self._note(f"E-STOP failed: {e}")
            raise MapAborted(f"the car does not move even at throttle {self.throttle}: E-STOP sent. Is it on the "
                             "track, in the camera view, and the right slot? Raise the cap only if you are sure")
        self.throttle += 1
        self.bumps += 1
        self._note(f"no progress for {self.cfg.stall_s:g} s: throttle {self.throttle - 1} -> {self.throttle}")
        self._act(throttle=self.throttle)

    def _release(self, outcome: str) -> None:
        """Zero and unclaim, whatever happened; each step on its own so one failure does not skip the rest."""
        if self.claimed:
            for step, fn in (("throttle 0", lambda: self.control.act(self.slot, throttle=0)),
                             ("unclaim", lambda: self.control.unclaim(self.slot))):
                try:
                    fn()
                except Exception as e:
                    self._note(f"cleanup: {step} failed ({type(e).__name__}: {e})")
            self.claimed = False
        elif outcome != "done":
            try:  # the claim may have succeeded without us knowing (exception right after): give it back
                self.control.unclaim(self.slot)
            except Exception:
                pass
        if self.session_started:
            try:
                self.control.stop_session()
            except Exception as e:
                self._note(f"cleanup: stop session failed ({type(e).__name__}: {e})")
            self.session_started = False
