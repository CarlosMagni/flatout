"""Race brain (profile kind "brain"): a pilot, a lane planner, a pit planner and a small arbiter.

- PILOT: the throttle, from a driving model of the `PILOTS` registry ("corners" by default, or
  "target"). In the pit lane it obeys the pit plan: capped at `pit.throttle`, 0 at the stop until
  the tank is full, then out at the cap.
- LANE PLANNER: the lane to be on after the next changer. `keep`; `fastest` (clean lap time per
  lane, hysteresis); `overtake` (a slower car close ahead in our lane and the other lane free
  alongside, otherwise like `fastest`); `alternate` (every N laps, for tests).
- PIT PLANNER: measures the fuel used per lap from cars[].fuel. `auto` pits when the fuel left after
  the next lap would be under `reserve_laps` laps of it (never on the last lap(s), never twice within
  `min_laps`); `now` pits once per session; `off`. Only with the CU's fuel mode and pit lane on.
- ARBITER: the manager's safety gates come first and are untouched (a gated car gets 0 whatever
  this says), then pit > manual request > overtake > fastest > alternate > keep. It sends at most one
  lane request per lane changer / pit entry pass, between `d_min_m` (+ `latency_s` of travel) and
  `d_max_m` before it, never in the pit lane, and says what it is doing (`status`).

Lane and pit moves need fresh positions (with the speed `v`) and a track map in metres; without them
the brain never guesses where the car is: it only drives (the pilot falls back like `corners` does)
and its intent says why. Geometry `changers` / `pit` are fractions along the drawn path; positions'
`s` and the `pit.zone` override are fractions from the finish line (s = path - finish).
"""

from __future__ import annotations

import importlib
import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .profiles import PROFILE_LABELS, PROFILES, Context, CornersProfile, TargetLapProfile

logger = logging.getLogger(__name__)

FUEL_FULL = 15  # the CU's tank scale (cars[].fuel)
REARM_M = 0.3  # a changer / pit entry counts as passed once this far behind the car (position jitter)
WINDOW_MIN_M = 0.5  # the request window is never narrower than this (a fast car, a slow tick)
PIT_BLIND_STOP_S = 0.5  # pit lane without a usable position: stop this long after the CU's pit flag
EXPLORE_LAPS = 2  # `fastest`: clean laps on a lane before trying, then judging, the other one
CRASH_RATIO = 1.25  # a lap this much slower than the lane's best is an incident: it teaches nothing
SLOWER_MPS = 0.2  # `overtake`: the car ahead is slower than us by at least this much

LANE_POLICIES = ("keep", "fastest", "overtake", "alternate")
PIT_POLICIES = ("off", "auto", "now")
LANE_DEFAULTS: dict[str, Any] = {
    "policy": "keep", "every": 1, "gap_m": 0.8, "clear_m": 0.4, "hysteresis_pct": 2.0,
    "d_min_m": 0.15, "d_max_m": 2.0, "latency_s": 0.25,
}
PIT_DEFAULTS: dict[str, Any] = {
    "policy": "auto", "reserve_laps": 2.0, "last_laps": 1, "min_laps": 5, "throttle": 4,
    "max_stop_s": 12.0, "zone": None, "lane": None,
}
LIMITS = {  # key: (low, high, whole number)
    "every": (1, 50, True), "gap_m": (0.1, 5, False), "clear_m": (0, 2, False),
    "hysteresis_pct": (0, 20, False), "d_min_m": (0, 2, False), "d_max_m": (0.2, 10, False),
    "latency_s": (0, 1, False), "reserve_laps": (0, 10, False), "last_laps": (0, 20, True),
    "min_laps": (0, 100, True), "throttle": (1, 15, True), "max_stop_s": (1, 60, False),
}


# ---------------------------------------------------------------- pilots


