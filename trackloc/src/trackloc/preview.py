"""Camera preview for AIMING the camera: `python -m trackloc preview [--map M] [--port 8140] [--snapshot FILE]`.

The OpenCV build is headless (no window), so this serves a tiny read-only page on 127.0.0.1 with an MJPEG
stream (~10 fps) of the camera, annotated with what `map` and `run` will need:

- the ArUco marker outline and "marker OK / not found" (the metres of the map come from it);
- the capture resolution and the MEASURED frame rate;
- a coverage hint: does the track fill the frame, is it cut by an edge (lane lines of `--map`, else where
  things have been seen moving so far);
- an outline of whatever moves (a hand, a car), and a warning when the WHOLE picture keeps changing
  (exposure / focus / white balance not locked);
- the lane polylines of `--map`, as recorded (not re-aligned: a bumped camera shifts them, `run` re-registers).

`--snapshot FILE` writes one annotated JPEG per second instead of serving (no browser needed). The page and
the terminal show the same short camera checklist.

Safety: this module imports nothing that can move a car (no RaceLab link, no driving). The page is protected
like any localhost page: bound to 127.0.0.1 only, GET only, the Host must be a loopback name (DNS rebinding),
requests flagged cross-site by the browser (`Sec-Fetch-Site`, `Origin`) are refused, no CORS header, a strict
Content-Security-Policy, no cookies, no state.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from aiohttp import web

from .geometry import MapModel, find_marker

LOCALHOST = "127.0.0.1"
DEFAULT_PORT = 8140
BOUNDARY = "frame"
ALLOWED_HOSTS = web.AppKey("allowed_hosts", set)
MAX_STREAMS = 4
VIEW_WIDTH = 1280  # frames wider than this are scaled down for analysis and streaming
MIN_MARKER_SIDE_PX = 48  # below this (in the capture resolution) the marker's corners are too coarse
FILL_OK = 0.6  # the track should span at least this fraction of the frame width or height
EDGE_MARGIN = 0.01

# ----------------------------------------------------------------------------- texts (English + French)

CHECKLIST_KEYS = ["lock", "fps_item", "mount", "light", "view", "marker_item"]

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "title": "Aim the camera",
        "status_title": "Status",
        "lead": "Live view, about 10 images per second. Read-only: nothing on this page can move a car.",
        "waiting": "waiting for the first frame...",
        "stopped": "the preview stopped (is the command still running?)",
        "marker_ok": "marker OK",
        "marker_missing": "marker not found",
        "marker_small": "marker OK but small ({px} px wide): bring it closer or print it bigger",
        "marker_help": "Lay the printed marker flat in view, no shadow or reflection on it.",
        "cov_none": "coverage unknown: give --map, or move a car round the track",
        "cov_ok": "whole track in view (spans {pct}% of the frame)",
        "cov_small": "the track spans only {pct}% of the frame: move closer or zoom in",
        "cov_cut": "the track touches the frame edge ({edges}): move back, the whole track must be in view",
        "cov_motion": "seen moving so far: {pct}% of the frame",
        "edge_left": "left", "edge_right": "right", "edge_top": "top", "edge_bottom": "bottom",
        "motion_none": "nothing moving",
        "motion_some": "{n} moving thing(s) outlined",
        "motion_global": "the WHOLE picture is changing: lock exposure, focus and white balance",
        "map_note": "lane lines = the map as recorded, not re-aligned (a bumped camera shifts them)",
        "learning": "learning the still background...",
        "res": "{w}x{h}",
        "fps": "{fps} fps",
        "fps_unknown": "fps ?",
        "checklist_title": "Camera checklist",
        "lock": "Lock exposure, focus and white balance (iPhone: touch and hold the picture until AE/AF LOCK shows; "
                "an IP-camera app has the same locks).",
        "fps_item": "Fixed 30 or 60 fps (no automatic frame rate: it drops in dim light).",
        "mount": "Rigid mount: tripod or clamp, no wobble when the table is knocked.",
        "light": "Flicker-free light: no bands rolling across the picture (cheap LED and fluorescent tubes flicker).",
        "view": "The whole track in view, with a margin all round.",
        "marker_item": "The printed marker flat on the table and visible (this page says \"marker OK\").",
        "language": "Language",
        "done_hint": "When it is all ticked: python -m trackloc record, then map.",
        "snap_writing": "writing {path} once a second (Ctrl-C to stop); open it in any image viewer",
        "open_url": "open http://{host}:{port}/ in a browser (this computer only; read-only; Ctrl-C to stop)",
    },
    "fr": {
        "title": "Viser la caméra",
        "status_title": "État",
        "lead": "Vue en direct, environ 10 images par seconde. Lecture seule : rien sur cette page ne peut déplacer une voiture.",
        "waiting": "en attente de la première image...",
        "stopped": "l'aperçu s'est arrêté (la commande tourne-t-elle encore ?)",
        "marker_ok": "marqueur OK",
        "marker_missing": "marqueur introuvable",
        "marker_small": "marqueur OK mais petit ({px} px de large) : rapprochez-le ou imprimez-le plus grand",
        "marker_help": "Posez le marqueur imprimé à plat dans le champ, sans ombre ni reflet dessus.",
        "cov_none": "couverture inconnue : donnez --map, ou faites rouler une voiture sur le circuit",
        "cov_ok": "tout le circuit est dans l'image (occupe {pct} % de l'image)",
        "cov_small": "le circuit n'occupe que {pct} % de l'image : rapprochez-vous ou zoomez",
        "cov_cut": "le circuit touche le bord de l'image ({edges}) : reculez, tout le circuit doit être visible",
        "cov_motion": "vu en mouvement jusqu'ici : {pct} % de l'image",
        "edge_left": "gauche", "edge_right": "droite", "edge_top": "haut", "edge_bottom": "bas",
        "motion_none": "rien ne bouge",
        "motion_some": "{n} objet(s) en mouvement entouré(s)",
        "motion_global": "TOUTE l'image change : verrouillez exposition, mise au point et balance des blancs",
        "map_note": "lignes de voie = la carte telle qu'enregistrée, non recalée (une caméra bougée les décale)",
        "learning": "apprentissage du fond immobile...",
        "res": "{w}x{h}",
        "fps": "{fps} i/s",
        "fps_unknown": "i/s ?",
        "checklist_title": "Check-list caméra",
        "lock": "Verrouillez exposition, mise au point et balance des blancs (iPhone : appui long sur l'image jusqu'à "
                "« Verrouillage AE/AF » ; une appli de caméra IP a les mêmes verrous).",
        "fps_item": "30 ou 60 i/s fixes (pas de cadence automatique : elle chute quand la lumière baisse).",
        "mount": "Fixation rigide : trépied ou pince, aucun balancement quand on cogne la table.",
        "light": "Lumière sans scintillement : pas de bandes qui défilent dans l'image (LED bon marché et néons scintillent).",
        "view": "Tout le circuit dans l'image, avec une marge tout autour.",
        "marker_item": "Le marqueur imprimé à plat sur la table et visible (cette page affiche « marqueur OK »).",
        "language": "Langue",
        "done_hint": "Quand tout est coché : python -m trackloc record, puis map.",
        "snap_writing": "écriture de {path} une fois par seconde (Ctrl-C pour arrêter) ; ouvrez-le dans une visionneuse d'images",
        "open_url": "ouvrez http://{host}:{port}/ dans un navigateur (cet ordinateur seulement ; lecture seule ; Ctrl-C pour arrêter)",
    },
}


def checklist_lines(lang: str) -> list[str]:
    return [MESSAGES[lang][k] for k in CHECKLIST_KEYS]


def checklist_text(lang: str = "en") -> str:
    m = MESSAGES[lang]
    return "\n".join([m["checklist_title"] + ":"] + [f"  [ ] {line}" for line in checklist_lines(lang)] + ["  " + m["done_hint"]])


def ascii_fold(text: str) -> str:
    """cv2.putText only draws ASCII: fold the French accents on the picture (the page and terminal keep them)."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().replace("  ", " ")


