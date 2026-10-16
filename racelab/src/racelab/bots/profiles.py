"""Local autopilots (bot profiles).

The CU gives no continuous position: only finish-line crossings (and Check
Lanes when present). A profile therefore drives "blind" between two passes,
estimating the fraction of the lap covered from the time elapsed since the
last pass and a reference lap time.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

POS_MAX_AGE_MS = 500  # positions older than this are not used to drive (profiles fall back)


@dataclass
class Context:
    now: float
    since_cross_s: float | None  # since the last pass over the line
    laps: int
    rival_lap_ms: float | None = None  # rival reference ("rival" profile)
    # own car, from the published positions: {s, lane, v, off, age_ms}; None = unknown or stale
    pos: dict[str, Any] | None = None
    # track shape (state["positions"] companion: meta.track / track.set), None = unknown
    geometry: dict[str, Any] | None = None
    # race state, for the race brain (bots/brain.py); the other profiles ignore it
    fuel: int | None = None  # own tank, 0..15 (cars[].fuel), None = unknown
    in_pit: bool = False  # own car in the pit lane (cars[].pit, the CU's flag)
    fuel_mode: bool = False  # cu.fuel_mode
    pit_lane: bool = False  # cu.pit_lane
    session_laps: int | None = None  # laps this car must complete (lap-target session), else None
    remaining_s: float | None = None  # time left in a timed session (session.remaining_s), else None
    others: list[dict[str, Any]] = field(default_factory=list)  # the other cars' fresh positions


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
        value = self.throttle(ctx)
        if self._launch is None:
            self._launch = ctx.now
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


# ---------------------------------------------------------------- corner speed

V_MAX = 4.2  # m/s at throttle 15 (virtual CU; a real car: set `v_max`, a test pins the sim value)
PAD_M = 0.12  # each corner is widened by this much: tolerates map smoothing and position error
SMOOTH_M = 0.10  # half-window of the curvature smoothing
BRAKE_PLAN = 0.8  # braking is planned at 80 % of what lifting off gives
HORIZON_S = 0.15  # the throttle is chosen to reach the target speed this far ahead
V_MIN = 0.3  # m/s, a target never asks the car to stall
F_MIN, F_MAX = 0.5, 1.2  # per-corner speed factor range; F_MAX is the default of the `f_max` parameter
# (1.2 = 1.44x the model's grip: raise it once a real car's grip is measured)
FALLBACK_HOLD_S = 1.0  # positions lost: for this long, never more throttle than the last driven value
F_UP, F_DOWN = 1.03, 0.92  # clean pass / off track
DRIVEN = 0.9  # a pass only teaches if the car kept at least this share of the target
CORNER_NS = 0.05  # m, spacing of the resampled track


def _unit_length(geometry: dict[str, Any]) -> float | None:
    """Centre line length in m from `length_m` (number, or [lane 0, lane 1]); None if unusable."""
    raw = geometry.get("length_m")
    vals = raw if isinstance(raw, (list, tuple)) else [raw]
    if not vals or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                           and math.isfinite(x) and x > 0 for x in vals):
        return None
    return sum(vals) / len(vals)


def curvature(geometry: dict[str, Any]) -> dict[str, Any] | None:
    """Signed curvature (1/m, + = left) of the centre line, on equidistant samples.

    The shape is normalised ([0, 1]², y up); `length_m` gives the scale. Samples are
    indexed by the fraction of the path from the first point (arc length).
    """
    try:
        pts = [(float(x), float(y)) for x, y in geometry["points"]]
        length = _unit_length(geometry)
        if length is None or len(pts) < 8:
            return None
        cum = [0.0]
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        if cum[-1] <= 0:
            return None
        k = length / cum[-1]  # m per normalised unit
        n = max(120, min(1200, round(length / CORNER_NS)))
        ds = length / n
        xs, ys, j = [], [], 0
        for t in range(n):
            target = cum[-1] * t / n
            while cum[j + 1] < target:
                j += 1
            a, b = pts[j], pts[(j + 1) % len(pts)]
            f = (target - cum[j]) / (cum[j + 1] - cum[j]) if cum[j + 1] > cum[j] else 0.0
            xs.append((a[0] + f * (b[0] - a[0])) * k)
            ys.append((a[1] + f * (b[1] - a[1])) * k)
        head = [math.atan2(ys[(i + 1) % n] - ys[i - 1], xs[(i + 1) % n] - xs[i - 1]) for i in range(n)]
        kap = [((head[(i + 1) % n] - head[i - 1] + math.pi) % (2 * math.pi) - math.pi) / (2 * ds)
               for i in range(n)]
        half = max(1, round(SMOOTH_M / ds))
        kap = [sum(kap[(i + d) % n] for d in range(-half, half + 1)) / (2 * half + 1) for i in range(n)]
        off = geometry.get("lane_offset")
        off = float(off) * k if isinstance(off, (int, float)) and math.isfinite(off) and off > 0 else 0.0
        finish = geometry.get("finish")
        finish = float(finish) if isinstance(finish, (int, float)) and math.isfinite(finish) else 0.0
        return {"n": n, "ds": ds, "length": length, "kappa": kap, "lane_off": off, "finish": finish % 1.0}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def lane_curvature(kappa: list[float], lane_off: float, lane: int) -> list[float]:
    """|curvature| of lane 0 (left of the driving direction) or 1 (right)."""
    sign = 1.0 if lane == 0 else -1.0
    return [abs(k / (1 - sign * lane_off * k)) for k in kappa]


def segment_corners(kappa: list[float], enter: float, leave: float) -> tuple[list[int], list[range]]:
    """Corners = runs with |kappa| above `enter` (until it falls under `leave`); a change of
    sign starts a new corner. Returns the corner id of each sample (-1 = straight) and each
    corner's span of samples, widened by PAD_M."""
    n = len(kappa)
    cid = [-1] * n
    first: list[int] = []
    last: list[int] = []
    start = min(range(n), key=lambda j: abs(kappa[j]))  # begin on a straight
    sign = 0
    for t in range(n):
        j = (start + t) % n
        k = kappa[j]
        if sign == 0 and abs(k) > enter or sign and abs(k) > enter and (k > 0) != (sign > 0):
            sign = 1 if k > 0 else -1
            first.append(t)
            last.append(t)
            cid[j] = len(first) - 1
        elif sign and abs(k) < leave:
            sign = 0
        elif sign:
            last[-1] = t
            cid[j] = len(first) - 1
    return cid, [range(start + a, start + b + 1) for a, b in zip(first, last)]


