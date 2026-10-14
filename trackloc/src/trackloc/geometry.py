"""Map geometry: the ground marker, polylines in the image, the map model and its registration.

Conventions (read before touching anything):

- Image coordinates: x to the right, y DOWN, in pixels of the full-resolution frame.
- Left of the driving direction: for a direction (dx, dy) in these coordinates the left
  normal is (dy, -dx) (a car going right has its left side up the image). Lane 0 is the
  left lane, lane 1 the right one.
- The printed marker is `DICT_4X4_50` id 0 (other tags in view are ignored). Its side is the
  black square (default 0.150 m, `--marker-size`). The ground frame is the marker's own frame:
  x along its top edge, y down the page, metres: seen from above it is not mirrored, so a
  polyline in it is drawn with `y_axis: "down"`.
- `c` is the lap fraction on the centre line (the midline of the lanes), 0 at the finish line:
  it is what `track.positions` calls `s`, and it does not depend on the lane.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ARUCO_DICT = cv2.aruco.DICT_4X4_50
MARKER_ID = 0
DEFAULT_MARKER_SIZE = 0.150  # m, side of the black square


# ----------------------------------------------------------------------------- marker

def _detector() -> Any:
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(ARUCO_DICT), params)


def find_marker(image: np.ndarray) -> np.ndarray | None:
    """Corners (tl, tr, br, bl; 4x2 pixels) of marker id 0, or None. Other ids are ignored."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corners, ids, _ = _detector().detectMarkers(gray)
    if ids is None:
        return None
    best, area = None, 0.0
    for c, i in zip(corners, ids.ravel()):
        if int(i) == MARKER_ID:
            a = cv2.contourArea(c.reshape(4, 2).astype(np.float32))
            if a > area:
                best, area = c.reshape(4, 2).astype(np.float64), a
    return best


def marker_homography(corners: np.ndarray, size_m: float) -> np.ndarray:
    """3x3 homography, pixels -> ground metres in the marker's frame."""
    dst = np.array([[0, 0], [size_m, 0], [size_m, size_m], [0, size_m]], np.float32)
    return cv2.getPerspectiveTransform(corners.astype(np.float32), dst).astype(np.float64)


def marker_sheet(size_px: int = 900, margin: float = 0.25) -> np.ndarray:
    """Grey image of the marker with a white margin, to print (see `python -m trackloc marker`)."""
    d = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
    m = cv2.aruco.generateImageMarker(d, MARKER_ID, size_px)
    pad = int(size_px * margin)
    sheet = np.full((size_px + 2 * pad, size_px + 2 * pad), 255, np.uint8)
    sheet[pad:pad + size_px, pad:pad + size_px] = m
    return sheet


def marker_pdf(size_mm: float = 150.0, landscape: bool = False) -> bytes:
    """A one-page A4 PDF with the marker drawn as vectors: printed at "actual size" (100 %), the black
    square measures exactly `size_mm`. (A PNG has no print scale: it could come out at any size.)"""
    bits = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(ARUCO_DICT), MARKER_ID, 6)  # 6x6 modules
    w, h = (297.0, 210.0) if landscape else (210.0, 297.0)
    pt = 72.0 / 25.4
    mod = size_mm / 6 * pt
    x0, y0 = (w * pt - size_mm * pt) / 2, (h * pt - size_mm * pt) / 2 + 12 * pt
    cmds = ["0 g", f"{x0:.3f} {y0:.3f} {mod * 6:.3f} {mod * 6:.3f} re f", "1 g"]  # the black square, then the white bits
    for r in range(6):
        for c in range(6):
            if bits[r, c] != 0:
                cmds.append(f"{x0 + c * mod:.3f} {y0 + (5 - r) * mod:.3f} {mod:.3f} {mod:.3f} re f")
    cmds.append("0 g")
    cmds += ["BT /F1 10 Tf 30 24 Td (trackloc marker - ArUco DICT_4X4_50 id 0 - black square = "
             f"{size_mm:g} mm - print at 100% (actual size), then measure it and pass --marker-size) Tj ET"]
    stream = "\n".join(cmds).encode("latin-1")
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {w * pt:.2f} {h * pt:.2f}]/Contents 4 0 R"
            "/Resources<</Font<</F1 5 0 R>>>>>>".encode(),
            b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def apply_h(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    p = np.asarray(pts, np.float64).reshape(-1, 2)
    q = np.column_stack([p, np.ones(len(p))]) @ H.T
    return q[:, :2] / q[:, 2:3]


# ----------------------------------------------------------------------------- polylines

def cum_length(pts: np.ndarray, closed: bool = True) -> np.ndarray:
    """Cumulative arc length at each vertex (n+1 values if closed, the last one = total)."""
    q = np.vstack([pts, pts[:1]]) if closed else pts
    return np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(q, axis=0).T))])


