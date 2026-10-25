"""Race engine: pure logic, no I/O, no thread, no asyncio.

It consumes the CU link messages (`on_status`, `on_timer`), is paced by
`tick()`, drives the CU only through `CUActions` and publishes events via
`on_event`. Contract: docs/architecture.md §4.

Two clocks, never mixed:
- CU clock (timestamps of the passes, ms, 32 bits): lap times, gaps,
  standings (unwrapped continuously to survive the wrap back to zero);
- host clock (`msg.host_t`, `clock()`): elapsed session time, pauses,
  `since_cross_ms`, wait timeouts.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from ..config import DEFAULT_COLORS, DEFAULT_NAMES, DriverConfig, RaceConfig
from ..events import (
    BUTTON_START,
    FUEL_MODE,
    LAP_COUNTER_MODE,
    PIT_LANE_MODE,
    REAL_MODE,
    StatusMsg,
    TimerMsg,
    lights_for_phase,
)
from .actions import CUActions
from .modes import BEST_LAP_TYPES, LAP_TARGET_TYPES, SESSION_TYPES, TIMED_TYPES, SessionSettings
from .tower import TowerManager

logger = logging.getLogger(__name__)

MOD32 = 1 << 32
COUNTING_STATES = ("running", "finishing", "paused")  # states where passes count
DEDUPE_WINDOW = 64  # remembered duplicates (address, timestamp, sector)
RECENT_LAPS = 5
ARMED_WAIT_S = 0.8  # phase 1 persisting: 2nd press (countdown)
NO_REACTION_S = 1.5  # phase 0 persisting after a press: the CU did not react
MIN_REPRESS_S = 2.0  # at most one re-press every 2 s
FALSE_START = (8, 9)  # CU phases of a false start: its lights blink until Start is pressed


def _signed_delta(a: int, b: int) -> int:
    """a - b on 32 bits, signed (negative = `a` is older than `b`)."""
    half = MOD32 >> 1
    return (a - b + half) % MOD32 - half


@dataclass
class _Car:
    slot: int
    cross: list[int] = field(default_factory=list)  # unwrapped CU time of each pass; index = laps completed
    last_ts: int | None = None  # last accepted CU timestamp (32 bits): debounce
    last_host: float | None = None  # host_t of the last sector 1 pass
    lap_ms: list[int] = field(default_factory=list)
    best_ms: int | None = None
    best_lap: int | None = None
    best_cu: int | None = None  # CU time of the pass that set the best lap (breaks ties)
    last_pb: bool = False
    last_sb: bool = False
    splits: list[int | None] = field(default_factory=lambda: [None, None])  # sectors 2 and 3, from the start of the lap
    has_sectors: bool = False
    finished: bool = False
    elim_index: int | None = None  # 0 = first car eliminated
    episode: int = 1
    ep_laps: list[int] = field(default_factory=list)
    ep_valid: list[int] = field(default_factory=list)  # non-neutralized laps of the episode
    ep_restart: bool = False  # the next lap includes the reset stop: outside the episode
    position: int | None = None
    gap_ms: int | None = None
    gap_laps: int = 0
    interval_ms: int | None = None
    interval_laps: int = 0

    @property
    def started(self) -> bool:
        return bool(self.cross)

    @property
    def laps(self) -> int:
        return len(self.lap_ms)


class RaceEngine:
    def __init__(
        self,
        drivers: list[DriverConfig],
        race_cfg: RaceConfig,
        actions: CUActions,
        *,
        tower: TowerManager | None = None,
        on_event: Callable[[str, dict], None] = lambda name, data: None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._race_cfg = race_cfg
        self._actions = actions
        self._tower = tower
        self._on_event = on_event
        self._clock = clock
        self._wall_clock = wall_clock
        self.state = "idle"
        self.settings = SessionSettings()
        self._drivers = self._index(drivers)
        # CU state (survives sessions)
        self._phase = 0
        self._phase_since = 0.0
        self._mode = 0
        self._display = 6
        self._fuel: list[int | None] = [None] * 8
        self._pit = [False] * 8
        self._issued_ids: set[str] = set()
        self._reset_session()

    # ------------------------------------------------------------------ inputs

    def on_status(self, msg: StatusMsg) -> None:
        self._mode = msg.mode
        self._display = msg.display
        self._track_fuel_and_pits(msg)
        previous, phase = self._phase, msg.start
        if phase == previous:
            return
        self._phase, self._phase_since = phase, msg.host_t
        lights, blink = lights_for_phase(phase)
        self._emit("phase", {"phase": phase, "lights": lights, "blink": blink})
        self._on_phase_change(previous, phase, msg.host_t)

    def on_timer(self, msg: TimerMsg) -> None:
        key = (msg.address, msg.timestamp, msg.sector)
        if key in self._seen:
            return
        self._seen.append(key)
        if not 0 <= msg.address < 8 or msg.sector not in (1, 2, 3):
            return
        if self.state == "running":
            self._check_deadline(msg.host_t)  # a pass after the deadline must not depend on the tick
        if self.state not in COUNTING_STATES:
            return
        car = self._cars.get(msg.address)
        if car is not None and (car.finished or car.elim_index is not None):
            return
        if msg.sector == 1:
            self._on_line(msg, car)
        elif car is not None and car.started:
            self._on_split(msg, car)

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        if self.state == "running":
            self._check_deadline(now)
        elif self.state == "starting" or (self.state == "paused" and self._resuming):
            self._maintain_countdown(now)
        if self._racing_state() == "finishing":
            self._check_finished(now)
        if self._tower is not None:
            self._tower.tick(now)

    # ----------------------------------------------------------------- commands

    def configure(self, settings: SessionSettings) -> None:
        """New session: reset everything and move to `grid`."""
        if self.state not in ("idle", "grid", "finished"):
            raise ValueError("Session in progress: stop it before configuring another")
        settings.validate()
        self.settings = replace(settings, title=settings.title or settings.default_title())
        self._reset_session()
        self._id = self._new_id()
        self._actions.reset_timer()
        self._push_ignore()
        if self._tower is not None:
            self._tower.clear()
            self._tower.update([], 0)  # lap counter reset to 0
        self._set_state("grid")

    def start(self) -> None:
        if self.state != "grid":
            raise ValueError("Cannot start: the session is not on the grid")
        now = self._clock()
        self._false_start = False
        if self._uses_lights():
            self._set_state("starting")
            self._countdown_t0 = self._last_press = now
            self._last_repress = None
            if self._phase == 0 or self._phase in FALSE_START:
                self._press_start(now)
        else:
            self._t_go = now
            self._set_state("running")
            self._release_cars(now)

    def pause(self) -> None:
        if self.state not in ("running", "finishing"):
            raise ValueError("Cannot pause: the race is not in progress")
        now = self._clock()
        self._press_start(now)
        self._enter_pause(now)

    def resume(self) -> None:
        if self.state != "paused":
            raise ValueError("Cannot resume: the race is not paused")
        now = self._clock()
        self._false_start = False
        if self._uses_lights():
            # the CU counts down, the return to racing happens at GO
            self._resuming = True
            self._countdown_t0 = self._last_press = now
            self._last_repress = None
            if self._phase in (0, 1) or self._phase in FALSE_START:
                self._press_start(now)
        else:
            self._leave_pause(now)
            self._release_cars(now)

    def stop(self) -> None:
        """End the session whatever its state (except `idle`)."""
        if self.state == "idle":
            raise ValueError("No session to stop")
        if self.state != "finished":
            self._end_session(self._clock())

    def reset_episode(self, slot: int) -> int:
        """AI training: new episode for `slot` (called on the agent's reset).

        The current lap includes the car's stop: it does not count in the
        next episode. Returns the number of the new episode.
        """
        if self.settings.type != "training" or self.state in ("idle", "finished"):
            raise ValueError("Episode reset: an AI training session in progress is required")
        car = self._cars.setdefault(slot, _Car(slot))
        if car.ep_laps:
            car.episode += 1
        car.ep_laps, car.ep_valid = [], []
        car.ep_restart = car.started
        self._emit("episode_reset", {"slot": slot, "episode": car.episode})
        return car.episode

    def set_drivers(self, drivers: list[DriverConfig]) -> None:
        self._drivers = self._index(drivers)
        for slot, driver in self._drivers.items():
            if driver.enabled:
                self._cars.setdefault(slot, _Car(slot))
        for slot, car in list(self._cars.items()):
            if not car.started and not self._is_enabled(slot):
                del self._cars[slot]
        if self.state != "idle":
            self._push_ignore()
            if self._racing_state() == "finishing":
                self._check_finished(self._clock())

    # ------------------------------------------------------------------- reading

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        lights, blink = lights_for_phase(self._phase)
        cars = sorted(self._cars.values(), key=lambda c: (c.position is None, c.position or 0, c.slot))
        return {
            "session": self._session_dict(now),
            "cu": {
                "phase": self._phase,
                "lights": lights,
                "blink": blink,
                "fuel_mode": bool(self._mode & FUEL_MODE),
                "real_mode": bool(self._mode & REAL_MODE),
                "pit_lane": bool(self._mode & PIT_LANE_MODE),
                "lap_counter": bool(self._mode & LAP_COUNTER_MODE),
                "display": self._display,
            },
            "cars": [self._car_dict(car, now) for car in cars],
        }

    def laps(self, slot: int | None = None) -> list[dict[str, Any]]:
        return [dict(lap) for lap in self._laps if slot is None or lap["slot"] == slot]

    def results(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "session": snap["session"],
            "classification": [car for car in snap["cars"] if car["position"] is not None],
            "laps": self.laps(),
        }

    # ------------------------------------------------------------ session state

    @staticmethod
    def _index(drivers: list[DriverConfig]) -> dict[int, DriverConfig]:
        for driver in drivers:
            if not 0 <= driver.slot < 8:
                raise ValueError(f"Invalid driver slot: {driver.slot}")
        return {driver.slot: driver for driver in drivers}

    def _reset_session(self) -> None:
        self._id: str | None = None
        self._cars = {slot: _Car(slot) for slot, d in self._drivers.items() if d.enabled}
        self._laps: list[dict[str, Any]] = []
        self._best: dict[str, int] | None = None
        self._order: list[int] = []
        self._published: list[int] = []
        self._seen: deque[tuple[int, int, int]] = deque(maxlen=DEDUPE_WINDOW)
        self._last_cu: int | None = None  # last unwrapped CU time (all drivers)
        self._pits = [0] * 8
        self._false_start = False
        # host timeline
        self._t_go: float | None = None
        self._paused_total = 0.0
        self._pause_start: float | None = None
        self._pauses: list[list[float | None]] = []  # [start, end] (end None = ongoing)
        self._resume_state = "running"
        self._elapsed_final = 0.0
        # finish
        self._finishing_elapsed = 0.0  # elapsed time on entering `finishing`
        self._deadline: float | None = None  # passes before this deadline do not finish the car
        # start / resume
        self._countdown_t0 = 0.0
        self._last_press = 0.0
        self._last_repress: float | None = None
        self._resuming = False  # resume requested by resume() (lights)
        self._releasing = False  # Start was pressed to release the cars (no lights)
        # elimination
        self._eliminated: list[int] = []
        self._last_elim_lap = 0

    def _new_id(self) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._wall_clock()))
        base = f"{stamp}-{self.settings.type}"
        session_id, n = base, 2
        while session_id in self._issued_ids:
            session_id, n = f"{base}-{n}", n + 1
        self._issued_ids.add(session_id)
        return session_id

    def _set_state(self, new: str) -> None:
        prev, self.state = self.state, new
        # the title rides along: a UI that announces the session cannot rely on a state it has not received yet
        self._emit("session", {"state": new, "prev": prev, "type": self.settings.type,
                               "title": self.settings.title})

    def _racing_state(self) -> str:
        """`running` or `finishing`, even during a pause."""
        return self._resume_state if self.state == "paused" else self.state

    def _uses_lights(self) -> bool:
        return SESSION_TYPES[self.settings.type]["uses_lights"]

    def _is_enabled(self, slot: int) -> bool:
        driver = self._drivers.get(slot)
        return driver is not None and driver.enabled

    def _duration(self) -> float | None:
        if self.settings.type in TIMED_TYPES:
            return self.settings.duration_s
        return None

    def _emit(self, name: str, data: dict[str, Any]) -> None:
        try:
            self._on_event(name, data)
        except Exception:  # a failing subscriber must never break the race
            logger.exception("on_event(%s) failed", name)

    def _press_start(self, now: float) -> None:
        self._actions.press(BUTTON_START)
        self._last_press = now

    def _push_ignore(self) -> None:
        mask = 0
        for slot, driver in self._drivers.items():
            if not driver.enabled:
                mask |= 1 << slot
        for car in self._cars.values():
            if car.elim_index is not None:
                mask |= 1 << car.slot
        self._actions.ignore(mask)

    # ------------------------------------------------------------ time and pauses

    def _elapsed(self, now: float) -> float:
        """Session time excluding pauses (s); frozen at the end."""
        if self.state == "finished":
            return self._elapsed_final
        if self._t_go is None:
            return 0.0
        paused = self._paused_total
        if self._pause_start is not None:
            paused += max(0.0, now - self._pause_start)
        return max(0.0, now - self._t_go - paused)

    def _enter_pause(self, now: float) -> None:
        self._resume_state = self.state
        self._pause_start = now
        self._pauses.append([now, None])
        self._resuming = self._releasing = False
        self._set_state("paused")

    def _close_pause(self, now: float) -> None:
        if self._pause_start is None:
            return
        self._paused_total += max(0.0, now - self._pause_start)
        self._pauses[-1][1] = now
        self._pause_start = None

    def _leave_pause(self, now: float) -> None:
        self._close_pause(now)
        self._resuming = False
        self._set_state(self._resume_state)

    def _covers_pause(self, start: float | None, end: float) -> bool:
        """Does the lap [start, end] overlap a pause?"""
        if start is None:
            return False
        return any(p0 < end and (p1 is None or p1 > start) for p0, p1 in self._pauses)

    def _release_cars(self, now: float) -> None:
        """Session without lights: if the CU is armed, Start releases the cars."""
        if self._phase == 1:
            self._press_start(now)
        if 1 <= self._phase <= 6:
            self._releasing = True  # the CU countdown is not a pause

    def _go(self, t: float) -> None:
        self._t_go = t
        self._set_state("running")

    def _end_session(self, now: float) -> None:
        self._elapsed_final = self._elapsed(now)
        self._close_pause(now)
        self._resuming = self._releasing = False
        self._set_state("finished")
        self._refresh()
        # Session over: hold the cars on the CU (one Start press arms it — the cars do
        # not move until the next start). Only from a freely running CU: never during
        # its countdown (phases 2..6) nor a fault show (>= 8, short circuit).
        if self._phase == 0:
            self._press_start(now)

    # ----------------------------------------------------------------- CU: phases

    def _on_phase_change(self, prev: int, phase: int, t: float) -> None:
        go = 2 <= prev <= 6 and phase in (0, 7)
        if phase in (0, 7):
            self._releasing = False
        if phase >= 10 and prev < 10:
            # the CU's short-circuit protection: track power cut for a few seconds,
            # beeping with all lights blinking (phases 10..11)
            self._emit("short_circuit", {"phase": phase})
        if self.state == "starting":
            if phase >= 8 and prev < 8:
                self._false_start = True
                self._emit("false_start", {})
                self._set_state("grid")
            elif go:
                self._go(t)
        elif self.state in ("running", "finishing"):
            if 1 <= phase <= 6 and not self._releasing:
                self._enter_pause(t)  # someone pressed Start on the CU
        elif self.state == "paused" and self._resuming and phase >= 8 and prev < 8:
            self._false_start = True  # during the countdown of a resume: still paused, resume again
            self._resuming = False
            self._emit("false_start", {})
        elif self.state == "paused" and go:
            self._leave_pause(t)

    def _maintain_countdown(self, now: float) -> None:
        """Press Start again if the CU did not react, still shows a false start, or stays armed (phase 1)."""
        if self._last_repress is not None and now - self._last_repress < MIN_REPRESS_S:
            return
        if self._phase == 0 or self._phase in FALSE_START:
            due = now - self._last_press >= NO_REACTION_S  # no reaction, or a false start still shown
        else:
            due = (
                self._phase == 1
                and self._race_cfg.auto_countdown
                and now - max(self._phase_since, self._countdown_t0) >= ARMED_WAIT_S
            )
        if due:
            self._last_repress = now
            self._press_start(now)

    def _track_fuel_and_pits(self, msg: StatusMsg) -> None:
        low = self._race_cfg.fuel_low
        for slot, (fuel, pit) in enumerate(zip(msg.fuel, msg.pit)):
            known = slot in self._cars
            before = self._fuel[slot]
            self._fuel[slot] = fuel
            if known and self._mode & FUEL_MODE and before is not None and before > low >= fuel:
                self._emit("fuel_low", {"slot": slot, "fuel": fuel})
            if pit != self._pit[slot]:
                self._pit[slot] = pit
                if pit:
                    self._pits[slot] += 1
                if known:
                    self._emit("pit_in" if pit else "pit_out", {"slot": slot, "pits": self._pits[slot]})

    # ------------------------------------------------------------ CU: passes

    def _unwrap(self, ts: int) -> int:
        """32-bit timestamp -> continuous CU time (survives the wrap back to zero)."""
        if self._last_cu is None:
            self._last_cu = ts
        else:
            self._last_cu += _signed_delta(ts, self._last_cu % MOD32)
        return self._last_cu

    def _on_line(self, msg: TimerMsg, car: _Car | None) -> None:
        cfg = self._race_cfg
        if (car is None or not car.started) and self._racing_state() == "finishing" and cfg.grid_behind_line:
            return  # never took the start: its reference pass no longer counts
        if car is None:
            car = self._cars[msg.address] = _Car(msg.address)
        # lap time = difference of this car's CU timestamps (mod 2³²); None on the 1st pass
        lap_ms = None if car.last_ts is None else _signed_delta(msg.timestamp, car.last_ts)
        if lap_ms is not None and lap_ms < cfg.min_lap_ms:
            return  # bounce, or event older than the previous one
        cu = self._unwrap(msg.timestamp)
        car.last_ts = msg.timestamp
        self._emit("crossing", {"slot": car.slot, "sector": 1, "cu_ts": msg.timestamp})
        if lap_ms is None and cfg.grid_behind_line:
            car.cross.append(cu)  # reference pass (lap 0)
            car.last_host = msg.host_t
            self._refresh()
            return
        self._count_lap(car, msg, cu, lap_ms)

    def _count_lap(self, car: _Car, msg: TimerMsg, cu: int, lap_ms: int | None) -> None:
        host = msg.host_t
        if lap_ms is not None:
            ms = lap_ms
            neutralized = self._covers_pause(car.last_host, host)
        else:
            # Grid in front of the line: the CU does not see the start, so we estimate
            # the 1st lap on the host clock and neutralize it (excluded from best laps).
            assert self._t_go is not None
            ms = max(1, round((host - self._t_go) * 1000))
            car.cross.append(cu - ms)
            neutralized = True
        car.cross.append(cu)
        number = car.laps + 1
        car.lap_ms.append(ms)
        personal_best = not neutralized and (car.best_ms is None or ms < car.best_ms)
        if personal_best:
            car.best_ms, car.best_lap, car.best_cu = ms, number, cu
        session_best = personal_best and (self._best is None or ms < self._best["ms"])
        if session_best:
            self._best = {"slot": car.slot, "ms": ms, "lap": number}
        car.last_pb, car.last_sb = personal_best, session_best
        car.splits = [None, None]
        car.last_host = host
        self._laps.append(
            {
                "slot": car.slot,
                "lap": number,
                "ms": ms,
                "cu_ts": msg.timestamp,
                "t": round(self._elapsed(host), 3),
                "neutralized": neutralized,
                "personal_best": personal_best,
                "session_best": session_best,
            }
        )
        self._classify()
        self._emit(
            "lap",
            {
                "slot": car.slot,
                "lap": number,
                "ms": ms,
                "neutralized": neutralized,
                "personal_best": personal_best,
                "session_best": session_best,
                "position": car.position,
            },
        )
        if self.settings.type == "training":
            self._episode_lap(car, ms, neutralized)
        self._after_lap(car, host)
        self._refresh()

    def _on_split(self, msg: TimerMsg, car: _Car) -> None:
        assert car.last_ts is not None
        ms = _signed_delta(msg.timestamp, car.last_ts)  # from the start of the current lap
        if ms < 0:
            return
        car.has_sectors = True
        car.splits[msg.sector - 2] = ms
        self._emit("crossing", {"slot": car.slot, "sector": msg.sector, "cu_ts": msg.timestamp})
        self._emit("sector", {"slot": car.slot, "sector": msg.sector, "ms": ms})

    # ---------------------------------------------------------- finish and rules

    def _check_deadline(self, now: float) -> None:
        duration = self._duration()
        if duration is not None and self._elapsed(now) >= duration:
            self._start_finishing(duration, deadline=duration)
            self._check_finished(now)

    def _start_finishing(self, elapsed: float, *, deadline: float | None = None) -> None:
        self._finishing_elapsed = elapsed
        self._deadline = deadline
        if self.state == "paused":
            self._resume_state = "finishing"
        else:
            self._set_state("finishing")

    def _after_lap(self, car: _Car, host: float) -> None:
        kind = self.settings.type
        if kind == "elimination":
            self._check_elimination(car, host)
            return
        state = self._racing_state()
        if state == "running":
            if kind in LAP_TARGET_TYPES and car.laps >= self.settings.laps:
                self._start_finishing(self._elapsed(host))
                self._finish_car(car, host)
        elif state == "finishing":
            if self._deadline is None or self._elapsed(host) >= self._deadline:
                self._finish_car(car, host)

    def _finish_car(self, car: _Car, now: float) -> None:
        car.finished = True
        self._classify()
        self._emit("finish", {"slot": car.slot, "position": car.position})
        self._check_finished(now)

    def _check_finished(self, now: float) -> None:
        """`finishing` -> `finished`: everyone has finished, or the timeout has passed."""
        if self._racing_state() != "finishing":
            return
        timeout = self._elapsed(now) - self._finishing_elapsed >= self._race_cfg.finish_timeout_s
        waiting = [
            c
            for c in self._cars.values()
            if c.started and c.elim_index is None and self._is_enabled(c.slot) and not c.finished
        ]
        if timeout or not waiting:
            self._end_session(now)

    def _check_elimination(self, car: _Car, host: float) -> None:
        """The leader completes a multiple of `elim_every`: the last car is out."""
        if car.laps % self.settings.elim_every or car.laps <= self._last_elim_lap:
            return
        self._last_elim_lap = car.laps
        alive = [s for s in self._order if self._cars[s].elim_index is None]
        if len(alive) < 2:
            return
        loser = self._cars[alive[-1]]
        loser.elim_index = len(self._eliminated)
        self._eliminated.append(loser.slot)
        self._emit("elimination", {"slot": loser.slot, "lap": car.laps})
        self._push_ignore()
        if len(alive) == 2:
            winner = self._cars[alive[0]]
            winner.finished = True
            self._classify()
            self._emit("finish", {"slot": winner.slot, "position": winner.position})
            self._end_session(host)

    def _episode_lap(self, car: _Car, ms: int, neutralized: bool) -> None:
        if car.ep_restart:
            car.ep_restart = False
            return
        car.ep_laps.append(ms)
        if not neutralized:
            car.ep_valid.append(ms)
        if len(car.ep_laps) < self.settings.episode_laps:
            return
        valid = car.ep_valid
        self._emit(
            "episode_end",
            {
                "slot": car.slot,
                "episode": car.episode,
                "laps": len(car.ep_laps),
                "best_ms": min(valid) if valid else None,
                "mean_ms": round(sum(valid) / len(valid), 1) if valid else None,
                "laps_ms": list(car.ep_laps),
            },
        )
        car.episode += 1
        car.ep_laps, car.ep_valid = [], []

    # ----------------------------------------------------------------- standings

    def _classify(self) -> None:
        """Recompute `_order`, positions, gaps to the leader and to the car ahead."""
        started = [c for c in self._cars.values() if c.started]
        if self.settings.type in BEST_LAP_TYPES:
            ordered = sorted(started, key=lambda c: (c.best_ms is None, c.best_ms or 0, c.best_cu or 0, c.slot))
        else:
            over = self.state == "finished"  # at the end, finishers rank ahead of non-finishers on equal laps
            alive = sorted(
                (c for c in started if c.elim_index is None),
                key=lambda c: (-c.laps, over and not c.finished, c.cross[-1], c.slot),
            )
            out = sorted((c for c in started if c.elim_index is not None), key=lambda c: -(c.elim_index or 0))
            ordered = alive + out
        self._order = [c.slot for c in ordered]
        for i, car in enumerate(ordered):
            car.position = i + 1
            if i == 0:
                car.gap_ms, car.gap_laps = None, 0
                car.interval_ms, car.interval_laps = None, 0
            else:
                car.gap_ms, car.gap_laps = self._delta(car, ordered[0])
                car.interval_ms, car.interval_laps = self._delta(car, ordered[i - 1])

    def _delta(self, car: _Car, ref: _Car) -> tuple[int | None, int]:
        """Gap of `car` to `ref`: (ms, 0) on equal laps, otherwise (None, laps behind)."""
        if self.settings.type in BEST_LAP_TYPES:
            if car.best_ms is None or ref.best_ms is None:
                return None, 0
            return car.best_ms - ref.best_ms, 0
        behind = ref.laps - car.laps
        if behind == 0:
            # negative only if `car` stayed behind a finisher on equal laps (final standings)
            return max(0, car.cross[car.laps] - ref.cross[car.laps]), 0
        return None, max(0, behind)

    def _refresh(self) -> None:
        """Recompute then publish: `positions` event if the order changes, position tower."""
        self._classify()
        if self._order != self._published:
            self._published = list(self._order)
            self._emit("positions", {"order": list(self._order)})
        if self._tower is not None:
            self._tower.update(self._order, self._leader_laps())

    def _leader_laps(self) -> int:
        return max((c.laps for c in self._cars.values()), default=0)

    # ------------------------------------------------------------------ snapshot

    def _session_dict(self, now: float) -> dict[str, Any]:
        s = self.settings
        elapsed = self._elapsed(now)
        duration = self._duration()
        flag = {"running": "green", "paused": "red", "finishing": "checkered", "finished": "checkered"}
        return {
            "id": self._id,
            "type": s.type,
            "title": s.title or s.default_title(),
            "state": self.state,
            "laps": s.laps if s.type in LAP_TARGET_TYPES else None,
            "duration_s": duration,
            "elapsed_s": round(elapsed, 3),
            "remaining_s": None if duration is None else round(max(0.0, duration - elapsed), 3),
            "leader_lap": self._leader_laps(),
            "best": dict(self._best) if self._best else None,
            "flag": flag.get(self.state, "none"),
            "false_start": self._false_start,
        }

    def _car_dict(self, car: _Car, now: float) -> dict[str, Any]:
        driver = self._drivers.get(car.slot)
        if car.has_sectors:
            sectors = [car.lap_ms[-1] if car.lap_ms else None, *car.splits]
        else:
            sectors = [None, None, None]
        return {
            "slot": car.slot,
            "name": driver.name if driver else DEFAULT_NAMES[car.slot],
            "color": driver.color if driver else DEFAULT_COLORS[car.slot],
            "enabled": bool(driver and driver.enabled),
            "position": car.position,
            "laps": car.laps,
            "last_ms": car.lap_ms[-1] if car.lap_ms else None,
            "best_ms": car.best_ms,
            "best_lap": car.best_lap,
            "gap_ms": car.gap_ms,
            "gap_laps": car.gap_laps,
            "interval_ms": car.interval_ms,
            "interval_laps": car.interval_laps,
            "since_cross_ms": None if car.last_host is None else max(0, round((now - car.last_host) * 1000)),
            "sectors": sectors,
            "last_pb": car.last_pb,
            "last_sb": car.last_sb,
            "recent": car.lap_ms[-RECENT_LAPS:],
            "fuel": self._fuel[car.slot],
            "pit": self._pit[car.slot],
            "pits": self._pits[car.slot],
            "finished": car.finished,
            "eliminated": car.elim_index is not None,
            "episode": car.episode if self.settings.type == "training" else None,
            "episode_laps": len(car.ep_laps) if self.settings.type == "training" else None,
        }