def say(lang: str, key: str, **kw: Any) -> str:
    return MESSAGES[lang][key].format(**kw)


# ----------------------------------------------------------------------------- analysis of one frame

def marker_status(image: np.ndarray, scale: float = 1.0, min_side_px: float = MIN_MARKER_SIDE_PX) -> dict[str, Any]:
    """Is the printed marker (DICT_4X4_50, id 0) visible? `scale` = this image / the capture (it may have been
    scaled down): sizes in the result are in the CAPTURE's pixels, corners in this image's.

    Returns {found, corners (4x2 list | None), side_px, hint} with hint None or "marker_small"."""
    corners = find_marker(image)
    if corners is None:
        return {"found": False, "corners": None, "side_px": None, "hint": None}
    edges = [float(np.hypot(*(corners[(i + 1) % 4] - corners[i]))) for i in range(4)]
    side = sum(edges) / 4 / scale
    return {"found": True, "corners": corners.tolist(), "side_px": round(side, 1),
            "hint": "marker_small" if side < min_side_px else None}


def coverage_status(points: np.ndarray | None, size: tuple[int, int], source: str) -> dict[str, Any]:
    """How much of the frame a set of points (lane polylines, or the places where things moved) spans, and which
    edges it touches. `size` = (width, height) of the frame the points are in."""
    w, h = size
    if points is None or len(points) < 2:
        return {"source": None, "fill": None, "cut": [], "hint": "cov_none"}
    x0, y0 = points.min(0)
    x1, y1 = points.max(0)
    cut = [name for name, hit in (("left", x0 <= EDGE_MARGIN * w), ("right", x1 >= (1 - EDGE_MARGIN) * w),
                                  ("top", y0 <= EDGE_MARGIN * h), ("bottom", y1 >= (1 - EDGE_MARGIN) * h)) if hit]
    fill = max((x1 - x0) / w, (y1 - y0) / h)
    hint = "cov_cut" if cut and source == "map" else "cov_small" if fill < FILL_OK and source == "map" else \
        "cov_ok" if source == "map" else "cov_motion"
    return {"source": source, "fill": round(float(fill), 3), "cut": cut, "hint": hint}


