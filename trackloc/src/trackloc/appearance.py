"""Appearance signature of a car: a small hue/saturation histogram of its pixels.

Used by the roll call (`identify`) to tell cars apart, and by the tracker ONLY as a tie-breaker when a
CU crossing is ambiguous. A signature is a tuple of `SIG_BINS` floats summing to 1.

- coloured pixels (S >= 45, V >= 45): hue (16 bins, circular, smoothed so a bin edge does not matter)
  x saturation (2 bins);
- grey / white / black pixels have no usable hue: they go to 4 brightness bins instead.

The module top is standard library (the tracker imports `distance` and `blend`); numpy and OpenCV
are imported only by `signature()`.
"""

from __future__ import annotations

import math
from typing import Any

HUE_BINS, SAT_BINS, GREY_BINS = 16, 2, 4
SIG_BINS = HUE_BINS * SAT_BINS + GREY_BINS
MIN_PIXELS = 30  # fewer car pixels than this: no signature (a glimpse of a car says nothing)

Signature = tuple[float, ...]


def distance(a: Signature, b: Signature) -> float:
    """Bhattacharyya distance in [0, 1]: 0 = same colours, 1 = nothing in common."""
    bc = sum(math.sqrt(x * y) for x, y in zip(a, b))
    return math.sqrt(max(0.0, 1.0 - bc))


def blend(old: Signature | None, new: Signature, w: float = 0.1) -> Signature:
    """Running average (a track keeps the signature of its own detections)."""
    return new if old is None else tuple((1 - w) * o + w * n for o, n in zip(old, new))


def signature(image: Any, mask: Any, box: tuple[int, int, int, int], mask_scale: float = 1.0) -> Signature | None:
    """Signature of the car pixels inside `box` = (x0, y0, x1, y1) of the BGR `image`: those where `mask`
    (the foreground, at `mask_scale` x the image resolution) is set. None if there are too few."""
    import cv2
    import numpy as np

    h, w = image.shape[:2]
    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1, y1 = min(w, int(math.ceil(box[2]))), min(h, int(math.ceil(box[3])))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    m = mask[int(y0 * mask_scale):max(int(y1 * mask_scale), int(y0 * mask_scale) + 1),
             int(x0 * mask_scale):max(int(x1 * mask_scale), int(x0 * mask_scale) + 1)]
    if mask_scale != 1.0 or m.shape[:2] != (y1 - y0, x1 - x0):
        m = cv2.resize(m, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    px = hsv[m > 0].astype(np.int32)
    if len(px) < MIN_PIXELS:
        return None
    hue, sat, val = px[:, 0], px[:, 1], px[:, 2]
    colour = (sat >= 45) & (val >= 45)
    hist = np.zeros((HUE_BINS, SAT_BINS), np.float64)
    hb = np.minimum(hue[colour] * HUE_BINS // 180, HUE_BINS - 1)
    sb = (sat[colour] >= 150).astype(np.int32)
    np.add.at(hist, (hb, sb), 1.0)
    hist = 0.25 * np.roll(hist, 1, 0) + 0.5 * hist + 0.25 * np.roll(hist, -1, 0)  # circular hue smoothing
    grey = np.bincount(np.minimum(val[~colour] * GREY_BINS // 256, GREY_BINS - 1), minlength=GREY_BINS).astype(np.float64)
    out = np.concatenate([hist.ravel(), grey])
    return tuple(float(v) for v in out / out.sum())
