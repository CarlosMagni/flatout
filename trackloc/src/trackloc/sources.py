"""Frame sources (the first seam): something that yields `(t, image)`.

`t` is the host `time.monotonic()` time of the MIDDLE of the exposure: the arrival time of the
frame minus half a frame period (`exposure_offset_s`, configurable). The rest of the camera's
latency (a phone through Continuity Camera or a webcam: tens to over 100 ms) is NOT known here:
it is estimated from the CU crossings (`replay` prints it) and given as `--latency`.

- `resolve_camera()`: `--camera` as an index, a part of the device name (`iphone`), a stream URL
  (`rtsp://…`, `http://…`), or nothing (asks which one, in a terminal).
- `CameraSource`: OpenCV `CAP_AVFOUNDATION` by index (or OpenCV's FFmpeg backend for a URL). A dedicated thread reads continuously (a
  late read hands back stale buffered frames with wrong times) and calls `on_frame(t, t_raw, image)`.
- `FfmpegSource`: the fallback, `ffmpeg -f avfoundation` piped as raw video.
- `FileSource`: a video file (replay, or the synthetic video written by `synth`).
- `probe()`: lists the cameras with the measured frame rate and resolution.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import cv2
import numpy as np

PERMISSION_HELP = (
    "No camera could be opened. On macOS, the app that runs this command (Terminal, iTerm, VS Code, "
    "Claude Code...) needs the camera permission: System Settings > Privacy & Security > Camera, enable it "
    "for that app, then quit and reopen the app and run the command again. If an iPhone is the camera, "
    "unlock it, keep it near the Mac with Wi-Fi and Bluetooth on (Continuity Camera), and check its index "
    "with `python -m trackloc probe`."
)


def transform(image: np.ndarray, mirror: bool = False, rotate: int = 0) -> np.ndarray:
    """Orientation fixes for odd cameras: a mirrored image would swap left and right lanes."""
    if mirror:
        image = cv2.flip(image, 1)
    if rotate:
        image = cv2.rotate(image, {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
                                   270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rotate % 360])
    return image


def ffmpeg_devices() -> dict[int, str]:
    """Names of the AVFoundation video devices as ffmpeg numbers them (this is not always OpenCV's order)."""
    if not shutil.which("ffmpeg"):
        return {}
    out = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                         capture_output=True, text=True, timeout=15).stderr
    names, video = {}, False
    for line in out.splitlines():
        if "video devices" in line:
            video = True
        elif "audio devices" in line:
            video = False
        elif video and (m := re.search(r"\[(\d+)\]\s+(.+)$", line)):
            names[int(m.group(1))] = m.group(2).strip()
    return names