class Motion:
    """Outline of what moves: a slow background model (MOG2) on a small copy of the frame."""

    WARMUP = 20
    SMALL = 320

    def __init__(self) -> None:
        self.bg = cv2.createBackgroundSubtractorMOG2(history=150, varThreshold=28, detectShadows=False)
        self.open_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.n = 0
        self.seen: np.ndarray | None = None  # union of the places where something moved (small-copy pixels)

    def update(self, image: np.ndarray) -> dict[str, Any]:
        """{learning, global_change, contours (in `image` pixels)}."""
        h, w = image.shape[:2]
        k = self.SMALL / w if w > self.SMALL else 1.0
        small = cv2.resize(image, (max(8, int(w * k)), max(8, int(h * k))), interpolation=cv2.INTER_AREA) if k != 1.0 else image
        mask = self.bg.apply(small)
        self.n += 1
        if self.n <= self.WARMUP:
            return {"learning": True, "global_change": False, "contours": []}
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.open_k)
        frac = float((mask > 0).mean())
        if frac > 0.4:  # the whole picture changed (exposure hunting, a flash): not an object
            return {"learning": False, "global_change": True, "contours": []}
        if self.seen is None:
            self.seen = np.zeros(mask.shape, bool)
        self.seen |= mask > 0
        cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        floor = 0.0003 * mask.size
        out = [(c.astype(np.float64) / k).astype(np.int32) for c in cs if cv2.contourArea(c) >= floor]
        return {"learning": False, "global_change": False, "contours": out}

    def seen_points(self, size: tuple[int, int]) -> np.ndarray | None:
        """Corners of the box of everything that moved so far, in a frame of `size` (needs some evidence)."""
        if self.seen is None or self.seen.sum() < 0.002 * self.seen.size:
            return None
        ys, xs = np.nonzero(self.seen)
        sx, sy = size[0] / self.seen.shape[1], size[1] / self.seen.shape[0]
        return np.array([[xs.min() * sx, ys.min() * sy], [(xs.max() + 1) * sx, (ys.max() + 1) * sy]])


GREEN, RED, ORANGE, CYAN, YELLOW, WHITE = (60, 220, 60), (60, 60, 235), (30, 150, 255), (255, 220, 0), (0, 230, 255), (245, 245, 245)


