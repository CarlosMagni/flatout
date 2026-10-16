"""Roll call: which car is which slot, before any race (`python -m trackloc identify`, `run --identify`).

Today identity comes from the CU's finish-line crossings only, so several parked or bunched cars stay
unnamed until each has crossed the line cleanly. The CU's `C` command lets software hold ANY slot's
throttle (RaceLab: `bot.claim` / `bot.act` / `bot.unclaim`), so we ask the cars one by one:

1. look at the track (cars parked), claim the slot, pulse a LOW throttle for ~0.5 s, then 0, wait for
   the car to stop, look again;
2. the car that moved is that slot: exactly one car-sized blob left its place and one appeared ahead of
   it along the lane, every other blob is where it was;
3. its colours (`appearance.signature`, taken while it still stood) are the slot's signature;
4. unclaim. No car or several moved (or a hand is in view): retry once (one throttle step up if no car
   moved, never above `cap`), then the slot is reported unidentified.

`apply_results` names the cars in the tracker at once (`Tracker.hint`) and stores the signatures
(`Tracker.set_signature`: used only as a tie-break of an ambiguous crossing).

SAFETY (this moves cars; same rules as `drive.DiscoveryDriver`, whose patterns it reuses):

- refused while a session runs or E-STOP is on, or if the CU link is down; checked before every pulse and
  on every throttle refresh; E-STOP, a dropped link or a refused `act` abort the whole roll call and the
  slot is never claimed again;
- the throttle is refreshed every `REFRESH_S` (RaceLab's dead-man switch cuts it after 0.6 s of silence)
  and never exceeds `cap`; a pulse is at most `MAX_PULSE_S` long;
- in a `finally`, whatever happens (exception, Ctrl-C, camera stall, lost link): throttle 0, then unclaim,
  each step on its own so one failure does not skip the next.

`control` is the RaceLab link: `claim(slot, free=True)` (outside a session), `act`, `unclaim`, `status()`.
`feed` has `latest() -> (arrival time, BGR image) | None` (`FrameTap` live). Both clocks are injectable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from .appearance import Signature, signature
from .drive import REFRESH_S
from .pipeline import Pipeline
from .tracker import Tracker, wrap

log = logging.getLogger("trackloc.identify")

MAX_PULSE_S = 2.0  # a longer pulse would drive the car round the track, not tell it apart


class RollCallAborted(RuntimeError):
    """The roll call was refused or stopped for safety (the message says why; the car was handed back)."""


class Control(Protocol):
    def claim(self, slot: int, free: bool = False) -> None: ...
    def act(self, slot: int, throttle: int | None = None, lane: bool = False) -> None: ...
    def unclaim(self, slot: int) -> None: ...
    def status(self) -> dict[str, Any]: ...


class FrameTap:
    """Keeps the latest frame of a live capture: `on_frame` is a capture callback (or `Live.frame_tap`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: tuple[float, np.ndarray] | None = None

    def on_frame(self, t: float, t_raw: float, image: np.ndarray) -> None:
        with self._lock:
            self._last = (t_raw, image)

    def latest(self) -> tuple[float, np.ndarray] | None:
        with self._lock:
            return self._last


@dataclass
class RollConfig:
    throttle: int = 4  # 0..15, the pulse
    cap: int = 8  # a retry never goes above this
    pulse_s: float = 0.5
    settle_s: float = 1.0  # after the pulse: the car stops (its speed has decayed to ~1 cm of creep) and the camera
    # catches up (its latency, 0.1 s or more on Continuity Camera, is unknown)
    still_tol_m: float = 0.03  # a blob within this of where it was has not moved
    move_min_m: float = 0.08  # the car must have gone at least this far ahead along its lane
    move_max_m: float = 1.5
    retries: int = 1
    frame_timeout_s: float = 3.0
    quiet_gap_s: float = 0.15  # the picture before a pulse must be unchanged this much later (the last car stopped)
    quiet_timeout_s: float = 2.5  # ... or we go on with what we have (the verdict then says what is wrong)
    tick_s: float = 0.02

    def __post_init__(self) -> None:
        if not 1 <= self.throttle <= self.cap <= 15:
            raise ValueError(f"need 1 <= throttle ({self.throttle}) <= cap ({self.cap}) <= 15")
        if not 0.1 <= self.pulse_s <= MAX_PULSE_S:
            raise ValueError(f"pulse_s must be between 0.1 and {MAX_PULSE_S} s")


@dataclass
class SlotResult:
    slot: int
    identified: bool = False
    lane: int | None = None
    x: float | None = None  # m on the centre line where the car stands after the pulse
    s: float | None = None  # the same as a lap fraction
    moved_m: float | None = None
    throttle: int | None = None  # of the pulse that identified it
    signature: Signature | None = None
    attempts: int = 0
    note: str = ""


@dataclass
class _Car:
    lane: int
    x: float
    blob: Any


@dataclass
class _Snap:
    t: float
    image: np.ndarray
    mask: np.ndarray | None
    cars: list[_Car]
    disturbed: bool  # something larger than a car (a hand) is in view


class RollCall:
    def __init__(self, control: Control, pipe: Pipeline, feed: Any, slots: tuple[int, ...] | list[int],
                 cfg: RollConfig | None = None, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.control, self.pipe, self.feed = control, pipe, feed
        self.slots = tuple(slots)
        self.cfg = cfg or RollConfig()
        self.clock, self.sleep = clock, sleep
        self.length = pipe.model.length_c
        self.results: list[SlotResult] = []
        self.log: list[str] = []

    def _note(self, text: str) -> None:
        self.log.append(text)
        log.warning(text)

    # ------------------------------------------------------------------ the roll call

    def run(self, stop: threading.Event | None = None) -> list[SlotResult]:
        self.results = []
        self._check()  # refuses before anything is claimed
        for slot in self.slots:
            if stop is not None and stop.is_set():
                raise RollCallAborted("stopped by the user: the car was handed back")
            self.results.append(self._identify(slot))
        return self.results

    def _identify(self, slot: int) -> SlotResult:
        cfg = self.cfg
        res = SlotResult(slot)
        thr = cfg.throttle
        for attempt in range(1 + cfg.retries):
            res.attempts = attempt + 1
            try:
                verdict, snap, before, after, moved = self._pulse(slot, thr)
            except _CannotClaim as e:  # not ours to drive (another agent holds it...): say so, go on
                res.note = f"cannot claim slot {slot + 1}: {e}"
                self._note(res.note)
                return res
            if verdict == "one":
                assert before is not None and after is not None
                res.identified, res.lane, res.x, res.s = True, after.lane, after.x, after.x / self.length
                res.moved_m, res.throttle = moved, thr
                res.signature = signature(snap.image, snap.mask, self.pipe.blob_box(before.blob),
                                          self.pipe.detector.scale) if snap.mask is not None else None
                res.note = "" if res.signature else "too few car pixels for a colour signature"
                return res
            res.note = {"none": f"no car moved at throttle {thr}", "many": "more than one car moved (or the picture changed)",
                        "vanished": "a car left its place and did not reappear ahead of it",
                        "disturbed": "something large (a hand?) is in view"}[verdict]
            self._note(f"slot {slot + 1}, attempt {attempt + 1}: {res.note}")
            if verdict == "none":
                thr = min(thr + 1, cfg.cap)  # a car that does not move at 4 is probably stalled: one step up
        res.note += " (after the retry)"
        return res

    # ------------------------------------------------------------------ one pulse

    def _pulse(self, slot: int, thr: int) -> tuple[str, _Snap, _Car | None, _Car | None, float]:
        """Claim, pulse, zero, look, unclaim. Returns (verdict, look before, car before, car after, metres moved)."""
        cfg = self.cfg
        self._check()
        before = self._still_look()
        claimed = False
        attempted = False
        try:
            attempted = True
            try:
                self.control.claim(slot, free=True)
            except (ConnectionError, OSError):
                raise
            except Exception as e:
                raise _CannotClaim(f"{type(e).__name__}: {e}") from e
            claimed = True
            self._hold(slot, thr, cfg.pulse_s)
            self._act(slot, 0)
            after = self._fresh(self.clock() + cfg.settle_s)
        except (RollCallAborted, _CannotClaim, KeyboardInterrupt):
            raise
        except Exception as e:
            raise RollCallAborted(f"{type(e).__name__}: {e}. The car was handed back") from e
        finally:
            self._release(slot, claimed, attempted)
        verdict, car_before, car_after, moved = self._compare(before, after)
        return verdict, before, car_before, car_after, moved

    def _hold(self, slot: int, thr: int, seconds: float) -> None:
        """Throttle `thr`, refreshed every REFRESH_S, for `seconds`."""
        end = self.clock() + seconds
        while True:
            self._check()
            self._act(slot, min(thr, self.cfg.cap))
            left = end - self.clock()
            if left <= 1e-9:
                return
            self.sleep(min(REFRESH_S, left))
            if end - self.clock() <= 1e-9:
                return

    def _act(self, slot: int, throttle: int) -> None:
        try:
            self.control.act(slot, throttle=throttle)
        except Exception as e:  # refused: E-STOP, agent dropped, link lost: stop everything
            raise RollCallAborted(f"the car was taken from us ({type(e).__name__}: {e}): E-STOP or lost link? "
                                  "Not claiming it again") from e

    def _check(self) -> None:
        try:
            st = self.control.status()
        except Exception as e:
            raise RollCallAborted(f"cannot read the RaceLab state ({type(e).__name__}: {e}): link lost?") from e
        if st.get("estop"):
            raise RollCallAborted("E-STOP is on: clear it by hand (never from software), then run the roll call again")
        if st.get("session", "idle") not in ("idle", "finished"):
            raise RollCallAborted(f"a session is {st.get('session')!r}: the roll call only runs between sessions "
                                  "(it would pulse cars among the racers). End the session first")
        if st.get("link") not in (None, "connected"):
            raise RollCallAborted(f"the CU link is {st.get('link')!r}: the roll call cannot move the cars")

    def _release(self, slot: int, claimed: bool, attempted: bool) -> None:
        """Zero and unclaim, whatever happened; each step on its own so one failure does not skip the next.
        A claim that raised may still have succeeded on the server: give it back too."""
        steps = [("unclaim", lambda: self.control.unclaim(slot))] if attempted else []
        if claimed:
            steps.insert(0, ("throttle 0", lambda: self.control.act(slot, throttle=0)))
        for step, fn in steps:
            try:
                fn()
            except Exception as e:
                self._note(f"cleanup: {step} failed ({type(e).__name__}: {e})")

    # ------------------------------------------------------------------ looking

    def _fresh(self, since: float | None = None) -> _Snap:
        """The first frame that arrived at or after `since` (default: now), seen by the detector."""
        since = self.clock() if since is None else since
        deadline = max(since, self.clock()) + self.cfg.frame_timeout_s
        while True:
            got = self.feed.latest()
            if got is not None and got[0] >= since:
                return self._look(*got)
            if self.clock() > deadline:
                raise RollCallAborted("no new frame from the camera: it stalled? The car was handed back")
            self.sleep(self.cfg.tick_s)

    def _still_look(self) -> _Snap:
        """A look at the track once nothing moves any more: the car of the previous slot may still be coasting (the
        camera's latency is unknown), and a creeping car would read as a mover. Two looks `quiet_gap_s` apart must agree."""
        end = self.clock() + self.cfg.quiet_timeout_s
        prev = self._fresh()
        while True:
            cur = self._fresh(self.clock() + self.cfg.quiet_gap_s)
            if self._compare(prev, cur)[0] == "none" or self.clock() > end:
                return cur
            prev = cur

    def _look(self, t: float, image: np.ndarray) -> _Snap:
        dets = self.pipe.detections(t, image)
        blobs = self.pipe.last_blobs
        assert len(dets) == len(blobs)
        cars = [_Car(d.lane, d.x, b) for d, b in zip(dets, blobs) if not d.occluder]
        return _Snap(t, image, self.pipe.detector.last_mask, cars, any(d.occluder for d in dets))

    def _compare(self, a: _Snap, b: _Snap) -> tuple[str, _Car | None, _Car | None, float]:
        """What changed between two looks: ("one", car before, car after, metres) when exactly one car moved."""
        cfg, L = self.cfg, self.length

        def same(p: _Car, q: _Car) -> bool:
            return p.lane == q.lane and abs(wrap(p.x - q.x, L)) <= cfg.still_tol_m

        if a.disturbed or b.disturbed:
            return "disturbed", None, None, 0.0
        left = [c for c in a.cars if not any(same(c, o) for o in b.cars)]
        new = [o for o in b.cars if not any(same(c, o) for c in a.cars)]
        if not left and not new:
            return "none", None, None, 0.0
        if len(left) != 1 or len(new) > 1:
            return "many", None, None, 0.0
        if not new:
            return "vanished", None, None, 0.0
        d = wrap(new[0].x - left[0].x, L)
        if abs(d) < cfg.move_min_m:  # crept a few centimetres: not a clear mover
            return "none", None, None, 0.0
        if not 0 < d <= cfg.move_max_m:  # a slot car does not drive backwards
            return "many", None, None, 0.0
        return "one", left[0], new[0], d


class _CannotClaim(RuntimeError):
    pass


def apply_results(tracker: Tracker, results: list[SlotResult]) -> list[int]:
    """Name the identified cars in the tracker and store their colours; returns the slots named."""
    named = []
    for r in results:
        if r.identified and r.x is not None and r.lane is not None:
            tracker.hint(r.slot, r.x, r.lane)
            if r.signature is not None:
                tracker.set_signature(r.slot, r.signature)
            named.append(r.slot)
    return named


def format_results(results: list[SlotResult]) -> str:
    lines = ["", "ROLL CALL", "---------"]
    for r in results:
        if r.identified:
            lines.append(f"slot {r.slot + 1}: identified: lane {r.lane}, {r.s:.3f} of a lap ({r.moved_m:.2f} m moved at throttle "
                         f"{r.throttle}/15){'' if r.signature else '; ' + r.note}")
        else:
            lines.append(f"slot {r.slot + 1}: NOT identified: {r.note}")
    bad = [r.slot + 1 for r in results if not r.identified]
    if bad:
        lines.append(f"unidentified: slots {bad}: they will be named by their first clean finish-line crossing. Is the car "
                     "on the track, in view, on that slot, and not blocked?")
    return "\n".join(lines)
