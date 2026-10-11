"""RL environment (Gymnasium API): an agent drives ONE slot of RaceLab.

`gymnasium` and `numpy` are optional. Without gymnasium, `RaceLabEnv` is a plain class
with the same methods (`reset`, `step`, `close`) and minimal spaces
(`Discrete`, `Box`); without numpy, observations are lists of floats.

Action. `Discrete(16)`: the throttle 0..15. With `allow_lane=True`, `Discrete(32)`:
`action = throttle + 16 * lane`, `lane=1` requests a lane change (one-shot).

Time step. `step()` sends the action then sleeps until the next control tick
(period `1 / control_hz`, 75 ms by default = one CU rail cycle) and reads the latest
state received (at most 50 ms old). If the agent is slower than the period, the tick is
re-synced to the present instant and `info["overrun_ms"]` gives the delay.

Observation (`OBS_FIELDS`, only what the real CU provides; clipped to [0, 3]):
  0 since_cross      time since the last line crossing / `ref_lap_ms` (0 before the 1st)
  1 last_lap         last lap of the episode / `ref_lap_ms` (0 if none)
  2 best_lap         best lap of the episode / `ref_lap_ms` (0 if none)
  3 throttle         last throttle sent / 15
  4 fuel             fuel / 15
  5 pit              1 if the car is in the pits
  6 laps             laps of the episode / `episode_laps`
  7 lane_pending     1 if a lane change is in progress
  8 gate             1 if the server forces the throttle to 0 (E-STOP, link, lights, dead-man…)
  9 deslot_suspected 1 if since_cross > 1.5 × `ref_lap_ms` (car probably derailed)
With `positions=True`, 5 more components are added, read from `state["positions"]`: provided
by the virtual CU, or on the real track by a track connector (camera…). With no
source, or if its positions are stale, they are 0 and `pos_ok` = 0: an agent that
uses them must be able to drive without (camera failure, track without a connector).
  10 pos_s    position in the lap (0..1)     11 pos_lane  lane (0/1)
  12 pos_v    speed / 4.2 m/s                13 pos_off   1 if off track
  14 pos_ok   1 if the positions are fresh

Default reward (`default_reward`, replaceable by `reward_fn(frame) -> float`):
  + `ref_lap_ms / lap_ms` for each lap completed during the step;
  + 0.01 × throttle/15 per step, unless a derailment is suspected or the throttle is forced;
  − 1 when a derailment is first suspected (once per incident).

Episode. It starts at `reset()`; the first line crossing after `reset()` starts the
timing (the corresponding lap, which includes the stop and the wait, is ignored) then
`episode_laps` timed laps are counted. `terminated` = those laps are done.
`truncated` = `max_steps` reached, session no longer running, or E-STOP. At the end of an
episode the throttle is reset to 0. The server's `episode_end` events are not used: the
server splits the session into consecutive episodes without knowing about `reset()`.
"""

from __future__ import annotations

import contextlib
import random
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from .client import DEFAULT_URL, Event, RaceLabError, SyncClient

try:
    import numpy as np
except ImportError:  # numpy is optional: observations as lists
    np = None
try:
    import gymnasium as gym
    from gymnasium import spaces
except ImportError:  # gymnasium is optional: plain class, minimal spaces
    gym = spaces = None

THROTTLE_LEVELS = 16
RUNNING = ("running", "finishing")  # session states where the bots drive
NO_SESSION = ("idle", "finished")  # only cases where the env creates its session; never overwrites
OBS_FIELDS = ("since_cross", "last_lap", "best_lap", "throttle", "fuel", "pit", "laps",
              "lane_pending", "gate", "deslot_suspected")
POS_OBS_FIELDS = ("pos_s", "pos_lane", "pos_v", "pos_off", "pos_ok")
OBS_MAX = 3.0  # bounds of the components: [0, OBS_MAX]
DESLOT_FACTOR = 1.5  # derailment suspected if since_cross > DESLOT_FACTOR × reference lap
SIM_VMAX = 4.2  # m/s, max speed of the simulator (normalization of sim_v)
SHAPING = 0.01
DESLOT_PENALTY = 1.0


@dataclass(frozen=True)
class Discrete:
    """Space {0..n-1} (minimal equivalent of `gymnasium.spaces.Discrete`)."""

    n: int

    def sample(self) -> int:
        return random.randrange(self.n)


@dataclass(frozen=True)
class Box:
    """Description of a bounded continuous space (minimal equivalent of `gymnasium.spaces.Box`)."""

    low: float
    high: float
    shape: tuple[int, ...]


