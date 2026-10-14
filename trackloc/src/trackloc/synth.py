"""Synthetic camera sessions with ground truth: the test bench of the vision connector.

A small closed circuit (two lanes, lane changer, optional Check Lane), seen through an
OBLIQUE pinhole camera (elevation, yaw, optional barrel distortion), with:

- a table texture, a dark track band with slots and rails, a finish line and a printed
  ArUco marker (DICT_4X4_50, id 0) lying flat on the table;
- cars (boxes), motion-blurred along their motion, driven by the motor constants of
  `racelab.sim.physics` BUT with a different true gain, a different time constant,
  speed limits in the corners and a motor that heats up: the tracker's model is NOT the
  truth (otherwise bot prediction would be perfect by construction);
- a "bridge" that hides about 0.5 m of track, a "hand" that sweeps over the track for
  1 s, sensor noise, global brightness drift;
- camera latency (frames reach the host later than their exposure) and BLE-like jitter on
  the CU events, which carry `cu_ts` on a CU clock with an unknown offset.

`Session.stream()` yields what the live pipeline would receive, in arrival order:
`(t_arrival, "frame" | "event" | "state", payload)`, all on one host monotonic clock.
Needs numpy and OpenCV (`pip install racelab[vision]`).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from racelab.sim.physics import TAU_ACC, TAU_BRAKE, VMAX

from .geometry import ARUCO_DICT, MARKER_ID

T0 = 5000.0  # host monotonic time of the start of a session (s)
CU_EPOCH = 1234.5  # the CU clock reads host_time - CU_EPOCH (unknown to the pipeline)
A_MAX = 14.0  # m/s², lateral acceleration the synthetic cars accept before slowing down
CAR_COLORS = [(40, 40, 215), (215, 120, 30), (30, 205, 225), (70, 190, 70), (200, 60, 170), (225, 225, 225)]

CONTROL = [(0.0, 0.0), (0.9, -0.05), (1.7, 0.0), (2.1, 0.3), (2.0, 0.8), (1.5, 1.0), (1.05, 0.72),
           (0.6, 0.95), (0.1, 0.9), (-0.25, 0.55), (-0.2, 0.15)]


# ----------------------------------------------------------------------------- geometry

def _catmull_rom(pts: np.ndarray, per: int = 300) -> np.ndarray:
    n = len(pts)
    out = []
    for i in range(n):
        p0, p1, p2, p3 = (pts[(i + k) % n] for k in (-1, 0, 1, 2))
        for j in range(per):
            t = j / per
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3))
    return np.array(out)


def _resample(points: np.ndarray, n: int) -> np.ndarray:
    closed = np.vstack([points, points[:1]])
    seg = np.hypot(*np.diff(closed, axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    t = np.linspace(0, cum[-1], n, endpoint=False)
    return np.column_stack([np.interp(t, cum, closed[:, 0]), np.interp(t, cum, closed[:, 1])])


class Circuit:
    """Centre line C(u) (u = fraction of its length, 0 = finish line), two lanes, lengths."""

    N = 4000

    def __init__(self, pitch: float = 0.10, scale: float = 1.0, changer: tuple[float, float] = (0.30, 0.34),
                 check_lane_u: float | None = None) -> None:
        self.pitch = pitch
        c = _resample(_catmull_rom(np.array(CONTROL) * scale), self.N)
        self.c = c
        d = np.roll(c, -1, axis=0) - np.roll(c, 1, axis=0)
        self.t = d / np.hypot(*d.T)[:, None]
        self.n_left = np.column_stack([-self.t[:, 1], self.t[:, 0]])  # y up: left of the driving direction
        seg = np.hypot(*(np.roll(c, -1, axis=0) - c).T)
        self.length = float(seg.sum())
        ang = np.unwrap(np.arctan2(self.t[:, 1], self.t[:, 0]))
        self.kappa = np.gradient(ang) / np.maximum(seg, 1e-9)
        self.kappa = np.convolve(np.r_[self.kappa[-20:], self.kappa, self.kappa[:20]], np.ones(41) / 41, "valid")[:self.N]
        self.lane_pts = [c + sgn * pitch / 2 * self.n_left for sgn in (1.0, -1.0)]  # lane 0 = left
        self.lane_s = []
        for p in self.lane_pts:
            s = np.hypot(*(np.roll(p, -1, axis=0) - p).T)
            self.lane_s.append(np.concatenate([[0.0], np.cumsum(s)]))
        self.lane_len = [float(s[-1]) for s in self.lane_s]
        self.changer = changer  # u0, u1 of the lane changer (lateral blend)
        self.check_lane_u = check_lane_u

    def u_of_s(self, lane: int, s: float) -> float:
        s = s % self.lane_len[lane]
        return float(np.interp(s, self.lane_s[lane], np.arange(self.N + 1) / self.N))

    def s_of_u(self, lane: int, u: float) -> float:
        return float(np.interp(u % 1.0, np.arange(self.N + 1) / self.N, self.lane_s[lane]))

    def point(self, u: float, offset: float) -> tuple[np.ndarray, float]:
        """World position and heading of centre-line fraction u with a lateral offset (left +)."""
        f = (u % 1.0) * self.N
        i, a = int(f) % self.N, f - int(f)
        j = (i + 1) % self.N
        c = self.c[i] * (1 - a) + self.c[j] * a
        nl = self.n_left[i] * (1 - a) + self.n_left[j] * a
        t = self.t[i] * (1 - a) + self.t[j] * a
        return c + offset * nl, math.atan2(t[1], t[0])

    def vcrit(self, u: float) -> float:
        k = abs(self.kappa[int((u % 1.0) * self.N) % self.N])
        return math.sqrt(A_MAX / k) if k > 1e-3 else 99.0

    def lateral(self, lane: int, u: float, to_lane: int | None = None) -> float:
        """Rendered lateral offset: smooth blend across the changer when switching lane."""
        base = self.pitch / 2 if lane == 0 else -self.pitch / 2
        if to_lane is None or to_lane == lane:
            return base
        u0, u1 = self.changer
        x = min(1.0, max(0.0, ((u % 1.0) - u0) / (u1 - u0)))
        x = x * x * (3 - 2 * x)
        other = self.pitch / 2 if to_lane == 0 else -self.pitch / 2
        return base + (other - base) * x


# ----------------------------------------------------------------------------- camera

@dataclass
class CameraSpec:
    width: int = 800
    height: int = 450
    hfov_deg: float = 70.0
    elevation_deg: float = 55.0
    yaw_deg: float = 25.0
    distance: float = 2.5  # camera to the look-at point (m)
    target: tuple[float, float] = (0.95, 0.45)
    k1: float = 0.0  # radial distortion (negative = barrel)
    fps: float = 30.0
    exposure_s: float = 1 / 120

    def __post_init__(self) -> None:
        el, yaw = math.radians(self.elevation_deg), math.radians(self.yaw_deg)
        dh = np.array([math.sin(yaw), math.cos(yaw)])
        tgt = np.array([*self.target, 0.0])
        self.center = tgt - self.distance * np.array([math.cos(el) * dh[0], math.cos(el) * dh[1], -math.sin(el)])
        f = (tgt - self.center) / self.distance
        right = np.array([f[1], -f[0], 0.0])
        right /= np.linalg.norm(right)
        down = np.cross(f, right)
        self.R = np.vstack([right, down, f])
        self.fx = (self.width / 2) / math.tan(math.radians(self.hfov_deg) / 2)
        self.cx, self.cy = self.width / 2, self.height / 2

    def project(self, pts: np.ndarray) -> np.ndarray:
        """World points (N, 3) -> pixels (N, 2) (with the radial distortion)."""
        pc = (np.asarray(pts, float) - self.center) @ self.R.T
        a, b = pc[:, 0] / pc[:, 2], pc[:, 1] / pc[:, 2]
        k = 1 + self.k1 * (a * a + b * b)
        return np.column_stack([self.fx * a * k + self.cx, self.fx * b * k + self.cy])

    def ground_maps(self, res: float, xmin: float, ymax: float) -> tuple[np.ndarray, np.ndarray]:
        """For every pixel, the (col, row) of the ground texture it sees (for cv2.remap)."""
        u, v = np.meshgrid(np.arange(self.width, dtype=np.float64), np.arange(self.height, dtype=np.float64))
        a, b = (u - self.cx) / self.fx, (v - self.cy) / self.fx
        ua, ub = a.copy(), b.copy()
        for _ in range(8):  # undistort by fixed point
            k = 1 + self.k1 * (ua * ua + ub * ub)
            ua, ub = a / k, b / k
        d = np.stack([ua, ub, np.ones_like(ua)], axis=-1) @ self.R  # camera ray in world
        lam = -self.center[2] / d[..., 2]
        gx, gy = self.center[0] + d[..., 0] * lam, self.center[1] + d[..., 1] * lam
        return ((gx - xmin) / res).astype(np.float32), ((ymax - gy) / res).astype(np.float32)


# ----------------------------------------------------------------------------- scenario

@dataclass
class SynthCar:
    slot: int
    kind: str = "bot"  # "bot": throttle program known to the CU state; "human": throttle by the corner, with
    # noise; "erratic": white random throttle (stress test)
    lane: int = 0
    u0: float = 0.0
    program: list[tuple[float, int]] = field(default_factory=lambda: [(0.5, 9)])  # (t, throttle)
    gain: float = 0.7  # TRUE motor gain vs sim.physics.VMAX (the tracker assumes 1.0 until it learns)
    tau_factor: float = 1.15
    heating: float = 0.08  # gain lost per minute of running


@dataclass
class Scenario:
    cars: list[SynthCar]
    duration: float = 30.0
    seed: int = 1  # physics, noise, events
    scene_seed: int = 7  # the static scene (table texture): the same across the sessions of one setup
    camera: CameraSpec = field(default_factory=CameraSpec)
    pitch: float = 0.10
    marker_size: float = 0.15
    marker_xy: tuple[float, float] = (0.85, 0.36)
    marker_deg: float = 12.0
    bridge: bool = True
    bridge_u: float = 0.62
    bridge_len: float = 0.5
    hands: list[float] = field(default_factory=list)  # start times of 1 s hand sweeps
    hand_u: float = 0.80
    noise: float = 2.5
    drift: float = 0.05  # brightness: relative amplitude of the slow drift
    latency_s: float = 0.060  # extra camera delay on top of half a frame (unknown to the pipeline)
    event_delay: tuple[float, float] = (0.015, 0.090)  # BLE delay of a CU event (s)
    cmd_delay: float = 0.075  # one rail cycle between command and applied throttle
    check_lane_u: float | None = None
    changes: list[tuple[int, float]] = field(default_factory=list)  # (slot, t): lane change requested
    off_events: list[tuple[int, float, float]] = field(default_factory=list)  # (slot, t0, t1)
    empty_prefix_s: float = 0.0  # the session starts with this long an empty track (no cars drawn)
    fps_jitter_s: float = 0.0015


class Session:
    """Runs a `Scenario`: physics at 240 Hz, then renders frames lazily (deterministically)."""

    DT = 1 / 240

    def __init__(self, sc: Scenario) -> None:
        self.sc = sc
        self.cam = sc.camera
        self.circuit = Circuit(pitch=sc.pitch, check_lane_u=sc.check_lane_u)
        self.rng = np.random.default_rng(sc.seed)
        self._simulate()
        self._build_texture()
        h, w = self.cam.height, self.cam.width
        self._noise_pool = [np.round(self.rng.normal(0, sc.noise, (2 * h, 2 * w, 1))).astype(np.int16)
                            for _ in range(4)]

    # ----------------------------------------------------------------- physics

    def _simulate(self) -> None:
        sc, cir = self.sc, self.circuit
        n = int(sc.duration / self.DT) + 1
        self.t = np.arange(n) * self.DT
        self.state: dict[int, dict[str, np.ndarray]] = {}
        self.cross: list[tuple[float, int, int]] = []  # (t_true, slot, sector)
        for car in sc.cars:
            rng = np.random.default_rng(sc.seed * 100 + car.slot)
            thr_cmd = np.zeros(n)
            if car.kind in ("human", "erratic"):
                t, thr, lane_now = 0.5, 0, car.lane
                for i in range(n):
                    if self.t[i] >= t:
                        if car.kind == "erratic":
                            thr = int(rng.integers(7, 14))
                            t += rng.exponential(0.9)
                        else:  # lift in the corners, flat on the straights, a little different every lap
                            u_now = cir.u_of_s(lane_now, cir.s_of_u(lane_now, car.u0) + 1.4 * (self.t[i] - 0.5))
                            thr = int(np.clip(round(12.5 - 6.0 * min(1.0, abs(cir.kappa[int(u_now * cir.N) % cir.N]) / 3.0)
                                                   + rng.normal(0, 0.8)), 5, 14))
                            t += 0.25
                    thr_cmd[i] = thr if self.t[i] >= 0.5 else 0
            else:
                for i, ti in enumerate(self.t):
                    prog = [p for p in car.program if p[0] <= ti]
                    thr_cmd[i] = prog[-1][1] if prog else 0
            delay = int(round(sc.cmd_delay / self.DT))
            thr = np.concatenate([np.zeros(delay), thr_cmd[:n - delay]])  # throttle actually applied
            lane, s = car.lane, cir.s_of_u(car.lane, car.u0)
            v, to_lane, off_until = 0.0, None, -1.0
            u_a, v_a, lane_a, off_a, lat_a, offflag = (np.zeros(n) for _ in range(6))
            lane_a = lane_a.astype(int)
            arm = sorted(t for sl, t in sc.changes if sl == car.slot)
            armed, off_state = False, None
            u_prev = cir.u_of_s(lane, s)
            for i in range(n):
                ti = self.t[i]
                while arm and ti >= arm[0]:
                    arm.pop(0)
                    armed = True
                for s_, t0, t1 in sc.off_events:
                    if s_ == car.slot and t0 <= ti < t1:
                        if off_state is None:
                            off_state = (cir.u_of_s(lane, s), lane)
                    elif s_ == car.slot and ti >= t1 and off_state is not None:
                        off_state = None
                        v = 0.0
                u = off_state[0] if off_state else cir.u_of_s(lane, s)
                if off_state is None:
                    gain = car.gain * (1 - car.heating * ti / 60)
                    target = min(gain * VMAX * thr[i] / 15, 0.85 * cir.vcrit(u))
                    tau = (TAU_ACC if target >= v else TAU_BRAKE) * car.tau_factor
                    v += (target - v) * min(1.0, self.DT / tau)
                    s += v * self.DT
                    if s >= cir.lane_len[lane]:
                        s -= cir.lane_len[lane]
                    u = cir.u_of_s(lane, s)
                    if u < u_prev - 0.5:  # crossed the finish line during this step
                        frac = (s / max(v * self.DT, 1e-9))
                        self.cross.append((ti - frac * self.DT + self.DT, car.slot, 1))
                    if cir.check_lane_u is not None and u_prev < cir.check_lane_u <= u:
                        self.cross.append((ti, car.slot, 2))
                    u0_, u1_ = cir.changer
                    if armed and to_lane is None and u_prev < u0_ <= u:
                        to_lane, armed = 1 - lane, False
                    if to_lane is not None and u >= (u0_ + u1_) / 2 and u_prev < (u0_ + u1_) / 2 + 0.2:
                        lane = to_lane
                        s = cir.s_of_u(lane, u)
                    if to_lane is not None and u >= u1_:
                        to_lane = None
                    u_prev = u
                lat = cir.lateral(lane if to_lane is None else (1 - to_lane), u, to_lane) if to_lane is not None \
                    else cir.lateral(lane, u)
                if off_state is not None:
                    lat = (cir.pitch / 2 if off_state[1] == 0 else -cir.pitch / 2) + (0.14 if off_state[1] == 0 else -0.14)
                u_a[i], v_a[i], lane_a[i], lat_a[i], offflag[i] = u, (0.0 if off_state else v), lane, lat, bool(off_state)
            self.state[car.slot] = {"u": u_a, "v": v_a, "lane": lane_a, "lat": lat_a, "off": offflag,
                                    "thr": thr, "thr_cmd": thr_cmd}
        self.cross.sort()

    def at(self, slot: int, t: float) -> dict[str, Any]:
        """Ground truth of a car at time `t` (s since the start of the session)."""
        st = self.state[slot]
        i = min(int(t / self.DT), len(self.t) - 1)
        u = float(st["u"][i])
        L = self.circuit.length
        return {"u": u, "x": u * L, "lane": int(st["lane"][i]), "v": float(st["v"][i]),
                "off": bool(st["off"][i]), "thr": int(st["thr"][i])}

    # ----------------------------------------------------------------- rendering

    RES = 0.002  # m per texture pixel

    def _build_texture(self) -> None:
        sc, cir, cam = self.sc, self.circuit, self.cam
        xs = [-1.0, 2.9]
        ys = [-1.1, 1.7]
        self.xmin, self.ymax = xs[0], ys[1]
        W, H = int((xs[1] - xs[0]) / self.RES), int((ys[1] - ys[0]) / self.RES)
        rng = np.random.default_rng(sc.scene_seed)
        base = np.array([150, 165, 178], np.float32)
        low = cv2.resize(rng.normal(0, 1, (H // 120 + 2, W // 120 + 2)).astype(np.float32), (W, H), interpolation=cv2.INTER_CUBIC)
        fine = rng.normal(0, 1, (H, W)).astype(np.float32)
        fine = cv2.GaussianBlur(fine, (0, 0), 1.5)
        tex = np.clip(base[None, None, :] + (low * 14 + fine * 40)[:, :, None], 0, 255).astype(np.uint8)
        for _ in range(60):  # grain lines and spots: texture for the ECC registration
            p = rng.integers(0, [W, H], 2)
            q = p + rng.integers(-150, 150, 2)
            cv2.line(tex, tuple(int(v) for v in p), tuple(int(v) for v in q), tuple(int(v) for v in rng.integers(90, 210, 3)), 2)
        for _ in range(40):
            cv2.circle(tex, tuple(int(v) for v in rng.integers(0, [W, H], 2)), int(rng.integers(4, 14)),
                       tuple(int(v) for v in rng.integers(60, 230, 3)), -1)

        def px(pts: np.ndarray) -> np.ndarray:
            return np.round(np.column_stack([(pts[:, 0] - self.xmin) / self.RES,
                                             (self.ymax - pts[:, 1]) / self.RES])).astype(np.int32)

        half = cir.pitch / 2 + 0.04
        cv2.polylines(tex, [px(cir.c)], True, (58, 56, 60), int(2 * half / self.RES), cv2.LINE_AA)
        for sgn in (1.0, -1.0):
            cv2.polylines(tex, [px(cir.c + sgn * half * cir.n_left)], True, (215, 215, 215), 4, cv2.LINE_AA)
        for lane_pts in cir.lane_pts:
            cv2.polylines(tex, [px(lane_pts)], True, (30, 30, 32), 7, cv2.LINE_AA)
            for off in (-0.006, 0.006):
                cv2.polylines(tex, [px(lane_pts + off * cir.n_left)], True, (80, 130, 165), 2, cv2.LINE_AA)
        # finish line (and Check Lane) across the track
        marks = [0.0] + ([cir.check_lane_u] if cir.check_lane_u is not None else [])
        for u in marks:
            c, _ = cir.point(u, 0.0)
            nl = cir.n_left[int(u * cir.N) % cir.N]
            tg = cir.t[int(u * cir.N) % cir.N]
            quad = np.array([c + half * nl - 0.012 * tg, c + half * nl + 0.012 * tg,
                             c - half * nl + 0.012 * tg, c - half * nl - 0.012 * tg])
            cv2.fillConvexPoly(tex, px(quad), (245, 245, 245) if u == 0.0 else (60, 220, 240))
        # printed marker, flat on the table (viewed from above it reads like the image)
        side = int(round(sc.marker_size / self.RES))
        d = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
        marker = cv2.aruco.generateImageMarker(d, MARKER_ID, side)
        pad = int(0.025 / self.RES)
        sheet = np.full((side + 2 * pad, side + 2 * pad), 255, np.uint8)
        sheet[pad:pad + side, pad:pad + side] = marker
        sheet = cv2.cvtColor(sheet, cv2.COLOR_GRAY2BGR)
        cx, cy = (sc.marker_xy[0] - self.xmin) / self.RES, (self.ymax - sc.marker_xy[1]) / self.RES
        M = cv2.getRotationMatrix2D((sheet.shape[1] / 2, sheet.shape[0] / 2), sc.marker_deg, 1.0)
        M[0, 2] += cx - sheet.shape[1] / 2
        M[1, 2] += cy - sheet.shape[0] / 2
        cv2.warpAffine(sheet, M, (W, H), dst=tex, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_TRANSPARENT)
        self.marker_center = np.array(sc.marker_xy)
        mapx, mapy = cam.ground_maps(self.RES, self.xmin, self.ymax)
        self.static = cv2.remap(tex, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
        self.bridge_hull = None
        if sc.bridge:
            self._bridge()

    def _box(self, center: np.ndarray, heading: float, length: float, width: float, h0: float, h1: float) -> np.ndarray:
        c, s = math.cos(heading), math.sin(heading)
        pts = []
        for z in (h0, h1):
            for dx, dy in ((1, 1), (1, -1), (-1, -1), (-1, 1)):
                lx, ly = dx * length / 2, dy * width / 2
                pts.append([center[0] + c * lx - s * ly, center[1] + s * lx + c * ly, z])
        return np.array(pts)

    def _bridge(self) -> None:
        sc, cir = self.sc, self.circuit
        u0 = sc.bridge_u
        u1 = u0 + sc.bridge_len / cir.length
        hulls = []
        for k in range(6):  # a few boxes along the curve: follows the track
            uu = u0 + (u1 - u0) * (k + 0.5) / 6
            p, hd = cir.point(uu, 0.0)
            box = self._box(p, hd, sc.bridge_len / 6 * 1.25, cir.pitch + 0.10, 0.0, 0.075)
            hulls.append(box)
        mask = np.zeros((self.cam.height, self.cam.width), np.uint8)
        layer = self.static.copy()
        for k, box in enumerate(hulls):
            pix = self.cam.project(box).astype(np.float32)
            hull = cv2.convexHull(pix).astype(np.int32)
            cv2.fillConvexPoly(mask, hull, 255)
            cv2.fillConvexPoly(layer, hull, (60 + 6 * k, 85 + 5 * k, 130 + 4 * k))
        for k, box in enumerate(hulls):  # roof edge lines
            top = self.cam.project(box[4:]).astype(np.int32)
            cv2.polylines(layer, [top], True, (40, 55, 95), 1, cv2.LINE_AA)
        ys, xs = np.nonzero(mask)
        self.bridge_roi = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
        self.bridge_mask = (mask > 0)[self.bridge_roi][:, :, None]
        self.static = np.where((mask > 0)[:, :, None], layer, self.static)

    def _bridge_overlay(self, img: np.ndarray) -> None:
        if self.sc.bridge:
            np.copyto(img[self.bridge_roi], self.static[self.bridge_roi], where=self.bridge_mask)

    def _draw_car(self, img: np.ndarray, slot: int, t: float) -> None:
        sc, cir, cam = self.sc, self.circuit, self.cam
        n_sub = 5
        st = self.state[slot]
        boxes = []
        for k in range(n_sub):
            tk = t + (k / (n_sub - 1) - 0.5) * cam.exposure_s
            i = min(max(int(tk / self.DT), 0), len(self.t) - 1)
            p, hd = cir.point(float(st["u"][i]), float(st["lat"][i]))
            boxes.append(cam.project(self._box(p, hd, 0.15, 0.065, 0.0, 0.032)).astype(np.float32))
        allp = np.vstack(boxes)
        x0, y0 = np.maximum(np.floor(allp.min(0)).astype(int) - 2, 0)
        x1, y1 = np.minimum(np.ceil(allp.max(0)).astype(int) + 3, [cam.width, cam.height])
        if x1 <= x0 or y1 <= y0:
            return
        h, w = y1 - y0, x1 - x0
        off = np.array([x0, y0], np.float32)
        acc = np.zeros((h, w, 3), np.float32)
        alpha = np.zeros((h, w), np.float32)
        color = np.array(CAR_COLORS[slot % len(CAR_COLORS)], np.float32)
        for pix in boxes:
            mask = np.zeros((h, w), np.uint8)
            cv2.fillConvexPoly(mask, cv2.convexHull(pix - off).astype(np.int32), 255)
            top = np.zeros((h, w), np.uint8)
            cv2.fillConvexPoly(top, cv2.convexHull(pix[4:] - off).astype(np.int32), 255)
            m = mask.astype(np.float32) / 255
            acc += (color[None, None, :] * (0.72 + 0.28 * top.astype(np.float32)[:, :, None] / 255)) * m[:, :, None]
            alpha += m
        alpha /= n_sub
        acc /= n_sub
        roi = img[y0:y1, x0:x1]
        roi[:] = np.clip(roi * (1 - alpha[:, :, None]) + acc, 0, 255).astype(np.uint8)

    def _hand(self, img: np.ndarray, t: float) -> None:
        sc, cir, cam = self.sc, self.circuit, self.cam
        for h0 in sc.hands:
            x = t - h0
            if not 0.0 <= x <= 1.0:
                continue
            c, hd = cir.point(sc.hand_u, 0.0)
            nl = cir.n_left[int(sc.hand_u * cir.N) % cir.N]
            tg = cir.t[int(sc.hand_u * cir.N) % cir.N]
            way = [(0.0, c + 0.55 * nl), (0.25, c + 0.02 * nl), (0.75, c - 0.02 * nl + 0.10 * tg), (1.0, c - 0.55 * nl)]
            for (ta, pa), (tb, pb) in zip(way, way[1:]):
                if ta <= x <= tb:
                    p = pa + (pb - pa) * (x - ta) / (tb - ta)
                    break
            ang = np.linspace(0, 2 * math.pi, 28, endpoint=False)
            ell = np.column_stack([p[0] + 0.095 * np.cos(ang) * math.cos(hd) - 0.05 * np.sin(ang) * math.sin(hd),
                                   p[1] + 0.095 * np.cos(ang) * math.sin(hd) + 0.05 * np.sin(ang) * math.cos(hd),
                                   np.full(28, 0.07)])
            pix = cam.project(ell).astype(np.int32)
            cv2.fillConvexPoly(img, pix, (118, 152, 215), cv2.LINE_AA)

    def render(self, k: int) -> tuple[float, np.ndarray]:
        """Frame `k`: (scene time since the start, BGR image). Deterministic."""
        sc, cam = self.sc, self.cam
        t = k / cam.fps
        img = self.static.copy()
        if t >= sc.empty_prefix_s:
            for slot in self.state:
                self._draw_car(img, slot, t)
        self._hand(img, t)
        self._bridge_overlay(img)
        gain = 1 + sc.drift * math.sin(2 * math.pi * t / 37.0) - 0.0006 * t
        rng = np.random.default_rng(sc.seed * 7919 + k)
        pool = self._noise_pool[int(rng.integers(len(self._noise_pool)))]
        oy, ox = int(rng.integers(0, cam.height)), int(rng.integers(0, cam.width))
        noise = pool[oy:oy + cam.height, ox:ox + cam.width]
        out = cv2.convertScaleAbs(img, alpha=gain).astype(np.int16)
        out += noise
        return t, np.clip(out, 0, 255).astype(np.uint8)

    # ----------------------------------------------------------------- streams

    @property
    def n_frames(self) -> int:
        return int((self.sc.duration - 0.5) * self.cam.fps)

    def frame_arrival(self, k: int) -> float:
        """Host time at which frame k reaches the pipeline (exposure + half a frame + latency + jitter)."""
        j = np.random.default_rng(self.sc.seed * 31 + k).normal(0, self.sc.fps_jitter_s)
        return T0 + k / self.cam.fps + 0.5 / self.cam.fps + self.sc.latency_s + float(j)

    def inputs(self) -> list[dict[str, Any]]:
        """The input stream of the connector (plain dicts on the host clock), sorted by time:
        CU crossings (`cu_ts` on the CU clock, `t` = when RaceLab reports them, BLE delay and jitter)
        and the throttle applied to the bots (on every change)."""
        sc = self.sc
        rng = np.random.default_rng(sc.seed + 99)
        out: list[dict[str, Any]] = []
        for t_true, slot, sector in self.cross:
            host = T0 + t_true
            cu_ts = int(round((host - CU_EPOCH) * 1000)) % (1 << 32)
            out.append({"type": "crossing", "t": host + float(rng.uniform(*sc.event_delay)), "slot": slot,
                        "sector": sector, "cu_ts": cu_ts})
        rng = np.random.default_rng(sc.seed + 5)
        last: dict[int, int | None] = {}
        for i in range(int(sc.duration * 20)):
            t = i / 20
            idx = min(int(t / self.DT), len(self.t) - 1)
            for car in sc.cars:
                applied = int(self.state[car.slot]["thr"][idx]) if car.kind == "bot" else None
                if last.get(car.slot, -1) != applied and (applied is not None or car.slot in last):
                    out.append({"type": "throttle", "t": T0 + t + float(rng.uniform(0.002, 0.02)),
                                "slot": car.slot, "applied": applied})
                last[car.slot] = applied
        return sorted(out, key=lambda m: m["t"])

    def frames(self, start: int = 0, stop: int | None = None) -> Iterator[tuple[float, np.ndarray]]:
        """The frame source: `(t, image)`, t = host time of the middle of the exposure as a capture
        would stamp it (arrival minus half a frame; the rest of the camera latency is NOT removed)."""
        stop = self.n_frames if stop is None else min(stop, self.n_frames)
        for k in range(start, stop):
            yield self.frame_arrival(k) - 0.5 / self.cam.fps, self.render(k)[1]

    def stream(self, start: int = 0, stop: int | None = None) -> Iterator[tuple[str, Any]]:
        """Frames and inputs merged in arrival order: ("frame", (t, image)) / ("input", dict)."""
        pending = iter(self.inputs())
        nxt = next(pending, None)
        for t_img, img in self.frames(start, stop):
            ta = t_img + 0.5 / self.cam.fps
            while nxt is not None and nxt["t"] <= ta:
                yield "input", nxt
                nxt = next(pending, None)
            yield "frame", (t_img, img)
        while nxt is not None:
            yield "input", nxt
            nxt = next(pending, None)

    # ----------------------------------------------------------------- truth helpers

    def truth_x(self, slot: int, host_t: float) -> dict[str, Any]:
        """Truth at a host time (what a perfect tracker would publish then)."""
        return self.at(slot, host_t - T0)

    def marker_corners_world(self) -> np.ndarray:
        """World (x, y) of the marker's four corners (tl, tr, br, bl), as the detector orders them."""
        s = self.sc.marker_size / 2
        a = math.radians(self.sc.marker_deg)
        # texture rows run south, rotate_matrix angle is counter-clockwise in the texture image
        corners = np.array([[-s, s], [s, s], [s, -s], [-s, -s]])  # (x east, y north) before rotation
        c, sn = math.cos(a), math.sin(a)
        rot = np.array([[c, -sn], [sn, c]])
        return corners @ rot.T + self.marker_center
