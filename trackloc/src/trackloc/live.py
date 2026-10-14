"""Live orchestration: one ordered stream, one worker, the publisher.

Capture thread (frames), the RaceLab link (inputs) and anything else put items in ONE `Stream`;
a single worker thread takes them in order, logs each to the recorder (same order, with a
sequence number) and feeds the pipeline. The 30 Hz publisher is a separate thread that predicts
the cars forward to `now` and calls the sink. Heavy vision work never runs in the thread that
drives a car (see `drive`).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import cv2
import numpy as np

from .pipeline import Pipeline, Sink
from .recorder import Recorder

log = logging.getLogger("trackloc.live")


class Stream:
    """The ordered stream of a live session. Frames are dropped when the consumer lags (never inputs)."""

    def __init__(self, max_frames_backlog: int = 6) -> None:
        self._q: queue.Queue[tuple[int, str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._seq = 0
        self.max_backlog = max_frames_backlog
        self._frames_pending = 0
        self.dropped = 0

    def put_frame(self, t: float, t_raw: float, image: np.ndarray) -> bool:
        with self._lock:
            if self._frames_pending >= self.max_backlog:
                self.dropped += 1
                return False
            self._seq += 1
            self._frames_pending += 1
            self._q.put((self._seq, "frame", (t, t_raw, image)))
        return True

    def put_input(self, msg: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            self._q.put((self._seq, "input", {**msg, "recv": time.monotonic()}))

    def get(self, timeout: float) -> tuple[int, str, Any] | None:
        try:
            item = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item[1] == "frame":
            with self._lock:
                self._frames_pending -= 1
        return item


class MotionMeter:
    """Cumulative amount of motion in the frames (thumbnail differences): the progress the stall
    detector watches. Independent of any background model, so it works during the discovery lap."""

    def __init__(self, noise: float = 6.0) -> None:
        self.value = 0.0
        self.noise = noise
        self._prev: np.ndarray | None = None

    def update(self, image: np.ndarray) -> None:
        small = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (96, 54), interpolation=cv2.INTER_AREA)
        if self._prev is not None:
            moved = int((cv2.absdiff(small, self._prev) > self.noise).sum())
            if moved >= 3:  # a few thumbnail pixels changed: something moved (not sensor noise)
                self.value += moved
        self._prev = small


class Live:
    def __init__(self, *, recorder: Recorder | None = None, pipeline: Pipeline | None = None,
                 meter: MotionMeter | None = None, overlay: Any = None, defer: bool = False) -> None:
        """`defer=True`: capture starts at once but nothing is logged or processed until `activate()`
        (the pipeline needs the first frame to register the map); `first_frame` is kept meanwhile."""
        self.stream = Stream()
        self.active = not defer
        self.first_frame: np.ndarray | None = None
        self.recorder, self.pipeline, self.meter, self.overlay = recorder, pipeline, meter, overlay
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._work, name="trackloc-worker", daemon=True)
        self._pub: threading.Thread | None = None
        self.frames = 0
        self.crossings: dict[int, int] = {}
        self.error: BaseException | None = None
        self.last_frame_t = 0.0

    def on_frame(self, t: float, t_raw: float, image: np.ndarray) -> None:
        self.stream.put_frame(t, t_raw, image)

    def on_input(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "crossing" and msg.get("sector", 1) == 1:
            self.crossings[msg["slot"]] = self.crossings.get(msg["slot"], 0) + 1
        self.stream.put_input(msg)

    def crossing_count(self, slot: int) -> int:
        return self.crossings.get(slot, 0)

    def progress(self) -> float:
        return self.meter.value if self.meter else 0.0

    def start(self) -> None:
        self._worker.start()

    def start_publisher(self, sink: Sink, hz: float = 30.0) -> None:
        assert self.pipeline is not None

        def loop() -> None:
            period = 1.0 / hz
            nxt = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= nxt:
                    try:
                        sink.publish(now, self.pipeline.positions(now))
                    except (ConnectionError, OSError) as e:
                        self.error = e
                        return
                    except Exception:
                        log.exception("publish")
                    nxt += period
                    if nxt < now - 1.0:
                        nxt = now
                self._stop.wait(max(0.0, min(period / 4, nxt - time.monotonic())))

        self._pub = threading.Thread(target=loop, name="trackloc-publisher", daemon=True)
        self._pub.start()

    def _work(self) -> None:
        while not (self._stop.is_set() and self.stream._q.empty()):
            item = self.stream.get(0.1)
            if item is None:
                continue
            seq, kind, payload = item
            if not self.active:  # not logged, not processed: a recording starts where the pipeline starts
                if kind == "frame" and self.first_frame is None:
                    self.first_frame = payload[2]
                continue
            try:
                if kind == "frame":
                    t, t_raw, image = payload
                    if self.recorder and not self.recorder.log_frame(seq, t, t_raw, image):
                        continue  # not logged, so not processed: replay sees what we saw
                    self.frames += 1
                    self.last_frame_t = t
                    if self.meter:
                        self.meter.update(image)
                    if self.pipeline:
                        self.pipeline.on_frame(t, image)
                        if self.overlay:
                            self.overlay.write(image, self.pipeline)
                else:
                    if self.recorder:
                        self.recorder.log_input(seq, payload)
                    if self.pipeline:
                        self.pipeline.on_input(payload)
            except Exception as e:
                log.exception("worker")
                self.error = e

    def stop(self) -> None:
        self._stop.set()
        self._worker.join(10)
        if self._pub:
            self._pub.join(3)
        if self.overlay:
            self.overlay.close()