@dataclass(frozen=True)
class Frame:
    """What the environment knows at the end of a step: input of `obs_fn` and `reward_fn`."""

    state: dict[str, Any]  # last full state (see docs/api.md)
    car: dict[str, Any]  # state["cars"][slot]
    bot: dict[str, Any]  # state["bots"][slot]
    pos: dict[str, Any] | None  # car position (virtual CU or connector), if positions=True
    throttle: int  # throttle sent at this step
    lap_ms: tuple[int, ...]  # timed laps of the episode
    new_lap_ms: tuple[int, ...]  # of which those completed during this step
    since_cross_s: float  # time since the last line crossing (0 if none)
    suspected: bool  # derailment suspected
    new_incident: bool  # the suspicion has just appeared (once per incident)
    ref_lap_ms: float
    episode_laps: int

    @property
    def laps(self) -> int:
        return len(self.lap_ms)

    @property
    def gate(self) -> str | None:
        """Reason why the server forces the throttle to 0, or None."""
        return self.bot.get("gate")


def default_obs(frame: Frame, positions: bool = False) -> list[float]:
    ref = frame.ref_lap_ms
    values = [
        frame.since_cross_s * 1000 / ref,
        frame.lap_ms[-1] / ref if frame.lap_ms else 0.0,
        min(frame.lap_ms) / ref if frame.lap_ms else 0.0,
        frame.throttle / (THROTTLE_LEVELS - 1),
        (frame.car.get("fuel") or 0) / (THROTTLE_LEVELS - 1),
        float(bool(frame.car.get("pit"))),
        frame.laps / frame.episode_laps,
        float(bool(frame.bot.get("lane_pending"))),
        float(frame.gate is not None),
        float(frame.suspected),
    ]
    if positions:
        pos = frame.pos or {}
        values += [pos.get("s", 0.0), pos.get("lane", 0), (pos.get("v") or 0.0) / SIM_VMAX,
                   float(bool(pos.get("off"))), float(frame.pos is not None)]
    return [min(max(float(v), 0.0), OBS_MAX) for v in values]


def default_reward(frame: Frame) -> float:
    reward = sum(frame.ref_lap_ms / ms for ms in frame.new_lap_ms)
    if frame.new_incident:
        reward -= DESLOT_PENALTY
    if not frame.suspected and frame.gate is None:
        reward += SHAPING * frame.throttle / (THROTTLE_LEVELS - 1)
    return reward


_Base = gym.Env if gym is not None else object


