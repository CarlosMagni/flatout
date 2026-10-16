"""The strategist's observation: computed from the RaceLab snapshot only.

The same code runs in the training environment and live (a `/ws` client cannot read the
brain's internals). Contract — exactly what the app publishes:

    state = {
      "session": {"state": "running", "laps": 15 | None, "remaining_s": None | float},
      "cu":      {"phase": 0, "fuel_mode": bool, "pit_lane": bool},
      "cars":    [{"slot": 0, "laps": int, "fuel": int, "pit": bool}, ...],
      "bots":    [{"slot": 0, "brain": {"fuel_per_lap": 0.9 | None}, ...}, ...],
      "positions": {"stale": bool, "age_ms": int,
                    "cars": [{"slot", "s", "lane", "v", "off"}, ...]},
    }

`ObsState.note(state, now)` every tick (lap bookkeeping, clean laps per lane, stops);
`ObsState.build(state, geometry, slot)` when a decision is due. `geometry` (metres,
changers, pit) is optional: without it the changer feature reads -1.

Vector (20 floats):
    0 laps done/target            1 laps left/target          2 remaining s/600 (-1)
    3 fuel/15                     4 fuel per lap/5            5 laps of fuel left/target (cap 2)
    6 lane                        7 last clean lap lane0/ref  8 same lane1 (0 = none)
    9 gap ahead same lane/10 m   10 its speed/5 (-1)         11-12 same, other lane
   13 gap behind same lane/10    14 its speed/5              15 other lane free at next changer
   16 stops/5                    17 in pit                   18 race position/n          19 pit allowed
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

OBS_DIM = 20
REF_LAP_MS = 6000.0  # the normaliser; the real one is measured on the track
MAX_GAP_M = 10.0
CHANGER_CLEAR_M = 0.6  # the other lane is free when no car is that close to the changer
DEFAULT_TARGET_LAPS = 20  # when the session gives no lap count


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value else None  # NaN


@dataclass
class ObsState:
    """The runner-side memory behind the vector. One per car; driven by note()."""

    ref_lap_ms: float = REF_LAP_MS
    pit_min_laps: int = 5
    lane_ms: list = field(default_factory=lambda: [None, None])  # last clean lap per lane (ms)
    stops: int = 0
    pit_lap: int | None = None
    last_laps: int = -1
    lap_start_t: float | None = None
    off_seen: bool = False
    pit_seen: bool = False
    _slot: int = 0
    _lane: int = 0
    _in_pit: bool = False
    lane_requests: int = 0
    pit_requests: int = 0

    # ------------------------------------------------------------------ every tick

    def note(self, state: dict, now: float) -> None:
        car = _car(state, self._slot)
        pos = _pos(state, self._slot)
        laps = int(car.get("laps", 0) or 0)
        if pos is not None:
            lane = pos.get("lane")
            if lane in (0, 1):
                self._lane = lane
            if pos.get("off"):
                self.off_seen = True
        if car.get("pit"):
            self.pit_seen = True
            if not self._in_pit:  # a pit visit starts
                self.stops += 1
                self.pit_lap = laps
        self._in_pit = bool(car.get("pit"))
        if laps != self.last_laps:
            if self.last_laps >= 0 and self.lap_start_t is not None:
                ms = round((now - self.lap_start_t) * 1000)
                if ms > 0 and not self.off_seen and not self.pit_seen:
                    self.lane_ms[self._lane] = ms  # a clean lap for that lane
            self.last_laps = laps
            self.lap_start_t = now
            self.off_seen = self.pit_seen = False

    def set_slot(self, slot: int) -> None:
        self._slot = slot

    # ------------------------------------------------------------------ at a decision

    def build(self, state: dict, geometry: dict | None = None) -> np.ndarray:
        car = _car(state, self._slot)
        pos = _pos(state, self._slot)
        session, cu = state.get("session") or {}, state.get("cu") or {}
        laps = int(car.get("laps", 0) or 0)
        target = _num(session.get("laps")) or max(laps, DEFAULT_TARGET_LAPS)
        remaining = _num(session.get("remaining_s"))
        fuel = _num(car.get("fuel"))
        fpl = _num(_brain(state, self._slot).get("fuel_per_lap"))
        fuel_laps = (fuel / fpl) if fuel is not None and fpl else None
        laps_left = max(target - laps, 0)
        ref = self.ref_lap_ms

        gaps = _gaps(state, self._slot, pos, geometry)
        rank, n_cars = _rank(state, self._slot)
        allowed = (bool(cu.get("fuel_mode")) and not car.get("pit")
                   and (self.pit_lap is None or laps - self.pit_lap >= self.pit_min_laps))

        v = np.array([
            laps / target,
            laps_left / target,
            (remaining / 600.0) if remaining is not None else -1.0,
            (fuel / 15.0) if fuel is not None else 0.0,
            min(fpl, 5.0) / 5.0 if fpl is not None else 0.0,
            min(fuel_laps / max(laps_left, 1), 2.0) if fuel_laps is not None else -1.0,
            float(pos.get("lane") or 0.0) if pos else 0.0,
            (self.lane_ms[0] or 0) / ref if self.lane_ms[0] else 0.0,
            (self.lane_ms[1] or 0) / ref if self.lane_ms[1] else 0.0,
            gaps[0], gaps[1], gaps[2], gaps[3], gaps[4], gaps[5], gaps[6],
            self.stops / 5.0,
            1.0 if car.get("pit") else 0.0,
            rank / n_cars if n_cars else 0.0,
            1.0 if allowed else 0.0,
        ], dtype=np.float32)
        assert v.shape == (OBS_DIM,)
        return v


# ---------------------------------------------------------------------- helpers


def _car(state: dict, slot: int) -> dict:
    return next((c for c in state.get("cars") or [] if c.get("slot") == slot), {})


def _pos(state: dict, slot: int) -> dict | None:
    feed = state.get("positions") or {}
    if feed.get("stale"):
        return None
    return next((c for c in feed.get("cars") or [] if c.get("slot") == slot), None)


def _brain(state: dict, slot: int) -> dict:
    bot = next((b for b in state.get("bots") or [] if b.get("slot") == slot), {})
    return bot.get("brain") or {}


def _fresh_others(state: dict, slot: int) -> list[dict]:
    feed = state.get("positions") or {}
    if feed.get("stale"):
        return []
    return [c for c in feed.get("cars") or [] if c.get("slot") != slot and _num(c.get("s")) is not None]


def _gaps(state: dict, slot: int, pos: dict | None, geometry: dict | None) -> list[float]:
    """Normalised [gap ahead same lane, speed, gap ahead other lane, speed, gap behind, speed,
    other lane free at the next changer]. Distances ÷ MAX_GAP_M (1.0 = none); -1 = unknown."""
    none = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, -1.0]
    feed = state.get("positions") or {}
    if pos is None or feed.get("stale"):
        return none
    s, lane = _num(pos.get("s")), pos.get("lane")
    if s is None:
        return none
    length = _num((geometry or {}).get("length_m"))
    others = _fresh_others(state, slot)
    if length is None:
        length = 1.0  # fractions then: usable above the short gaps of the changers
    same, other, behind = [MAX_GAP_M, -1.0], [MAX_GAP_M, -1.0], [MAX_GAP_M, -1.0]
    for c in others:
        cs = _num(c.get("s"))
        if cs is None:
            continue
        v = _num(c.get("v"))
        v = v if v is not None else -1.0
        ahead_m = ((cs - s) % 1.0) * length
        behind_m = ((s - cs) % 1.0) * length
        if behind_m < behind[0]:
            behind = [behind_m, v]
        if c.get("lane") == lane:
            if ahead_m < same[0]:
                same = [ahead_m, v]
        elif ahead_m < other[0]:
            other = [ahead_m, v]
    free = -1.0
    geo = geometry or {}
    changer_list = [c for c in (geo.get("changers") or []) if _num(c) is not None]
    if changer_list and _num(geo.get("length_m")) is not None:
        nxt = min(changer_list, key=lambda c: (c - s) % 1.0)
        occupied = False
        for c in others:
            if c.get("lane") == lane:
                continue
            cs = _num(c.get("s"))
            if cs is None:
                continue
            d = min((cs - nxt) % 1.0, (nxt - cs) % 1.0) * length
            if d <= CHANGER_CLEAR_M:
                occupied = True
                break
        free = 0.0 if occupied else 1.0
    return [same[0] / MAX_GAP_M, same[1] if same[1] < 0 else same[1] / 5.0,
            other[0] / MAX_GAP_M, other[1] if other[1] < 0 else other[1] / 5.0,
            behind[0] / MAX_GAP_M, behind[1] if behind[1] < 0 else behind[1] / 5.0, free]


def _rank(state: dict, slot: int) -> tuple[int, int]:
    cars = state.get("cars") or []
    rows = []
    for c in cars:
        s = _pos(state, int(c.get("slot", -1)))
        rows.append((int(c.get("laps", 0) or 0), _num((s or {}).get("s")) or 0.0, int(c.get("slot", -1))))
    rows.sort(key=lambda r: (-r[0], -r[1]))
    for i, (_l, _s, sl) in enumerate(rows):
        if sl == slot:
            return i + 1, len(rows)
    return len(rows), len(rows)
