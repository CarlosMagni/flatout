"""Map discovery: from a recorded discovery session to a `MapModel` (offline, deterministic).

Input: the frames of the session (a re-iterable factory of `(t_host, image)`), the CU
crossings of the driven car (input dicts on the host clock) and a `MapConfig`. Live, the
`map` command drives the car (`drive.py`), records the session, then calls this with the
recording: a map can always be rebuilt from a recording with other parameters.

Steps:
1. Clock: CU timestamps -> host clock (`ClockSync`), so each crossing has a host time.
2. Background: temporal median of frames spread over the laps ("median", default) or the
   mean of about 30 frames of the empty track ("empty"), then the car is the blob
   that differs from it (largest plausible one, continuity-checked).
3. The pixel trajectory is cut into laps by the crossings; the finish line is the position of
   the car AT the crossing time (interpolated), so every lap polyline starts there.
4. Metres: pixel arc length is not metric under perspective. The ground-plane homography
   comes from the printed marker; the arc length of each lap is accumulated in metres, with
   the stretches where the homography cannot be trusted (hidden car, elevated section:
   apparent speed far from the lap's median at a constant throttle) replaced by median
   speed x time. Without a marker the whole lap is time-based (length from the motor model:
   a guess, flagged `metric: "model"`); `track_length_m` rescales to a known length.
5. Lanes: laps are compared with the first lap by lateral offset; two clusters mean two
   lanes (left = lane 0). A lap that switches from one to the other marks a lane changer.
6. Centre line: points of the two lanes at the same position across the track; the lap
   fraction `c` of the centre line is what RaceLab calls `s`.
7. Blind zones: stretches with no detection in at least half of the laps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from racelab.sim.physics import VMAX

from .detect import Blob, Detector, DetectorConfig
from .geometry import (
    DEFAULT_MARKER_SIZE,
    Lane,
    MapModel,
    apply_h,
    closest_on_closed,
    cum_length,
    find_marker,
    interp_closed,
    marker_homography,
    smooth_closed,
)
from .timing import ClockSync

FramesFactory = Callable[[], Iterable[tuple[float, np.ndarray]]]


class MapError(RuntimeError):
    """The discovery session cannot make a map (the message says what to do)."""


@dataclass
class MapConfig:
    marker_size: float = DEFAULT_MARKER_SIZE  # m, side of the black square of the printed marker
    throttle: int = 4  # throttle of the discovery lap (for the model speed and the gain hint)
    background: str = "median"  # "median" | "empty"
    bg_frames: int = 30  # frames kept for the median
    empty_frames: int = 30  # frames averaged for the empty-track background
    grid: int = 400  # vertices per lane polyline
    smooth_m: float = 0.05  # polyline smoothing window (m)
    lane: int = 0  # lane of the car when only one lane is found (0 left, 1 right)
    lane_pitch_m: float = 0.10  # lane spacing, used when only one lane is found
    gap_s: float = 0.15  # no detection for this long = a gap
    speed_tol: float = 0.35  # a stretch whose speed is this far from the median is not metric
    min_coverage: float = 0.7  # share of a lap that must be seen to use the lap
    latency_s: float = 0.0  # camera latency to remove from the frame times
    track_length_m: float | None = None  # known length of lane 0 (or of the single lane)
    name: str = "camera"
    self_calibrate: bool = True  # refine the marker's perspective with the car's constant speed
    bg_width: int = 960  # background image width (px)
    detector: DetectorConfig | None = None
    slot: int = 0


# ----------------------------------------------------------------------------- pieces

def crossing_times(inputs: Iterable[dict[str, Any]], slot: int) -> tuple[dict[int, list[float]], ClockSync]:
    """Host times of the crossings of `slot`, by sector, from the CU timestamps."""
    clock = ClockSync(window_s=1e9)
    items = [m for m in inputs if m.get("type") == "crossing" and "cu_ts" in m]
    items.sort(key=lambda m: m["t"])
    for m in items:
        clock.add(m["cu_ts"], m["t"])
    out: dict[int, list[float]] = {}
    for m in items:
        if m["slot"] == slot:
            t = clock.cu_to_host(m["cu_ts"])
            if t is not None:
                out.setdefault(int(m.get("sector", 1)), []).append(t)
    return out, clock


def make_background(frames: FramesFactory, window: tuple[float, float], cfg: MapConfig,
                    empty_range: tuple[float, float] | None) -> np.ndarray:
    """Median of frames spread over `window`, or mean of the frames of `empty_range`."""
    keep: list[np.ndarray] = []
    stride, i = 1, 0
    lo, hi = empty_range if cfg.background == "empty" and empty_range else window
    limit = cfg.empty_frames if cfg.background == "empty" else cfg.bg_frames
    for t, img in frames():
        if not lo <= t <= hi:
            continue
        if cfg.background == "empty":
            keep.append(_shrink(img, cfg.bg_width))
            if len(keep) >= limit:
                break
            continue
        if i % stride == 0:
            keep.append(_shrink(img, cfg.bg_width))
            if len(keep) > 2 * limit:
                keep, stride = keep[::2], stride * 2
        i += 1
    if not keep:
        raise MapError("no frame in the discovery session: is the video the right one?")
    stack = np.stack(keep)
    if cfg.background == "empty":
        return np.round(stack.mean(0)).astype(np.uint8)
    return np.median(stack, axis=0).astype(np.uint8)


def _shrink(img: np.ndarray, width: int) -> np.ndarray:
    h, w = img.shape[:2]
    return img if w <= width else cv2.resize(img, (width, int(round(h * width / w))), interpolation=cv2.INTER_AREA)


def track_car(frames: FramesFactory, bg_small: np.ndarray, native: tuple[int, int], window: tuple[float, float],
              cfg: MapConfig) -> tuple[np.ndarray, float]:
    """Pixel trajectory of the car (native pixels): rows (t, x, y, area), and its median blob area.

    A car looks several times bigger near the camera than far from it, so sizes are judged against the
    car's own neighbours in time: a sample whose area falls well below the local median is a car that is
    half hidden (bridge edge), its centroid is biased towards the visible half, so it is dropped (the gap
    is then interpolated). No metric is needed for that, so it works before the homography is trusted."""
    dcfg = cfg.detector or DetectorConfig()
    bg = bg_small if bg_small.shape[1] == native[0] else cv2.resize(bg_small, native, interpolation=cv2.INTER_LINEAR)
    det = Detector(bg, DetectorConfig(**{**dcfg.__dict__, "bg_alpha": 0.0}))
    per_frame: list[tuple[float, list[Blob]]] = []
    for t, img in frames():
        if window[0] <= t <= window[1]:
            per_frame.append((t, det.detect(img)))
    if not per_frame:
        raise MapError("no frame during the laps")
    largest = [max(b.area for b in bl) for _, bl in per_frame if bl]
    if not largest:
        raise MapError("the car was never detected: check the camera view, the lighting and that the car moves")
    ref = float(np.median(largest))
    rows, last = [], None
    for t, blobs in per_frame:
        ok = [b for b in blobs if 0.2 * ref <= b.area <= 3.5 * ref]
        if last is not None:  # continuity: nearest to the previous detection, within what a car can do
            gate = 0.12 * native[0] * max(t - last[0], 1 / 60) + 0.02 * native[0]
            near = [b for b in ok if np.hypot(b.cx - last[1], b.cy - last[2]) <= gate]
            ok = near or (ok if t - last[0] > 0.6 else [])
        if not ok:
            continue
        b = min(ok, key=lambda b: np.hypot(b.cx - last[1], b.cy - last[2])) if last else max(ok, key=lambda b: b.area)
        rows.append((t, b.cx, b.cy, b.area))
        last = rows[-1]
    arr = np.array(rows, np.float64)
    if len(arr) < 30:
        raise MapError("the car was seen in fewer than 30 frames: check the view and the lighting")
    # drop the half-hidden samples: much smaller than the median area of the samples within +-0.5 s
    t_all, area = arr[:, 0], arr[:, 3]
    local = np.array([np.median(area[(t_all > t - 0.5) & (t_all < t + 0.5)]) for t in t_all])
    arr = arr[(area >= 0.75 * local) & (area <= 1.6 * local)]
    return _hampel(arr, native), float(np.median(arr[:, 3]))


def _tilt(p: np.ndarray, pivot: np.ndarray, a: float, b: float) -> np.ndarray:
    """Perspective tilt of the image plane around `pivot`: identity at the pivot, scale changes away from it."""
    d = p - pivot
    return pivot + d / (1.0 + d @ np.array([a, b]))[:, None]


def calibrate_perspective(t: np.ndarray, p: np.ndarray, H: np.ndarray, pivot: np.ndarray, gap_s: float,
                          stride: int = 5) -> tuple[np.ndarray, dict[str, Any]]:
    """Refine the perspective of the marker homography with the car's own motion.

    A printed marker of a few centimetres fixes the scale and orientation where it lies, but
    its perspective terms are noisy (a fraction of a pixel at its corners is a few percent of
    scale across the track). At a constant throttle the car's speed is nearly constant, so a
    wrong perspective shows up as a speed that varies around the lap. Two parameters of a
    tilt of the image plane around the marker (identity there: the marker still sets the
    absolute scale) are fitted to make the ground speed as constant as possible (robust loss).
    The correction is applied only when it clearly improves the consistency."""
    dt = t[stride:] - t[:-stride]
    ok = dt < max(gap_s, 2.0 * stride / 30) * 1.0
    reach = float(np.abs(p - pivot).max())
    lim = 0.35 / max(reach, 1.0)

    def cost(a: float, b: float) -> float:
        g = apply_h(H, _tilt(p, pivot, a, b))
        v = np.hypot(*(g[stride:] - g[:-stride]).T)[ok] / dt[ok]
        lv = np.log(np.maximum(v, 1e-6))
        r = (lv - np.median(lv)) / 0.10
        return float(np.mean(np.where(np.abs(r) < 1.0, r * r, 2 * np.abs(r) - 1.0)))  # Huber

    base = cost(0.0, 0.0)
    best, (ba, bb) = base, (0.0, 0.0)
    span = lim
    ca = cb = 0.0
    for level in range(3):  # coarse to fine grid
        for a in np.linspace(ca - span, ca + span, 21):
            for b in np.linspace(cb - span, cb + span, 21):
                c = cost(a, b)
                if c < best:
                    best, ba, bb = c, a, b
        ca, cb, span = ba, bb, span / 5
    info = {"cost_before": base, "cost_after": best, "tilt": [ba, bb], "applied": False}
    if best < 0.7 * base and best < 0.5:
        info["applied"] = True
        q = np.eye(3)
        # p'' = pivot + d / (1 + a dx + b dy)  ==  homography [[1,0,0],[0,1,0],[a,b,1]] in pivot-centred coordinates
        T = np.array([[1, 0, -pivot[0]], [0, 1, -pivot[1]], [0, 0, 1.0]])
        Ti = np.linalg.inv(T)
        q = Ti @ np.array([[1, 0, 0], [0, 1, 0], [ba, bb, 1.0]]) @ T
        return H @ q, info
    return H, info


def _hampel(arr: np.ndarray, native: tuple[int, int]) -> np.ndarray:
    """Drop isolated outliers of the trajectory (a reflection, a hand edge).

    A sample is an outlier when it is far from the straight line between its neighbours at
    distance 1 AND at distance 2 (a median of x and y over a window would cut the tight
    corners: on a curve the point is not near the median of its neighbours, but it is near the
    chord between two of them)."""
    floor = max(5.0, 0.007 * native[0])
    keep = np.ones(len(arr), bool)
    p = arr[:, 1:3]
    t = arr[:, 0]
    for i in range(2, len(arr) - 2):
        worst = []
        for k in (1, 2):
            a, b = p[i - k], p[i + k]
            f = (t[i] - t[i - k]) / max(t[i + k] - t[i - k], 1e-9)
            worst.append(np.hypot(*(p[i] - (a + f * (b - a)))))
        if min(worst) > floor:
            keep[i] = False
    return arr[keep]


def lap_profile(t: np.ndarray, p: np.ndarray, H: np.ndarray | None, v_model: float, cfg: MapConfig
                ) -> dict[str, Any]:
    """Metric progress along one lap: u (fraction of the lap, increasing from 0 to 1) per sample."""
    dt = np.diff(t)
    gaps = dt > max(cfg.gap_s, 4 * float(np.median(dt)))
    if H is not None:
        # centroid noise inflates chord lengths (E|d + noise| > d): smooth the positions first
        # (a few samples), segment by segment so that the gaps are not bridged by the average
        ps = p.copy()
        edges = np.concatenate([[0], np.nonzero(gaps)[0] + 1, [len(p)]])
        for a, b in zip(edges[:-1], edges[1:]):
            if b - a >= 5:
                pad = np.pad(p[a:b], ((2, 2), (0, 0)), mode="edge")
                ps[a + 1:b - 1] = np.column_stack([np.convolve(pad[:, k], np.ones(5) / 5, "valid")[1:-1] for k in (0, 1)])
        g = apply_h(H, ps)
        d = np.hypot(*np.diff(g, axis=0).T)
        v = d / np.maximum(dt, 1e-6)
        pad = np.pad(v, 3, mode="edge")
        vs = np.median(np.lib.stride_tricks.sliding_window_view(pad, 7), axis=1)
        v_med = float(np.median(v[~gaps])) if (~gaps).any() else v_model
        bad = gaps | (vs < (1 - cfg.speed_tol) * v_med) | (vs > (1 + cfg.speed_tol) * v_med)
        ds = np.where(bad, v_med * dt, d)
        l_hom = float(d.sum())
    else:
        v_med, bad = v_model, np.ones(len(dt), bool)
        ds, l_hom = v_model * dt, None
    s = np.concatenate([[0.0], np.cumsum(ds)])
    return {"u": s / s[-1], "length": float(s[-1]), "gaps": gaps, "bad": bad, "v_med": float(v_med),
            "length_hom": l_hom, "suspect_frac": float((ds * bad).sum() / ds.sum()), "T": float(t[-1] - t[0])}


def _hermite(ug: np.ndarray, u: np.ndarray, p: np.ndarray, a: int, b: int) -> np.ndarray:
    """Positions at the grid values of `ug` inside the gap between samples a and b (Hermite)."""
    ia, ib = max(a - 3, 0), min(b + 3, len(u) - 1)
    ma = (p[a] - p[ia]) / max(u[a] - u[ia], 1e-9) if ia < a else (p[b] - p[a]) / (u[b] - u[a])
    mb = (p[ib] - p[b]) / max(u[ib] - u[b], 1e-9) if ib > b else (p[b] - p[a]) / (u[b] - u[a])
    h = u[b] - u[a]
    x = (ug - u[a]) / h
    h00, h10 = 2 * x ** 3 - 3 * x ** 2 + 1, x ** 3 - 2 * x ** 2 + x
    h01, h11 = -2 * x ** 3 + 3 * x ** 2, x ** 3 - x ** 2
    return h00[:, None] * p[a] + h10[:, None] * ma * h + h01[:, None] * p[b] + h11[:, None] * mb * h


def lap_polyline(prof: dict[str, Any], p: np.ndarray, n: int) -> np.ndarray:
    """The lap on a grid uniform in metric progress, gaps filled by a Hermite curve."""
    u = prof["u"]
    ug = np.arange(n) / n
    out = np.column_stack([np.interp(ug, u, p[:, 0]), np.interp(ug, u, p[:, 1])])
    for k in np.nonzero(prof["gaps"])[0]:
        inside = (ug > u[k]) & (ug < u[k + 1])
        if inside.any():
            out[inside] = _hermite(ug[inside], u, p, int(k), int(k + 1))
    return out


def _fill_nan(poly: np.ndarray) -> np.ndarray:
    out = poly.copy()
    for c in range(2):
        v = out[:, c]
        bad = np.isnan(v)
        if bad.all():
            raise MapError("a lane could not be built: not enough clean laps")
        if bad.any():
            idx = np.arange(len(v))
            ext = np.concatenate([idx[~bad] - len(v), idx[~bad], idx[~bad] + len(v)])
            val = np.concatenate([v[~bad]] * 3)
            v[bad] = np.interp(idx[bad], ext, val)
    return out


def _two_means(x: np.ndarray) -> tuple[float, float]:
    lo, hi = float(np.percentile(x, 10)), float(np.percentile(x, 95))
    for _ in range(30):
        mid = (lo + hi) / 2
        a, b = x[x <= mid], x[x > mid]
        if not len(a) or not len(b):
            break
        lo, hi = float(a.mean()), float(b.mean())
    return lo, hi


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """Cyclic runs of True as (start, length)."""
    n = len(flags)
    if flags.all():
        return [(0, n)]
    if not flags.any():
        return []
    start = int(np.argmin(flags))  # a False element: begin right after
    f = np.roll(flags, -start)
    runs, i = [], 0
    while i < n:
        if f[i]:
            j = i
            while j < n and f[j]:
                j += 1
            runs.append(((i + start) % n, j - i))
            i = j
        else:
            i += 1
    return runs


def _mpp(H: np.ndarray | None, pts: np.ndarray, fallback: float) -> np.ndarray:
    """Local metres per pixel at the given pixel points."""
    if H is None:
        return np.full(len(pts), fallback)
    g0 = apply_h(H, pts)
    gx = apply_h(H, pts + [1.0, 0.0])
    gy = apply_h(H, pts + [0.0, 1.0])
    a = np.abs((gx - g0)[:, 0] * (gy - g0)[:, 1] - (gx - g0)[:, 1] * (gy - g0)[:, 0])
    return np.sqrt(np.maximum(a, 1e-12))


def _smooth_vec(v: np.ndarray, half: int) -> np.ndarray:
    ext = np.concatenate([v[-half:], v, v[:half]])
    return np.convolve(ext, np.ones(2 * half + 1) / (2 * half + 1), "valid")


# ----------------------------------------------------------------------------- main

def _classify(laps: list[dict[str, Any]], ref_i: int, cfg: MapConfig, native: tuple[int, int]) -> dict[str, Any] | None:
    """Lateral offset of every lap relative to lap `ref_i`: one lane, or two (and which side).
    None when the offsets do not make sense (the reference lap itself changed lane)."""
    ref = laps[ref_i]["poly"]
    for k, lap in enumerate(laps):
        lap["d"] = np.zeros(cfg.grid) if k == ref_i else closest_on_closed(ref, lap["poly"])[2]
    others = np.abs(np.concatenate([lap["d"] for k, lap in enumerate(laps) if k != ref_i])) if len(laps) > 1 \
        else np.zeros(1)
    lo, hi = _two_means(others)
    sep = hi - lo
    mid = (lo + hi) / 2
    if not (sep > max(2.5, 0.004 * native[0]) and (others > mid).mean() > 0.10):
        return {"two": False}
    far = np.concatenate([lap["d"][np.abs(lap["d"]) > mid] for k, lap in enumerate(laps) if k != ref_i])
    pos = float((far > 0).mean())
    if max(pos, 1 - pos) < 0.85:
        return None
    return {"two": True, "lo": lo, "hi": hi, "sep": sep, "sign": 1.0 if pos > 0.5 else -1.0}


def build_map(frames: FramesFactory, inputs: Iterable[dict[str, Any]], cfg: MapConfig, *,
              empty_range: tuple[float, float] | None = None) -> MapModel:
    """Build the map of the circuit from a discovery session. Raises `MapError` with advice."""
    times, clock = crossing_times(list(inputs), cfg.slot)
    t1 = sorted(times.get(1, []))
    if len(t1) < 2:
        raise MapError(f"need at least 2 finish-line crossings of slot {cfg.slot} (one full lap), got {len(t1)}: "
                       "is the session running, and is the car the right slot?")

    def shifted() -> Iterable[tuple[float, np.ndarray]]:  # frame times with the camera latency removed
        for t, img in frames():
            yield t - cfg.latency_s, img

    window = (t1[0] - 0.4, t1[-1] + 0.4)
    first = next(iter(frames()), None)
    if first is None:
        raise MapError("the recording has no frame")
    native = (first[1].shape[1], first[1].shape[0])
    erange = None if empty_range is None else (empty_range[0] - cfg.latency_s, empty_range[1] - cfg.latency_s)
    bg_small = make_background(shifted, window, cfg, erange)
    # ground homography from the marker (found in the background, at its own resolution)
    corners = find_marker(bg_small)
    H = marker_homography(corners * (native[0] / bg_small.shape[1]), cfg.marker_size) if corners is not None else None
    samples, car_area = track_car(shifted, bg_small, native, window, cfg)
    calibration = None
    if H is not None and cfg.self_calibrate:
        in_laps = (samples[:, 0] >= t1[0]) & (samples[:, 0] <= t1[-1])
        H, calibration = calibrate_perspective(samples[in_laps, 0], samples[in_laps, 1:3], H,
                                               (corners * (native[0] / bg_small.shape[1])).mean(0), cfg.gap_s)
    v_model = VMAX * cfg.throttle / 15

    # laps: the trajectory between consecutive crossings, from the finish line to the finish line
    notes: list[str] = []
    laps: list[dict[str, Any]] = []
    t, x, y = samples[:, 0], samples[:, 1], samples[:, 2]
    for i in range(len(t1) - 1):
        a, b = t1[i], t1[i + 1]
        inside = (t > a) & (t < b)
        if inside.sum() < 10:
            notes.append(f"lap {i + 1}: too few detections, ignored")
            continue
        tt = np.concatenate([[a], t[inside], [b]])
        pp = np.column_stack([np.concatenate([[np.interp(a, t, x)], x[inside], [np.interp(b, t, x)]]),
                              np.concatenate([[np.interp(a, t, y)], y[inside], [np.interp(b, t, y)]])])
        prof = lap_profile(tt, pp, H, v_model, cfg)
        seen = 1.0 - float(np.diff(tt)[prof["gaps"]].sum()) / prof["T"]
        if seen < cfg.min_coverage:
            notes.append(f"lap {i + 1}: only {seen:.0%} seen, ignored")
            continue
        laps.append({"i": i, "t": tt, "p": pp, "prof": prof, "poly": lap_polyline(prof, pp, cfg.grid)})
    if not laps:
        raise MapError("no usable lap: " + "; ".join(notes) + ". Check the view, the lighting and the car speed.")

    # lanes: classify the laps against a reference lap (the first one that gives a coherent answer)
    cls = None
    for ref_i in range(len(laps)):
        cls = _classify(laps, ref_i, cfg, native)
        if cls is not None:
            break
    if cls is None:
        notes.append("the laps do not agree on a second lane: treated as a single lane")
        cls = {"two": False}
    two = bool(cls["two"])
    changer_u: list[float] = []
    n = cfg.grid
    if two:
        # the lane pitch in pixels depends on where we are (perspective): estimate it per vertex as the
        # largest signed offset any lap has there, smoothed (a running median, robust to blending laps)
        vals = np.stack([cls["sign"] * lap["d"] for lap in laps])
        pmax = vals.max(axis=0)
        ext = np.concatenate([pmax[-10:], pmax, pmax[:10]])
        local = np.median(np.lib.stride_tricks.sliding_window_view(ext, 21), axis=1)
        local = np.maximum(local, 0.35 * float(np.median(local)))
    for lap in laps:
        if two:
            dd = cls["sign"] * lap["d"]
            lap["on_b"] = dd > 0.5 * local
            lap["amb"] = (dd > 0.2 * local) & (dd < 0.8 * local)
            frac = float(lap["on_b"].mean())
            lap["kind"] = "A" if frac < 0.08 else "B" if frac > 0.92 else "hybrid"
            if lap["kind"] == "hybrid":
                sm = np.convolve(np.r_[lap["on_b"][-4:], lap["on_b"], lap["on_b"][:4]].astype(float),
                                 np.ones(9) / 9, "valid") > 0.5
                changer_u += [j / n for j in range(1, n) if sm[j] != sm[j - 1]]  # not the seam of the loop
        else:
            lap["on_b"], lap["amb"], lap["kind"] = np.zeros(n, bool), np.zeros(n, bool), "A"

    def lane_poly(on_b: bool) -> tuple[np.ndarray, float]:
        acc = np.full((len(laps), n, 2), np.nan)
        used = []
        for k_, lap in enumerate(laps):
            sel = (lap["on_b"] == on_b) & ~lap["amb"]
            acc[k_, sel] = lap["poly"][sel]
            if sel.mean() > 0.5:
                used.append(lap)
        count = np.sum(~np.isnan(acc[:, :, 0]), axis=0)
        total = np.where(np.isnan(acc), 0.0, acc).sum(axis=0)
        pts = _fill_nan(np.where(count[:, None] > 0, total / np.maximum(count, 1)[:, None], np.nan))
        length = float(np.mean([lp["prof"]["length"] for lp in (used or laps)]))
        return smooth_closed(pts, max(1, int(round(cfg.smooth_m / (length / n) / 2)))), length

    raw = {"A": lane_poly(False)}
    ids = {"A": cfg.lane, "B": 1 - cfg.lane}
    if two:
        raw["B"] = lane_poly(True)
        ids = {"A": 1, "B": 0} if cls["sign"] > 0 else {"A": 0, "B": 1}  # B on the left of A = lane 0

    # known length of lane 0 (or of the single lane): one scale factor for every metre
    lane0 = next(k_ for k_ in raw if ids[k_] == (0 if two else cfg.lane))
    scale = cfg.track_length_m / raw[lane0][1] if cfg.track_length_m else 1.0
    lengths = {k_: v[1] * scale for k_, v in raw.items()}

    # centre line and the lap fraction c of every lane vertex
    if two:
        pa, pb = raw["A"][0], raw["B"][0]
        fab, _, _ = closest_on_closed(pb, pa)  # for each vertex of A, the point of B across the track
        f = np.unwrap(fab * 2 * np.pi / n) * n / (2 * np.pi)
        f = np.maximum.accumulate(f - f[0])
        f = np.append(f, max(f[0] + n, f[-1]))
        dc = (lengths["A"] / n + np.diff(f) * lengths["B"] / n) / 2
        length_c = float(dc.sum())
        c_a = np.concatenate([[0.0], np.cumsum(dc)[:-1]]) / length_c
        fba, _, _ = closest_on_closed(pa, pb)
        c_b = np.maximum.accumulate(np.unwrap((interp_closed(c_a, fba, 1.0) % 1.0) * 2 * np.pi) / (2 * np.pi))
        cs = {"A": c_a, "B": c_b}
        pitch_a = np.hypot(*(pb[np.round(fab).astype(int) % n] - pa).T)
        pitch_b = np.hypot(*(pa[np.round(fba).astype(int) % n] - pb).T)
        pitches = {"A": _smooth_vec(pitch_a, 6), "B": _smooth_vec(pitch_b, 6)}
        pitch_px = float(np.median(pitch_a))
        if H is not None:
            pitch_m = float(np.median(np.hypot(*(apply_h(H, pa) - apply_h(H, pb[np.round(fab).astype(int) % n])).T))) * scale
        else:
            pitch_m = pitch_px * length_c / float(cum_length(pa)[-1])
    else:
        pa = raw["A"][0]
        length_c = lengths["A"]
        cs = {"A": np.arange(n) / n}
        pitch_m = cfg.lane_pitch_m
        local = cfg.lane_pitch_m / _mpp(H, pa, length_c / float(cum_length(pa)[-1]))
        pitches = {"A": _smooth_vec(local, 6)}
        pitch_px = float(np.median(local))
    lanes = sorted((Lane(raw[k_][0], cs[k_], lengths[k_], ids[k_], pitches[k_]) for k_ in raw), key=lambda ln: ln.lane_id)
    by_key = {k_: next(ln for ln in lanes if ln.lane_id == ids[k_]) for k_ in raw}

    # blind zones: lap fractions without detection in at least half of the laps
    bins = 1000
    cover = np.zeros(bins)
    for lap in laps:
        cl = by_key["B" if lap["kind"] == "B" else "A"].c
        u = lap["prof"]["u"]
        for g_ in np.nonzero(lap["prof"]["gaps"])[0]:
            ca, cb = float(cl[min(int(u[g_] * n), n - 1)]), float(cl[min(int(u[g_ + 1] * n), n - 1)])
            first_bin = int(ca * bins)
            cover[np.arange(first_bin, first_bin + max(1, int(((cb - ca) % 1.0) * bins)) + 1) % bins] += 1
    blind = []
    for start, length in _runs(cover >= max(1, int(np.ceil(0.5 * len(laps))))):
        if length / bins * length_c >= 0.08:
            blind.append((float((start / bins - 0.03 / length_c) % 1.0),
                          float(((start + length) / bins + 0.03 / length_c) % 1.0)))

    # lane changers: where a lap goes from one lane to the other (lap fractions on the centre line)
    merged: list[float] = []
    for c_ in sorted(round(float(by_key["A"].c[min(int(u_ * n), n - 1)]), 4) for u_ in changer_u):
        if not merged or c_ - merged[-1] > 0.06:
            merged.append(c_)

    # Check Lane / sector anchors: where the car was when sectors 2 and 3 were reported
    anchors: dict[int, float] = {1: 0.0}
    for sector in (2, 3):
        found = []
        for ts in times.get(sector, []):
            if t[0] <= ts <= t[-1]:
                f_, _, _ = closest_on_closed(by_key["A"].pts, np.array([[np.interp(ts, t, x), np.interp(ts, t, y)]]))
                found.append(float(interp_closed(by_key["A"].c, f_, 1.0)[0] % 1.0))
        if found:
            anchors[sector] = float((np.angle(np.mean(np.exp(2j * np.pi * np.array(found)))) / (2 * np.pi)) % 1.0)

    # honest numbers
    tl = float(np.mean([lp["prof"]["T"] for lp in laps]))
    l_hom = [lp["prof"]["length_hom"] for lp in laps if lp["prof"]["length_hom"] is not None]
    length_model = v_model * tl
    speed = lengths["A"] / tl
    metric = "user" if cfg.track_length_m else "marker" if H is not None else "model"
    if cfg.track_length_m and H is not None:
        H = np.diag([scale, scale, 1.0]) @ H
    report = {
        "laps": [{"lap": lp["i"] + 1, "kind": lp["kind"], "length_m": lp["prof"]["length"] * scale,
                  "time_s": lp["prof"]["T"], "suspect_frac": lp["prof"]["suspect_frac"]} for lp in laps],
        "notes": notes, "marker_found": corners is not None, "perspective_calibration": calibration, "lanes_found": len(lanes), "metric": metric,
        "length_marker_m": float(np.mean(l_hom)) * scale if l_hom else None,
        "length_model_m": length_model,
        "model_discrepancy_pct": 100.0 * (lengths["A"] / length_model - 1.0),
        "user_length_discrepancy_pct": 100.0 * (scale - 1.0) if cfg.track_length_m else None,
        "speed_mps": speed, "lap_time_s": tl, "clock_offset_s": clock.offset,
        "changers": merged, "blind": blind, "anchors": anchors,
        "model_note": "length_model_m is the simulator's motor model (racelab.sim.physics) at this throttle: "
                      "a guess on real hardware; trust the marker, or give --track-length-m",
    }
    mpp = _mpp(H, samples[:, 1:3], length_c / float(cum_length(lanes[0].pts)[-1]))
    car_area_m2 = float(np.median(samples[:, 3] * mpp ** 2))
    return MapModel(native, lanes, length_c, H, merged, blind, anchors, pitch_px, pitch_m, car_area, car_area_m2,
                    speed / v_model if v_model else None, metric, cfg.name, report, background=bg_small)
