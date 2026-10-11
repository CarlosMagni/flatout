"""Local autopilots (bot profiles).

The CU gives no continuous position: only finish-line crossings (and Check
Lanes when present). A profile therefore drives "blind" between two passes,
estimating the fraction of the lap covered from the time elapsed since the
last pass and a reference lap time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Context:
    now: float
    since_cross_s: float | None  # since the last pass over the line
    laps: int
    rival_lap_ms: float | None = None  # rival reference ("rival" profile)


@dataclass
class Profile:
    """Base: gradual start + periodic lane change."""

    kind = "base"
    ramp_s: float = 1.2  # throttle ramp-up at the start (avoids wheelspin / leaving the track)
    lane_every: int = 0  # change lane every N laps (0 = never)

    def __post_init__(self) -> None:
        self._launch: float | None = None
        self._lane_due = False

    def reset(self) -> None:
        """New session: forget everything that was learned."""
        self.relaunch()
        self._lane_due = False

    def relaunch(self) -> None:
        """Next non-zero throttle = new start (ramp)."""
        self._launch = None

    def on_lap(self, laps: int, ms: int, neutralized: bool) -> None:
        if self.lane_every and laps % self.lane_every == 0:
            self._lane_due = True

    def take_lane_request(self) -> bool:
        due, self._lane_due = self._lane_due, False
        return due

    def throttle(self, ctx: Context) -> float:
        raise NotImplementedError

    def output(self, ctx: Context) -> int:
        if self._launch is None:
            self._launch = ctx.now
        value = self.throttle(ctx)
        if self.ramp_s > 0:
            value *= min(1.0, (ctx.now - self._launch) / self.ramp_s)
        return max(0, min(15, int(value + 0.5)))  # round to nearest, .5 rounds up

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **asdict(self)}


@dataclass
class ConstantProfile(Profile):
    """Fixed throttle: the simplest, handy for tuning a car or an RL test."""

    kind = "constant"
    value: int = 8

    def throttle(self, ctx: Context) -> float:
        return self.value


@dataclass
class TargetLapProfile(Profile):
    """Target pace: adjusts the throttle after each lap to hold `target_ms`.

    Bounded integral correction (±`max_step` per lap). A lap longer than
    `crash_ratio` × the best of the last clean laps is read as a crash (the
    putting the car back on track costs 2 to 4 s): the bot lifts and keeps a throttle
    ceiling just under the one that caused the crash. After `recover_laps`
    clean laps in a row, the ceiling goes back up by half a notch: the bot
    thus learns the maximum throttle the track can take, even when the
    target is out of reach.
    """

    kind = "target"
    target_ms: int = 6000
    start: float = 8.0
    min_value: float = 3.0
    max_value: float = 12.0
    gain: float = 8.0  # throttle notches per 100 % deviation
    max_step: float = 1.5
    crash_ratio: float = 1.25
    recover_laps: int = 5

    def __post_init__(self) -> None:
        super().__post_init__()
        self._current_target = float(self.target_ms)
        self.reset()

    def reset(self) -> None:
        super().reset()
        self._value = float(self.start)
        self._ceiling = float(self.max_value)
        self._clean: list[int] = []  # last clean laps
        self._streak = 0

    def target(self, ctx: Context) -> float:
        return float(self.target_ms)

    def on_lap(self, laps: int, ms: int, neutralized: bool) -> None:
        super().on_lap(laps, ms, neutralized)
        if neutralized:
            return
        if self._clean and ms > self.crash_ratio * min(self._clean):
            # probable crash: ceiling below the throttle that caused it
            self._ceiling = max(self.min_value, min(self._ceiling, self._value - 1))
            self._value = min(self._value - 1, self._ceiling)
            self._streak = 0
        else:
            self._clean = (self._clean + [ms])[-4:]
            self._streak += 1
            if self._streak >= self.recover_laps and self._ceiling < self.max_value:
                self._ceiling = min(self.max_value, self._ceiling + 0.5)
                self._streak = 0
            step = self.gain * (ms - self._current_target) / self._current_target  # too slow -> more throttle
            self._value += max(-self.max_step, min(self.max_step, step))
        self._value = max(self.min_value, min(self._ceiling, self._value))

    def throttle(self, ctx: Context) -> float:
        self._current_target = self.target(ctx)
        return self._value


@dataclass
class RivalProfile(TargetLapProfile):
    """Beat the bot: aims at the rival's pace, within `margin_pct` (+ = slower)."""

    kind = "rival"
    rival_slot: int = 0
    margin_pct: float = 3.0

    def target(self, ctx: Context) -> float:
        if ctx.rival_lap_ms:
            return ctx.rival_lap_ms * (1 + self.margin_pct / 100)
        return float(self.target_ms)


@dataclass
class MapProfile(Profile):
    """Throttle map: throttle per lap fraction (straights / corners).

    `points` = [[fraction 0..1, throttle], …] ascending; the throttle of the
    nearest point behind applies. The lap reference follows the average of
    the last valid laps. Beyond 1.3 laps without a pass (probable crash), it
    drives at the most cautious throttle of the map.
    """

    kind = "map"
    points: list[list[float]] = field(
        default_factory=lambda: [[0.0, 11], [0.3, 7], [0.45, 11], [0.7, 6], [0.85, 10]]
    )
    ref_lap_ms: int = 6500

    def __post_init__(self) -> None:
        super().__post_init__()
        self.points = sorted([[float(f), float(v)] for f, v in self.points])
        self._ref = float(self.ref_lap_ms)

    def reset(self) -> None:
        super().reset()
        self._ref = float(self.ref_lap_ms)

    def on_lap(self, laps: int, ms: int, neutralized: bool) -> None:
        super().on_lap(laps, ms, neutralized)
        if not neutralized and 0.6 * self._ref < ms < 1.4 * self._ref:
            self._ref = 0.8 * self._ref + 0.2 * ms

    def throttle(self, ctx: Context) -> float:
        if not self.points:
            return 0.0
        if ctx.since_cross_s is None:
            return self.points[0][1]
        frac = ctx.since_cross_s * 1000 / self._ref
        if frac > 1.3:
            return min(v for _, v in self.points)
        frac %= 1.0
        value = self.points[-1][1]
        for f, v in self.points:
            if f <= frac:
                value = v
        return value


PROFILES: dict[str, type[Profile]] = {
    p.kind: p for p in (ConstantProfile, TargetLapProfile, RivalProfile, MapProfile)
}

PROFILE_LABELS = {
    "constant": "Constant throttle",
    "target": "Target pace",
    "map": "Throttle map",
    "rival": "Rival",
}


def profile_from_dict(data: dict[str, Any]) -> Profile:
    data = dict(data)
    kind = data.pop("kind", "constant")
    cls = PROFILES.get(kind)
    if cls is None:
        raise ValueError(f"unknown profile: {kind}")
    names = set(cls.__dataclass_fields__)
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"unknown parameters for {kind}: {', '.join(sorted(unknown))}")
    return cls(**data)
