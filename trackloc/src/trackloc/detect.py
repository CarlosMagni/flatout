"""Car detection by background subtraction.

Foreground = max over the colour channels of |frame - background| (both blurred), after a
global gain correction (the exposure drifts), thresholded relative to the measured noise,
then cleaned with a morphological opening and closing.

With a map, the foreground is cut into connected components; the zones of the map (one
corridor per lane, plus an "arena" around the track) decide the lane of each. Sizes are
judged in metres (a car is about the same area everywhere on the ground, but its image is
three times larger near the camera than far from it):

- about one car: one detection, in the lane that holds most of its pixels (an oblique
  camera sees the body of a car spill over the next lane's strip: that is NOT a second car);
- about two cars: cars in adjacent lanes overlapping in the image: one detection per zone part;
- much more than a car (a hand): one "big" blob, an occluder, never a car;
- mostly in the arena, outside every corridor: an off-track blob (a derailed car).

Without a map (discovery lap) any blob counts.

Everything runs at a reduced processing width; blob coordinates come back in full-resolution
pixels. Deterministic: same frames, same blobs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from .geometry import MapModel

OFF = -1  # zone of the blobs outside every lane corridor
ARENA, IGNORE = 200, 255  # codes in the zone image (lane corridors are 0, 1)


@dataclass
class DetectorConfig:
    width: int = 640  # processing width (0 = native resolution)
    blur: float = 1.5  # sigma (processing pixels)
    thr_min: float = 14.0
    thr_max: float = 80.0
    thr_noise_k: float = 6.0  # threshold = k * median of the foreground score, within [thr_min, thr_max]
    open_px: int = 3
    close_px: int = 7
    min_area_px: float = 10.0  # processing pixels, floor when the car size is unknown
    min_area_frac: float = 0.25  # of the expected car area
    merge_area_frac: float = 1.5  # up to this: one car (split into zone parts above it)
    max_area_frac: float = 2.3  # above: not a car (a hand): an occluder
    bg_alpha: float = 0.003  # slow background adaptation outside the foreground (0 = off)
    gain_normalise: bool = True
    corridor_half: float = 0.58  # lane corridor half width, in lane pitches
    arena_half: float = 3.0  # arena half width, in lane pitches


@dataclass
class Blob:
    cx: float  # full-resolution pixels
    cy: float
    area: float  # full-resolution pixels²
    w: float  # bounding box, full-resolution pixels
    h: float
    zone: int  # lane id, or OFF
    big: bool = False  # larger than a car can be


class Detector:
    def __init__(self, background: np.ndarray, cfg: DetectorConfig | None = None, *,
                 model: MapModel | None = None, expected_area: Callable[[float, float], float] | None = None) -> None:
        self.cfg = cfg or DetectorConfig()
        h, w = background.shape[:2]
        self.full = (w, h)
        self.scale = 1.0 if not self.cfg.width or self.cfg.width >= w else self.cfg.width / w
        self.size = (max(8, int(round(w * self.scale))), max(8, int(round(h * self.scale))))
        self.bg_u8 = self._prep(background)
        self.bg = self.bg_u8.astype(np.float32)  # the slowly adapting copy
        self.bg_med = self._median(self.bg_u8)
        self._n = 0
        self.expected_area = expected_area  # (x, y) in full-resolution pixels -> pixel area of one car there
        self.zones: np.ndarray | None = None
        self.lane_ids: list[int] = []
        self.open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (self.cfg.open_px,) * 2)
        self.close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (self.cfg.close_px,) * 2)
        self.last_thr = 0.0
        if model is not None:
            self.set_model(model)

    # ------------------------------------------------------------------ setup

    def _prep(self, img: np.ndarray) -> np.ndarray:
        if self.scale != 1.0:
            img = cv2.resize(img, self.size, interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(img, (0, 0), self.cfg.blur)

    @staticmethod
    def _median(img: np.ndarray) -> float:
        return float(np.median(cv2.resize(img, (48, 27), interpolation=cv2.INTER_AREA))) or 1.0

    def set_model(self, model: MapModel) -> None:
        """Build the zone image: lane corridors (Voronoi by lane), arena, ignored elsewhere."""
        cfg = self.cfg
        pitch = model.pitch_px * self.scale or 14.0
        w, h = self.size
        dts = []
        for lane in model.lanes:
            line = np.zeros((h, w), np.uint8)
            pts = np.round(lane.pts * self.scale).astype(np.int32)
            cv2.polylines(line, [pts], True, 255, 1)
            dts.append(cv2.distanceTransform(255 - line, cv2.DIST_L2, 3))
        dt = np.stack(dts)
        nearest = dt.argmin(0)
        mind = dt.min(0)
        zones = np.full((h, w), IGNORE, np.uint8)
        zones[mind <= cfg.arena_half * pitch] = ARENA
        ids = np.array([ln.lane_id for ln in model.lanes], np.uint8)
        inside = mind <= cfg.corridor_half * pitch
        zones[inside] = ids[nearest[inside]]
        self.zones, self.lane_ids = zones, [ln.lane_id for ln in model.lanes]
        if self.expected_area is None and model.car_area_m2:
            m2 = model.car_area_m2
            self.expected_area = lambda x, y: m2 / model.metres_per_pixel((x, y)) ** 2

    # ------------------------------------------------------------------ per frame

    def foreground(self, frame: np.ndarray) -> np.ndarray:
        """Binary foreground mask (processing resolution)."""
        cfg = self.cfg
        cur = self._prep(frame)
        if cfg.gain_normalise:
            g = self.bg_med / self._median(cur)
            if abs(g - 1.0) > 0.003:
                cur = cv2.convertScaleAbs(cur, alpha=g)
        b, g_, r = cv2.split(cv2.absdiff(cur, self.bg_u8))
        score = cv2.max(cv2.max(b, g_), r)
        thr = float(np.clip(cfg.thr_noise_k * np.median(score[::4, ::4]), cfg.thr_min, cfg.thr_max))
        self.last_thr = thr
        _, mask = cv2.threshold(score, thr, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.open_k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.close_k)
        if cfg.bg_alpha > 0:  # follow the slow changes of the scene, outside what moves
            keep = cv2.bitwise_not(cv2.dilate(mask, self.close_k, iterations=2))
            cv2.accumulateWeighted(cur, self.bg, cfg.bg_alpha, keep)
            self._n += 1
            if self._n % 8 == 0:
                self.bg_u8 = cv2.convertScaleAbs(self.bg)
                self.bg_med = self._median(self.bg_u8)
        return mask

    def _plain(self, mask: np.ndarray) -> list[Blob]:
        """Every blob above the floor (discovery lap: no map, nothing known about the car yet)."""
        n, _, stats, cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
        s2 = (1.0 / self.scale) ** 2
        return [Blob(float(cent[i][0]) / self.scale, float(cent[i][1]) / self.scale, float(stats[i, cv2.CC_STAT_AREA]) * s2,
                     stats[i, cv2.CC_STAT_WIDTH] / self.scale, stats[i, cv2.CC_STAT_HEIGHT] / self.scale, 0)
                for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= self.cfg.min_area_px]

    def detect(self, frame: np.ndarray) -> list[Blob]:
        """Blobs of the frame (see the module docstring)."""
        cfg = self.cfg
        mask = self.foreground(frame)
        if self.zones is None or self.expected_area is None:
            return self._plain(mask)
        mask = cv2.bitwise_and(mask, mask, mask=(self.zones != IGNORE).astype(np.uint8))
        n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
        inv = 1.0 / self.scale
        out: list[Blob] = []
        for i in range(1, n):
            area = float(stats[i, cv2.CC_STAT_AREA])
            if area < cfg.min_area_px:
                continue
            cx, cy = float(cent[i][0]) * inv, float(cent[i][1]) * inv
            ratio = area * inv * inv / max(self.expected_area(cx, cy), 1.0)
            if ratio < cfg.min_area_frac:
                continue
            x0, y0 = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
            w, h = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
            member = lab[y0:y0 + h, x0:x0 + w] == i
            zone_px = self.zones[y0:y0 + h, x0:x0 + w]
            counts = {z: int((member & (zone_px == z)).sum()) for z in (*self.lane_ids, ARENA)}
            lane_best = max(self.lane_ids, key=lambda z: counts[z])
            big = ratio > cfg.max_area_frac
            if big or ratio <= cfg.merge_area_frac:
                zone = lane_best if counts[lane_best] >= counts[ARENA] and not big else (lane_best if big else OFF)
                out.append(Blob(cx, cy, area * inv * inv, w * inv, h * inv, zone, big))
                continue
            for z in (*self.lane_ids, ARENA):  # about two cars: one detection per zone part
                part = member & (zone_px == z)
                pa = float(part.sum())
                if pa * inv * inv / max(self.expected_area(cx, cy), 1.0) < cfg.min_area_frac:
                    continue
                ys, xs = np.nonzero(part)
                out.append(Blob(float(xs.mean() + x0) * inv, float(ys.mean() + y0) * inv, pa * inv * inv,
                                (xs.max() - xs.min() + 1) * inv, (ys.max() - ys.min() + 1) * inv,
                                OFF if z == ARENA else z))
        return out