def draw_overlay(view: np.ndarray, st: dict[str, Any], lang: str = "en") -> np.ndarray:
    """The annotated copy of `view`. `st` = what `Preview.analyse` returns, with the geometry in the pixels of `view`."""
    out = view.copy()
    h, w = out.shape[:2]
    scale = max(0.5, h / 720.0)
    th = max(1, int(round(2 * scale)))
    for lane_id, pts in st.get("lanes", []):
        cv2.polylines(out, [np.round(pts).astype(np.int32)], True, YELLOW if lane_id == 0 else CYAN, max(1, th - 1), cv2.LINE_AA)
    for c in st.get("motion_contours", []):
        cv2.polylines(out, [c.reshape(-1, 1, 2)], True, ORANGE, th, cv2.LINE_AA)
    mk = st["marker"]
    if mk["found"]:
        cv2.polylines(out, [np.round(np.array(mk["corners"])).astype(np.int32)], True, GREEN, th + 1, cv2.LINE_AA)
    lines: list[tuple[str, tuple[int, int, int]]] = []
    if mk["found"]:
        lines.append((say(lang, "marker_small", px=round(mk["side_px"])) if mk["hint"] else say(lang, "marker_ok"),
                      ORANGE if mk["hint"] else GREEN))
    else:
        lines.append((say(lang, "marker_missing"), RED))
    fps = st.get("fps")
    lines.append((say(lang, "res", w=st["width"], h=st["height"]) + "  " + (say(lang, "fps", fps=f"{fps:.1f}") if fps else say(lang, "fps_unknown")), WHITE))
    cov = st["coverage"]
    lines.append((coverage_text(cov, lang), GREEN if cov["hint"] in ("cov_ok", "cov_motion") else ORANGE if cov["hint"] != "cov_none" else WHITE))
    mo = st["motion"]
    if mo["global"]:
        lines.append((say(lang, "motion_global"), RED))
    elif mo["learning"]:
        lines.append((say(lang, "learning"), WHITE))
    elif mo["blobs"]:
        lines.append((say(lang, "motion_some", n=mo["blobs"]), ORANGE))
    font, fs = cv2.FONT_HERSHEY_SIMPLEX, 0.8 * scale  # readable once the page scales the picture to a phone
    pad = int(8 * scale)
    lh = int(32 * scale)
    texts = [ascii_fold(t) for t, _ in lines]
    bar_w = min(w, max(cv2.getTextSize(t, font, fs, 1)[0][0] for t in texts) + 2 * pad)
    bar = out[:lh * len(lines) + pad, :bar_w].copy()
    out[:lh * len(lines) + pad, :bar_w] = (bar * 0.35).astype(np.uint8)  # dark translucent bar for legibility
    for i, ((_, col), text) in enumerate(zip(lines, texts)):
        cv2.putText(out, text, (pad, pad + lh * (i + 1) - int(8 * scale)), font, fs, col, max(1, int(scale + 0.5)), cv2.LINE_AA)
    return out


def coverage_text(cov: dict[str, Any], lang: str) -> str:
    pct = round(100 * (cov["fill"] or 0))
    edges = ", ".join(say(lang, "edge_" + e) for e in cov["cut"])
    return say(lang, cov["hint"], pct=pct, edges=edges)


def status_line(st: dict[str, Any], lang: str = "en") -> str:
    """One terminal line: marker, resolution, fps, coverage."""
    mk = st["marker"]
    marker = say(lang, "marker_missing") if not mk["found"] else say(lang, "marker_small", px=round(mk["side_px"])) if mk["hint"] else say(lang, "marker_ok")
    fps = say(lang, "fps", fps=f"{st['fps']:.1f}") if st.get("fps") else say(lang, "fps_unknown")
    mo = st["motion"]
    extra = "; " + say(lang, "motion_global") if mo["global"] else ""
    return f"{marker} | {say(lang, 'res', w=st['width'], h=st['height'])} {fps} | {coverage_text(st['coverage'], lang)}{extra}"


# ----------------------------------------------------------------------------- the preview (frames in, JPEG + status out)

