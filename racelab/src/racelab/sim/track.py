"""Virtual track: centerline (closed Catmull-Rom spline) and two lanes.

Frame: meters, y axis up, counter-clockwise racing direction. Lane 0 is
offset to the left of the centerline (inner side: shorter, but tighter in
left-hand corners), lane 1 to the right. Sample 0 is the finish line; a car's
"physical" position is the lap fraction along the centerline (independent of
the lane).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache

LANE_OFFSET = 0.06  # m, offset of each lane from the centerline
LAP_LENGTH = 18.0  # m, length of lane 0
A_MAX = 20.0  # m/s², max lateral acceleration before breaking away
N_SAMPLES = 3600  # centerline samples (≈ 5 mm)
GEOMETRY_POINTS = 200  # points returned to the UI
SMOOTH_M = 0.10  # half-window for curvature smoothing (m)
V_C_MAX = 99.0  # critical speed of a straight

# Lap fractions (centerline), all on straights.
CHANGERS = (0.385, 0.60)
PIT = (0.87, 0.97)


def _arc(cx: float, cy: float, r: float, a0: float, a1: float, n: int) -> list[tuple[float, float]]:
    """Points of a circular arc (angles in degrees, n segments)."""
    angles = (math.radians(a0 + (a1 - a0) * k / n) for k in range(n + 1))
    return [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles]


def _control_points() -> list[tuple[float, float]]:
    """Layout in m, counter-clockwise; the first point is the finish line.

    Pit straight at the bottom, big left-hand curve, an S, a right-hand
    hairpin, a second tight curve, the top straight, then the return along
    the left side.
    """
    pts = [(3.55, 0.0)]
    pts += _arc(3.8, 0.65, 0.65, -90, 90, 6)  # big left-hand curve (180°)
    pts += [(3.3, 1.35), (2.85, 1.12), (2.4, 1.08)]  # S
    pts += _arc(1.45, 1.46, 0.38, -90, -270, 6)  # right-hand hairpin
    pts += [(2.1, 1.84), (2.8, 1.84)]
    pts += _arc(3.45, 2.3, 0.46, -90, 90, 4)  # tight left-hand curve
    pts += [(2.6, 2.76), (1.6, 2.76)]
    pts += _arc(0.7, 2.1, 0.66, 90, 180, 3)
    pts += [(0.04, 1.4)]
    pts += _arc(0.7, 0.66, 0.66, 180, 270, 3)
    pts += [(1.6, 0.0)]
    return pts


def _catmull_rom(points: list[tuple[float, float]], per_segment: int = 400) -> list[tuple[float, float]]:
    """Closed centripetal Catmull-Rom spline (no loop or cusp)."""
    n = len(points)
    out: list[tuple[float, float]] = []

    def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1]) ** 0.5

    for i in range(n):
        p0, p1, p2, p3 = (points[(i + k) % n] for k in (-1, 0, 1, 2))
        t0 = 0.0
        t1 = t0 + dist(p0, p1)
        t2 = t1 + dist(p1, p2)
        t3 = t2 + dist(p2, p3)
        for j in range(per_segment):
            t = t1 + (t2 - t1) * j / per_segment

            def mix(a, b, ta, tb):
                u = (t - ta) / (tb - ta)
                return (a[0] * (1 - u) + b[0] * u, a[1] * (1 - u) + b[1] * u)

            a1, a2, a3 = mix(p0, p1, t0, t1), mix(p1, p2, t1, t2), mix(p2, p3, t2, t3)
            out.append(mix(mix(a1, a2, t0, t2), mix(a2, a3, t1, t3), t1, t2))
    return out


def _resample(points: list[tuple[float, float]], n: int) -> tuple[list[float], list[float], float]:
    """Resample a closed curve into n equidistant points (arc length); returns x, y, length."""
    m = len(points)
    cum = [0.0]
    for i in range(m):
        a, b = points[i], points[(i + 1) % m]
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = cum[-1]
    xs: list[float] = []
    ys: list[float] = []
    j = 0
    for k in range(n):
        target = total * k / n
        while cum[j + 1] < target:
            j += 1
        a, b = points[j], points[(j + 1) % m]
        f = (target - cum[j]) / (cum[j + 1] - cum[j])
        xs.append(a[0] + f * (b[0] - a[0]))
        ys.append(a[1] + f * (b[1] - a[1]))
    return xs, ys, total


def _smooth(values: list[float], half: int) -> list[float]:
    """Cyclic moving average with half-window `half` (samples)."""
    n = len(values)
    ext = values[-half:] + values + values[:half]
    prefix = [0.0]
    for v in ext:
        prefix.append(prefix[-1] + v)
    width = 2 * half + 1
    return [(prefix[i + width] - prefix[i]) / width for i in range(n)]


@dataclass
class Lane:
    """A lane: samples indexed like those of the centerline."""

    length: float  # m
    s: list[float]  # arc length at point i (n+1 values, s[n] = length)
    vc: list[float]  # critical speed at point i (n values)
    changer_s: list[float]  # arc length of the lane changers
    pit_in: float  # arc lengths of the pit lane zone
    pit_stop: float
    pit_out: float


class Track:
    """Precomputed geometry: two lanes, lane changers, pit lane zone."""

    def __init__(self) -> None:
        n = N_SAMPLES
        raw = _control_points()
        xs, ys, length = _resample(_catmull_rom(raw), n)
        # scaling: lane 0 (inner, total turning of 2π) measures LAP_LENGTH
        k = (LAP_LENGTH + 2 * math.pi * LANE_OFFSET) / length
        xs, ys, h = [x * k for x in xs], [y * k for y in ys], length * k / n

        tx, ty = [], []
        for i in range(n):
            dx, dy = xs[(i + 1) % n] - xs[i - 1], ys[(i + 1) % n] - ys[i - 1]
            m = math.hypot(dx, dy)
            tx.append(dx / m)
            ty.append(dy / m)
        kappa = []
        for i in range(n):
            a = math.atan2(ty[i - 1], tx[i - 1])
            b = math.atan2(ty[(i + 1) % n], tx[(i + 1) % n])
            kappa.append(((b - a + math.pi) % (2 * math.pi) - math.pi) / (2 * h))
        kappa = _smooth(kappa, round(SMOOTH_M / h))

        self.n = n
        self.changers = CHANGERS
        self.pit = PIT
        self.lanes = tuple(
            self._lane(xs, ys, tx, ty, kappa, offset) for offset in (LANE_OFFSET, -LANE_OFFSET)
        )
        self._xs, self._ys = xs, ys

    def _lane(self, xs, ys, tx, ty, kappa, offset: float) -> Lane:
        n = self.n
        lx = [xs[i] - offset * ty[i] for i in range(n)]  # left normal = (-ty, tx)
        ly = [ys[i] + offset * tx[i] for i in range(n)]
        s = [0.0]
        for i in range(n):
            s.append(s[-1] + math.hypot(lx[(i + 1) % n] - lx[i], ly[(i + 1) % n] - ly[i]))
        vc = []
        for k in kappa:
            k_lane = abs(k / (1 - offset * k))  # curvature of the offset line
            vc.append(min(V_C_MAX, math.sqrt(A_MAX / k_lane)) if k_lane > 1e-6 else V_C_MAX)
        lane = Lane(s[-1], s, vc, [], 0.0, 0.0, 0.0)

        def at(u: float) -> float:
            i = int(u * n)
            return s[i] + (u * n - i) * (s[i + 1] - s[i])

        lane.changer_s = [at(u) for u in CHANGERS]
        lane.pit_in, lane.pit_out = at(PIT[0]), at(PIT[1])
        lane.pit_stop = at((PIT[0] + PIT[1]) / 2)
        return lane

    def locate(self, lane: int, s: float) -> int:
        """Index of the lane sample just before arc length `s`."""
        return min(bisect_right(self.lanes[lane].s, s) - 1, self.n - 1)

    def fraction(self, lane: int, s: float) -> float:
        """Physical position (centerline lap fraction) of the lane's arc length `s`."""
        ln = self.lanes[lane]
        i = self.locate(lane, s)
        return (i + (s - ln.s[i]) / (ln.s[i + 1] - ln.s[i])) / self.n

    def arc(self, lane: int, u: float) -> float:
        """Arc length on the lane of the physical position `u` (lap fraction)."""
        ln = self.lanes[lane]
        i = int(u * self.n) % self.n
        return ln.s[i] + (u * self.n - i) * (ln.s[i + 1] - ln.s[i])

    def geometry(self) -> dict:
        """Drawing for the UI: centerline normalized into [0, 1]²."""
        xs, ys = self._xs, self._ys
        x0, y0 = min(xs), min(ys)
        size = max(max(xs) - x0, max(ys) - y0)
        step = self.n // GEOMETRY_POINTS
        return {
            "length_m": [round(ln.length, 3) for ln in self.lanes],
            "points": [[round((xs[i] - x0) / size, 4), round((ys[i] - y0) / size, 4)]
                       for i in range(0, self.n, step)],
            "lane_offset": round(LANE_OFFSET / size, 5),
            "changers": list(self.changers),
            "pit": list(self.pit),
            "finish": 0.0,
        }


@lru_cache(maxsize=1)
def default_track() -> Track:
    """The track (immutable, built once)."""
    return Track()
