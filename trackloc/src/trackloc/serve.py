"""`python -m trackloc serve`: the camera connector's own local web page.

ONE page on 127.0.0.1 does what the commands do: pick the camera, see the live preview with its overlay, record, map
the track, name the cars (roll call), run, stop. The actions run HERE, in background threads of this process, never in
the browser: closing the tab stops and orphans nothing, and Stop works after a reload (`/status` says what runs).

Safety (this process can move cars through RaceLab):
- 127.0.0.1 only; the Host must be a loopback name of our port (421); cross-site `Sec-Fetch-Site` / `Origin` -> 403
  (the same checks as `preview`, reused from there);
- every POST needs the random per-run token (`X-Trackloc-Token`, which the page takes from the `?k=` of its URL) -> 403,
  and a JSON body (`Content-Type: application/json`) -> 415;
- ONE action at a time (409); Stop is always accepted and does nothing when idle;
- every value is validated here, never trusted from the page: the throttle is capped at the CLI's default cap (8/15),
  the slots are 0..5, a moving action needs `confirm: true` (the page asks the person first);
- Map / Identify / Run are `actions.do_map` / `do_identify` / `do_run`: every refusal of the command line (session
  running, E-STOP, link down) and every zero-and-release in a `finally` is theirs, unchanged. E-STOP stays in RaceLab;
- on exit (Ctrl-C) the running action is stopped and JOINED before the process leaves, so its `finally` runs.

The RaceLab "connector card": a separate, persistent RaceLab connection (it claims nothing) tells RaceLab our name,
page URL and status (`connector.set`); RaceLab clears it when we disconnect. An older RaceLab that does not know the op
gets one warning, then nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import math
import os
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import cv2
from aiohttp import web

from . import preview as pv
from .actions import Ctx, TracklocError, do_identify, do_map, do_record, do_run, slots_of
from .preview import ALLOWED_HOSTS, Preview

log = logging.getLogger("trackloc.serve")

UI_THROTTLE_MAX = 8  # the page never asks for more than the command line's default cap (the CLI can go higher)
STREAM_SCHEMES = ("rtsp", "rtsps", "http", "https")
CARD_NAME = "Camera (trackloc)"
TOKEN_HEADER = "X-Trackloc-Token"
STATUS_OF = {"record": "recording", "map": "mapping", "identify": "identifying", "run": "running"}
STOP_JOIN_S = 20.0


class BadRequest(Exception):
    """A value the page sent is not acceptable (HTTP 400, the message is shown to the person)."""


class Busy(Exception):
    """An action is already running (HTTP 409)."""


# ----------------------------------------------------------------------------- texts (English + French)

UI: dict[str, dict[str, str]] = {
    "en": {
        "title": "Camera connector",
        "lead": "Everything from this page: aim the camera, record, map the track, name the cars, run. The emergency stop stays in RaceLab.",
        "no_token": "This page was opened without its key: open the address printed in the terminal (it ends with ?k=...).",
        "lost": "the page lost the connection to `trackloc serve` (is the command still running?)",
        "cam_title": "Camera",
        "cam_device": "Device",
        "cam_url": "or a stream URL",
        "cam_url_ph": "rtsp://... or http://...",
        "cam_refresh": "Refresh the list",
        "cam_open": "Open the camera",
        "cam_close": "Close the camera",
        "cam_opened": "Camera open: {cam}",
        "cam_closed": "No camera open.",
        "cam_none": "(choose)",
        "cam_nodev": "no device listed (is ffmpeg installed?): type an index or a stream URL",
        "marker_mm": "Marker size (mm)",
        "marker_hint": "Measure the black square of the printed marker with a ruler.",
        "latency_ms": "Camera latency (ms)",
        "latency_hint": "From the estimate after a run; 0 if unknown. Used by Map and Run.",
        "rec_title": "Record",
        "rec_duration": "Duration (s, empty: until Stop)",
        "rec_start": "Start recording",
        "map_title": "Map the track",
        "slot": "Car (slot 1-6)",
        "throttle": "Throttle (1-8 of 15)",
        "lanes": "Lanes",
        "lanes_1": "1 lane",
        "lanes_2": "2 lanes (lane change)",
        "background": "Background",
        "bg_median": "median of the lap",
        "bg_empty": "empty track (clear it first)",
        "map_start": "Start the map...",
        "id_title": "Name the cars",
        "slots": "Cars on the track",
        "id_start": "Start the roll call...",
        "run_title": "Run",
        "run_identify": "Roll call first (moves each car a little)",
        "run_start": "Start the run",
        "stop": "STOP",
        "stop_hint": "Ends the current action and hands the cars back. It is not the emergency stop: that stays in RaceLab.",
        "confirm_map": "A car will run at throttle {n}/15: keep a hand on STOP in RaceLab.",
        "confirm_empty": "The empty track is captured {s} s after you start: remove every car and keep your hands out of the view.",
        "confirm_identify": "Each selected car will be pulsed at throttle {n}/15 for half a second: keep a hand on STOP in RaceLab.",
        "confirm_ok": "Start",
        "confirm_cancel": "Cancel",
        "status_title": "Status",
        "st_idle": "idle", "st_recording": "recording", "st_mapping": "mapping", "st_identifying": "identifying",
        "st_running": "running", "st_error": "error",
        "crossings": "crossings {n}/{need}",
        "frames": "frames {n}",
        "phase_clear": "Remove every car from the track and keep your hands out of the view...",
        "phase_place": "Put ONE car on the track, just behind the finish line.",
        "phase_drive": "The car is running...",
        "last_ok": "Last action finished: {action}",
        "last_stopped": "Last action stopped: {action}",
        "err_title": "Error",
        "log_title": "Log",
        "map_title2": "Map",
        "map_none": "No map yet: start the map, or start `serve` with --map FILE.",
        "map_file": "File: {path}",
        "map_len": "Length {m} m (centre line)",
        "map_lanes": "Lanes: {lanes}",
        "map_blind": "Blind zones (share of the lap): {blind}",
        "map_blind_none": "no blind zone",
        "map_metric": "Metres from: {metric}",
        "map_nomarker": "Marker not found: the lengths are a guess. Print the marker, lay it flat in view and map again.",
        "id_results": "Roll call: {text}",
        "id_ok": "slot {slot}: identified",
        "id_ko": "slot {slot}: NOT identified ({note})",
        "latency_est": "Latency still missing from the frame times: {ms} ms (from {n} crossings).",
        "latency_none": "Latency estimate: none yet (it comes from a run with cars crossing the finish line).",
        "latency_use": "Use {ms} ms",
        "racelab": "RaceLab",
        "rl_ok": "connected",
        "rl_down": "not reachable (Record then saves video only; Map, Identify and Run need it)",
        "rl_old": "connected (this RaceLab does not show the connector card)",
        "language": "Language",
    },
    "fr": {
        "title": "Connecteur caméra",
        "lead": "Tout depuis cette page : viser la caméra, enregistrer, cartographier la piste, nommer les voitures, lancer. L'arrêt d'urgence reste dans RaceLab.",
        "no_token": "Cette page a été ouverte sans sa clé : ouvrez l'adresse affichée dans le terminal (elle finit par ?k=...).",
        "lost": "la page a perdu la connexion à `trackloc serve` (la commande tourne-t-elle encore ?)",
        "cam_title": "Caméra",
        "cam_device": "Appareil",
        "cam_url": "ou une URL de flux",
        "cam_url_ph": "rtsp://... ou http://...",
        "cam_refresh": "Actualiser la liste",
        "cam_open": "Ouvrir la caméra",
        "cam_close": "Fermer la caméra",
        "cam_opened": "Caméra ouverte : {cam}",
        "cam_closed": "Aucune caméra ouverte.",
        "cam_none": "(choisir)",
        "cam_nodev": "aucun appareil listé (ffmpeg est-il installé ?) : saisissez un index ou une URL de flux",
        "marker_mm": "Taille du marqueur (mm)",
        "marker_hint": "Mesurez à la règle le carré noir du marqueur imprimé.",
        "latency_ms": "Latence de la caméra (ms)",
        "latency_hint": "Issue de l'estimation après un lancement ; 0 si inconnue. Utilisée par Carte et Lancer.",
        "rec_title": "Enregistrer",
        "rec_duration": "Durée (s, vide : jusqu'à Stop)",
        "rec_start": "Démarrer l'enregistrement",
        "map_title": "Cartographier la piste",
        "slot": "Voiture (slot 1-6)",
        "throttle": "Gaz (1-8 sur 15)",
        "lanes": "Voies",
        "lanes_1": "1 voie",
        "lanes_2": "2 voies (changement de voie)",
        "background": "Fond",
        "bg_median": "médiane du tour",
        "bg_empty": "piste vide (dégagez-la d'abord)",
        "map_start": "Lancer la carte...",
        "id_title": "Nommer les voitures",
        "slots": "Voitures sur la piste",
        "id_start": "Lancer l'appel...",
        "run_title": "Lancer",
        "run_identify": "Appel d'abord (déplace un peu chaque voiture)",
        "run_start": "Lancer le suivi",
        "stop": "STOP",
        "stop_hint": "Termine l'action en cours et rend les voitures. Ce n'est pas l'arrêt d'urgence : il reste dans RaceLab.",
        "confirm_map": "Une voiture va rouler aux gaz {n}/15 : gardez une main sur STOP dans RaceLab.",
        "confirm_empty": "La piste vide est capturée {s} s après le départ : retirez toutes les voitures et gardez les mains hors du champ.",
        "confirm_identify": "Chaque voiture choisie recevra une impulsion aux gaz {n}/15 pendant une demi-seconde : gardez une main sur STOP dans RaceLab.",
        "confirm_ok": "Démarrer",
        "confirm_cancel": "Annuler",
        "status_title": "État",
        "st_idle": "au repos", "st_recording": "enregistrement", "st_mapping": "cartographie", "st_identifying": "appel en cours",
        "st_running": "suivi en cours", "st_error": "erreur",
        "crossings": "passages {n}/{need}",
        "frames": "images {n}",
        "phase_clear": "Retirez toutes les voitures de la piste et gardez les mains hors du champ...",
        "phase_place": "Posez UNE voiture sur la piste, juste derrière la ligne d'arrivée.",
        "phase_drive": "La voiture roule...",
        "last_ok": "Dernière action terminée : {action}",
        "last_stopped": "Dernière action arrêtée : {action}",
        "err_title": "Erreur",
        "log_title": "Journal",
        "map_title2": "Carte",
        "map_none": "Pas encore de carte : lancez la carte, ou démarrez `serve` avec --map FICHIER.",
        "map_file": "Fichier : {path}",
        "map_len": "Longueur {m} m (ligne centrale)",
        "map_lanes": "Voies : {lanes}",
        "map_blind": "Zones aveugles (part du tour) : {blind}",
        "map_blind_none": "aucune zone aveugle",
        "map_metric": "Mètres obtenus par : {metric}",
        "map_nomarker": "Marqueur introuvable : les longueurs sont une estimation. Imprimez le marqueur, posez-le à plat dans le champ et recommencez.",
        "id_results": "Appel : {text}",
        "id_ok": "slot {slot} : identifiée",
        "id_ko": "slot {slot} : NON identifiée ({note})",
        "latency_est": "Latence encore absente des horodatages : {ms} ms (d'après {n} passages).",
        "latency_none": "Estimation de latence : aucune pour l'instant (elle vient d'un suivi où des voitures passent la ligne).",
        "latency_use": "Utiliser {ms} ms",
        "racelab": "RaceLab",
        "rl_ok": "connecté",
        "rl_down": "injoignable (Enregistrer garde alors la vidéo seule ; Carte, Appel et Lancer en ont besoin)",
        "rl_old": "connecté (ce RaceLab n'affiche pas la carte du connecteur)",
        "language": "Langue",
    },
}
# the aiming messages (marker, resolution, coverage...) are the preview's own
PAGE_KEYS = ["marker_ok", "marker_missing", "marker_small", "marker_help", "cov_none", "cov_ok", "cov_small", "cov_cut",
             "cov_motion", "edge_left", "edge_right", "edge_top", "edge_bottom", "motion_none", "motion_some",
             "motion_global", "learning", "res", "fps", "fps_unknown", "waiting"]


def messages() -> dict[str, dict[str, str]]:
    return {lang: {**{k: pv.MESSAGES[lang][k] for k in PAGE_KEYS}, **UI[lang]} for lang in UI}


# ----------------------------------------------------------------------------- validation (the page is never trusted)

def _number(body: dict[str, Any], key: str, lo: float, hi: float, default: Any = ..., *, integer: bool = False) -> Any:
    v = body.get(key, default)
    if v is ...:
        raise BadRequest(f"{key} is required")
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int if integer else (int, float)):
        raise BadRequest(f"{key} must be {'an integer' if integer else 'a number'}")
    if not (lo <= v <= hi) or (isinstance(v, float) and not math.isfinite(v)):
        raise BadRequest(f"{key} must be between {lo:g} and {hi:g}")
    return v


def _choice(body: dict[str, Any], key: str, allowed: tuple[Any, ...], default: Any) -> Any:
    v = body.get(key, default)
    if v not in allowed or isinstance(v, bool):
        raise BadRequest(f"{key} must be one of {', '.join(str(a) for a in allowed)}")
    return v


def _slot_list(body: dict[str, Any], key: str = "slots", *, required: bool) -> list[int]:
    v = body.get(key, [])
    if not isinstance(v, list) or any(isinstance(s, bool) or not isinstance(s, int) or not 0 <= s <= 5 for s in v):
        raise BadRequest(f"{key} must be a list of slots between 0 and 5")
    if len(set(v)) != len(v):
        raise BadRequest(f"{key} must not repeat a slot")
    if required and not v:
        raise BadRequest(f"{key}: choose at least one car")
    return sorted(v)


def _flag(body: dict[str, Any], key: str) -> bool:
    v = body.get(key, False)
    if not isinstance(v, bool):
        raise BadRequest(f"{key} must be true or false")
    return v


def _need_confirm(body: dict[str, Any]) -> None:
    if body.get("confirm") is not True:
        raise BadRequest("this action moves cars: it needs `confirm: true` (the page asks the person first)")


def check_camera_spec(spec: Any) -> str:
    """A camera index, a stream URL of an allowed scheme, or a part of a device name: never a file path or another scheme."""
    if not isinstance(spec, str) or not spec.strip() or len(spec) > 300:
        raise BadRequest("camera: give an index, a stream URL or part of a device name")
    spec = spec.strip()
    if "://" in spec:
        parts = urlsplit(spec)
        if parts.scheme.lower() not in STREAM_SCHEMES or not parts.hostname:
            raise BadRequest(f"camera: a stream URL must start with {', '.join(s + '://' for s in STREAM_SCHEMES)}")
    elif "/" in spec or "\\" in spec:
        raise BadRequest("camera: files are not accepted here (use `serve --video FILE`)")
    return spec


# ----------------------------------------------------------------------------- the one camera, shared

class Camera:
    """The ONE open camera of the page. It feeds the preview all the time and, through a lease, the running action
    (a camera cannot be opened twice reliably, and an action must not open or close it)."""

    def __init__(self, factory: Callable[[str], Any], preview: Preview) -> None:
        self._factory, self.preview = factory, preview
        self._lock = threading.Lock()
        self._src: Any = None
        self._subs: dict[int, Callable[[float, float, Any], None]] = {}
        self._ids = itertools.count(1)
        self.spec: str | None = None
        self.info: dict[str, Any] | None = None

    @property
    def is_open(self) -> bool:
        return self._src is not None

    def open(self, spec: str) -> None:
        """Blocking (a device can take seconds to open): call it from an executor."""
        self.close()
        src = self._factory(spec)
        info = src.info()
        src.start(self._fan)
        with self._lock:
            self._src, self.spec, self.info = src, spec, info

    def close(self) -> None:
        with self._lock:
            src, self._src, self.spec, self.info = self._src, None, None, None
        if src is not None:
            src.stop()

    def _fan(self, t: float, t_raw: float, image: Any) -> None:
        self.preview.feed(image, t_raw)
        for cb in list(self._subs.values()):
            cb(t, t_raw, image)

    def lease(self) -> Lease:
        return Lease(self)


class Lease:
    """What an action sees as its camera: `info()`, `start(cb)`, `stop()`. `stop` only unsubscribes."""

    def __init__(self, cam: Camera) -> None:
        self._cam, self._id = cam, 0

    def info(self) -> dict[str, Any]:
        if self._cam.info is None:
            raise TracklocError("the camera was closed")
        return self._cam.info

    def start(self, cb: Callable[[float, float, Any], None]) -> None:
        self._id = next(self._cam._ids)
        self._cam._subs[self._id] = cb

    def stop(self) -> None:
        self._cam._subs.pop(self._id, None)


class LoopVideoSource:
    """`serve --video FILE`: a video file played in a loop as if it were a camera (a demo, and the tests)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise OSError(f"cannot open the video {path}")
        self._info = {"width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                      "fps": float(cap.get(cv2.CAP_PROP_FPS)) or 30.0}
        cap.release()

    def info(self) -> dict[str, Any]:
        return dict(self._info)

    def start(self, on_frame: Callable[[float, float, Any], None]) -> None:
        def loop() -> None:
            period = 1.0 / self._info["fps"]
            while not self._stop.is_set():
                cap = cv2.VideoCapture(self.path)
                nxt = time.monotonic()
                while not self._stop.is_set():
                    ok, image = cap.read()
                    if not ok:
                        break
                    now = time.monotonic()
                    on_frame(now, now, image)
                    nxt += period
                    self._stop.wait(max(0.0, nxt - time.monotonic()))
                cap.release()

        self._thread = threading.Thread(target=loop, name="trackloc-video", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(3)


def default_camera_factory(args: argparse.Namespace) -> Callable[[str], Any]:
    """spec -> an open source (`--video`: the file, whatever the spec). The capture options of the command line apply."""
    from .actions import size_of
    from .sources import CameraSource, FfmpegSource, resolve_camera

    def factory(spec: str) -> Any:
        if args.video:
            return LoopVideoSource(args.video)
        cam = resolve_camera(spec)  # an index, a URL, or a part of a device name (no prompt)
        size = size_of(args.size)
        if args.ffmpeg:
            return FfmpegSource(cam, size=size or (1280, 720), fps=args.fps or 30.0, mirror=args.mirror,
                                rotate=args.rotate, exposure_offset_s=args.exposure_offset)
        return CameraSource(cam, size=size, fps=args.fps, mirror=args.mirror, rotate=args.rotate,
                            exposure_offset_s=args.exposure_offset).open()

    return factory


# ----------------------------------------------------------------------------- one action at a time

class Runner:
    """Runs ONE action in a background thread. `status` is what the RaceLab card shows: idle, recording, mapping,
    identifying, running, or error (until the next action starts)."""

    def __init__(self, notify: Callable[[str], None] = lambda status: None) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._notify = notify
        self.name: str | None = None
        self.status = "idle"
        self.stop_event = threading.Event()
        self.info: dict[str, Any] = {}
        self.lines: deque[str] = deque(maxlen=200)
        self.started = 0.0
        self.error: str | None = None
        self.last: dict[str, Any] | None = None
        self.stopping = False

    def say(self, text: str) -> None:
        for line in str(text).splitlines():
            if line.strip():
                self.lines.append(line.rstrip())

    def _set(self, status: str) -> None:
        self.status = status
        try:
            self._notify(status)
        except Exception:
            log.exception("status notification")

    def start(self, name: str, fn: Callable[[Ctx], Any], ctx: Ctx, on_done: Callable[[Any], dict[str, Any] | None]) -> None:
        """`fn(ctx)` in a thread. `on_done(result)` returns the summary kept in `last` (and may keep the map)."""
        with self._lock:
            if self._thread is not None:
                raise Busy(f"{self.name} is running: stop it first")
            self.name, self.error, self.stopping = name, None, False
            self.stop_event, self.info, self.started = ctx.stop, ctx.info, time.time()
            self.lines.clear()
            self._thread = threading.Thread(target=self._body, args=(name, fn, ctx, on_done), name=f"trackloc-{name}", daemon=False)
            self._set(STATUS_OF[name])
            self._thread.start()

    def _body(self, name: str, fn: Callable[[Ctx], Any], ctx: Ctx, on_done: Callable[[Any], dict[str, Any] | None]) -> None:
        from .drive import MapAborted
        from .identify import RollCallAborted
        from .mapper import MapError

        error: str | None = None
        summary: dict[str, Any] | None = None
        try:
            result = fn(ctx)
            if isinstance(result, dict) and result.get("error"):
                error = str(result["error"])
            summary = on_done(result)
        except (MapAborted, RollCallAborted, MapError, TracklocError) as e:
            error = None if ctx.stop.is_set() else str(e)
            self.say(f"{type(e).__name__}: {e}")
        except Exception as e:  # a bug: say it, still end cleanly (the actions released the car in their finally)
            log.exception("action %s", name)
            error = f"{type(e).__name__}: {e}"
            self.say(error)
        finally:
            with self._lock:
                self.error = error
                self.last = {"action": name, "ok": error is None, "stopped": ctx.stop.is_set(), "summary": summary}
                self._thread = None
                self.name = None
                self._set("error" if error else "idle")

    def request_stop(self) -> bool:
        with self._lock:
            if self._thread is None:
                return False
            self.stopping = True
            self.stop_event.set()
            return True

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def join(self, timeout: float = STOP_JOIN_S) -> None:
        th = self._thread
        if th is not None:
            th.join(timeout)

    def snapshot(self) -> dict[str, Any]:
        return {"status": self.status, "action": self.name, "since": self.started if self.name else None,
                "stopping": self.stopping, "info": dict(self.info) if self.name else {}, "error": self.error,
                "log": list(self.lines)[-14:], "last": self.last}


# ----------------------------------------------------------------------------- the RaceLab connector card

class Card:
    """`connector.set` on a persistent RaceLab connection of its own (it claims nothing, so it never holds a car).
    Retries in the background (RaceLab may start after us), re-sends after a reconnection, warns once on an old RaceLab."""

    def __init__(self, url: str, token: str | None, ui_url: Callable[[], str], *, connect: Callable[..., Any] | None = None,
                 period: float = 5.0) -> None:
        self.url, self.token, self.ui_url, self.period = url, token, ui_url, period
        self._connect = connect
        self._client: Any = None
        self._status = "idle"
        self._sent: tuple[str, str] | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = "down"  # down | ok | old
        self._warned = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="trackloc-card", daemon=True)
        self._thread.start()

    def set_status(self, status: str) -> None:
        self._status = status
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._sync()
            except Exception:
                log.exception("connector card")
            self._wake.wait(self.period)
            self._wake.clear()

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect(self.url, self.token)
        from racelab.client import SyncClient

        return SyncClient.connect(self.url, agent="camera-ui", state_hz=0.5, token=self.token)

    def _sync(self) -> None:
        from racelab.client import RaceLabError

        if self.state == "old":
            return
        if self._client is None or getattr(self._client, "closed", False):
            self._client, self._sent = None, None
            try:
                self._client = self._open()
            except (ConnectionError, OSError, TimeoutError, RaceLabError) as e:
                self.state = "down"
                log.debug("RaceLab not reachable for the connector card: %s", e)
                return
        payload = (self._status, self.ui_url())
        if self._sent == payload:
            return
        try:
            self._client.request("connector.set", name=CARD_NAME, ui_url=payload[1], status=payload[0])
        except RaceLabError as e:
            if "unknown command" in str(e):
                self.state = "old"
                log.warning("this RaceLab does not know `connector.set` (older version): no connector card")
                return
            if not self._warned:
                self._warned = True
                log.warning("RaceLab refused the connector card: %s", e)
            return
        except (ConnectionError, OSError, TimeoutError):
            self.state = "down"
            self._close_client()
            return
        self._sent, self.state = payload, "ok"

    def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(5)
        self._close_client()  # RaceLab clears the card when we disconnect


# ----------------------------------------------------------------------------- the application state

def summarize_map(model: Any, path: str) -> dict[str, Any]:
    r = model.report or {}
    blind = [[round(a, 3), round(b, 3)] for a, b in model.blind]
    return {"path": str(path), "length_m": round(model.length_c, 2), "pitch_cm": round(model.pitch_m * 100, 1),
            "lanes": [{"id": ln.lane_id, "length_m": round(ln.length, 2)} for ln in model.lanes],
            "blind": blind, "blind_share": round(sum(b - a for a, b in model.blind), 3), "metric": model.metric,
            "marker_found": r.get("marker_found"), "lap_time_s": r.get("lap_time_s"), "notes": list(r.get("notes", []))}


def summarize_roll(results: list[Any]) -> list[dict[str, Any]]:
    return [{"slot": r.slot, "identified": r.identified, "lane": r.lane, "s": None if r.s is None else round(r.s, 3),
             "note": r.note} for r in results]


class Serve:
    """Everything the page talks to: the camera, the runner, the card, the map, the preview."""

    def __init__(self, args: argparse.Namespace, lang: str, out_dir: Path, *, camera_factory: Callable[[str], Any] | None = None,
                 connect: Callable[[argparse.Namespace], Any] | None = None, card_connect: Callable[..., Any] | None = None,
                 card_period: float = 5.0) -> None:
        self.args, self.lang, self.out_dir = args, lang, out_dir
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.connect = connect
        self.preview = Preview(None, lang)
        self.camera = Camera(camera_factory or default_camera_factory(args), self.preview)
        self.card = Card(args.racelab_url, args.token, self.ui_url, connect=card_connect, period=card_period)
        self.runner = Runner(self.card.set_status)
        self.map_summary: dict[str, Any] | None = None
        self.map_path: str | None = None
        self.latency: dict[str, Any] | None = None
        self.opening = False
        self.camera_error: str | None = None

    def ui_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?k={self.token}"

    # -- the map

    def load_map(self, path: str) -> None:
        from .geometry import MapModel

        model = MapModel.load(path)
        self.set_map(model, path)

    def set_map(self, model: Any, path: str) -> None:
        self.map_path = str(path)
        self.map_summary = summarize_map(model, path)
        self.preview.set_model(model)

    # -- actions

    def _args(self, cmd: str, **values: Any) -> argparse.Namespace:
        """The command line's own defaults for `cmd`, then the validated values of the page."""
        from .__main__ import build_parser

        required = {"record": [], "map": ["--slot", "0"], "identify": ["--map", "-", "--slots", "0"], "run": ["--map", "-"]}[cmd]
        ns = build_parser().parse_args([cmd, *required])
        for k, v in values.items():
            setattr(ns, k, v)
        ns.camera, ns.name, ns.token = self.camera.spec, self.args.name, self.args.token
        ns.racelab_url = self.args.racelab_url
        return ns

    def _ctx(self, **kw: Any) -> Ctx:
        ctx = Ctx(say=self.runner.say, source=self.camera.lease(), connect=self.connect, interactive=False,
                  empty_countdown_s=5.0, empty_place_s=10.0, **kw)
        return ctx

    def _need_camera(self) -> None:
        if not self.camera.is_open:
            raise BadRequest("open the camera first")

    def _need_map(self) -> str:
        if not self.map_path:
            raise BadRequest("no map yet: run Map first (or start `serve` with --map FILE)")
        return self.map_path

    def start(self, name: str, body: dict[str, Any]) -> None:
        """Validate `body`, then run the action. BadRequest -> 400, Busy -> 409. Nothing starts on a bad request."""
        if self.runner.busy or self.opening:
            raise Busy(f"{self.runner.name or 'the camera'} is busy: stop it first")
        self._need_camera()
        getattr(self, "_start_" + name)(body)

    def _launch(self, name: str, fn: Callable[[Any, Ctx], Any], ns: argparse.Namespace, on_done: Callable[[Any], dict[str, Any] | None]) -> None:
        ctx = self._ctx()
        self.runner.start(name, lambda c: fn(ns, c), ctx, on_done)

    def _start_record(self, body: dict[str, Any]) -> None:
        duration = _number(body, "duration", 1, 3600, None)
        out = self.out_dir / (time.strftime("%Y%m%d-%H%M%S") + "-record")
        link = self.args.racelab_url if (self.card.state in ("ok", "old") or self.connect is not None) else None
        ns = self._args("record", out=str(out), duration=duration, racelab_url=link)
        self._launch("record", do_record, ns, lambda r: {"out": r["out"], "frames": r["frames"], "crossings": r["crossings"]})

    def _start_map(self, body: dict[str, Any]) -> None:
        _need_confirm(body)
        slot = _number(body, "slot", 0, 5, integer=True)
        throttle = _number(body, "throttle", 1, UI_THROTTLE_MAX, 4, integer=True)
        lanes = _choice(body, "lanes", (1, 2), 2)
        background = _choice(body, "background", ("median", "empty"), "median")
        marker_mm = _number(body, "marker_mm", 20, 500, 150)
        latency_ms = _number(body, "latency_ms", 0, 1000, 0)
        folder = self.out_dir / (time.strftime("%Y%m%d-%H%M%S") + "-map")
        folder.mkdir(parents=True, exist_ok=True)
        ns = self._args("map", slot=slot, throttle=throttle, cap=UI_THROTTLE_MAX, lanes=lanes, background=background,
                        marker_size=marker_mm / 1000.0, latency=latency_ms / 1000.0, out=str(folder / "track_map.json"), yes=True)

        def done(result: dict[str, Any]) -> dict[str, Any]:
            self.set_map(result["model"], result["map"])
            self.latency = None  # the old estimate was for the old map
            return dict(self.map_summary or {})

        self._launch("map", do_map, ns, done)

    def _start_identify(self, body: dict[str, Any]) -> None:
        _need_confirm(body)
        slots = _slot_list(body, required=True)
        path = self._need_map()
        ns = self._args("identify", map=path, slots=",".join(str(s) for s in slots))
        self._launch("identify", do_identify, ns, lambda r: {"results": summarize_roll(r["results"])})

    def _start_run(self, body: dict[str, Any]) -> None:
        identify = _flag(body, "identify")
        slots = _slot_list(body, required=identify)
        latency_ms = _number(body, "latency_ms", 0, 1000, 0)
        if identify:
            _need_confirm(body)
        path = self._need_map()
        ns = self._args("run", map=path, slots=",".join(str(s) for s in slots), identify=identify, latency=latency_ms / 1000.0)

        def done(result: dict[str, Any]) -> dict[str, Any]:
            if result.get("latency"):
                self.latency = result["latency"]
            return {"latency": result.get("latency")}

        self._launch("run", do_run, ns, done)

    # -- status

    def status(self) -> dict[str, Any]:
        r = self.runner.snapshot()
        return {**r, "camera": {"open": self.camera.is_open, "spec": self.camera.spec, "info": self.camera.info,
                                "opening": self.opening, "error": self.camera_error},
                "racelab": {"url": self.args.racelab_url, "card": self.card.state},
                "map": self.map_summary, "latency": self.latency, "preview": self.preview.status()}

    def shutdown(self) -> None:
        """Stop the action and WAIT for it: its `finally` hands the car back (a daemon thread would be killed first)."""
        self.runner.request_stop()
        self.runner.join()
        self.camera.close()
        self.card.close()


# ----------------------------------------------------------------------------- the page

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>trackloc</title>
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
.lang button, button.btn { min-height:44px; min-width:44px; border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:8px; font:inherit; cursor:pointer; padding:0 14px; }
.lang button[aria-pressed="true"] { border-color:var(--accent); color:var(--accent); font-weight:600; }
button.primary { background:var(--accent); color:var(--bg); border-color:var(--accent); font-weight:600; }
button:disabled { opacity:.45; cursor:not-allowed; }
:focus-visible { outline:3px solid var(--accent); outline-offset:2px; }
.view { background:#000; border-radius:12px; overflow:hidden; border:1px solid var(--line); }
.view img { display:block; width:100%; height:auto; min-height:120px; }
.grid { display:grid; gap:16px; margin-top:16px; grid-template-columns:1fr; }
@media (min-width:800px) { .grid { grid-template-columns:1fr 1fr; } }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
.row { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:end; margin:8px 0; }
label.f { display:flex; flex-direction:column; gap:2px; font-size:.9rem; color:var(--muted); }
label.f input, label.f select { min-height:44px; font:inherit; color:var(--fg); background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:0 8px; }
label.f input[type=number] { width:9em; }
label.chk { display:flex; gap:8px; align-items:center; min-height:44px; cursor:pointer; }
label.chk input { width:22px; height:22px; }
fieldset { border:0; margin:0; padding:0; }
legend { font-size:.9rem; color:var(--muted); padding:0; }
ul.list { list-style:none; margin:0; padding:0; display:grid; gap:6px; }
ul.list li.ok { color:var(--ok); } ul.list li.warn { color:var(--warn); } ul.list li.bad { color:var(--bad); } ul.list li.info { color:var(--muted); }
.note { color:var(--muted); font-size:.9rem; margin:4px 0; }
.err { color:var(--bad); font-weight:600; margin:8px 0; }
pre { margin:8px 0 0; padding:8px; background:var(--bg); border:1px solid var(--line); border-radius:8px; max-height:14em; overflow:auto; font-size:.85rem; white-space:pre-wrap; }
.statusline { font-size:1.1rem; font-weight:600; margin:0 0 4px; }
.stopbar { position:sticky; bottom:0; padding:8px 0; background:var(--bg); display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; margin-top:16px; }
button.stop { background:var(--bad); color:#fff; border-color:var(--bad); font-weight:700; font-size:1.1rem; min-height:56px; min-width:140px; }
dialog { border:1px solid var(--line); border-radius:12px; background:var(--card); color:var(--fg); max-width:30em; padding:20px; }
dialog::backdrop { background:rgba(0,0,0,.5); }
dialog .row { justify-content:flex-end; }
</style>
</head>
<body>
<main>
<header>
  <h1 id="title"></h1>
  <div class="lang" role="group" id="langs"><button type="button" data-lang="en" aria-pressed="false">EN</button> <button type="button" data-lang="fr" aria-pressed="false">FR</button></div>
</header>
<p class="lead" data-i="lead"></p>
<p class="err" id="notoken" role="alert" hidden data-i="no_token"></p>

<div class="view"><img id="cam" src="/stream" alt=""></div>
<ul class="list" id="aim" aria-live="polite"></ul>

<div class="stopbar">
  <button type="button" class="btn stop" id="stop" data-i="stop"></button>
  <span class="note" data-i="stop_hint"></span>
</div>

<div class="grid">
  <section class="card" aria-labelledby="h-status">
    <h2 id="h-status" data-i="status_title"></h2>
    <p class="statusline" id="statusline"></p>
    <p class="note" id="phase"></p>
    <p class="err" id="error" role="alert"></p>
    <p class="note" id="last"></p>
    <p class="note" id="rl"></p>
    <pre id="log" aria-label="log"></pre>
  </section>

  <section class="card" aria-labelledby="h-map">
    <h2 id="h-map" data-i="map_title2"></h2>
    <ul class="list" id="mapsum"></ul>
    <p class="note" id="latsum"></p>
    <button type="button" class="btn" id="uselat" hidden></button>
  </section>

  <section class="card" aria-labelledby="h-cam">
    <h2 id="h-cam" data-i="cam_title"></h2>
    <div class="row">
      <label class="f"><span data-i="cam_device"></span><select id="device"></select></label>
      <label class="f"><span data-i="cam_url"></span><input id="url" type="text" autocomplete="off" spellcheck="false"></label>
    </div>
    <div class="row">
      <button type="button" class="btn primary" id="camopen" data-i="cam_open"></button>
      <button type="button" class="btn" id="camclose" data-i="cam_close"></button>
      <button type="button" class="btn" id="camrefresh" data-i="cam_refresh"></button>
    </div>
    <p class="note" id="camstate"></p>
    <div class="row">
      <label class="f"><span data-i="marker_mm"></span><input id="marker" type="number" min="20" max="500" step="1" value="150"></label>
      <label class="f"><span data-i="latency_ms"></span><input id="latency" type="number" min="0" max="1000" step="1" value="0"></label>
    </div>
    <p class="note"><span data-i="marker_hint"></span> <span data-i="latency_hint"></span></p>
  </section>

  <section class="card" aria-labelledby="h-rec">
    <h2 id="h-rec" data-i="rec_title"></h2>
    <div class="row">
      <label class="f"><span data-i="rec_duration"></span><input id="duration" type="number" min="1" max="3600" step="1"></label>
      <button type="button" class="btn primary act" id="rec" data-i="rec_start"></button>
    </div>
  </section>

  <section class="card" aria-labelledby="h-mapact">
    <h2 id="h-mapact" data-i="map_title"></h2>
    <div class="row">
      <label class="f"><span data-i="slot"></span><select id="mapslot"></select></label>
      <label class="f"><span data-i="throttle"></span><input id="throttle" type="number" min="1" max="8" step="1" value="4"></label>
      <label class="f"><span data-i="lanes"></span><select id="lanes"><option value="2" data-i="lanes_2"></option><option value="1" data-i="lanes_1"></option></select></label>
      <label class="f"><span data-i="background"></span><select id="background"><option value="median" data-i="bg_median"></option><option value="empty" data-i="bg_empty"></option></select></label>
    </div>
    <button type="button" class="btn primary act" id="map" data-i="map_start"></button>
  </section>

  <section class="card" aria-labelledby="h-id">
    <h2 id="h-id" data-i="id_title"></h2>
    <fieldset><legend data-i="slots"></legend><div class="row" id="idslots"></div></fieldset>
    <button type="button" class="btn primary act" id="identify" data-i="id_start"></button>
  </section>

  <section class="card" aria-labelledby="h-run">
    <h2 id="h-run" data-i="run_title"></h2>
    <fieldset><legend data-i="slots"></legend><div class="row" id="runslots"></div></fieldset>
    <label class="chk"><input type="checkbox" id="runid"><span data-i="run_identify"></span></label>
    <button type="button" class="btn primary act" id="run" data-i="run_start"></button>
  </section>
</div>

<dialog id="dlg" aria-labelledby="dlgtext">
  <p id="dlgtext"></p>
  <div class="row"><button type="button" class="btn" id="dlgno" data-i="confirm_cancel"></button><button type="button" class="btn primary" id="dlgyes" data-i="confirm_ok"></button></div>
</dialog>
</main>
<script nonce="__NONCE__">
"use strict";
const MSG = __MESSAGES__;
const DEFAULT_LANG = "__LANG__";
const K = new URLSearchParams(location.search).get("k") || "";
const $ = (id) => document.getElementById(id);
const fmt = (s, o) => String(s).replace(/\\{(\\w+)\\}/g, (_, k) => (o && o[k] !== undefined ? o[k] : ""));
let lang = DEFAULT_LANG, last = null, lastOk = 0, devices = [], busy = false;
try { lang = localStorage.getItem("trackloc-lang") || lang; } catch (e) { /* private window */ }
if (!MSG[lang]) lang = "en";
const t = (k, o) => fmt(MSG[lang][k] || k, o);

function applyLang() {
  document.documentElement.lang = lang;
  document.title = "trackloc - " + t("title");
  $("title").textContent = t("title");
  document.querySelectorAll("[data-i]").forEach((el) => { el.textContent = t(el.dataset.i); });
  $("url").placeholder = t("cam_url_ph");
  $("cam").alt = t("title");
  $("langs").setAttribute("aria-label", t("language"));
  document.querySelectorAll(".lang button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));
  renderDevices(); render();
}
function slotBoxes(box, name) {
  for (let i = 1; i <= 6; i++) {
    const l = document.createElement("label"); l.className = "chk";
    const c = document.createElement("input"); c.type = "checkbox"; c.value = String(i - 1); c.name = name;
    l.append(c, document.createTextNode(String(i))); box.append(l);
  }
}
slotBoxes($("idslots"), "idslot"); slotBoxes($("runslots"), "runslot");
for (let i = 1; i <= 6; i++) { const o = document.createElement("option"); o.value = String(i - 1); o.textContent = String(i); $("mapslot").append(o); }
const picked = (name) => [...document.querySelectorAll("input[name=" + name + "]:checked")].map((c) => Number(c.value));

async function post(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Trackloc-Token": K }, body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { showError(j.error || ("HTTP " + r.status)); return null; }
    showError(""); return j;
  } catch (e) { showError(t("lost")); return null; }
}
let localError = "";
function showError(text) { localError = text; render(); }

function confirmDialog(text) {
  const d = $("dlg");
  $("dlgtext").textContent = text;
  if (typeof d.showModal !== "function") return Promise.resolve(window.confirm(text));
  return new Promise((resolve) => {
    const done = (v) => { $("dlgyes").onclick = $("dlgno").onclick = null; d.onclose = null; if (d.open) d.close(); resolve(v); };
    $("dlgyes").onclick = () => done(true); $("dlgno").onclick = () => done(false); d.onclose = () => done(false);
    d.showModal(); $("dlgno").focus();
  });
}

async function refreshDevices() {
  try { const r = await fetch("/devices", { cache: "no-store" }); devices = r.ok ? (await r.json()).devices : []; } catch (e) { devices = []; }
  renderDevices();
}
function renderDevices() {
  const sel = $("device"), keep = sel.value; sel.textContent = "";
  const none = document.createElement("option"); none.value = ""; none.textContent = devices.length ? t("cam_none") : t("cam_nodev"); sel.append(none);
  devices.forEach((d) => { const o = document.createElement("option"); o.value = String(d.index); o.textContent = "[" + d.index + "] " + d.name; sel.append(o); });
  sel.value = keep;
}

function render() {
  const el = $("statusline"), s = last;
  document.querySelectorAll(".act").forEach((b) => { b.disabled = busy || !(s && s.camera.open); });
  $("camopen").disabled = busy; $("camclose").disabled = busy;
  $("notoken").hidden = !!K;
  if (!s) { el.textContent = ""; $("error").textContent = localError; return; }
  if (Date.now() - lastOk > 4000) { el.textContent = t("lost"); }
  else {
    const bits = [t("st_" + s.status)];
    if (s.action && s.info) {
      if (s.info.need !== undefined) bits.push(t("crossings", { n: s.info.crossings || 0, need: s.info.need }));
      else if (s.info.frames !== undefined) bits.push(t("frames", { n: s.info.frames }));
    }
    el.textContent = bits.join(" - ");
  }
  const ph = s.info && s.info.phase;
  $("phase").textContent = s.action && ph ? t("phase_" + ph) : "";
  $("error").textContent = localError || s.error || "";
  const l = s.last;
  $("last").textContent = !s.action && l ? t(l.stopped ? "last_stopped" : "last_ok", { action: l.action }) + (l.summary && l.summary.results ? " - " + l.summary.results.map((r) => t(r.identified ? "id_ok" : "id_ko", { slot: r.slot + 1, note: r.note })).join("; ") : "") : "";
  $("rl").textContent = t("racelab") + ": " + t(s.racelab.card === "ok" ? "rl_ok" : s.racelab.card === "old" ? "rl_old" : "rl_down");
  $("log").textContent = (s.log || []).join("\\n");
  $("camstate").textContent = s.camera.open ? t("cam_opened", { cam: s.camera.spec }) : (s.camera.error || t("cam_closed"));
  renderMap(s); renderAim(s.preview);
}
function renderMap(s) {
  const ul = $("mapsum"); ul.textContent = "";
  const add = (cls, text) => { const li = document.createElement("li"); li.className = cls; li.textContent = text; ul.append(li); };
  const m = s.map;
  if (!m) add("info", t("map_none"));
  else {
    add("ok", t("map_len", { m: m.length_m.toFixed(2) }));
    add("info", t("map_lanes", { lanes: m.lanes.map((x) => "#" + x.id + " " + x.length_m.toFixed(2) + " m").join(", ") }));
    add("info", m.blind.length ? t("map_blind", { blind: m.blind.map((b) => Math.round(b[0] * 100) + "-" + Math.round(b[1] * 100) + " %").join(", ") }) : t("map_blind_none"));
    add("info", t("map_metric", { metric: m.metric }));
    if (m.marker_found === false) add("warn", t("map_nomarker"));
    add("info", t("map_file", { path: m.path }));
  }
  const lat = s.latency, btn = $("uselat");
  if (lat) {
    $("latsum").textContent = t("latency_est", { ms: Math.round(lat.missing_s * 1000), n: lat.n });
    btn.hidden = false; btn.textContent = t("latency_use", { ms: Math.round(lat.try_s * 1000) }); btn.dataset.ms = String(Math.round(lat.try_s * 1000));
  } else { $("latsum").textContent = t("latency_none"); btn.hidden = true; }
}
function covText(c) {
  const edges = (c.cut || []).map((e) => t("edge_" + e)).join(", ");
  return t(c.hint, { pct: Math.round(100 * (c.fill || 0)), edges });
}
function renderAim(p) {
  const ul = $("aim"); ul.textContent = "";
  const add = (cls, text) => { const li = document.createElement("li"); li.className = cls; li.textContent = text; ul.append(li); };
  if (!p || p.waiting) { add("info", t("waiting")); return; }
  const mk = p.marker;
  if (!mk.found) { add("bad", t("marker_missing")); add("info", t("marker_help")); }
  else if (mk.hint) add("warn", t(mk.hint, { px: Math.round(mk.side_px) }));
  else add("ok", t("marker_ok"));
  add("info", t("res", { w: p.width, h: p.height }) + "  " + (p.fps ? t("fps", { fps: p.fps.toFixed(1) }) : t("fps_unknown")));
  add({ cov_ok: "ok", cov_motion: "info", cov_none: "info" }[p.coverage.hint] || "warn", covText(p.coverage));
  if (p.motion.global) add("bad", t("motion_global"));
}

document.querySelectorAll(".lang button").forEach((b) => b.addEventListener("click", () => {
  lang = b.dataset.lang; try { localStorage.setItem("trackloc-lang", lang); } catch (e) { /* private window */ } applyLang(); }));
$("camrefresh").addEventListener("click", refreshDevices);
$("camopen").addEventListener("click", async () => {
  const camera = $("url").value.trim() || $("device").value;
  if (!camera) return;
  $("camopen").disabled = true; await post("/api/camera", { camera }); poll(true);
});
$("camclose").addEventListener("click", async () => { await post("/api/camera", { camera: null }); poll(true); });
$("uselat").addEventListener("click", () => { $("latency").value = $("uselat").dataset.ms; });
$("stop").addEventListener("click", async () => { await post("/api/stop", {}); poll(true); });
const num = (id) => (document.getElementById(id).value === "" ? null : Number(document.getElementById(id).value));
$("rec").addEventListener("click", async () => { await post("/api/record", { duration: num("duration") }); poll(true); });
$("map").addEventListener("click", async () => {
  const n = num("throttle"), empty = $("background").value === "empty";
  const text = t("confirm_map", { n }) + (empty ? " " + t("confirm_empty", { s: 5 }) : "");
  if (!(await confirmDialog(text))) return;
  await post("/api/map", { confirm: true, slot: Number($("mapslot").value), throttle: n, lanes: Number($("lanes").value),
    background: $("background").value, marker_mm: num("marker"), latency_ms: num("latency") }); poll(true);
});
$("identify").addEventListener("click", async () => {
  const slots = picked("idslot");
  if (!(await confirmDialog(t("confirm_identify", { n: 4 })))) return;
  await post("/api/identify", { confirm: true, slots }); poll(true);
});
$("run").addEventListener("click", async () => {
  const identify = $("runid").checked;
  if (identify && !(await confirmDialog(t("confirm_identify", { n: 4 })))) return;
  await post("/api/run", { confirm: identify, identify, slots: picked("runslot"), latency_ms: num("latency") }); poll(true);
});
let timer = null;
async function poll(now) {
  clearTimeout(timer);
  try {
    const r = await fetch("/status", { cache: "no-store" });
    if (r.ok) { last = await r.json(); lastOk = Date.now(); busy = !!last.action || last.camera.opening; }
  } catch (e) { /* shown as lost */ }
  render(); timer = setTimeout(poll, 1000);
}
applyLang(); refreshDevices(); poll();
</script>
</body>
</html>
"""


def page(nonce: str, lang: str) -> str:
    blob = json.dumps(messages(), ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace("__NONCE__", nonce).replace("__MESSAGES__", blob).replace("__LANG__", lang)


# ----------------------------------------------------------------------------- the server

def make_app(sv: Serve) -> web.Application:
    """GET: `/` `/stream` `/status` `/devices`. POST (token + JSON): `/api/camera|record|map|identify|run|stop`."""

    @web.middleware
    async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
        if request.method not in ("GET", "POST"):
            return web.Response(status=405, headers={"Allow": "GET, POST"}, text="GET and POST only")
        refused = pv.check_request(request)  # Host (421), cross-site / cross-origin (403): the preview's own checks
        if refused is not None:
            return refused
        if request.method == "POST":
            if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, "").encode(), sv.token.encode()):
                return web.json_response({"ok": False, "error": "missing or wrong token"}, status=403)
            if request.content_type != "application/json":
                return web.json_response({"ok": False, "error": "Content-Type must be application/json"}, status=415)
        return await handler(request)

    async def body_of(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise BadRequest("the body must be JSON") from None
        if not isinstance(body, dict):
            raise BadRequest("the body must be a JSON object")
        return body

    def ok() -> web.Response:
        return web.json_response({"ok": True, "status": sv.status()})

    def fail(e: Exception) -> web.Response:
        return web.json_response({"ok": False, "error": str(e)}, status=409 if isinstance(e, Busy) else 400)

    def action(name: str) -> Callable[[web.Request], Any]:
        async def handler(request: web.Request) -> web.Response:
            try:
                sv.start(name, await body_of(request))
            except (BadRequest, Busy) as e:
                return fail(e)
            return ok()

        return handler

    async def camera(request: web.Request) -> web.Response:
        try:
            body = await body_of(request)
            if sv.runner.busy or sv.opening:
                raise Busy(f"{sv.runner.name or 'the camera'} is busy: stop it first")
            spec = body.get("camera")
            if spec is None:
                sv.camera.close()
                sv.camera_error = None
                return ok()
            spec = check_camera_spec(spec)
            sv.opening = True
        except (BadRequest, Busy) as e:
            return fail(e)
        try:
            await asyncio.get_running_loop().run_in_executor(None, sv.camera.open, spec)
            sv.camera_error = None
        except (OSError, TracklocError, ValueError) as e:
            sv.camera_error = str(e)
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        finally:
            sv.opening = False
        return ok()

    async def stop(request: web.Request) -> web.Response:
        sv.runner.request_stop()
        return ok()

    async def devices(request: web.Request) -> web.Response:
        from .sources import ffmpeg_devices

        try:
            names = await asyncio.get_running_loop().run_in_executor(None, ffmpeg_devices)
        except Exception:  # ffmpeg missing or hung: the page then offers the index / URL fields
            names = {}
        return web.json_response({"devices": [{"index": i, "name": n} for i, n in names.items()]})

    async def index(request: web.Request) -> web.Response:
        nonce = secrets.token_urlsafe(16)
        return web.Response(text=page(nonce, sv.lang), content_type="text/html", charset="utf-8",
                            headers={"Content-Security-Policy": pv.csp(f"'nonce-{nonce}'")})

    async def status(request: web.Request) -> web.Response:
        return web.json_response(sv.status())

    async def on_shutdown(app: web.Application) -> None:
        state["closing"] = True

    state = {"streams": 0, "closing": False}
    app = web.Application(middlewares=[guard])
    app[ALLOWED_HOSTS] = set()
    app.on_response_prepare.append(pv.add_headers)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index, allow_head=False)
    app.router.add_get("/stream", pv.stream_handler(sv.preview, state), allow_head=False)
    app.router.add_get("/status", status, allow_head=False)
    app.router.add_get("/devices", devices, allow_head=False)
    app.router.add_post("/api/camera", camera)
    for name in STATUS_OF:
        app.router.add_post(f"/api/{name}", action(name))
    app.router.add_post("/api/stop", stop)
    return app


def run_serve(args: argparse.Namespace) -> int:
    lang = args.lang or ("fr" if os.environ.get("LANG", "").lower().startswith("fr") else "en")
    out_dir = Path(args.out_dir) if args.out_dir else Path.home() / ".racelab" / "trackloc" / "serve"
    sv = Serve(args, lang, out_dir)
    if args.map:
        try:
            sv.load_map(args.map)
        except (OSError, ValueError, KeyError) as e:
            sys.exit(f"cannot read the map {args.map}: {e}")
    stop = threading.Event()
    threading.Thread(target=sv.preview.run, args=(stop,), daemon=True, name="trackloc-preview").start()
    first = args.camera if args.camera is not None else ("video" if args.video else None)
    if first is not None:
        try:
            sv.camera.open(check_camera_spec(str(first)))
        except (OSError, BadRequest, ValueError) as e:
            sv.camera_error = str(e)
            print(f"camera not opened: {e} (choose one on the page)", flush=True)

    def ready(host: str, port: int) -> None:
        sv.port = port
        sv.card.start()
        print(f"\ntrackloc serve: open {sv.ui_url()}\n(this computer only; the key after ?k= is new at every start; "
              "Ctrl-C stops everything)", flush=True)

    try:
        asyncio.run(pv.serve(sv.preview, args.port, stop, ready, app=make_app(sv)))
    except OSError as e:
        sys.exit(f"cannot listen on 127.0.0.1:{args.port}: {e} (another `serve` or `preview` running? try --port)")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        print("stopping: handing the cars back and closing the camera...", flush=True)
        sv.shutdown()
    return 0