@dataclass
class CornersProfile(TargetLapProfile):
    """Corner speed from the published positions and the track shape ("AI driver v1").

    From the shape (curvature, scaled by `length_m`) it builds a target speed along the lap
    for each lane: sqrt(`a_lat` / curvature) in corners, cut by `margin_pct`, then a backward
    pass so that it brakes in time (the motor is first order: lifting off sheds speed
    linearly with distance). Every tick it predicts the car's speed `latency_s` ahead with the
    motor model and picks the throttle that reaches the target speed of the road just ahead:
    it lifts when above it, and never exceeds `cap`.

    Each corner has a speed factor, learned in the session (reset with `reset()`): +3 % after a
    clean pass that really ran at the target, -8 % and frozen after an off track (position
    `off`, or a lap much slower than the best, blamed on the corner where most time was lost),
    always within [0.5, 1.5]. A frozen corner never goes up again (no spiral).

    Without usable positions or shape (missing, stale: older than 500 ms) it falls back to the
    "target pace" behaviour (`target_ms`, `start`, `min_value`, `max_value`), which keeps
    learning in the background.
    """

    kind = "corners"
    a_lat: float = 10.0  # m/s², lateral acceleration assumed at the start
    margin_pct: float = 5.0  # % taken off every target speed
    cap: float = 12.0  # highest throttle this profile ever outputs
    latency_s: float = 0.1  # delay between the decision and its effect on the car
    v_max: float = V_MAX  # m/s at throttle 15
    tau_acc: float = 0.45  # s, motor time constant
    tau_brake: float = 0.30  # s, lifting off
    f_max: float = F_MAX  # highest per-corner speed factor learned (the virtual CU tolerates 1.5)

    _src = None  # shape the tables were computed from (a cache: survives reset())
    _geo = None
    source = None  # "map" (driven from positions + shape) or "pace" (fallback), None before the first tick; not a field

    def __post_init__(self) -> None:
        super().__post_init__()
        for name, lo, hi in (("a_lat", 0.5, 100), ("margin_pct", 0, 50), ("cap", 0, 15),
                             ("latency_s", 0, 1), ("v_max", 0.5, 20), ("tau_acc", 0.02, 5),
                             ("tau_brake", 0.02, 5)):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{name}: number between {lo} and {hi} expected")

    def reset(self) -> None:
        super().reset()
        self.source = None
        self._f: dict[tuple[int, int], float] = {}  # (lane, corner) -> speed factor
        self._frozen: set[tuple[int, int]] = set()
        self._key = None
        self._prof: list[list[float] | None] = [None, None]
        self._lost: dict[tuple[int, int], float] = {}  # time lost against the target, per corner, this lap
        self._off_lap = False
        self._warned = False
        self._driven: tuple[float, float] | None = None  # (time, throttle) of the last position-driven output
        self._drop()

    def _drop(self) -> None:
        """Positions went away: forget the tracking state (the factors are kept)."""
        self._cur: int | None = None  # corner the car is in (-1 = straight)
        self._last_c: int | None = None  # last corner entered
        self._vmin = self._tmin = math.inf
        self._pass_off = self._was_off = False
        self._t_prev: float | None = None
        self._u_prev = 0.0
        self._vf = 0.0
        self._pt: tuple[float, float] | None = None

    # ------------------------------------------------------------ learning

    def on_lap(self, laps: int, ms: int, neutralized: bool) -> None:
        slow = bool(self._clean) and not neutralized and ms > self.crash_ratio * min(self._clean)
        super().on_lap(laps, ms, neutralized)
        if slow and not self._off_lap and self._lost:  # a stop nobody flagged: blame where time went
            (lane, c), lost = max(self._lost.items(), key=lambda kv: kv[1])
            if lost > 0:
                self._bump((lane, c), F_DOWN, freeze=True)
        self._off_lap = False
        self._lost.clear()

    def _bump(self, key: tuple[int, int], factor: float, *, freeze: bool = False) -> None:
        self._f[key] = max(F_MIN, min(max(1.0, min(self.f_max, 1.5)), self._f.get(key, 1.0) * factor))
        if freeze:
            self._frozen.add(key)
        self._prof[key[0]] = None

    # ------------------------------------------------------------ tables

    def _prepare(self, geo: dict[str, Any]) -> None:
        key = (id(geo), self.a_lat, self.margin_pct, self.cap, self.v_max, self.tau_brake)
        if key == self._key:
            return
        self._key = key
        self._prof = [None, None]
        n, ds, kap = geo["n"], geo["ds"], geo["kappa"]
        enter = self.a_lat / self.v_max ** 2  # where the nominal target is below top speed
        cid, spans = segment_corners(kap, enter, enter / 2)
        pad = round(PAD_M / ds)
        self._spans = [range(r.start - pad, r.stop + pad) for r in spans]
        near = list(cid)
        for c, r in enumerate(self._spans):
            for i in r:
                if near[i % n] < 0:
                    near[i % n] = c
        self._near = near
        self._raw = []
        for lane in (0, 1):
            ak = lane_curvature(kap, geo["lane_off"], lane)
            kd = [max(ak[(i + d) % n] for d in range(-pad, pad + 1)) for i in range(n)]
            self._raw.append([math.sqrt(self.a_lat / max(k, 1e-6)) for k in kd])

    def _table(self, geo: dict[str, Any], lane: int) -> list[float]:
        if self._prof[lane] is None:
            self._prof[lane] = self._speeds(geo, lane)
        return self._prof[lane]

    def _speeds(self, geo: dict[str, Any], lane: int) -> list[float]:
        """Target speed (m/s) along the lap for `lane`, with the learned corner factors."""
        n, ds = geo["n"], geo["ds"]
        scale: list[float | None] = [None] * n
        for c, r in enumerate(self._spans):
            f = self._f.get((lane, c), 1.0)
            for i in r:
                scale[i % n] = f if scale[i % n] is None else min(scale[i % n], f)
        top = self.v_max * max(0.0, min(15.0, self.cap)) / 15 * 0.98
        k = 1 - self.margin_pct / 100
        v = [max(V_MIN, min(top, k * (1.0 if scale[i] is None else scale[i]) * self._raw[lane][i]))
             for i in range(n)]
        rate = BRAKE_PLAN / self.tau_brake * ds  # speed that can be shed per sample
        for i in range(2 * n - 1, -1, -1):  # two passes: the lap is periodic
            j = i % n
            v[j] = min(v[j], v[(j + 1) % n] + rate)
        return v

    # ------------------------------------------------------------ driving

    def throttle(self, ctx: Context) -> float:
        base = super().throttle(ctx)  # target pace: the fallback, kept warm
        value = None
        try:
            value = self._drive(ctx)
        except Exception:
            if not self._warned:
                logger.exception("corners profile: falling back to target pace")
                self._warned = True
            self._drop()
        self.source = "map" if value is not None else "pace"
        if value is not None:
            self._driven = (ctx.now, value)
        else:
            value = base
            # the lap-average pace is usually too hot for a corner: right after losing the positions, hold
            # at most the last driven throttle (the car may be in a corner) instead of jumping to it
            if self._driven is not None and ctx.now - self._driven[0] <= FALLBACK_HOLD_S:
                value = min(value, self._driven[1])
        return max(0.0, min(math.floor(self.cap), value, 15.0))  # output() rounds: stay on a whole notch

    def _drive(self, ctx: Context) -> float | None:
        pos, geometry = ctx.pos, ctx.geometry
        if not pos or not geometry or pos.get("age_ms", 0) > POS_MAX_AGE_MS:
            self._drop()
            return None
        src = (geometry.get("points"), geometry.get("length_m"))
        if self._src is None or src[0] is not self._src[0] or src[1] != self._src[1]:
            self._src, self._geo, self._key = src, curvature(geometry), None
        geo = self._geo
        s = pos.get("s")
        if (geo is None or isinstance(s, bool) or not isinstance(s, (int, float))
                or not math.isfinite(s) or not 0 <= s <= 1):
            self._drop()
            return None
        self._prepare(geo)
        n, ds = geo["n"], geo["ds"]
        lane = 1 if pos.get("lane") == 1 else 0
        fi = ((s + geo["finish"]) % 1.0) * n
        v = self._speed(pos, fi * ds, ctx.now - pos.get("age_ms", 0) / 1000, geo["length"])
        off = bool(pos.get("off"))
        self._observe(geo, lane, int(fi) % n, v, off, ctx.now)
        if off:
            self._u_prev = 0.0
            return 0.0
        prof = self._table(geo, lane)  # rebuilt if what was just learned changed it
        vp = self._model(v, self._u_prev, self.latency_s)  # speed when this decision takes effect
        j = int((fi * ds + (v + vp) / 2 * self.latency_s) / ds)
        vt = min(prof[(j + k) % n] for k in range(int(vp * HORIZON_S / ds) + 2))
        tau = self.tau_acc if vt >= vp else self.tau_brake
        e = math.exp(-HORIZON_S / tau)
        u = 15 * max(0.0, (vt - vp * e) / (1 - e)) / self.v_max  # first-order motor: reach vt in HORIZON_S
        self._u_prev = max(0.0, min(float(self.cap), u, 15.0))
        return self._u_prev

    def _model(self, v: float, u: float, h: float) -> float:
        vss = self.v_max * u / 15
        return vss + (v - vss) * math.exp(-h / (self.tau_acc if vss >= v else self.tau_brake))

    def _speed(self, pos: dict[str, Any], s_m: float, ts: float, length: float) -> float:
        v = pos.get("v")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
            return float(v)
        prev = self._pt  # no speed published: differentiate the positions
        if prev is None:
            self._pt = (s_m, ts)
        elif ts - prev[1] > 1e-3:
            d = (s_m - prev[0] + length / 2) % length - length / 2
            self._vf = 0.5 * self._vf + 0.5 * max(0.0, d / (ts - prev[1]))
            self._pt = (s_m, ts)
        return self._vf

    def _observe(self, geo: dict[str, Any], lane: int, i: int, v: float, off: bool, now: float) -> None:
        """Learning: corner passes, off tracks, time lost per corner."""
        dt = 0.0 if self._t_prev is None else max(0.0, min(0.5, now - self._t_prev))
        self._t_prev = now
        prof = self._table(geo, lane)
        c = self._near[i]
        if c != self._cur:
            prev = self._cur
            if prev is not None and prev >= 0:  # left a corner
                key = (lane, prev)
                if not self._pass_off and key not in self._frozen and self._vmin >= DRIVEN * self._tmin:
                    self._bump(key, F_UP)
            if c >= 0:
                self._vmin = self._tmin = math.inf
                self._pass_off = False
            self._cur = c
        if c >= 0:
            self._last_c = c
            self._vmin = min(self._vmin, v)
            self._tmin = min(self._tmin, prof[i])
        if off and not self._was_off:
            corner = self._last_c if self._last_c is not None else self._nearest(i)
            if corner is not None:
                self._bump((lane, corner), F_DOWN, freeze=True)
            self._off_lap = self._pass_off = True
        elif self._was_off and not off:
            self.relaunch()  # put back on the track: ramp up again
        self._was_off = off
        if self._last_c is not None and prof[i] > 0:
            key = (lane, self._last_c)
            self._lost[key] = self._lost.get(key, 0.0) + dt * max(0.0, 1 - v / prof[i])

    def _nearest(self, i: int) -> int | None:
        n = len(self._near)
        for d in range(n // 2 + 1):
            for j in (i - d, i + d):
                if self._near[j % n] >= 0:
                    return self._near[j % n]
        return None


PROFILES: dict[str, type[Profile]] = {
    p.kind: p for p in (ConstantProfile, TargetLapProfile, RivalProfile, MapProfile, CornersProfile)
}

PROFILE_LABELS = {
    "constant": "Constant throttle",
    "target": "Target pace",
    "map": "Throttle map",
    "rival": "Rival",
    "corners": "Corner speed (map)",
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