class RaceLabEnv(_Base):
    """One agent, one slot. See the module docstring for the action, observation,
    reward and end of episode.

    Args:
        url, token, agent: connection to the server (`RaceLabClient`).
        slot: driven car (0..5).
        control_hz: control rate (13.3 Hz ≈ 75 ms, one CU rail cycle).
        episode_laps: timed laps per episode.
        use_training_session: if true, `reset()` configures and starts a `training`
            session when no session is active; an active session is respected.
            If false, `reset()` waits for a session to be running (started by a human).
        allow_lane: adds the lane change to the actions (`Discrete(32)`).
        reward_fn: `Frame -> float`, replaces `default_reward`.
        obs_fn: `Frame -> Sequence[float]`, replaces `default_obs`; `obs_size` is then
            required (size of the vector).
        positions: adds the car's position to the observation (virtual CU or
            track connector; see module);
            `reset()` fails if the server does not send `sim` (real circuit).
        ref_lap_ms: reference lap (ms), normalization and reward constant:
            an order of magnitude of a good lap on THIS track is enough (6 s for the simulator).
        max_steps: maximum steps per episode (otherwise `truncated`).
        settle_s: throttle 0 for this delay at `reset()` (except the first) so the car
            stops before the next episode.
        reset_timeout_s: maximum delay of `reset()` (waiting for the link, the session, the E-STOP).

    The throttle is set to 0 at the end of each episode; if the agent leaves more than
    `deadman_ms` (600 ms) between two `step()` calls during an episode (e.g. a network
    update), the server cuts the throttle: do the updates between episodes.
    """

    metadata: dict[str, Any] = {"render_modes": []}

    def __init__(self, url: str = DEFAULT_URL, slot: int = 0, *, agent: str = "rl-agent",
                 token: str | None = None, control_hz: float = 13.3, episode_laps: int = 5,
                 use_training_session: bool = True, allow_lane: bool = False,
                 reward_fn: Callable[[Frame], float] = default_reward,
                 obs_fn: Callable[[Frame], Sequence[float]] | None = None,
                 obs_size: int | None = None, positions: bool = False,
                 ref_lap_ms: float = 6000.0, max_steps: int = 2000, settle_s: float = 1.0,
                 reset_timeout_s: float = 30.0) -> None:
        if obs_fn is not None and obs_size is None:
            raise ValueError("obs_size is required with obs_fn")
        self.url, self.slot, self.agent, self.token = url, slot, agent, token
        self.episode_laps = episode_laps
        self.use_training_session = use_training_session
        self.reward_fn = reward_fn
        self.obs_fn = obs_fn or partial(default_obs, positions=positions)
        self.positions = positions
        self.ref_lap_ms = ref_lap_ms
        self.max_steps = max_steps
        self.settle_s = settle_s
        self.reset_timeout_s = reset_timeout_s
        self.control_period = 1.0 / control_hz
        self.client: SyncClient | None = None  # opened at the first reset()

        n_actions = THROTTLE_LEVELS * (2 if allow_lane else 1)
        size = obs_size if obs_fn is not None else len(OBS_FIELDS) + (len(POS_OBS_FIELDS) if positions else 0)
        low, high = (-float("inf"), float("inf")) if obs_fn is not None else (0.0, OBS_MAX)
        if gym is not None:
            self.action_space = spaces.Discrete(n_actions)
            self.observation_space = spaces.Box(low, high, shape=(size,), dtype=np.float32)
        else:
            self.action_space = Discrete(n_actions)
            self.observation_space = Box(low, high, (size,))

        self._n_actions = n_actions
        self._events: deque[Event] = deque()  # filled by the client thread, drained by step()
        self._first = True
        self._active = False
        self._lap_ms: list[int] = []
        self._skip_lap = False
        self._suspected = False
        self._throttle = 0
        self._steps = 0
        self._tick = 0.0

    # ------------------------------------------------------------ Gymnasium API

    def reset(self, *, seed: int | None = None,
              options: dict[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
        """Claim the slot, throttle 0, wait until the car is ready (stopped, session running,
        no E-STOP or lost link). `seed` is ignored without gymnasium."""
        if gym is not None:
            super().reset(seed=seed)
        client = self._connect()
        session = client.state.get("session", {})
        if self.use_training_session and session.get("state") in NO_SESSION:
            client.setup(type="training", episode_laps=self.episode_laps)
            client.start()
        elif session.get("state") == "grid" and session.get("type") == "training":
            client.start()  # training session prepared in the UI: start it
        client.claim(self.slot, free=False)
        self._hold_until_ready(client, 0.0 if self._first else self.settle_s)
        self._first = False
        self._events.clear()
        self._lap_ms = []
        # the lap in progress at reset includes the stop and the wait: unless the car has
        # never crossed the line in this session, its 1st lap is to be ignored
        self._skip_lap = self._car(client.state).get("since_cross_ms") is not None
        self._throttle = self._steps = 0
        self._active = True
        self._tick = time.monotonic()
        frame = self._frame(client.state, ())
        return self._obs(frame), self._info(frame)

    def step(self, action: Any) -> tuple[Any, float, bool, bool, dict[str, Any]]:
        if not self._active or self.client is None:
            raise RuntimeError("call reset() before step() (and after the end of an episode)")
        throttle, lane = self._decode(action)
        self._throttle = throttle
        self.client.act(self.slot, throttle, lane)
        overrun_ms = self._wait_tick()
        new_laps, link_reset = self._collect_events()
        self._steps += 1
        state = self.client.state
        frame = self._frame(state, new_laps)
        reward = float(self.reward_fn(frame))
        terminated = frame.laps >= self.episode_laps
        truncated = not terminated and (self._steps >= self.max_steps or bool(state.get("estop"))
                                        or state.get("session", {}).get("state") not in RUNNING)
        if terminated or truncated:
            self._active = False
            self.client.act(self.slot, 0)
        info = self._info(frame)
        info.update(step=self._steps, overrun_ms=overrun_ms, bots_reset=link_reset)
        return self._obs(frame), reward, terminated, truncated, info

    def close(self) -> None:
        """Throttle 0, hand the slot back to the physical controller, close the connection. Idempotent."""
        client, self.client, self._active = self.client, None, False
        if client is None:
            return
        try:
            with contextlib.suppress(RaceLabError, OSError):
                client.act(self.slot, 0)
                client.unclaim(self.slot)
        finally:
            client.close()

    def __enter__(self) -> RaceLabEnv:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ internals

    def _connect(self) -> SyncClient:
        if self.client is None or self.client.closed:
            if self.client is not None:
                self.client.close()
            self.client = SyncClient.connect(self.url, self.agent, 20.0, self.token)
            self.client.on_event("lap", self._events.append)
            self.client.on_event("bots_reset", self._events.append)
        return self.client

    def _car(self, state: dict[str, Any]) -> dict[str, Any]:
        return next((c for c in state.get("cars", ()) if c["slot"] == self.slot), {})

    def _bot(self, state: dict[str, Any]) -> dict[str, Any]:
        return next((b for b in state.get("bots", ()) if b["slot"] == self.slot), {})

    def _ready(self, state: dict[str, Any]) -> bool:
        return (state.get("session", {}).get("state") in RUNNING and not state.get("estop")
                and self._bot(state).get("gate") is None)

    def _hold_until_ready(self, client: SyncClient, settle_s: float) -> None:
        """Throttle 0 (resent every tick: the dead-man switch stays fresh) until `settle_s`
        has elapsed and the slot is no longer blocked."""
        start = time.monotonic()
        while True:
            client.act(self.slot, 0)
            elapsed = time.monotonic() - start
            if elapsed >= settle_s and self._ready(client.state):
                return
            if elapsed > self.reset_timeout_s:
                state = client.state
                raise TimeoutError(
                    f"reset: slot {self.slot} still blocked after {self.reset_timeout_s:g} s "
                    f"(session {state.get('session', {}).get('state')}, "
                    f"gate {self._bot(state).get('gate')}, estop {state.get('estop')})")
            time.sleep(self.control_period)

    def _decode(self, action: Any) -> tuple[int, bool]:
        a = int(action)
        if not 0 <= a < self._n_actions:
            raise ValueError(f"action {a} out of [0, {self._n_actions})")
        return a % THROTTLE_LEVELS, a >= THROTTLE_LEVELS

    def _wait_tick(self) -> float:
        """Sleep until the next tick; return the delay (ms) if the agent exceeded the period."""
        target = self._tick + self.control_period
        now = time.monotonic()
        if now < target:
            time.sleep(target - now)
            self._tick = target
            return 0.0
        self._tick = now
        return (now - target) * 1000

    def _collect_events(self) -> tuple[list[int], bool]:
        """Laps of our slot completed since the previous step, and whether `bots_reset` was seen."""
        new_laps, link_reset = [], False
        while self._events:
            event = self._events.popleft()
            if event.name == "bots_reset":
                link_reset = True
            elif event.data.get("slot") == self.slot:
                if self._skip_lap:
                    self._skip_lap = False
                elif not event.data.get("neutralized"):
                    new_laps.append(event.data["ms"])
        self._lap_ms += new_laps
        return new_laps, link_reset

    def _frame(self, state: dict[str, Any], new_laps: Sequence[int]) -> Frame:
        """Build the step's `Frame` and update the derailment tracking."""
        car, bot = self._car(state), self._bot(state)
        since = car.get("since_cross_ms")
        suspected = since is not None and since > DESLOT_FACTOR * self.ref_lap_ms
        # no penalty if the throttle is forced to 0 (E-STOP, link, dead-man…): not the agent's fault
        new_incident = suspected and not self._suspected and bot.get("gate") is None
        self._suspected = suspected
        pos = None
        feed = state.get("positions")
        if self.positions and feed and not feed.get("stale"):
            pos = next((c for c in feed.get("cars", ()) if c["slot"] == self.slot), None)
        return Frame(state=state, car=car, bot=bot, pos=pos, throttle=self._throttle,
                     lap_ms=tuple(self._lap_ms), new_lap_ms=tuple(new_laps),
                     since_cross_s=(since or 0) / 1000, suspected=suspected,
                     new_incident=new_incident, ref_lap_ms=self.ref_lap_ms,
                     episode_laps=self.episode_laps)

    def _obs(self, frame: Frame) -> Any:
        values = [float(v) for v in self.obs_fn(frame)]
        return np.asarray(values, dtype=np.float32) if np is not None else values

    def _info(self, frame: Frame) -> dict[str, Any]:
        state = frame.state
        return {
            "laps": frame.laps,
            "lap_ms": list(frame.lap_ms),
            "gate": frame.gate,
            "link": state.get("link", {}).get("state"),
            "session_state": state.get("session", {}).get("state"),
            "estop": bool(state.get("estop")),
            "deslot_suspected": frame.suspected,
        }
