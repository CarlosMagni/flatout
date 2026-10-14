"""Debug overlay: lane polylines, detections, tracks with their slot, written as a video
(`opencv-python-headless` has no window: `--debug out.mp4`)."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from .pipeline import Pipeline

COLORS = [(40, 40, 230), (230, 130, 30), (40, 220, 230), (80, 200, 80), (200, 70, 180), (230, 230, 230)]


def draw(image: np.ndarray, pipe: Pipeline) -> np.ndarray:
    out = image.copy()
    m = pipe.model
    for lane in m.lanes:
        cv2.polylines(out, [np.round(lane.pts).astype(np.int32)], True, (0, 255, 255) if lane.lane_id == 0 else (255, 255, 0), 1, cv2.LINE_AA)
    for b in pipe.last_blobs:
        color = (0, 0, 255) if b.big else (0, 140, 255) if b.zone < 0 else (0, 255, 0)
        cv2.circle(out, (int(b.cx), int(b.cy)), 4, color, 1, cv2.LINE_AA)
    L = m.length_c
    for tr in pipe.tracker.tracks:
        lane = m.lane_by_id(tr.lane) if any(ln.lane_id == tr.lane for ln in m.lanes) else m.lanes[0]
        c = (tr.x % L) / L
        i = int(np.searchsorted(lane.c, c)) % len(lane.pts)
        x, y = int(lane.pts[i][0]), int(lane.pts[i][1])
        label = f"#{tr.slot + 1}" if tr.slot is not None else f"?{tr.id}"
        col = COLORS[(tr.slot or 0) % len(COLORS)] if tr.slot is not None else (160, 160, 160)
        cv2.circle(out, (x, y), 9, col, 2, cv2.LINE_AA)
        cv2.putText(out, label + (" OFF" if tr.off else ""), (x + 10, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
    return out


class Overlay:
    """Writes the overlay frames of a live or replayed session to a video file."""

    def __init__(self, path: str, fps: float, size: tuple[int, int]) -> None:
        self.writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
        if not self.writer.isOpened():
            raise OSError(f"cannot write the overlay video {path}")

    def write(self, image: np.ndarray, pipe: Pipeline) -> None:
        self.writer.write(draw(image, pipe))

    def close(self) -> None:
        self.writer.release()
