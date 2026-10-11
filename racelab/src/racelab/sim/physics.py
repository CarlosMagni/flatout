"""Simulated cars (dynamics, breakaway, fuel, pit lane) and "human" drivers."""

from __future__ import annotations

import random

from .track import LAP_LENGTH, Lane, Track

VMAX = 4.2  # m/s at full speed (speed setting 15)
SPEED_MIN_SCALE = 0.55  # programmed max speed: 55 % (setting 0) .. 100 % (setting 15)
TAU_ACC = 0.45  # s, acceleration time constant
TAU_BRAKE = 0.30  # s, lifting off / braking (brake 15; brake 0 = 2× softer)
TAU_HOLD = 0.08  # s, car held by the CU (lights, pause) or slowed at the pit entry
PACE_SCALE = 0.40  # speed ceiling in pace car mode

SLIP_LIMIT = 0.08  # m, accumulated slip before leaving the track
SLIP_DECAY = 0.10  # m/s, slip recovers when the car is under the critical speed
MARSHAL_S = (2.0, 4.0)  # s, time before being put back on track

FUEL_MAX = 15.0
FUEL_LAPS = 20  # a full tank = 20 laps at race pace
RACE_PACE = 0.75  # average throttle (fraction) at race pace
FUEL_PER_M = FUEL_MAX / (FUEL_LAPS * LAP_LENGTH * RACE_PACE)  # per meter, at full throttle
EMPTY_SCALE = 0.35  # max speed without fuel

PIT_SPEED = 0.9  # m/s, pit lane speed limit
PIT_STOP_S = 2.5  # s, stop in the middle of the zone (refueling)
PIT_FUEL = 3  # a human pits below this level

GRID_GAP = 0.25  # m between two cars on the grid (behind the line)

# Human drivers
JUMP_START_PROB = 0.002  # probability of a false start, per human and per start
MISTAKE_RATE = 0.0005  # probability of a mistake per decision (20 Hz) when a corner approaches
LANE_CHANGE_PROB = 0.2  # probability of changing lane at each changer
SKILL = (0.88, 0.97)  # share of the critical speed aimed for in corners
REACTION_S = (0.15, 0.45)  # reaction delay to the lights
CONTROL_DT = 0.05  # s, decision period


class Car:
    """A car on the track. `s` = arc length (m) along its lane."""

    def __init__(self, slot: int, track: Track, rng: random.Random, *,
                 fuel_mode: bool, pit_lane: bool) -> None:
        self.slot = slot
        self.track = track
        self.rng = rng
        self.fuel_mode = fuel_mode
        self.pit_lane = pit_lane
        self.lane = slot % 2
        self.s = track.lanes[self.lane].length - 0.2 - GRID_GAP * slot  # grid behind the line
        self.v = 0.0
        self.throttle = 0  # throttle actually applied
        self.laps = 0  # line crossings
        self.armed = False  # lane change requested
        self.off = False  # off track
        self.off_left = 0.0
        self.slip = 0.0
        self.fuel = FUEL_MAX
        self.in_pit = False
        self.pit_stopped = False  # the stop of this visit is done
        self.stop_left = 0.0
        self.speed = self.brake = self.tank = 15  # J settings (words 0, 1, 2)

    def vmax(self) -> float:
        v = VMAX * (SPEED_MIN_SCALE + (1 - SPEED_MIN_SCALE) * self.speed / 15)
        return v * EMPTY_SCALE if self.fuel_mode and self.fuel <= 0 else v

    def step(self, dt: float, throttle: int, *, hold: bool = False, pace: bool = False) -> float | None:
        """Advance by `dt` s with throttle `throttle` (0..15).

        Returns the time (s) elapsed since the finish line was crossed if the
        car crossed it during this step, else None.
        """
        self.throttle = throttle
        if self.off:
            self.off_left -= dt
            self.off = self.off_left > 0
            return None
        if self.stop_left > 0:  # pit stop: refueling
            self.stop_left = max(0.0, self.stop_left - dt)
            self.fuel = FUEL_MAX if self.stop_left == 0 else min(FUEL_MAX, self.fuel + FUEL_MAX / PIT_STOP_S * dt)
            return None
        if self.v == 0.0 and throttle == 0:
            return None

        # first-order dynamics towards the target speed
        vmax = self.vmax() * (PACE_SCALE if pace else 1.0)
        target = vmax * throttle / 15
        if self.in_pit:
            target = min(target, PIT_SPEED)
        if hold or (self.in_pit and self.v > PIT_SPEED):
            tau = TAU_HOLD
        elif target >= self.v:
            tau = TAU_ACC
        else:
            tau = TAU_BRAKE / (0.5 + 0.5 * self.brake / 15)
        self.v += (target - self.v) * min(1.0, dt / tau)
        if self.v < 1e-3 and target == 0:
            self.v = 0.0
            return None

        ds = self.v * dt
        if self.fuel_mode:
            self.fuel = max(0.0, self.fuel - FUEL_PER_M * throttle / 15 * ds * (2 - self.tank / 15))
        track = self.track
        length = track.lanes[self.lane].length
        s_old = self.s
        self.s += ds

        crossed = None
        if self.s >= length:  # finish line
            self.s -= length
            s_old = 0.0
            crossed = self.s / self.v
            self.laps += 1
        self._events(s_old)
        if self.in_pit:
            return crossed

        # critical speed: breakaway if exceeded for too long
        vc = track.lanes[self.lane].vc[track.locate(self.lane, self.s)]
        if self.v > vc:
            self.slip += (self.v - vc) * dt
            if self.slip > SLIP_LIMIT:
                self.off, self.off_left = True, self.rng.uniform(*MARSHAL_S)
                self.v = self.slip = 0.0
        else:
            self.slip = max(0.0, self.slip - SLIP_DECAY * dt)
        return crossed

    def _events(self, s_old: float) -> None:
        """Lane changers, pit lane entry/stop/exit between `s_old` and `self.s`."""
        lane = self.track.lanes[self.lane]
        s = self.s
        if self.in_pit:
            if not self.pit_stopped and s_old < lane.pit_stop <= s:
                self.s, self.v = lane.pit_stop, 0.0
                self.stop_left, self.pit_stopped = PIT_STOP_S, True
            elif s_old < lane.pit_out <= s:
                self.in_pit = self.pit_stopped = False
        elif self.armed:
            if self.pit_lane and s_old < lane.pit_in <= s:
                self.in_pit, self.armed = True, False
            elif any(s_old < c <= s for c in lane.changer_s):
                u = self.track.fraction(self.lane, s)
                self.lane = 1 - self.lane
                self.s = self.track.arc(self.lane, u)
                self.armed = False


