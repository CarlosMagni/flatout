"""The localisation pipeline: frames and inputs in, car positions out (no I/O).

`Pipeline` is a single synchronous consumer of ONE ordered stream: frames `(t, image)` and
input dicts on the host clock (`crossing`, `throttle`, `settings`). Live, the stream is what
the capture thread and the RaceLab link produce; offline, it is read back from a recording
(`recorder.RecordingReader`) or produced by the synthetic camera: the same code runs, so a
replay gives the same output as the live session, bit for bit when the video is lossless.

The three seams (plain duck typing):

- frame source: iterable of `(t_host_monotonic_mid_exposure, image_bgr)`;
- inputs: dicts `{"type": "crossing", "t", "slot", "cu_ts", "sector"?}`,
  `{"type": "throttle", "t", "slot", "applied"}` (None = a human drives),
  `{"type": "settings", "slot", "speed", "brake"}`;
- sink: `set_track(geometry)` and `publish(t, cars)`, in the RaceLab wire format
  (geometry `{points, y_axis, finish, changers, lane_offset, name, length_m}`; cars
  `[{slot, s, lane, v, off}]`, `s` = fraction of a lap from the finish line).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from .detect import OFF, Detector, DetectorConfig
from .geometry import MapModel
from .timing import ClockSync
from .tracker import Detection, Tracker, TrackerConfig, TrackerMap, estimate_latency


class Sink(Protocol):
    def set_track(self, geometry: dict[str, Any]) -> None: ...
    def publish(self, t: float, cars: list[dict[str, Any]]) -> None: ...


def tracker_map(model: MapModel) -> TrackerMap:
    """The circuit as the tracker sees it: metres on the centre line."""
    L = model.length_c
    return TrackerMap(
        length=L, kappa=model.kappa(),
        blind=tuple((a * L, b * L) for a, b in model.blind),
        changers=tuple(c * L for c in model.changers),
        anchors={s: c * L for s, c in model.anchors.items()},
        gain_hint=model.gain_hint)


@dataclass
class PipelineConfig:
    latency_s: float = 0.0  # camera latency still to remove from the frame times (frames show the past)
    expected_slots: tuple[int, ...] = ()  # cars on the track, if known (one car: no crossing needed to name it)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)


class Pipeline:
    def __init__(self, model: MapModel, cfg: PipelineConfig | None = None, *, background: np.ndarray | None = None) -> None:
        self.cfg = cfg or PipelineConfig()
        self.model = model
        bg = background if background is not None else model.background
        if bg is None:
            raise ValueError("the map has no background image: re-run `map`")
        self.detector = Detector(bg, self.cfg.detector, model=model)
        self.tracker = Tracker(tracker_map(model), self.cfg.tracker, expected_slots=self.cfg.expected_slots)
        self.clock = ClockSync()
        self.log: list[dict[str, Any]] = []  # one entry per frame: the output, for comparisons
        self.times_ms: list[float] = []
        self.last_blobs: list[Any] = []
        self.keep_log = True
        self.lock = threading.RLock()  # the live publisher reads while the worker updates

    # ------------------------------------------------------------------ inputs

    def on_input(self, msg: dict[str, Any]) -> None:
        with self.lock:
            self._on_input(msg)

    def _on_input(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "crossing":
            if "cu_ts" not in msg:
                return
            # plain Python numbers everywhere: numpy scalars round differently (a replay must match to the bit)
            self.clock.add(int(msg["cu_ts"]), float(msg["t"]))
            t_cross = self.clock.cu_to_host(int(msg["cu_ts"]))
            if t_cross is not None:
                self.tracker.add_anchor(int(msg["slot"]), int(msg.get("sector", 1)), float(t_cross))
        elif kind == "throttle":
            applied = msg.get("applied")
            self.tracker.set_applied(int(msg["slot"]), None if applied is None else int(applied),
                                     None if msg.get("t") is None else float(msg["t"]))
        elif kind == "settings":
            self.tracker.set_settings(int(msg["slot"]), int(msg.get("speed", 15)), int(msg.get("brake", 15)))

    # ------------------------------------------------------------------ frames

    def detections(self, t: float, image: np.ndarray) -> list[Detection]:
        m = self.model
        L = m.length_c
        out: list[Detection] = []
        blobs = self.detector.detect(image)
        self.last_blobs = blobs
        for b in blobs:
            if b.zone == OFF:
                lane, c = min(((ln.lane_id, m.project_lane(ln.lane_id, (b.cx, b.cy))) for ln in m.lanes),
                              key=lambda e: e[1][2])[0:2]
                out.append(Detection(t, lane, float(c[0] * L), float(b.area), off=True))
                continue
            c, _, _ = m.project_lane(b.zone, (b.cx, b.cy))
            mpp = m.metres_per_pixel((b.cx, b.cy))
            if b.big:
                out.append(Detection(t, b.zone, float(c * L), float(b.area), occluder=True, extent=float(0.5 * max(b.w, b.h) * mpp)))
            else:
                # a car that is only partly visible (bridge edge, hand) has its centroid pulled towards the
                # visible part: trust it less in proportion to what is missing
                seen = b.area * mpp * mpp / m.car_area_m2 if m.car_area_m2 else 1.0
                sigma = self.cfg.tracker.sigma_meas * (1.0 + 8.0 * max(0.0, 0.85 - seen))
                out.append(Detection(t, b.zone, float(c * L), float(b.area), sigma=float(min(sigma, 0.15))))
        return out

    def on_frame(self, t: float, image: np.ndarray) -> list[dict[str, Any]]:
        """Process a frame; returns the positions at the frame's effective time."""
        t0 = time.perf_counter()
        t_eff = float(t) - self.cfg.latency_s
        dets = self.detections(t_eff, image)  # the heavy part, outside the lock
        with self.lock:
            self.tracker.update(t_eff, dets)
            cars = self.tracker.positions(t_eff)
        self.times_ms.append((time.perf_counter() - t0) * 1000)
        if self.keep_log:
            self.log.append({"t": t_eff, "cars": cars})
        return cars

    # ------------------------------------------------------------------ outputs

    def positions(self, now: float) -> list[dict[str, Any]]:
        """Positions at host time `now` (predicted forward from the last frame), for publication.
        `now` is on the host clock; the frame times carry the latency correction already."""
        with self.lock:
            return self.tracker.positions(now - self.cfg.latency_s)

    def latency_estimate(self) -> dict[str, float | None]:
        return estimate_latency(self.tracker.crossings)