def resample_closed(pts: np.ndarray, n: int) -> np.ndarray:
    cum = cum_length(pts)
    q = np.vstack([pts, pts[:1]])
    t = np.linspace(0.0, cum[-1], n, endpoint=False)
    return np.column_stack([np.interp(t, cum, q[:, 0]), np.interp(t, cum, q[:, 1])])


def smooth_closed(pts: np.ndarray, half: int) -> np.ndarray:
    """Cyclic moving average of a closed polyline (window 2*half+1 vertices)."""
    if half <= 0:
        return pts
    ext = np.vstack([pts[-half:], pts, pts[:half]])
    k = np.ones(2 * half + 1) / (2 * half + 1)
    return np.column_stack([np.convolve(ext[:, i], k, "valid") for i in range(2)])


def left_normal(d: np.ndarray) -> np.ndarray:
    """Left of the driving direction, in image coordinates (y down)."""
    d = np.asarray(d, float)
    n = np.column_stack([d[..., 1], -d[..., 0]]) if d.ndim > 1 else np.array([d[1], -d[0]])
    return n


def tangents(pts: np.ndarray) -> np.ndarray:
    d = np.roll(pts, -1, axis=0) - np.roll(pts, 1, axis=0)
    return d / np.maximum(np.hypot(*d.T), 1e-12)[:, None]