class Preview:
    """Capture side: `feed(image)` from the camera thread (fast). Analysis side: `step()` (a thread calls `run`)."""

    def __init__(self, model: MapModel | None = None, lang: str = "en", fps: float = 10.0, jpeg_quality: int = 75) -> None:
        if lang not in MESSAGES:
            raise ValueError(f"language must be one of {sorted(MESSAGES)}")
        self.model, self.lang, self.period = model, lang, 1.0 / fps
        self.quality = jpeg_quality
        self.motion = Motion()
        self._lock = threading.Lock()
        self._raw: np.ndarray | None = None
        self._raw_seq = self._done_seq = 0
        self._arrivals: deque[float] = deque(maxlen=30)
        self._model_cache: tuple[tuple[int, int], MapModel] | None = None
        self._jpeg = self._placeholder()
        self._seq = 0
        self._status: dict[str, Any] = {"frames": 0, "fps": None, "width": None, "height": None, "waiting": True}
        self.frames = 0

    def _placeholder(self) -> bytes:
        img = np.full((360, 640, 3), 30, np.uint8)
        cv2.putText(img, ascii_fold(say(self.lang, "waiting")), (30, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.8, WHITE, 1, cv2.LINE_AA)
        return bytes(cv2.imencode(".jpg", img)[1])

    # -- capture thread

    def feed(self, image: np.ndarray, t: float | None = None) -> None:
        with self._lock:
            self._raw = image
            self._raw_seq += 1
            self.frames += 1
            self._arrivals.append(time.monotonic() if t is None else t)

    def set_model(self, model: MapModel | None) -> None:
        """Draw this map's lanes from now on (a map just built by `serve`)."""
        self._model_cache = None
        self.model = model

    def measured_fps(self) -> float | None:
        with self._lock:
            a = list(self._arrivals)
        return (len(a) - 1) / (a[-1] - a[0]) if len(a) >= 3 and a[-1] > a[0] else None

    # -- analysis

    def _map_for(self, size: tuple[int, int]) -> MapModel | None:
        if self.model is None:
            return None
        if self._model_cache is None or self._model_cache[0] != size:
            k = np.diag([size[0] / self.model.size[0], size[1] / self.model.size[1], 1.0])
            self._model_cache = (size, self.model.warped(k))
        return self._model_cache[1]

    def process(self, image: np.ndarray, fps: float | None = None) -> tuple[np.ndarray, dict[str, Any]]:
        """Annotate one frame: (annotated view, status). The status geometry is in the capture's pixels."""
        h, w = image.shape[:2]
        k = VIEW_WIDTH / w if w > VIEW_WIDTH else 1.0
        view = cv2.resize(image, (VIEW_WIDTH, int(round(h * k))), interpolation=cv2.INTER_AREA) if k != 1.0 else image
        vsize = (view.shape[1], view.shape[0])
        mk = marker_status(view, k)
        mo = self.motion.update(view)
        model = self._map_for(vsize)
        if model is not None:
            lanes = [(ln.lane_id, ln.pts) for ln in model.lanes]
            cov = coverage_status(np.vstack([p for _, p in lanes]), vsize, "map")
        else:
            lanes = []
            cov = coverage_status(self.motion.seen_points(vsize), vsize, "motion")
        st = {"frames": self.frames, "fps": None if fps is None else round(fps, 1), "width": w, "height": h,
              "marker": mk, "coverage": cov, "map": model is not None,
              "motion": {"blobs": len(mo["contours"]), "global": mo["global_change"], "learning": mo["learning"]}}
        shown = dict(st, lanes=lanes, motion_contours=mo["contours"])
        if k != 1.0:  # the status the page and terminal read speaks capture pixels
            if mk["corners"]:
                st["marker"] = dict(mk, corners=(np.array(mk["corners"]) / k).tolist())
        return draw_overlay(view, shown, self.lang), st

    def step(self) -> bool:
        """Annotate the newest frame not seen yet; False if there is none."""
        with self._lock:
            if self._raw is None or self._raw_seq == self._done_seq:
                return False
            image, self._done_seq = self._raw, self._raw_seq
        out, st = self.process(image, self.measured_fps())
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if ok:
            with self._lock:
                self._jpeg, self._status, self._seq = bytes(buf), st, self._seq + 1
        return True

    def run(self, stop: threading.Event) -> None:
        nxt = time.monotonic()
        while not stop.is_set():
            self.step()
            nxt += self.period
            stop.wait(max(0.0, nxt - time.monotonic()))
            if nxt < time.monotonic() - 1.0:
                nxt = time.monotonic()

    def latest(self) -> tuple[bytes, int]:
        with self._lock:
            return self._jpeg, self._seq

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)