def merge(frames: Iterable[tuple[float, np.ndarray]], inputs: Iterable[dict[str, Any]],
          half_frame_s: float = 1 / 60) -> Iterator[tuple[str, Any]]:
    """Frames and inputs in arrival order: ("frame", (t, image)) / ("input", dict).

    A frame arrives half a frame after the middle of its exposure; an input at its `recv` time
    if it has one (it was logged by a live session), else at `t`."""
    pending = iter(sorted(inputs, key=lambda m: m.get("recv", m["t"])))
    nxt = next(pending, None)
    for t, img in frames:
        arrival = t + half_frame_s
        while nxt is not None and nxt.get("recv", nxt["t"]) <= arrival:
            yield "input", nxt
            nxt = next(pending, None)
        yield "frame", (t, img)
    while nxt is not None:
        yield "input", nxt
        nxt = next(pending, None)


def run_stream(pipe: Pipeline, items: Iterable[tuple[str, Any]], sink: Sink | None = None,
               publish_hz: float = 30.0, after_frame: Any = None) -> Pipeline:
    """Feed a whole stream to the pipeline (offline); publishes at `publish_hz` of stream time.
    `after_frame(image, pipe)` is called after each frame (debug overlay)."""
    last_pub = None
    if sink is not None:
        sink.set_track(pipe.model.geometry())
    for kind, payload in items:
        if kind == "input":
            pipe.on_input(payload)
            continue
        t, image = payload
        cars = pipe.on_frame(t, image)
        if sink is not None and (last_pub is None or t - last_pub >= 1.0 / publish_hz - 1e-6):
            sink.publish(t - pipe.cfg.latency_s, cars)
            last_pub = t
        if after_frame is not None:
            after_frame(image, pipe)
    return pipe
