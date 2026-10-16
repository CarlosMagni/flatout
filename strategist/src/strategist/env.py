"""Headless race environment (Strategy v2): RaceLab's virtual CU and physics, the real race
brain on slot 0, simulated humans on the other slots. Faster than real time, no server, no
wall clock: `step(action)` runs one decision interval (grid to lap, then lap to lap).

The car: a `brain` profile with `lane.policy` keep and `pit.policy` off — the strategist
decides, the brain still times and executes the requests (its own safety gates stay first).
Action = 12 discrete: lane (stay / change at the next changer) x pit (no / yes at the next
entry) x risk (0 / 0.5 / 1, the MPC pilot's, applied in place through the `bot.params` path).

Reward: a small pace shaping on each clean lap, then the race result — position and laps
completed, minus running dry and off-tracks. `info()` carries the numbers an evaluation
reports. Same observation contract as live (observation.py); see README.md.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
from carreralib import ControlUnit

from racelab.bots.manager import BotManager
from racelab.config import SafetyConfig, SimConfig
from racelab.events import LinkState
from racelab.sim import Simulator

from .observation import REF_LAP_MS, ObsState

STEP = 0.05  # s, one CU rail-cycle tick (20 Hz)
RISK_LEVELS = (0.0, 0.5, 1.0)
N_ACTIONS = 12  # lane 2 x pit 2 x risk 3

DEFAULT_PROFILE: dict[str, Any] = {
    "kind": "brain", "pilot": "mpc", "pilot_params": {},
    "lane": {"policy": "keep"}, "pit": {"policy": "off", "min_laps": 5},
}


def action_parts(action: int) -> tuple[int, int, float]:
    """(lane: 0 stay / 1 change, pit: 0 no / 1 yes, risk)."""
    a = int(action) % N_ACTIONS
    return a // 6, (a % 6) // 3, RISK_LEVELS[a % 3]


def default_action() -> int:
    """Stay in lane, no pit, risk 0.5."""
    return 1


class SimLink:
    """The manager's link, straight into the virtual CU's bot firmware (B commands).

    Mirrors tests/test_brain.py::SimLink (the tests' own harness): a lane request reaches
    the car `delay` ticks later, like the real link and Bluetooth; it also exercises the
    same gate path as the app (throttle 0 when not connected, ...) — no shortcuts."""

    def __init__(self, cu: ControlUnit, delay: int = 1) -> None:
        self.cu, self.delay = cu, delay
        self.queue: list[list[int]] = []
        self.estopped = False

    def set_bot(self, slot: int, value: int | None) -> None:
        if value is None:
            self.cu.botrelease(slot)
        else:
            self.cu.setbot(slot, value)

    def lane(self, slot: int) -> None:
        self.queue.append([self.delay, slot])

    def flush(self) -> None:
        for item in list(self.queue):
            item[0] -= 1
            if item[0] < 0:
                self.cu.botlane(item[1])
                self.queue.remove(item)

    def estop(self) -> None:
        self.estopped = True

    def clear_estop(self) -> None:
        self.estopped = False

    def resume_bots(self) -> None:
        pass


class RaceEnv:
    """One race; slot 0 is the strategist's car. `seed` fixes the whole race (track physics,
    humans, marshals): same seed + same actions = same race."""

    def __init__(self, profile: dict[str, Any] | None = None, *, seed: int = 0, laps: int = 12,
                 opponents: int = 2, fuel_mode: bool = True, pit_lane: bool = True,
                 ref_lap_ms: float = REF_LAP_MS, guard_s: float | None = None, slot: int = 0,
                 track_seed: int | None = None, track_per_seed: bool = False) -> None:
        self.profile_data = profile or DEFAULT_PROFILE
        self.seed, self.laps_target, self.opponents = seed, int(laps), int(opponents)
        self.fuel_mode, self.pit_lane = fuel_mode, pit_lane
        self.ref_lap_ms, self.slot = ref_lap_ms, slot
        self.track_seed, self.track_per_seed = track_seed, track_per_seed
        # a slow lap with traffic and a pit stop stays well under 9 s: never truncate a real race
        self.guard_s = guard_s if guard_s is not None else self.laps_target * 9.0 + 30.0
        self._build(seed)

    # ------------------------------------------------------------------ build / reset

    def _build(self, seed: int) -> None:
        self.sim = Simulator(SimConfig(seed=seed, humans=list(range(1, 1 + self.opponents)),
                                       latency_ms=0.0, jitter_ms=0.0, duplicate_rate=0.0,
                                       fuel_mode=self.fuel_mode, pit_lane=self.pit_lane,
                                       track_seed=seed if self.track_per_seed else self.track_seed),
                             realtime=False)
        self.sim.bot_watchdog_s = None  # the manager feeds every tick; no firmware watchdog here
        self.cu = ControlUnit(self.sim.connect(0.01))
        self.link = SimLink(self.cu)
        self.bots = BotManager(self.link, SafetyConfig(deadman_ms=500, max_throttle=15),
                               clock=lambda: self.sim.t)
        self.bots.on_link(LinkState("connected", "sim", bots_supported=True))
        self.bots.set_profile(self.slot, self.profile_data)
        self.bots.take(self.slot, "profile")
        self.profile = self.bots.slots[self.slot].profile
        self.car = self.sim.cars[self.slot]
        self.car.tank, self.car.fuel = 15, 15.0
        self.geo = self.sim.track_geometry()
        self.obs = ObsState(ref_lap_ms=self.ref_lap_ms, pit_min_laps=int(self.profile.pit["min_laps"]))
        self.obs.set_slot(self.slot)
        self.supports_risk = "risk" in getattr(self.profile, "pilot_params", {})
        self.completed = 0  # race laps of our car (the grid-to-line crossing does not count)
        self._crossings, self._last_cross = self.car.laps, None
        self._reward, self._lap_done = 0.0, False
        self._dry, self._offs, self._was_off = False, 0, False
        self.requests: list[dict] = []
        self._t0 = self.sim.t

    def reset(self, seed: int | None = None) -> np.ndarray:
        self._build(self.seed if seed is None else seed)
        return self.observe()

    # ------------------------------------------------------------------ the loop

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict]:
        """Apply the decision, then run to the next lap (or the end)."""
        self._apply(action)
        self._reward, self._lap_done = 0.0, False
        terminated = truncated = False
        while True:
            self._tick_once()
            if self.completed >= self.laps_target:
                terminated = True
                break
            if self._lap_done:
                break
            if self.sim.t - self._t0 > self.guard_s:
                truncated = True
                break
        self._reward += self._terminal_reward()
        return self.observe(), float(self._reward), terminated, truncated, self.info()

    def run_episode(self, choose: Callable[[np.ndarray, "RaceEnv"], int] | None = None) -> dict:
        """Play one race to the end: `choose(obs, env)` every decision, default action else.
        Returns the final `info` (plus `reward`)."""
        obs, total = self.observe(), 0.0
        while True:
            action = choose(obs, self) if choose is not None else default_action()
            obs, reward, terminated, truncated, info = self.step(action)
            total += reward
            if terminated or truncated:
                return {**info, "reward": round(total, 4), "truncated": truncated}

    # ------------------------------------------------------------------ internals

    def _apply(self, action: int) -> None:
        lane, pit, risk = action_parts(action)
        if self.supports_risk:
            try:
                self.bots.set_params(self.slot, {"risk": risk})
            except ValueError:  # a pilot without a live risk: stop trying
                self.supports_risk = False
        if lane:
            self.bots.brain_request(self.slot, "lane")
            self.obs.lane_requests += 1
        if pit and self._pit_allowed():
            self.bots.brain_request(self.slot, "pit")
            self.obs.pit_requests += 1

    def _pit_allowed(self) -> bool:
        """The strategist's own spacing rule (the spec: never two pit requests within
        `pit.min_laps`), on top of the brain's arbiter."""
        return (self.sim.cfg.fuel_mode and not self.car.in_pit
                and (self.obs.pit_lap is None or self.completed - self.obs.pit_lap >= self.obs.pit_min_laps))

    def _tick_once(self) -> None:
        engine, positions, state = self._assemble()
        asked = len(self.link.queue)
        self.bots.tick(engine, positions, self.geo)
        if len(self.link.queue) > asked:
            self.requests.append({"t": round(self.sim.t, 3), "lap": self.completed, "kind": "lane"})
        self.link.flush()
        self.sim.step(STEP)
        if self.car.laps > self._crossings:  # line crossing
            self._crossings = self.car.laps
            if self._last_cross is not None:
                self.completed += 1
                ms = round((self.sim.t - self._last_cross) * 1000)
                self.bots.on_engine_event("lap", {"slot": self.slot, "lap": self.completed,
                                                  "ms": ms, "neutralized": False})
                if not self.obs.off_seen and not self.obs.pit_seen:
                    self._reward += 0.1 * min(max(self.ref_lap_ms / ms, 0.5), 1.5)
                self._lap_done = True
            self._last_cross = self.sim.t
        if self.fuel_mode and self.car.fuel <= 0 and not self._dry:
            self._dry = True
            self._reward -= 2.0
        if self.car.off and not self._was_off and self._offs < 3:
            self._offs += 1
            self._reward -= 0.15
        self._was_off = self.car.off
        self.obs.note(state, self.sim.t)

    def _terminal_reward(self) -> float:
        n = max(self._slots_count(), 2)
        return (n - self.position) / (n - 1) + 0.2 * min(self.completed / self.laps_target, 1.0)

    def _slots_count(self) -> int:
        return 1 + self.opponents

    def _participants(self) -> list[int]:
        return [self.slot] + list(range(1, 1 + self.opponents))

    def _assemble(self) -> tuple[dict, dict, dict]:
        participants = self._participants()
        snap = self.sim.snapshot()
        cars = []
        for slot in participants:
            c = snap["cars"][slot]
            raw = self.sim.cars[slot].laps
            laps = self.completed if slot == self.slot else max(0, raw - 1)
            cars.append({"slot": slot, "laps": laps,
                         "fuel": min(15, int(self.sim.cars[slot].fuel)), "pit": c["pit"]})
        engine = {"session": {"state": "running", "laps": self.laps_target, "remaining_s": None},
                  "cu": {"phase": 0, "fuel_mode": self.sim.cfg.fuel_mode,
                         "pit_lane": self.sim.cfg.pit_lane},
                  "cars": cars}
        positions = {"source": "sim", "age_ms": 0, "stale": False,
                     "cars": [{k: c[k] for k in ("slot", "s", "lane", "v", "off")}
                              for c in snap["cars"] if c["slot"] in participants]}
        state = {"session": engine["session"], "cu": engine["cu"], "cars": cars,
                 "positions": positions, "bots": self.bots.snapshot()}
        return engine, positions, state

    # ------------------------------------------------------------------ reading

    def observe(self) -> np.ndarray:
        _engine, _positions, state = self._assemble()
        return self.obs.build(state, self.geo)

    @property
    def position(self) -> int:
        rows = []
        for slot in self._participants():
            raw = self.sim.cars[slot].laps
            laps = self.completed if slot == self.slot else max(0, raw - 1)
            rows.append((laps, self.sim.cars[slot].s, slot))
        rows.sort(reverse=True)
        return next(i for i, (_l, _s, slot) in enumerate(rows) if slot == self.slot) + 1

    def info(self) -> dict:
        return {"position": self.position, "laps": self.completed,
                "race_ms": round((self.sim.t - self._t0) * 1000),
                "dry": self._dry, "offs": self._offs, "stops": self.obs.stops,
                "pit_requests": self.obs.pit_requests, "lane_requests": self.obs.lane_requests,
                "risk": getattr(getattr(self.profile, "_model", None), "risk", None),
                "requests": list(self.requests)}