def closest_on_closed(pts: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Nearest point of a closed polyline for each query point.

    Returns (fractional vertex index in [0, n), distance, signed lateral offset with the
    left of the driving direction positive), all of length len(q)."""
    q = np.asarray(q, np.float64).reshape(-1, 2)
    n = len(pts)
    rows = np.arange(len(q))
    i = ((q[:, None, :] - pts[None, :, :]) ** 2).sum(-1).argmin(1)  # nearest vertex
    best_d = np.full(len(q), np.inf)
    best_f = np.zeros(len(q))
    best_side = np.zeros(len(q))
    for first in (-1, 0):  # the two segments around that vertex: (i-1, i) and (i, i+1)
        a, b = pts[(i + first) % n], pts[(i + first + 1) % n]
        ab = b - a
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((q - a) * ab).sum(1) / L2, 0.0, 1.0)
        foot = a + t[:, None] * ab
        dist = np.hypot(*(q - foot).T)
        side = ((q - foot) * np.column_stack([ab[:, 1], -ab[:, 0]])).sum(1) / np.sqrt(L2)  # left normal (dy, -dx)
        better = dist < best_d
        best_f = np.where(better, (i + first + t) % n, best_f)
        best_side = np.where(better, side, best_side)
        best_d = np.where(better, dist, best_d)
    del rows
    return best_f, best_d, best_side


def interp_closed(values: np.ndarray, f: np.ndarray, period: float | None = None) -> np.ndarray:
    """Linear interpolation of per-vertex values at fractional indices on a closed polyline.
    `period` is added to the wrapped segment (for a monotone quantity such as a lap fraction)."""
    n = len(values)
    i = np.floor(f).astype(int) % n
    a = f - np.floor(f)
    v0, v1 = values[i], values[(i + 1) % n]
    if period is not None:
        v1 = np.where(i == n - 1, v1 + period, v1)
    return v0 + a * (v1 - v0)


# ----------------------------------------------------------------------------- map model

@dataclass
class Lane:
    pts: np.ndarray  # (N, 2) pixels, closed, vertex 0 at the finish line, driving direction
    c: np.ndarray  # (N,) lap fraction on the centre line at each vertex (increasing from 0)
    length: float  # m (lane arc length)
    lane_id: int  # 0 = left in the driving direction
    pitch_px: np.ndarray | None = None  # (N,) local lane separation, pixels (corridor width)


@dataclass
class MapModel:
    size: tuple[int, int]  # (width, height) of the background image
    lanes: list[Lane]
    length_c: float  # m, centre line length
    H: np.ndarray | None  # pixels -> ground metres (marker frame, y down); None without a marker
    changers: list[float] = field(default_factory=list)  # lap fractions
    blind: list[tuple[float, float]] = field(default_factory=list)  # lap fraction intervals [c0, c1]
    anchors: dict[int, float] = field(default_factory=lambda: {1: 0.0})  # sector -> lap fraction
    pitch_px: float = 0.0  # lane separation in pixels (median)
    pitch_m: float = 0.0
    car_area_px: float = 0.0  # median blob area of the car in the discovery lap (pixels²)
    car_area_m2: float = 0.0  # the same on the ground (metres²): the size test of the detector
    gain_hint: float | None = None
    metric: str = "marker"  # how metres were obtained: "marker" | "model" | "user"
    name: str = "camera"
    report: dict[str, Any] = field(default_factory=dict)
    background: np.ndarray | None = None  # BGR, not serialised in the JSON (a PNG next to it)

    # -- projection (the measurement model of the tracker)

    def project(self, pts: np.ndarray) -> list[tuple[int, float, float, float]]:
        """For each pixel point: (lane_id, lap fraction c, signed lateral offset px, distance px)
        on the nearest lane."""
        pts = np.asarray(pts, np.float64).reshape(-1, 2)
        if not len(pts):
            return []
        results = []
        per_lane = []
        for lane in self.lanes:
            f, d, side = closest_on_closed(lane.pts, pts)
            per_lane.append((interp_closed(lane.c, f, 1.0) % 1.0, d, side))
        for k in range(len(pts)):
            j = min(range(len(self.lanes)), key=lambda j: per_lane[j][1][k])
            c, d, side = per_lane[j]
            results.append((self.lanes[j].lane_id, float(c[k]), float(side[k]), float(d[k])))
        return results

    def lane_by_id(self, lane_id: int) -> Lane:
        return next(ln for ln in self.lanes if ln.lane_id == lane_id)

    def project_lane(self, lane_id: int, pt: tuple[float, float]) -> tuple[float, float, float]:
        """(lap fraction c, signed lateral offset px, distance px) of a pixel point on one lane."""
        lane = self.lane_by_id(lane_id)
        f, d, side = closest_on_closed(lane.pts, np.array([pt], np.float64))
        return float(interp_closed(lane.c, f, 1.0)[0] % 1.0), float(side[0]), float(d[0])

    def metres_per_pixel(self, pt: tuple[float, float]) -> float:
        """Local ground scale at a pixel (homography), else the average over the lap."""
        if self.H is None:
            lane = self.lanes[0]
            return lane.length / float(cum_length(lane.pts)[-1])
        p = np.array([pt], np.float64)
        g0, gx, gy = apply_h(self.H, p), apply_h(self.H, p + [1.0, 0.0]), apply_h(self.H, p + [0.0, 1.0])
        area = abs((gx - g0)[0, 0] * (gy - g0)[0, 1] - (gx - g0)[0, 1] * (gy - g0)[0, 0])
        return math.sqrt(max(area, 1e-12))

    # -- registration to a bumped camera

    def warped(self, W: np.ndarray) -> MapModel:
        """The same map seen through a homography W (stored-background px -> current-frame px)."""
        lanes = []
        for ln in self.lanes:
            moved = apply_h(W, ln.pts)
            pitch = None
            if ln.pitch_px is not None:  # the local scale of the warp changes the corridor width
                a = np.abs((apply_h(W, ln.pts + [1.0, 0.0]) - moved)[:, 0] * (apply_h(W, ln.pts + [0.0, 1.0]) - moved)[:, 1]
                           - (apply_h(W, ln.pts + [1.0, 0.0]) - moved)[:, 1] * (apply_h(W, ln.pts + [0.0, 1.0]) - moved)[:, 0])
                pitch = ln.pitch_px * np.sqrt(a)
            lanes.append(Lane(moved, ln.c, ln.length, ln.lane_id, pitch))
        H = None if self.H is None else self.H @ np.linalg.inv(W)
        return MapModel(self.size, lanes, self.length_c, H, list(self.changers), list(self.blind),
                        dict(self.anchors), self.pitch_px, self.pitch_m, self.car_area_px, self.car_area_m2,
                        self.gain_hint, self.metric, self.name, dict(self.report), self.background)

    # -- views for the tracker and for RaceLab

    def kappa(self) -> tuple[float, ...]:
        out = []
        for lane_id in (0, 1):
            ln = next((ln for ln in self.lanes if ln.lane_id == lane_id), self.lanes[0])
            out.append(ln.length / self.length_c)
        return tuple(out)

    def geometry(self) -> dict[str, Any]:
        """The `track.set` geometry (RaceLab wire format): the centre line in metres, upright."""
        ref = self.lanes[0]
        if len(self.lanes) == 2:
            a, b = self.lanes
            # midline: each vertex of lane `a` with the point of lane `b` at the same lap fraction
            fb = np.interp(a.c, np.append(b.c, 1.0 + b.c[0]), np.arange(len(b.pts) + 1))
            bpts = np.vstack([b.pts, b.pts[:1]])
            mb = np.column_stack([np.interp(fb, np.arange(len(bpts)), bpts[:, 0]),
                                  np.interp(fb, np.arange(len(bpts)), bpts[:, 1])])
            mid = (a.pts + mb) / 2
        else:
            mid = ref.pts
        if self.H is not None:
            g = apply_h(self.H, mid)
            h, w = self.size[1], self.size[0]
            centre = apply_h(self.H, np.array([[w / 2, h / 2], [w / 2, h / 2 - 50.0]]))
            up = centre[1] - centre[0]  # image "up" on the ground: rotate it to point up the screen
            ang = math.atan2(up[1], up[0]) + math.pi / 2
            c, s = math.cos(-ang), math.sin(-ang)
            g = g @ np.array([[c, -s], [s, c]]).T
            pts, offset = g, self.pitch_m / 2 if self.pitch_m else 0.05
        else:
            pts, offset = mid, (self.pitch_px / 2 if self.pitch_px else 10.0)
        step = max(1, len(pts) // 400)
        out = {
            "points": [[round(float(x), 5), round(float(y), 5)] for x, y in pts[::step]],
            "y_axis": "down", "finish": 0.0,
            "changers": [round(float(c) % 1.0, 5) for c in self.changers],
            "lane_offset": round(float(offset), 5),
            "name": self.name,
        }
        out["length_m"] = [round(float(next((ln for ln in self.lanes if ln.lane_id == i), ref).length), 3)
                           for i in (0, 1)] if len(self.lanes) == 2 else [round(float(ref.length), 3)]
        return out

    # -- files

    def save(self, path: str | Path) -> None:
        path = Path(path)
        bg = None
        if self.background is not None:
            bg = path.with_suffix(".bg.png").name
            cv2.imwrite(str(path.with_suffix(".bg.png")), self.background)
        data = {
            "version": 1, "name": self.name, "size": list(self.size), "length_c": self.length_c,
            "H": None if self.H is None else self.H.tolist(), "metric": self.metric,
            "changers": self.changers, "blind": [list(b) for b in self.blind],
            "anchors": {str(k): v for k, v in self.anchors.items()},
            "pitch_px": self.pitch_px, "pitch_m": self.pitch_m, "car_area_px": self.car_area_px, "car_area_m2": self.car_area_m2,
            "gain_hint": self.gain_hint, "background": bg, "report": self.report,
            "lanes": [{"lane_id": ln.lane_id, "length": ln.length, "points": ln.pts.tolist(), "c": ln.c.tolist(),
                       "pitch_px": None if ln.pitch_px is None else ln.pitch_px.tolist()} for ln in self.lanes],
        }
        path.write_text(json.dumps(data), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> MapModel:
        path = Path(path)
        d = json.loads(path.read_text(encoding="utf-8"))
        lanes = [Lane(np.array(ln["points"], np.float64), np.array(ln["c"], np.float64), ln["length"], ln["lane_id"],
                      None if ln.get("pitch_px") is None else np.array(ln["pitch_px"], np.float64)) for ln in d["lanes"]]
        bg = None
        if d.get("background") and (path.parent / d["background"]).exists():
            bg = cv2.imread(str(path.parent / d["background"]))
        return cls(tuple(d["size"]), lanes, d["length_c"], None if d["H"] is None else np.array(d["H"]),
                   d["changers"], [tuple(b) for b in d["blind"]], {int(k): v for k, v in d["anchors"].items()},
                   d["pitch_px"], d["pitch_m"], d["car_area_px"], d.get("car_area_m2", 0.0), d.get("gain_hint"), d.get("metric", "marker"),
                   d.get("name", "camera"), d.get("report", {}), bg)


# ----------------------------------------------------------------------------- registration

class RegistrationError(RuntimeError):
    pass


def _prep(img: np.ndarray, scale: float) -> np.ndarray:
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if scale != 1.0:
        g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(g, (0, 0), 1.2).astype(np.float32)


def _orb_homography(a: np.ndarray, b: np.ndarray) -> np.ndarray | None:
    orb = cv2.ORB_create(3000)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 12 or len(kb) < 12:
        return None
    matches = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db), key=lambda m: m.distance)[:400]
    if len(matches) < 12:
        return None
    src = np.float32([ka[m.queryIdx].pt for m in matches])
    dst = np.float32([kb[m.trainIdx].pt for m in matches])
    H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    return None if H is None or inl.sum() < 10 else H


def register(background: np.ndarray, frame: np.ndarray, *, scale: float = 0.5,
             min_cc: float = 0.80) -> tuple[np.ndarray, float]:
    """Homography W with frame(W x) = background(x): stored-background pixels -> current-frame pixels.

    ECC maximisation (cv2.findTransformECC, homography), started from identity and, if that does
    not converge, from an ORB + RANSAC match. Returns (W at full resolution, correlation).
    Raises RegistrationError when the two images cannot be aligned: re-map."""
    a8 = _prep(background, scale)
    b8 = _prep(frame, scale)
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
    S = np.diag([scale, scale, 1.0])
    best_w, best_cc = None, -1.0
    inits = [np.eye(3, dtype=np.float32)]
    orb = _orb_homography(a8.astype(np.uint8), b8.astype(np.uint8))
    if orb is not None:
        inits.append(orb.astype(np.float32))
    for init in inits:
        w = init.copy()
        try:
            cc, w = cv2.findTransformECC(a8, b8, w, cv2.MOTION_HOMOGRAPHY, crit, None, 5)
        except cv2.error:
            continue
        if cc > best_cc:
            best_w, best_cc = w, float(cc)
        if best_cc >= 0.95:
            break
    if best_w is None or best_cc < min_cc:
        raise RegistrationError(f"the camera view does not match the map (correlation {best_cc:.2f}): "
                                "run `map` again, or put the camera back where it was")
    return np.linalg.inv(S) @ best_w.astype(np.float64) @ S, best_cc