def feed_video(preview: Preview, path: str, stop: threading.Event, loop: bool = True) -> None:
    """Play a video file into the preview in real time (`--video`; a stand-in for a camera)."""
    from .sources import FileSource

    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()
    while not stop.is_set():
        t0 = time.monotonic()
        for i, (_, img) in enumerate(FileSource(path).frames()):
            if stop.is_set():
                return
            preview.feed(img)
            stop.wait(max(0.0, t0 + (i + 1) / fps - time.monotonic()))
        if not loop:
            return


# ----------------------------------------------------------------------------- snapshots (no browser)

def run_snapshots(preview: Preview, path: str, stop: threading.Event, *, period: float = 1.0, count: int | None = None,
                  out: Callable[[str], None] = print) -> int:
    """Write the latest annotated JPEG to `path` every `period` s (atomically) and print the status line."""
    n, last_seq = 0, -1
    while not stop.is_set() and (count is None or n < count):
        jpg, seq = preview.latest()
        if seq != last_seq:  # nothing is written until the first real frame was annotated
            last_seq = seq
            tmp = f"{path}.tmp"
            Path(tmp).write_bytes(jpg)
            os.replace(tmp, path)
            n += 1
            out(f"{time.strftime('%H:%M:%S')}  {status_line(preview.status(), preview.lang) if preview.frames else say(preview.lang, 'waiting')}")
        stop.wait(period)
    return n