@dataclass(frozen=True)
class Pilot:
    """A driving model the brain can wrap. `make(**params)` returns an object with the profile
    interface: `output(ctx) -> 0..15`, `on_lap(laps, ms, neutralized)`, `reset()`, `relaunch()`,
    `to_dict()` (its parameters) and optionally `source` ("map" / "pace")."""

    make: Callable[..., Any] | None
    label: str  # English, the fallback of `label_key` (UI dictionary)
    label_key: str
    params: tuple[dict[str, Any], ...] = ()  # the few the UI shows: {key, label_key, min, max, scale?}
    reason: str | None = None  # why it cannot be used (a missing dependency...); None = available

    def meta(self, name: str) -> dict[str, Any]:
        ok = self.reason is None and self.make is not None
        defaults = {k: v for k, v in self.make().to_dict().items() if k != "kind"} if ok else {}
        return {"name": name, "label": self.label, "label_key": self.label_key, "available": ok,
                "reason": self.reason, "params": [dict(p) for p in self.params], "defaults": defaults}


def load_pilot(module: str) -> Pilot:
    """A pilot kept in its own module (racelab/bots/<module>.py, exposing `PILOT`): listed as not
    available, with the reason, when that module cannot be loaded (a missing dependency...)."""
    try:
        return importlib.import_module(f"{__package__}.{module}").PILOT
    except Exception as e:  # an optional pilot never takes the app down
        return Pilot(None, module, f"pilot.{module}.label", reason=f"{type(e).__name__}: {e}")


def pilots_meta() -> list[dict[str, Any]]:
    """The pilot choices for the UI (meta.pilots)."""
    return [p.meta(name) for name, p in PILOTS.items()]


# ---------------------------------------------------------------- helpers