class Human:
    """Simulated driver: anticipates corners, obeys the lights, changes lane, pits."""

    def __init__(self, car: Car, rng: random.Random) -> None:
        self.car = car
        self.rng = rng
        self.skill = rng.uniform(*SKILL)
        self.wait = 0.0  # reaction at the start
        self.jump_left: float | None = None  # scheduled false start
        self.mistake_left = 0.0
        self._acc = 0.0
        self._last = 0
        self._rolled: tuple[int, int] | None = None  # last changer rolled for

    def countdown(self) -> None:
        """Countdown start: randomly decide on a possible false start."""
        self.jump_left = self.rng.uniform(0.3, 4.0) if self.rng.random() < JUMP_START_PROB else None

    def go(self) -> None:
        """Lights out: reaction time before moving off."""
        self.jump_left = None
        self.wait = self.rng.uniform(*REACTION_S)
        self._last = 0

    def throttle(self, dt: float, phase: int) -> int:
        if 2 <= phase <= 6:
            if self.jump_left is not None:
                self.jump_left -= dt
                if self.jump_left <= 0:
                    return 8
            return 0
        if phase not in (0, 7):
            return 0
        if self.wait > 0:
            self.wait -= dt
            return 0
        self._acc += dt
        if self._acc >= CONTROL_DT:
            self._acc -= CONTROL_DT
            self._last = self._decide()
        return self._last

    def _decide(self) -> int:
        car, rng = self.car, self.rng
        track = car.track
        lane = track.lanes[car.lane]
        vmax = car.vmax()

        # minimum critical speed over ~0.5 s of road ahead
        i = track.locate(car.lane, car.s)
        count = max(1, int((0.5 * car.v + 0.25) * track.n / lane.length))
        window = lane.vc[i:i + count] + lane.vc[:max(0, i + count - track.n)]
        vc_ahead = min(window)
        target = min(vmax, self.skill * vc_ahead)

        throttle = 15 * target / vmax + rng.gauss(0, 0.35)
        if vc_ahead < 0.8 * vmax and self.mistake_left <= 0 and rng.random() < MISTAKE_RATE:
            self.mistake_left = rng.uniform(0.4, 1.0)  # missed braking
        if self.mistake_left > 0:
            self.mistake_left -= CONTROL_DT
            throttle += 5

        self._plan_lane(car, lane)
        return max(0, min(15, round(throttle)))

    def _plan_lane(self, car: Car, lane: Lane) -> None:
        """Set the lane change request: pits if the tank is low, otherwise random."""
        if car.in_pit or car.armed:
            return
        if car.pit_lane and car.fuel < PIT_FUEL and 0 < lane.pit_in - car.s < 1.5:
            car.armed = True
            return
        for k, changer in enumerate(lane.changer_s):
            if 0 < changer - car.s < 1.0 and self._rolled != (car.laps, k):
                self._rolled = (car.laps, k)
                car.armed = self.rng.random() < LANE_CHANGE_PROB