def resolve_camera(spec: str | int | None, ask: Callable[[str], str] | None = None) -> int | str:
    """`--camera` to what the sources open: an AVFoundation index (int) or a stream URL (str).

    - `None`: in a terminal, list the cameras and ask (`ask` = `input`); otherwise an error;
    - digits: that index;
    - `scheme://...`: a network stream (an IP-camera app on a phone, another computer), kept as is;
    - anything else: a case-insensitive part of the device name, e.g. `iphone`, `desk view`."""
    if isinstance(spec, int):
        return spec
    if spec is not None and spec.strip().isdigit():
        return int(spec)
    if spec is not None and "://" in spec:
        return spec.strip()
    names = ffmpeg_devices()
    if spec is None:
        if ask is None:
            raise OSError("no camera chosen: give --camera INDEX, --camera NAME (e.g. iphone) or --camera URL "
                          "(see `python -m trackloc probe`)")
        if not names:
            raise OSError("no camera listed (ffmpeg missing?): give --camera INDEX or --camera URL")
        for i, n in names.items():
            print(f"  [{i}] {n}")
        answer = ask("camera (number, part of the name, or a stream URL): ").strip()
        if not answer:
            raise OSError("no camera chosen")
        return resolve_camera(answer)
    want = spec.casefold().strip()
    hits = [i for i, n in names.items() if want in n.casefold()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        listed = "; ".join(f"[{i}] {n}" for i, n in names.items()) or "none listed (is ffmpeg installed?)"
        raise OSError(f"no camera name contains {spec!r}. Cameras: {listed}")
    raise OSError(f"{spec!r} matches several cameras: " + "; ".join(f"[{i}] {names[i]}" for i in hits)
                  + ". Be more precise, or give the index")


class CameraSource:
    def __init__(self, index: int | str = 0, *, size: tuple[int, int] | None = None, fps: float | None = None,
                 mirror: bool = False, rotate: int = 0, exposure_offset_s: float | None = None) -> None:
        self.index, self.size, self.fps = index, size, fps
        self.mirror, self.rotate = mirror, rotate
        self.exposure_offset_s = exposure_offset_s
        self.cap: cv2.VideoCapture | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.frames_read = 0
        self.gaps = 0  # reads that failed (camera dropped): counted, then the camera is reopened

    def open(self) -> CameraSource:
        url = isinstance(self.index, str)
        cap = cv2.VideoCapture(self.index, cv2.CAP_FFMPEG if url else cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            raise OSError(f"stream {self.index} did not open (check the URL, and that the phone/computer is on the "
                          "same network)" if url else f"camera {self.index} did not open. " + PERMISSION_HELP)
        if self.size:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        if self.fps:
            cap.set(cv2.CAP_PROP_FPS, self.fps)
        self.cap = cap
        return self

    def info(self) -> dict[str, Any]:
        assert self.cap is not None
        return {"width": int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)), "height": int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "fps": float(self.cap.get(cv2.CAP_PROP_FPS)) or self.fps or 30.0}

    def read_one(self) -> tuple[float, np.ndarray] | None:
        assert self.cap is not None
        ok, image = self.cap.read()
        t_raw = time.monotonic()
        if not ok:
            return None
        self.frames_read += 1
        return t_raw, transform(image, self.mirror, self.rotate)

    def start(self, on_frame: Callable[[float, float, np.ndarray], None]) -> None:
        """Read in a thread; `on_frame(t, t_raw, image)` must return fast."""
        half = self.exposure_offset_s if self.exposure_offset_s is not None else 0.5 / max(self.info()["fps"], 1.0)

        def loop() -> None:
            while not self._stop.is_set():
                got = self.read_one()
                if got is None:  # the camera dropped (Continuity can): reopen, count the gap
                    self.gaps += 1
                    self.close_cap()
                    time.sleep(0.5)
                    try:
                        self.open()
                    except OSError:
                        time.sleep(1.0)
                    continue
                on_frame(got[0] - half, got[0], got[1])

        self._thread = threading.Thread(target=loop, name="trackloc-capture", daemon=True)
        self._thread.start()

    def close_cap(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(3)
        self.close_cap()


class FfmpegSource:
    """Fallback capture: `ffmpeg -f avfoundation` (or a stream URL) piped as raw BGR frames (the size must be
    given; a stream is scaled to it)."""

    def __init__(self, index: int | str = 0, *, size: tuple[int, int] = (1280, 720), fps: float = 30.0,
                 mirror: bool = False, rotate: int = 0, exposure_offset_s: float | None = None) -> None:
        if not shutil.which("ffmpeg"):
            raise OSError("ffmpeg is not installed (brew install ffmpeg)")
        self.index, self.size, self.fps = index, size, fps
        self.mirror, self.rotate = mirror, rotate
        self.half = exposure_offset_s if exposure_offset_s is not None else 0.5 / fps
        self.proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def info(self) -> dict[str, Any]:
        return {"width": self.size[0], "height": self.size[1], "fps": self.fps}

    def start(self, on_frame: Callable[[float, float, np.ndarray], None]) -> None:
        w, h = self.size
        if isinstance(self.index, str):  # network stream
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-i", self.index,
                   "-vf", f"scale={w}:{h}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
        else:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-framerate", f"{self.fps:g}",
                   "-video_size", f"{w}x{h}", "-i", f"{self.index}:none", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        n = w * h * 3

        def loop() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            while not self._stop.is_set():
                buf = self.proc.stdout.read(n)
                t_raw = time.monotonic()
                if len(buf) < n:
                    err = (self.proc.stderr.read() if self.proc.stderr else b"").decode(errors="replace")
                    raise OSError("ffmpeg stopped: " + (err.strip() or "no output") + "\n" + PERMISSION_HELP)
                image = np.frombuffer(buf, np.uint8).reshape(h, w, 3).copy()
                on_frame(t_raw - self.half, t_raw, transform(image, self.mirror, self.rotate))

        self._thread = threading.Thread(target=loop, name="trackloc-ffmpeg", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.proc:
            self.proc.terminate()
        if self._thread:
            self._thread.join(3)


class FileSource:
    """A video file as a frame source. Times: from `times` (one per frame) if given, else
    `t0 + i / fps` (so a plain video replays on a clock of its own)."""

    def __init__(self, path: str, *, times: list[float] | None = None, t0: float = 0.0, mirror: bool = False,
                 rotate: int = 0) -> None:
        self.path, self.times, self.t0 = path, times, t0
        self.mirror, self.rotate = mirror, rotate

    def frames(self) -> Iterator[tuple[float, np.ndarray]]:
        cap = cv2.VideoCapture(self.path)
        if not cap.isOpened():
            raise OSError(f"cannot open the video {self.path}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        try:
            i = 0
            while True:
                ok, image = cap.read()
                if not ok:
                    return
                if self.times is not None and i >= len(self.times):
                    return
                yield (self.times[i] if self.times is not None else self.t0 + i / fps), transform(image, self.mirror, self.rotate)
                i += 1
        finally:
            cap.release()


def probe(max_index: int = 4, only: int | None = None, frames: int = 30) -> list[dict[str, Any]]:
    """Open the cameras one by one, read some frames, report resolution and measured frame rate.
    Opening a camera turns it on (an iPhone shows its Continuity Camera banner)."""
    names = ffmpeg_devices()
    found = []
    for idx in ([only] if only is not None else range(max_index)):
        cap = cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)
        info: dict[str, Any] = {"index": idx, "name": names.get(idx), "opened": cap.isOpened()}
        if cap.isOpened():
            t = []
            shape = None
            for _ in range(frames):
                ok, image = cap.read()
                if ok:
                    t.append(time.monotonic())
                    shape = image.shape
            info.update(frames_read=len(t))
            if shape is not None:
                info.update(width=shape[1], height=shape[0])
            if len(t) > 5:
                info["fps"] = round((len(t) - 1) / (t[-1] - t[0]), 1)
                gaps = np.diff(t)
                info["jitter_ms"] = round(float(np.std(gaps)) * 1000, 1)
        cap.release()
        found.append(info)
    return found