def _fraction(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 0 <= x <= 1


def _speed(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _lane_length(raw: Any, lane: int) -> float | None:
    """Metres of `lane` from `length_m` (a number, or [lane 0, lane 1]); None if unusable."""
    vals = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    if not vals or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                           and math.isfinite(x) and x > 0 for x in vals):
        return None
    return float(vals[lane] if len(vals) > lane else vals[0])


def _section(name: str, given: Any, defaults: dict[str, Any], policies: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(given, dict):
        raise ValueError(f"{name}: object expected")
    unknown = set(given) - set(defaults)
    if unknown:
        raise ValueError(f"unknown parameters for brain {name}: {', '.join(sorted(unknown))}")
    out = {**defaults, **given}
    if out["policy"] not in policies:
        raise ValueError(f"{name}.policy: one of {', '.join(policies)}")
    for key, (lo, hi, whole) in LIMITS.items():
        if key in out:
            v = out[key]
            if isinstance(v, bool) or not isinstance(v, int if whole else (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{name}.{key}: {'integer' if whole else 'number'} between {lo} and {hi} expected")
    return out


# ---------------------------------------------------------------- the profile


@dataclass
class BrainProfile:
    """The race brain (module docstring). Not a `Profile` subclass: the throttle is the pilot's."""

    kind = "brain"
    pilot: str = "corners"
    pilot_params: dict[str, Any] = field(default_factory=dict)
    lane: dict[str, Any] = field(default_factory=dict)
    pit: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        entry = PILOTS.get(self.pilot)
        if entry is None:
            raise ValueError(f"unknown pilot: {self.pilot} ({', '.join(PILOTS)})")
        if entry.reason is not None or entry.make is None:
            raise ValueError(f"pilot {self.pilot} unavailable: {entry.reason}")
        if not isinstance(self.pilot_params, dict):
            raise ValueError("pilot_params: object expected")
        try:
            self._model = entry.make(**self.pilot_params)
        except TypeError as e:
            raise ValueError(f"pilot_params: {e}") from None
        self.pilot_params = {k: v for k, v in self._model.to_dict().items() if k != "kind"}
        self.lane = _section("lane", self.lane, LANE_DEFAULTS, LANE_POLICIES)
        self.pit = _section("pit", self.pit, PIT_DEFAULTS, PIT_POLICIES)
        if self.lane["d_min_m"] >= self.lane["d_max_m"]:
            raise ValueError("lane.d_min_m must be below lane.d_max_m")
        zone = self.pit["zone"]
        if zone is not None:
            if not (isinstance(zone, (list, tuple)) and len(zone) == 2 and all(map(_fraction, zone))
                    and zone[0] % 1.0 != zone[1] % 1.0):
                raise ValueError("pit.zone: [entry, exit] as lap fractions from the finish line, or null")
            self.pit["zone"] = [float(zone[0]) % 1.0, float(zone[1]) % 1.0]
        if isinstance(self.pit["lane"], bool) or self.pit["lane"] not in (None, 0, 1):
            raise ValueError("pit.lane: 0, 1 or null (either lane)")
        self._per_lap: float | None = None  # fuel per lap: a property of the car, kept across sessions
        self.reset()

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **asdict(self)}

    @property
    def source(self) -> str | None:
        """The pilot's: "map" / "pace" for corners (snapshot `drive`)."""
        return getattr(self._model, "source", None)

    # ------------------------------------------------------------ profile interface

    def reset(self) -> None:
        """New session (also E-STOP, reconnection): every plan is dropped; the fuel per lap is kept."""
        self._model.reset()
        self._ask: tuple[int, float] | None = None  # (point, metres to it) to request this tick
        self._asked: tuple[int, float] | None = None  # the same, once sent: until that point is passed
        self._fuel: int | None = None  # last tank reading (fuel mode on), for the laps
        self._fuel_rose = False
        self._stint: list[int] | None = None  # this tank: [laps, fuel] at its first and its last crossing
        self._used = [0, 0]  # the session's finished tanks: laps, fuel used
        self._lap_ms: list[int] = []  # recent clean laps (laps left in a timed session)
        self._lane_laps: tuple[list[int], list[int]] = ([], [])  # clean lap times per lane
        self._ticks = [0, 0]  # ticks on each lane this lap
        self._dirty = False  # this lap had an off track or missing positions: no lane timing
        self._lap_pit = False  # the CU's pit flag was on this lap: it teaches nothing
        self._lane: int | None = None  # last known lane
        self._alt: int | None = None  # alternate: the lane to reach
        self._manual: int | str | None = None  # one-shot lane change: lane to reach, "flip" until known
        self._pit_now = self.pit["policy"] == "now"  # one-shot pit stop ("now": once per session)
        self._last_pit_lap: int | None = None
        self._pit_phase: list[Any] | None = None  # in the pit lane: ["in" | "stop" | "out", since]
        self._warned = False
        self._said: tuple[str, dict[str, Any], str | None, str | None, str | None] = (
            "brain.intent.pace", {}, None, None, None)  # intent, args, next, lane_why, pit_why

    def relaunch(self) -> None:
        self._model.relaunch()

    def on_lap(self, laps: int, ms: int, neutralized: bool) -> None:
        pit, self._lap_pit = self._lap_pit, self._pit_phase is not None
        self._model.on_lap(laps, ms, neutralized or pit)  # a lap with a pit stop teaches the pilot nothing
        if not (neutralized or pit or self._dirty):
            self._lap_ms = (self._lap_ms + [ms])[-5:]
            for lane in (0, 1):  # a lap timed on one lane (90 % of its ticks), not an incident
                times = self._lane_laps[lane]
                if self._ticks[lane] >= 0.9 * sum(self._ticks) > 0 and (not times or ms < CRASH_RATIO * min(times)):
                    times.append(ms)
                    del times[:-3]
        self._ticks, self._dirty = [0, 0], False
        if self._fuel is not None:  # fuel per lap over the session's tanks (readings are whole units)
            self._stint = [laps, self._fuel] * 2 if self._stint is None else self._stint[:2] + [laps, self._fuel]
            n, used = self._used[0] + self._stint[2] - self._stint[0], self._used[1] + self._stint[1] - self._fuel
            if n >= 2 and used > 0:
                self._per_lap = used / n
        if self.lane["policy"] == "alternate" and laps % self.lane["every"] == 0 and self._lane is not None:
            self._alt = 1 - self._lane

    def take_lane_request(self) -> bool:
        """True once when a request is due now: the manager sends it through its lane path."""
        if self._ask is None:
            return False
        self._asked, self._ask = self._ask, None
        return True

    def request(self, what: str) -> None:
        """Manual one-shot, timed like the rest: "pit" = pit at the next pit entry, "lane" = change
        lane at the next changer. Dropped by `reset()` (new session, E-STOP)."""
        if what == "pit":
            self._pit_now = True
        elif what == "lane":
            self._manual = "flip"
        else:
            raise ValueError("brain request: 'pit' or 'lane'")

    def set_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Live parameters (`bot.params`): applied in place, everything learned is kept.

        Known: `risk` (the MPC pilot's 0..1, the deslot penalty). Unknown keys and invalid values
        are refused (the caller — the RL strategist — must see a mistake, not a silent no-op)."""
        if not isinstance(params, dict):
            raise ValueError("params: object expected")
        applied: dict[str, Any] = {}
        for key, value in params.items():
            if key != "risk":
                raise ValueError(f"unknown live parameter: {key}")
            if not hasattr(self._model, "risk"):
                raise ValueError(f"pilot {self.pilot} has no live risk")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError("risk: number between 0 and 1 expected")
            self._model.risk = float(value)
            self.pilot_params["risk"] = self._model.risk
            applied[key] = self._model.risk
        return applied

    def output(self, ctx: Context) -> int:
        self._ask = None
        value = self._model.output(ctx)  # a pilot failure goes up to the manager's guard: throttle 0
        try:
            return self._arbiter(ctx, value)
        except Exception:
            if not self._warned:
                logger.exception("race brain: planner failure, the pilot drives alone")
                self._warned = True
            self._ask = None
            self._said = (self._pilot_intent(), {"why": "brain.why.error"}, None, None, None)
            return min(value, self.pit["throttle"]) if ctx.in_pit else value

    # ------------------------------------------------------------ arbiter

    def _arbiter(self, ctx: Context, value: int) -> int:
        fuel = ctx.fuel if ctx.fuel_mode else None
        self._fuel_rose = fuel is not None and self._fuel is not None and fuel > self._fuel
        if self._fuel_rose and self._stint is not None:  # refuelled: that tank is done, a new one starts
            self._used = [self._used[0] + self._stint[2] - self._stint[0], self._used[1] + self._stint[1] - self._stint[3]]
            self._stint = None
        self._fuel = fuel
        if ctx.in_pit:
            self._lap_pit = True
            return self._pit_lane(ctx, value)
        if self._pit_phase is not None:  # out of the pit lane: the visit is over
            self._pit_phase, self._pit_now, self._last_pit_lap = None, False, ctx.laps
        view, why = self._view(ctx)
        if view is None:
            self._dirty = True
        else:
            self._lane = view["lane"]
            self._ticks[view["lane"]] += 1
            self._dirty = self._dirty or bool(ctx.pos.get("off"))
        lane_why = why or (None if view["changers"] else "brain.why.changers")
        pit_why = ("brain.why.fuel_mode" if not ctx.fuel_mode else "brain.why.pit_lane" if not ctx.pit_lane
                   else why or (None if view["zone"] else "brain.why.pit_zone"))
        pit_want = self._pit_now or (self.pit["policy"] == "auto" and self._pit_due(ctx))
        target, intent = self._lane_target(ctx, view) if view else (None, None)
        side = self.pit["lane"]
        if pit_want and side is not None and view:
            target, intent = side, None  # the pit entry is taken from that lane: get there, then stay
        occupied = False
        points = self._points(view, ctx.pit_lane) if view else []
        if self._asked is not None and view is not None:
            i, d0 = self._asked
            if i >= len(points) or (points[i][0] - view["s"]) % 1.0 * view["length"] > d0 + REARM_M:
                self._asked = None  # passed (its distance jumped to about a lap): its next pass may be asked
        if points:
            ahead = sorted((((p - view["s"]) % 1.0) * view["length"], i, kind) for i, (p, kind) in enumerate(points))
            d, i, kind = ahead[0]
            since = view["length"] - ahead[-1][0]  # metres after the previous point
            if kind == "pit":
                want = pit_want and pit_why is None and (side is None or view["lane"] == side)
            else:
                want = target is not None and target != view["lane"]
                if want and not self._free(ctx, view, target, d):
                    want, occupied = False, True
            lo = self.lane["d_min_m"] + view["v"] * self.lane["latency_s"]
            d_now = d - view["v"] * ctx.pos.get("age_ms", 0) / 1000  # where the car is now, not when seen
            if (want and self._asked is None and since >= REARM_M and not ctx.pos.get("off")
                    and lo <= d_now <= max(self.lane["d_max_m"], lo + WINDOW_MIN_M)):
                self._ask = (i, d)  # the distance as seen: the "passed" test above compares like with like
        shown = "–" if ctx.fuel is None else ctx.fuel
        if pit_want:
            args = {"fuel": shown} if pit_why is None else {"fuel": shown, "why": pit_why}
            self._said = ("brain.intent.pit", args, "pit", lane_why, pit_why)
        elif target is not None and target != view["lane"]:
            key, args = intent or ("brain.intent.lane", {})
            if occupied:
                args = {**args, "why": "brain.why.occupied"}
            self._said = (key, args, "lane", lane_why, pit_why)
        else:
            active = (lane_why if self.lane["policy"] != "keep" or self._manual is not None else None) or (
                pit_why if self.pit["policy"] != "off" else None)
            self._said = (self._pilot_intent(), {"why": active} if active else {}, None, lane_why, pit_why)
        return value

    def _view(self, ctx: Context) -> tuple[dict[str, Any] | None, str | None]:
        """The car on the map: lap fraction, lane, speed, metres per lap, and the points that take a
        lane request (changers, pit zone) as fractions from the line; (None, why) when unusable."""
        pos, geo = ctx.pos, ctx.geometry
        if not pos or not _fraction(pos.get("s")):
            return None, "brain.why.positions"
        if not _speed(pos.get("v")):
            return None, "brain.why.speed"
        if not geo:
            return None, "brain.why.map"
        lane = 1 if pos.get("lane") == 1 else 0
        length = _lane_length(geo.get("length_m"), lane)
        if length is None:
            return None, "brain.why.length"
        finish = geo.get("finish") if _fraction(geo.get("finish")) else 0.0
        changers = sorted((c - finish) % 1.0 for c in geo.get("changers") or [] if _fraction(c))
        zone = self.pit["zone"]
        pit = geo.get("pit")
        if zone is None and isinstance(pit, (list, tuple)) and len(pit) == 2 and all(map(_fraction, pit)):
            zone = [(pit[0] - finish) % 1.0, (pit[1] - finish) % 1.0]
        return {"s": float(pos["s"]), "lane": lane, "v": float(pos["v"]), "length": length,
                "changers": changers, "zone": zone}, None

    @staticmethod
    def _points(view: dict[str, Any], pit_lane: bool) -> list[tuple[float, str]]:
        """The points that take a lane request, (lap fraction, "lane" | "pit"): the lane changers and,
        with the CU's pit lane on, the pit entry (a car armed there goes into the pit lane)."""
        points = [(c, "lane") for c in view["changers"]]
        if pit_lane and view["zone"]:
            points.append((view["zone"][0], "pit"))
        return points

    def _pilot_intent(self) -> str:
        return "brain.intent.map" if self.source == "map" else "brain.intent.pace"

    @property
    def status(self) -> dict[str, Any]:
        """What the brain does and why (snapshot `brain`): intent as an i18n key + args."""
        key, args, nxt, lane_why, pit_why = self._said
        return {"intent_key": key, "intent_args": args, "pilot": self.pilot,
                "lane_policy": self.lane["policy"], "pit_policy": self.pit["policy"], "next": nxt,
                "fuel_per_lap": None if self._per_lap is None else round(self._per_lap, 2),
                "lane_why": lane_why, "pit_why": pit_why}

    # ------------------------------------------------------------ lane planner

    def _lane_target(self, ctx: Context, view: dict[str, Any]) -> tuple[int | None, tuple[str, dict] | None]:
        """The lane to be on after the next changer, and why; (None, None) = no wish."""
        lane = view["lane"]
        if self._manual == "flip":
            self._manual = 1 - lane
        if self._manual is not None:
            if self._manual != lane:
                return self._manual, ("brain.intent.lane", {})
            self._manual = None  # done
        policy = self.lane["policy"]
        if policy == "overtake":
            slot = self._blocker(ctx, view)
            if slot is not None:
                return 1 - lane, ("brain.intent.overtake", {"slot": slot})
        if policy in ("fastest", "overtake"):
            best = self._fastest(lane)
            if best is not None:
                return best, ("brain.intent.fastest", {"lane": best + 1})
        if policy == "alternate" and self._alt is not None:
            return self._alt, ("brain.intent.alternate", {"n": self.lane["every"]})
        return None, None

    def _fastest(self, lane: int) -> int | None:
        # ponytail: best of the last 3 clean laps per lane; the pilot still learns in the first laps,
        # which favours the lane timed last: re-time the other lane every N laps if that bias matters
        here, there = self._lane_laps[lane], self._lane_laps[1 - lane]
        if len(here) < EXPLORE_LAPS:
            return None  # still timing this lane
        if len(there) < EXPLORE_LAPS:
            return 1 - lane  # time the other lane
        return 1 - lane if min(here) > min(there) * (1 + self.lane["hysteresis_pct"] / 100) else lane

    def _blocker(self, ctx: Context, view: dict[str, Any]) -> int | None:
        """The closest car ahead in our lane, within `gap_m` and slower than us; None if there is none."""
        best = None
        for o in ctx.others:
            v = o.get("v")
            if o.get("lane", 0) != view["lane"] or o.get("off") or not _fraction(o.get("s")) or not _speed(v):
                continue
            ahead = ((o["s"] - view["s"]) % 1.0) * view["length"]
            if 0 < ahead <= self.lane["gap_m"] and v < view["v"] - SLOWER_MPS and (best is None or ahead < best[0]):
                best = (ahead, o["slot"])
        return None if best is None else best[1]

    def _free(self, ctx: Context, view: dict[str, Any], lane: int, d: float) -> bool:
        """No car on `lane` within `clear_m` of us now, nor when we reach the changer `d` metres ahead."""
        t = d / max(view["v"], 0.3)
        for o in ctx.others:
            if o.get("lane", 0) != lane or o.get("off") or not _fraction(o.get("s")):
                continue
            rel = ((o["s"] - view["s"] + 0.5) % 1.0 - 0.5) * view["length"]  # + = ahead of us
            v = o["v"] if _speed(o.get("v")) else view["v"]
            if min(abs(rel), abs(rel + (v - view["v"]) * t)) < self.lane["clear_m"]:
                return False
        return True

    # ------------------------------------------------------------ pit planner

    def _pit_due(self, ctx: Context) -> bool:
        """`auto`: the fuel left after the next lap would be under `reserve_laps` laps of fuel."""
        per, fuel = self._per_lap, self._fuel
        if per is None or per <= 0 or fuel is None:
            return False  # still measuring (or no fuel mode)
        if self._last_pit_lap is not None and ctx.laps - self._last_pit_lap < self.pit["min_laps"]:
            return False
        left = self._laps_left(ctx)
        if left is not None and (left <= self.pit["last_laps"] or fuel >= left * per):
            return False  # the last lap(s), or enough fuel to finish
        return fuel - per < self.pit["reserve_laps"] * per

    def _laps_left(self, ctx: Context) -> float | None:
        if ctx.session_laps:
            return ctx.session_laps - ctx.laps
        if ctx.remaining_s is not None and self._lap_ms:
            return ctx.remaining_s * 1000 / (sum(self._lap_ms) / len(self._lap_ms)) + 1  # + the lap in progress
        return None

    def _pit_lane(self, ctx: Context, value: int) -> int:
        """In the pit lane (the CU's flag): to the stop at the pit throttle, 0 until the tank is full
        (or `max_stop_s`), then out at the pit throttle. No lane request in here."""
        now = ctx.now
        phase, since = self._pit_phase or ("in", now)
        refuel = ctx.fuel_mode and ctx.fuel is not None and ctx.fuel < FUEL_FULL
        if phase == "in" and not refuel:
            phase = "out"  # nothing to fill: drive through
        elif phase == "in" and self._at_stop(ctx, since):
            phase, since = "stop", now
        if phase == "stop" and (not refuel or now - since >= self.pit["max_stop_s"]):
            phase = "out"
        self._pit_phase = [phase, since]
        if phase == "stop":
            self._said = ("brain.intent.refuel", {"fuel": ctx.fuel}, None, None, None)
            return 0
        self._said = ("brain.intent.pit_lane", {}, None, None, None)
        return min(value, self.pit["throttle"])

    def _at_stop(self, ctx: Context, since: float) -> bool:
        """Time to stop: the CU is filling the tank, or the car reached the middle of the pit zone
        (without a usable position: PIT_BLIND_STOP_S after the CU's pit flag)."""
        if self._fuel_rose:
            return True
        view, _ = self._view(ctx)
        if view is None or view["zone"] is None:
            return ctx.now - since >= PIT_BLIND_STOP_S
        a, b = view["zone"]
        stop = (a + (b - a) % 1.0 / 2) % 1.0
        return (view["s"] - stop) % 1.0 < 0.5  # at or past the stop point


# a new pilot = one module exposing PILOT = Pilot(...) (it may import Pilot from here) plus one line
# below, e.g. "mpc": load_pilot("mpc") for bots/mpc.py; the UI lists it from meta.pilots, the manager
# needs nothing, and a module that cannot load (numpy missing) is listed as unavailable with the reason
_LAP_S = {"key": "target_ms", "min": 1, "max": 120, "scale": 0.001}  # stored in ms, shown in s
PILOTS: dict[str, Pilot] = {
    "corners": Pilot(CornersProfile, "Corner speed (map)", "profile.corners.label", (
        {"key": "a_lat", "label_key": "profile.a_lat", "min": 1, "max": 40},
        {"key": "margin_pct", "label_key": "profile.corner_margin", "min": 0, "max": 50},
        {"key": "cap", "label_key": "profile.cap", "min": 0, "max": 15},
        {**_LAP_S, "label_key": "profile.fallback_time"})),
    "target": Pilot(TargetLapProfile, "Target pace", "profile.target.label", (
        {**_LAP_S, "label_key": "profile.target_time"},
        {"key": "start", "label_key": "profile.start", "min": 0, "max": 15},
        {"key": "min_value", "label_key": "profile.min", "min": 0, "max": 15},
        {"key": "max_value", "label_key": "profile.max", "min": 0, "max": 15})),
    "mpc": load_pilot("mpc"),
}

PROFILES[BrainProfile.kind] = BrainProfile  # type: ignore[assignment]  (duck-typed profile)
PROFILE_LABELS[BrainProfile.kind] = "Race brain"
