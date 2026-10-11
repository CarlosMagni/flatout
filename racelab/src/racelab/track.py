"""Track shape and car positions, supplied by a connector.

A connector (camera, estimator, another program) declares the circuit shape
(`track.set`) then sends positions at its own rate (`track.positions`).
RaceLab publishes them in the same format as the virtual CU: an agent
developed on the simulator reads the same fields on the real track.

- Shape: closed centerline, in the connector's units (pixels, meters…);
  RaceLab scales it into [0, 1]², y axis pointing up.
- Positions: `s` = lap fraction (0 = finish line), `lane` 0/1, `v` in m/s
  (optional), `off` = off track (optional).
- The connector owns the track: if it disconnects, shape and positions
  disappear (and so does the UI map).
"""

from __future__ import annotations

import math
import time
from typing import Any

MIN_POINTS, MAX_POINTS = 8, 4000
STALE_S = 1.0  # older positions are flagged stale
DEFAULT_LANE_OFFSET = 0.012  # gap between centerline and lane, normalized track


class TrackError(ValueError):
    pass


def _fraction(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise TrackError(f"{name}: number expected")
    if not 0 <= value <= 1:
        raise TrackError(f"{name}: lap fraction between 0 and 1 expected")
    return float(value) % 1.0


def normalize_geometry(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a track shape and convert it to RaceLab's format."""
    if not isinstance(data, dict):
        raise TrackError("track shape: object expected")
    raw = data.get("points")
    if not isinstance(raw, list) or not MIN_POINTS <= len(raw) <= MAX_POINTS:
        raise TrackError(f"points: list of {MIN_POINTS} to {MAX_POINTS} [x, y] points")
    pts = []
    for p in raw:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or not all(isinstance(c, (int, float)) and math.isfinite(c) for c in p)):
            raise TrackError("points: each point is [x, y] (numbers)")
        pts.append((float(p[0]), float(p[1])))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    scale = max(maxx - minx, maxy - miny)
    if scale <= 0:
        raise TrackError("points: the track has no extent")
    y_down = data.get("y_axis", "up") == "down"  # camera images: y pointing down
    if data.get("y_axis", "up") not in ("up", "down"):
        raise TrackError("y_axis: 'up' or 'down'")
    norm = [[round((x - minx) / scale, 6),
             round(((maxy - y) if y_down else (y - miny)) / scale, 6)] for x, y in pts]

    lane_offset = data.get("lane_offset")
    if lane_offset is None:
        lane_offset = DEFAULT_LANE_OFFSET
    elif not isinstance(lane_offset, (int, float)) or lane_offset <= 0:
        raise TrackError("lane_offset: positive number, in the units of the points")
    else:
        lane_offset = lane_offset / scale

    out: dict[str, Any] = {
        "points": norm,
        "lane_offset": round(float(lane_offset), 6),
        "finish": _fraction(data.get("finish", 0.0), "finish"),
        "changers": [_fraction(c, "changers") for c in data.get("changers") or []],
        "pit": None,
        "length_m": data.get("length_m"),
        "name": str(data.get("name") or "")[:60],
    }
    pit = data.get("pit")
    if pit is not None:
        if not isinstance(pit, (list, tuple)) or len(pit) != 2:
            raise TrackError("pit: [start, end] as lap fractions")
        out["pit"] = [_fraction(pit[0], "pit"), _fraction(pit[1], "pit")]
    return out


def normalize_positions(cars: Any) -> list[dict[str, Any]]:
    if not isinstance(cars, list) or len(cars) > 8:
        raise TrackError("cars: list of at most 8 cars")
    out, seen = [], set()
    for c in cars:
        if not isinstance(c, dict):
            raise TrackError("cars: objects {slot, s, lane, v?, off?} expected")
        slot = c.get("slot")
        if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot <= 7 or slot in seen:
            raise TrackError("slot: integer 0..7, once per car")
        seen.add(slot)
        lane = c.get("lane", 0)
        if lane not in (0, 1):
            raise TrackError("lane: 0 or 1")
        car = {"slot": slot, "s": _fraction(c.get("s"), "s"), "lane": lane,
               "v": None, "off": bool(c.get("off", False))}
        v = c.get("v")
        if v is not None:
            if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise TrackError("v: speed in m/s, positive")
            car["v"] = round(float(v), 3)
        out.append(car)
    return out


class TrackFeed:
    """The track supplied by an external connector (a single source at a time)."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.owner: str | None = None
        self.geometry: dict[str, Any] | None = None
        self.cars: list[dict[str, Any]] = []
        self.updated = 0.0

    def set_geometry(self, owner: str, data: dict[str, Any]) -> dict[str, Any]:
        self._check_owner(owner)
        self.geometry = normalize_geometry(data)
        self.owner = owner
        return self.geometry

    def set_positions(self, owner: str, cars: Any) -> None:
        self._check_owner(owner)
        self.cars = normalize_positions(cars)
        self.owner = owner
        self.updated = self._clock()

    def clear(self, owner: str | None = None) -> bool:
        """Clear the track (only `owner`'s if given); True if something changed."""
        if self.owner is None or (owner is not None and owner != self.owner):
            return False
        self.owner, self.geometry, self.cars, self.updated = None, None, [], 0.0
        return True

    def snapshot(self) -> dict[str, Any] | None:
        if self.owner is None or not self.updated:
            return None
        age = self._clock() - self.updated
        return {"source": self.owner, "age_ms": round(age * 1000),
                "stale": age > STALE_S, "cars": self.cars}

    def _check_owner(self, owner: str) -> None:
        if self.owner is not None and self.owner != owner:
            raise TrackError(f"track already provided by \"{self.owner}\" (it must call track.clear "
                             "or disconnect)")
