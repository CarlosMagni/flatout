"""Recording and replaying a session: the log of the one stream the pipeline consumed.

A recording is a folder:

    video.mkv        the frames (MJPG by default; FFV1 = lossless, bit-exact replay)
    frames.jsonl     {"seq", "i", "t", "t_raw"} per recorded frame: t = host time of the middle of
                     the exposure, t_raw = arrival, i = index in the video
    inputs.jsonl     the input dicts (crossing / throttle / settings) with their arrival stamp
                     "recv" and sequence number "seq"; also {"type": "mark", ...} lines
                     (e.g. the empty-track frames of `map --background empty`)
    racelab.jsonl    RaceLab events and state snapshots as received (raw data, for analysis)
    meta.json        parameters, camera, the CU->host clock offset estimate

`seq` numbers the items in the order the pipeline consumed them (frames and inputs share one
counter), so `RecordingReader.items()` restores EXACTLY that order. Everything is on the host
`time.monotonic()` clock. The JSONL files are flushed line by line: a crash loses at most the
frames still in the writer queue.

`JsonlSink` is the sink seam for tests and replay: `set_track` / `publish` written as JSON lines.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .timing import ClockSync

CODECS = {"mjpg": "MJPG", "ffv1": "FFV1"}
VIDEO = "video.mkv"


class Recorder:
    """Writes a recording. Thread safe: `log_frame` from the stream worker, `log_input` from any thread."""

    def __init__(self, path: str | Path, *, fps: float, size: tuple[int, int], codec: str = "mjpg",
                 meta: dict[str, Any] | None = None, queue_frames: int = 90) -> None:
        import cv2  # noqa: PLC0415 - only needed (and imported) when a video is written

        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        if codec not in CODECS:
            raise ValueError(f"codec: one of {', '.join(CODECS)}")
        self.fps, self.size, self.codec = fps, size, codec
        self.meta = dict(meta or {})
        self._writer = cv2.VideoWriter(str(self.path / VIDEO), cv2.VideoWriter_fourcc(*CODECS[codec]), fps, size)
        if not self._writer.isOpened():
            raise OSError(f"cannot write {self.path / VIDEO} with codec {codec}")
        self._frames = open(self.path / "frames.jsonl", "w", encoding="utf-8")
        self._inputs = open(self.path / "inputs.jsonl", "w", encoding="utf-8")
        self._raw = open(self.path / "racelab.jsonl", "w", encoding="utf-8")
        self._lock = threading.Lock()
        self._q: queue.Queue[tuple[int, int, float, float, Any] | None] = queue.Queue(maxsize=queue_frames)
        self._n = 0
        self.dropped = 0
        self.clock = ClockSync()
        self.started = time.time()
        self._thread = threading.Thread(target=self._write_loop, name="trackloc-writer", daemon=True)
        self._thread.start()
        self._write_meta()

    # ------------------------------------------------------------------ logging

    def log_frame(self, seq: int, t: float, t_raw: float, image: Any) -> bool:
        """Queue a frame for the video. False (and counted) when the writer is too far behind:
        the caller must then not process the frame either, so replay sees what the live run saw."""
        try:
            self._q.put_nowait((seq, self._n, t, t_raw, image))
        except queue.Full:
            self.dropped += 1
            return False
        self._n += 1
        return True

    def log_input(self, seq: int | None, msg: dict[str, Any]) -> None:
        rec = {**msg, "seq": seq}
        with self._lock:
            self._inputs.write(json.dumps(rec) + "\n")
            self._inputs.flush()
        if msg.get("type") == "crossing" and "cu_ts" in msg:
            self.clock.add(msg["cu_ts"], msg["t"])

    def log_raw(self, kind: str, name: str, data: Any, t: float | None = None) -> None:
        """A raw RaceLab event or state snapshot, as received."""
        with self._lock:
            self._raw.write(json.dumps({"t": time.monotonic() if t is None else t, "kind": kind, "name": name,
                                        "data": data}) + "\n")
            self._raw.flush()

    def mark(self, name: str, **data: Any) -> None:
        self.log_input(None, {"type": "mark", "t": time.monotonic(), "name": name, **data})

    def _write_loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            seq, i, t, t_raw, image = item
            self._writer.write(image)
            with self._lock:
                self._frames.write(json.dumps({"seq": seq, "i": i, "t": t, "t_raw": t_raw}) + "\n")
                self._frames.flush()

    # ------------------------------------------------------------------ end

    def _write_meta(self) -> None:
        meta = {"fps": self.fps, "size": list(self.size), "codec": self.codec, "frames": self._n,
                "dropped_frames": self.dropped, "started_wall": self.started,
                "clock": {"offset_s": self.clock.offset, "events": self.clock.n, "window_s": self.clock.window_s,
                          "note": "host = cu_time + offset; minimum of (host receive - cu time), rolling window"},
                **self.meta}
        (self.path / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    def close(self) -> None:
        self._q.put(None)
        self._thread.join(30)
        self._writer.release()
        self._write_meta()
        for f in (self._frames, self._inputs, self._raw):
            f.close()


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        break  # a torn last line after a crash


def load_inputs(path: str | Path) -> list[dict[str, Any]]:
    """Input dicts from a JSONL file, or from the `inputs.jsonl` of a recording folder."""
    p = Path(path)
    p = p / "inputs.jsonl" if p.is_dir() else p
    return [m for m in _lines(p) if m.get("type") in ("crossing", "throttle", "settings", "mark")]


class RecordingReader:
    """Reads a recording back as the frame source (`frames()`), the inputs and the merged stream."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.meta: dict[str, Any] = json.loads((self.path / "meta.json").read_text(encoding="utf-8")) \
            if (self.path / "meta.json").exists() else {}
        self.frame_log = list(_lines(self.path / "frames.jsonl"))
        self.input_log = list(_lines(self.path / "inputs.jsonl"))

    @property
    def fps(self) -> float:
        return float(self.meta.get("fps") or 30.0)

    def marks(self, name: str) -> list[dict[str, Any]]:
        return [m for m in self.input_log if m.get("type") == "mark" and m.get("name") == name]

    def inputs(self) -> list[dict[str, Any]]:
        return [m for m in self.input_log if m.get("type") in ("crossing", "throttle", "settings")]

    def frames(self) -> Iterator[tuple[float, Any]]:
        """The frame source seam: (t, image) in recording order."""
        import cv2  # noqa: PLC0415

        cap = cv2.VideoCapture(str(self.path / VIDEO))
        try:
            for rec in self.frame_log:
                ok, image = cap.read()
                if not ok:
                    return
                yield rec["t"], image
        finally:
            cap.release()

    def items(self) -> Iterator[tuple[str, Any]]:
        """Frames and inputs in the order the pipeline consumed them: ("frame", (t, image)) / ("input", dict)."""
        pending = sorted((m for m in self.input_log if m.get("seq") is not None and m.get("type") != "mark"),
                         key=lambda m: m["seq"])
        k = 0
        for (t, image), rec in zip(self.frames(), self.frame_log):
            while k < len(pending) and pending[k]["seq"] < rec["seq"]:
                yield "input", _strip(pending[k])
                k += 1
            yield "frame", (t, image)
        for m in pending[k:]:
            yield "input", _strip(m)


def _strip(msg: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in msg.items() if k != "seq"}


class JsonlSink:
    """Sink seam writing what would be sent to RaceLab as JSON lines (tests, replay, dry runs)."""

    def __init__(self, path: str | Path) -> None:
        self._f = open(path, "w", encoding="utf-8")

    def set_track(self, geometry: dict[str, Any]) -> None:
        self._f.write(json.dumps({"type": "track", "geometry": geometry}) + "\n")

    def publish(self, t: float, cars: list[dict[str, Any]]) -> None:
        self._f.write(json.dumps({"type": "positions", "t": t, "cars": cars}) + "\n")

    def close(self) -> None:
        self._f.close()