# ----------------------------------------------------------------------------- the page

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>trackloc - aim the camera</title>
<style nonce="__NONCE__">
:root { --bg:#f6f7f9; --fg:#14181f; --muted:#5a6472; --card:#fff; --line:#d5dae1; --ok:#12703a; --warn:#9a5b00; --bad:#b3261e; --accent:#0b57d0; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151a; --fg:#e8ebf0; --muted:#9aa4b2; --card:#1b2028; --line:#303846; --ok:#5fd68a; --warn:#ffb454; --bad:#ff8a80; --accent:#8ab4f8; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1100px; margin:0 auto; padding:16px; }
header { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; justify-content:space-between; }
h1 { font-size:1.4rem; margin:0; }
h2 { font-size:1.05rem; margin:0 0 8px; }
.lead { color:var(--muted); margin:4px 0 16px; }
.lang button { min-height:44px; min-width:44px; border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:8px; font:inherit; cursor:pointer; }
.lang button[aria-pressed="true"] { border-color:var(--accent); color:var(--accent); font-weight:600; }
:focus-visible { outline:3px solid var(--accent); outline-offset:2px; }
.view { background:#000; border-radius:12px; overflow:hidden; border:1px solid var(--line); }
.view img { display:block; width:100%; height:auto; min-height:120px; }
.grid { display:grid; gap:16px; margin-top:16px; grid-template-columns:1fr; }
@media (min-width:800px) { .grid { grid-template-columns:1fr 1fr; } }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
ul.status { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
ul.status li { padding-left:28px; position:relative; }
ul.status li::before { content:"\\25CF"; position:absolute; left:4px; }
ul.status li.ok { color:var(--ok); } ul.status li.warn { color:var(--warn); } ul.status li.bad { color:var(--bad); } ul.status li.info { color:var(--muted); }
ul.status li.warn::before { content:"\\25B2"; } ul.status li.bad::before { content:"\\2716"; }
.checks { list-style:none; margin:0; padding:0; display:grid; gap:4px; }
.checks label { display:flex; gap:12px; align-items:flex-start; min-height:44px; padding:6px 0; cursor:pointer; }
.checks input { width:22px; height:22px; margin-top:2px; flex:none; }
.note { color:var(--muted); font-size:.9rem; margin-top:12px; }
</style>
</head>
<body>
<main>
<header>
  <h1 id="title"></h1>
  <div class="lang" role="group" id="langs" aria-label="Language"><button type="button" data-lang="en" aria-pressed="false">EN</button> <button type="button" data-lang="fr" aria-pressed="false">FR</button></div>
</header>
<p class="lead" id="lead"></p>
<div class="view"><img id="cam" src="/stream" alt=""></div>
<div class="grid">
  <section class="card" aria-labelledby="st-h"><h2 id="st-h">Status</h2><ul class="status" id="status" aria-live="polite"></ul><p class="note" id="mapnote" hidden></p></section>
  <section class="card" aria-labelledby="ck-h"><h2 id="ck-h"></h2><ul class="checks" id="checks"></ul><p class="note" id="done"></p></section>
</div>
</main>
<script nonce="__NONCE__">
"use strict";
const MSG = __MESSAGES__;
const ORDER = __ORDER__;
let lang = "en", last = null, lastOk = 0;
try { lang = localStorage.getItem("trackloc-lang") || (navigator.language || "en").slice(0, 2); } catch (e) { lang = (navigator.language || "en").slice(0, 2); }
if (!MSG[lang]) lang = "en";
const $ = (id) => document.getElementById(id);
const fmt = (s, o) => s.replace(/\\{(\\w+)(?::[^}]*)?\\}/g, (_, k) => (o && o[k] !== undefined ? o[k] : ""));
function renderStatic() {
  const m = MSG[lang];
  document.documentElement.lang = lang;
  document.title = "trackloc - " + m.title;
  $("title").textContent = m.title; $("lead").textContent = m.lead; $("st-h").textContent = m.status_title;
  $("ck-h").textContent = m.checklist_title; $("done").textContent = m.done_hint; $("cam").alt = m.title;
  $("mapnote").textContent = m.map_note; $("langs").setAttribute("aria-label", m.language);
  const box = $("checks"); const had = [...box.querySelectorAll("input")].map((i) => i.checked); box.textContent = "";
  ORDER.forEach((key, i) => { const li = document.createElement("li"), lab = document.createElement("label"), cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!had[i]; lab.append(cb, document.createTextNode(m[key])); li.append(lab); box.append(li); });
  document.querySelectorAll(".lang button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));
  renderStatus();
}
function covText(c, m) {
  const edges = (c.cut || []).map((e) => m["edge_" + e]).join(", ");
  return fmt(m[c.hint], { pct: Math.round(100 * (c.fill || 0)), edges });
}
function renderStatus() {
  const m = MSG[lang], ul = $("status"); ul.textContent = "";
  const add = (cls, text) => { const li = document.createElement("li"); li.className = cls; li.textContent = text; ul.append(li); };
  if (Date.now() - lastOk > 4000 && lastOk) { add("bad", m.stopped); return; }
  if (!last || last.waiting) { add("info", m.waiting); return; }
  const mk = last.marker;
  if (!mk.found) { add("bad", m.marker_missing); add("info", m.marker_help); }
  else if (mk.hint) add("warn", fmt(m[mk.hint], { px: Math.round(mk.side_px) }));
  else add("ok", m.marker_ok);
  add("info", fmt(m.res, { w: last.width, h: last.height }) + "  " + (last.fps ? fmt(m.fps, { fps: last.fps.toFixed(1) }) : m.fps_unknown));
  add({ cov_ok: "ok", cov_motion: "info", cov_none: "info" }[last.coverage.hint] || "warn", covText(last.coverage, m));
  const mo = last.motion;
  if (mo.global) add("bad", m.motion_global); else if (mo.learning) add("info", m.learning);
  else add("info", mo.blobs ? fmt(m.motion_some, { n: mo.blobs }) : m.motion_none);
  $("mapnote").hidden = !last.map;
}
document.querySelectorAll(".lang button").forEach((b) => b.addEventListener("click", () => {
  lang = b.dataset.lang; try { localStorage.setItem("trackloc-lang", lang); } catch (e) { /* private window */ } renderStatic(); }));
async function poll() {
  try { const r = await fetch("/status", { cache: "no-store" }); if (r.ok) { last = await r.json(); lastOk = Date.now(); } } catch (e) { /* shown as stopped */ }
  renderStatus(); setTimeout(poll, 500);
}
renderStatic(); poll();
</script>
</body>
</html>
"""


def page(nonce: str) -> str:
    blob = json.dumps(MESSAGES, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace("__NONCE__", nonce).replace("__MESSAGES__", blob).replace("__ORDER__", json.dumps(CHECKLIST_KEYS))


# ----------------------------------------------------------------------------- the server

def check_request(request: web.Request) -> web.Response | None:
    """The guards every page of ours applies (also `serve`'s): the Host must be a loopback name of OUR port (DNS
    rebinding), and a request the browser flags cross-site, or whose Origin is not ours, is refused. None: fine."""
    allowed = request.app[ALLOWED_HOSTS]
    if request.headers.get("Host", "").lower() not in allowed:  # DNS rebinding: a page on evil.example resolving to 127.0.0.1
        return web.Response(status=421, text="unexpected Host")
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        return web.Response(status=403, text="cross-site request refused")
    origin = request.headers.get("Origin")
    if origin is not None and origin.lower() not in {f"http://{h}" for h in allowed}:
        return web.Response(status=403, text="cross-origin request refused")
    return None


def allow_loopback(app: web.Application, port: int) -> None:
    """The Host values this app answers to, once its port is known (127.0.0.1, localhost, ::1)."""
    app[ALLOWED_HOSTS].update({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})


def csp(src: str) -> str:
    return (f"default-src 'none'; img-src 'self'; connect-src 'self'; script-src {src}; style-src {src}; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


async def add_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("Content-Security-Policy", csp("'none'"))  # the page sets its own, with its nonce
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Referrer-Policy"] = "no-referrer"


def stream_handler(preview: Preview, state: dict[str, Any]) -> Callable[[web.Request], Any]:
    """The MJPEG `/stream` handler; `state` = {"streams": n, "closing": bool} (shared with the app's shutdown hook)."""

    async def stream(request: web.Request) -> web.StreamResponse:
        if state["streams"] >= MAX_STREAMS:
            return web.Response(status=503, text="too many viewers")
        resp = web.StreamResponse(headers={"Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}"})
        await resp.prepare(request)
        state["streams"] += 1
        last = -1
        try:
            while not state["closing"]:
                jpg, seq = preview.latest()
                if seq != last:
                    last = seq
                    await resp.write(b"--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % (BOUNDARY.encode(), len(jpg))
                                     + jpg + b"\r\n")
                await asyncio.sleep(0.03)
        except ConnectionError:
            pass  # the viewer closed the tab
        finally:
            state["streams"] -= 1
        return resp

    return stream


def make_app(preview: Preview) -> web.Application:
    """GET only: `/` (the page), `/stream` (MJPEG), `/status` (JSON). Nothing here can drive anything."""

    @web.middleware
    async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
        if request.method != "GET":
            return web.Response(status=405, headers={"Allow": "GET"}, text="GET only")
        return check_request(request) or await handler(request)

    async def index(request: web.Request) -> web.Response:
        nonce = secrets.token_urlsafe(16)
        return web.Response(text=page(nonce), content_type="text/html", charset="utf-8",
                            headers={"Content-Security-Policy": csp(f"'nonce-{nonce}'")})

    async def status(request: web.Request) -> web.Response:
        return web.json_response(preview.status())

    async def on_shutdown(app: web.Application) -> None:
        state["closing"] = True

    state = {"streams": 0, "closing": False}  # (not app[...]: aiohttp warns when a running app's items change)
    app = web.Application(middlewares=[guard])
    app[ALLOWED_HOSTS] = set()  # filled once the port is known
    app.on_response_prepare.append(add_headers)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index, allow_head=False)
    app.router.add_get("/stream", stream_handler(preview, state), allow_head=False)
    app.router.add_get("/status", status, allow_head=False)
    return app


async def serve(preview: Preview, port: int = DEFAULT_PORT, stop: threading.Event | None = None,
                ready: Callable[[str, int], None] | None = None, app: web.Application | None = None) -> None:
    """Serve on 127.0.0.1 only (there is deliberately no option for another address) until `stop` is set or cancelled.
    `ready(host, port)` is called once listening (`port=0` picks a free one). `app`: another application built from
    the pieces above (`trackloc serve`); it must keep `ALLOWED_HOSTS`."""
    app = app if app is not None else make_app(preview)
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    await web.TCPSite(runner, LOCALHOST, port).start()
    actual = runner.addresses[0][1]
    allow_loopback(app, actual)
    if ready:
        ready(LOCALHOST, actual)
    try:
        while stop is None or not stop.is_set():
            await asyncio.sleep(0.1)
    finally:
        await runner.cleanup()
