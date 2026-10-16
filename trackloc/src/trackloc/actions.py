"""The things `python -m trackloc` does with a camera, as callables: `do_record`, `do_map`, `do_identify`, `do_run`.

Both front ends call these: the command line (`__main__`, in the main thread: Ctrl-C works as it always did) and
`python -m trackloc serve` (a background thread; a Stop button sets `Ctx.stop`). So they

- take their settings as an `argparse.Namespace` (the command line's own parser gives every default),
- say what they do through `Ctx.say` (the terminal: `print`; the page: a log),
- raise `TracklocError` (bad input, no camera, RaceLab down...) or the existing `MapAborted` / `RollCallAborted` /
  `MapError` instead of calling `sys.exit`; the command line turns them into the same messages and exit codes,
- never open the camera themselves when `Ctx.source` is given (the page owns ONE camera and shares it),
- clean up in `finally` blocks: whatever happens, the camera and the RaceLab link are released, and the car is
  handed back at throttle 0 by `drive` / `identify` (their own `finally`; closing the link also makes RaceLab unclaim).

A Stop (`Ctx.stop`) ends a map or a roll call with the same hand-back as an E-STOP-less abort; it never moves anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class TracklocError(Exception):
    """Something the user can fix (bad value, no camera, RaceLab not running): the message says what to do."""


@dataclass
class Ctx:
    say: Callable[[str], None] = field(default=lambda text: print(text, flush=True))
    stop: threading.Event = field(default_factory=threading.Event)
    source: Any = None  # an open frame source with info() / start(cb) / stop(), shared with the caller (the page)
    connect: Callable[[argparse.Namespace], Any] | None = None  # RaceLab link factory (tests); None: RaceLabLink
    interactive: bool = field(default_factory=lambda: sys.stdin.isatty())  # may ask a question on the terminal
    empty_countdown_s: float = 3.0  # `--background empty`: time to clear the track before the capture
    empty_place_s: float = 6.0  # ... and to put the car back (the terminal asks for Enter instead)
    info: dict[str, Any] = field(default_factory=dict)  # live numbers for the page (crossings, frames...)


def size_of(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    w, h = text.lower().split("x")
    return int(w), int(h)


def slots_of(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split(",") if x.strip() != "")


def data_dir(name: str) -> Path:
    return Path.home() / ".racelab" / "trackloc" / (time.strftime("%Y%m%d-%H%M%S") + "-" + name)


# ----------------------------------------------------------------------------- shared pieces

def open_source(args: argparse.Namespace, ctx: Ctx) -> Any:
    """The shared camera of the page, or the one `--camera` names (opened here, and stopped by the caller)."""
    if ctx.source is not None:
        return ctx.source
    from .sources import CameraSource, FfmpegSource, resolve_camera

    try:
        args.camera = resolve_camera(args.camera, ask=input if ctx.interactive else None)
    except OSError as e:
        raise TracklocError(f"{e}\n(or --video FILE to work offline)") from e
    size = size_of(args.size)
    if args.ffmpeg:
        return FfmpegSource(args.camera, size=size or (1280, 720), fps=args.fps or 30.0, mirror=args.mirror,
                            rotate=args.rotate, exposure_offset_s=args.exposure_offset)
    return CameraSource(args.camera, size=size, fps=args.fps, mirror=args.mirror, rotate=args.rotate,
                        exposure_offset_s=args.exposure_offset).open()


def connect_link(args: argparse.Namespace, ctx: Ctx) -> Any:
    if ctx.connect is not None:
        return ctx.connect(args)
    from .link_racelab import RaceLabLink

    try:
        return RaceLabLink(args.racelab_url, args.name, args.token).connect()
    except (ConnectionError, OSError) as e:
        raise TracklocError(f"{e}\nStart RaceLab (`racelab`) first, or use --inputs FILE to work offline.") from e


def format_report(model: Any) -> str:
    r = model.report
    lines = ["", "MAP", "---"]
    lanes = ", ".join(f"lane {ln.lane_id}: {ln.length:.2f} m" for ln in model.lanes)
    lines.append(f"{lanes}; centre line {model.length_c:.2f} m; lane spacing {model.pitch_m * 100:.1f} cm; metres from: {r['metric']}")
    lines.append(f"laps used: " + ", ".join(f"{lp['lap']}={lp['kind']} ({lp['length_m']:.2f} m, {lp['time_s']:.1f} s)" for lp in r["laps"]))
    if not r["marker_found"]:
        lines.append("WARNING: marker (DICT_4X4_50 id 0) not found in the background: lengths come from the motor model "
                     "(a guess). Print the marker (`marker`), lay it flat in view and map again, or give --track-length-m.")
    cal = r.get("perspective_calibration")
    if cal:
        lines.append(f"perspective: marker alone gave speed spread {cal['cost_before']:.2f}; "
                     f"{'tilted by the car motion to ' + format(cal['cost_after'], '.2f') if cal['applied'] else 'kept (no better fit)'}"
                     " (lower = more constant speed around the lap)")
    if r.get("length_marker_m"):
        lines.append(f"length from the marker {r['length_marker_m']:.2f} m; from steady speed x lap time (simulator motor model, "
                     f"UNCALIBRATED on real cars) {r['length_model_m']:.2f} m: discrepancy {r['model_discrepancy_pct']:+.0f} %")
    if r.get("user_length_discrepancy_pct") is not None:
        lines.append(f"known length given: the markers' length was {r['user_length_discrepancy_pct']:+.1f} % off")
    lines.append(f"speed {r['speed_mps']:.2f} m/s (gain vs the model: {model.gain_hint:.2f}); lap {r['lap_time_s']:.1f} s")
    lines.append(f"lane changers at {[round(c, 3) for c in r['changers']]}; blind zones (lap fractions) {[(round(a, 3), round(b, 3)) for a, b in r['blind']]}; "
                 f"sector anchors {r['anchors']}")
    for n in r["notes"]:
        lines.append("note: " + n)
    if r.get("clock_offset_s") is not None:
        lines.append(f"CU -> host clock offset {r['clock_offset_s']:.3f} s")
    return "\n".join(lines)


def prepare(model_path: str, first_frame: Any, register: bool, stored: dict[str, Any] | None = None,
            say: Callable[[str], None] = print) -> tuple[Any, Any, dict[str, Any] | None]:
    """Load the map; re-register its background to the current view (or reuse the registration stored by
    a recording: replay then matches the live session exactly); returns (model, background, registration)."""
    import cv2
    import numpy as np

    from .geometry import MapModel, RegistrationError, register as do_register

    model = MapModel.load(model_path)
    if model.background is None:
        raise TracklocError("the map has no background image next to it (…bg.png): run `map` again")
    h, w = first_frame.shape[:2]
    bg = cv2.resize(model.background, (w, h), interpolation=cv2.INTER_LINEAR)
    if (w, h) != tuple(model.size):  # a different capture size: scale the map
        k = np.diag([w / model.size[0], h / model.size[1], 1.0])
        model = model.warped(k)
    if not register:
        return model, bg, None
    if stored:
        W, cc = np.array(stored["W"]), stored["cc"]
    else:
        try:
            W, cc = do_register(bg, first_frame)
        except RegistrationError as e:
            raise TracklocError(str(e)) from e
    shift = float(np.hypot(W[0, 2], W[1, 2]))
    say(f"map registered to the current view (correlation {cc:.2f}, shift {shift:.1f} px)" + (" [as stored in the recording]" if stored else ""))
    return (model.warped(W), cv2.warpPerspective(bg, W, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE),
            {"W": W.tolist(), "cc": float(cc)})


def wait_frame(feed: Any, ctx: Ctx, seconds: float = 10.0) -> Any:
    for _ in range(int(seconds / 0.05)):
        got = feed.latest()
        if got is not None:
            return got[1]
        if ctx.stop.wait(0.05):
            raise TracklocError("stopped before the first camera frame")
    raise TracklocError(f"no frame from the camera in {seconds:g} s")


def roll_setup(args: argparse.Namespace) -> tuple[tuple[int, ...], Any]:
    """The slots and the settings of the roll call, validated BEFORE anything is opened or connected."""
    from .identify import RollConfig

    slots = slots_of(args.slots)
    if not slots:
        raise TracklocError("the roll call needs --slots: the slots of the cars on the track, e.g. 0,1,2")
    try:
        return slots, RollConfig(throttle=args.throttle, cap=args.cap, pulse_s=args.pulse_s)
    except ValueError as e:
        raise TracklocError(str(e)) from e


def roll_call(args: argparse.Namespace, link: Any, pipe: Any, feed: Any, ctx: Ctx) -> list[Any]:
    """The roll call of `identify` and `run --identify`; raises RollCallAborted (the cars were handed back)."""
    from .identify import RollCall, format_results

    slots, cfg = roll_setup(args)
    ctx.say(f"roll call of slots {[s + 1 for s in slots]}: one pulse each at throttle {cfg.throttle}/15 for {cfg.pulse_s:g} s "
            "(refused while a session runs or E-STOP is on; Ctrl-C hands the car back)...")
    results = RollCall(link, pipe, feed, slots, cfg).run(ctx.stop)
    ctx.say(format_results(results))
    return results


def build_and_report(args: argparse.Namespace, frames: Any, inputs: Any, empty_range: Any, ctx: Ctx) -> Any:
    from .mapper import MapConfig, MapError, build_map

    cfg = MapConfig(marker_size=args.marker_size, throttle=args.throttle, background=args.background,
                    empty_frames=args.empty_frames, lane=args.lane, lane_pitch_m=args.lane_pitch_m,
                    latency_s=args.latency, track_length_m=args.track_length_m, name=args.name, slot=args.slot)
    try:
        model = build_map(frames, inputs, cfg, empty_range=empty_range)
    except MapError as e:
        ctx.say(f"cannot build the map: {e}")
        raise
    model.save(args.out)
    ctx.say(format_report(model))
    ctx.say(f"\nmap saved: {args.out} (+ .bg.png). Next: python -m trackloc run --camera N --map {args.out} --racelab-url ...")
    return model


def map_recording_dir(args: argparse.Namespace) -> Path:
    out_dir = Path(args.out).with_suffix("")
    return out_dir.parent / (out_dir.name + "_recording")


# ----------------------------------------------------------------------------- record

def do_record(args: argparse.Namespace, ctx: Ctx) -> dict[str, Any]:
    from .live import Live
    from .recorder import Recorder

    if args.video:
        raise TracklocError("`record` records a live camera")
    out = Path(args.out) if args.out else data_dir("record")
    src = open_source(args, ctx)
    rec = live = link = None
    live_started = False
    try:
        info = src.info()
        rec = Recorder(out, fps=info["fps"], size=(info["width"], info["height"]), codec=args.codec,
                       meta={"camera": args.camera, "args": vars(args), "info": info})
        live = Live(recorder=rec)
        if args.racelab_url:
            link = connect_link(args, ctx)
            link.raw = rec.log_raw
            link.start_inputs(live.on_input)
            ctx.say(f"RaceLab connected ({args.racelab_url}): crossings and throttles are recorded")
        else:
            ctx.say("no --racelab-url: video only")
        src.start(live.on_frame)
        live.start()
        live_started = True
        ctx.say(f"recording to {out} ({info['width']}x{info['height']} at {info['fps']:.0f} fps, {args.codec}); Ctrl-C to stop")
        t0 = time.monotonic()
        try:
            while args.duration is None or time.monotonic() - t0 < args.duration:
                if ctx.stop.wait(2.0):
                    break
                off = rec.clock.offset
                ctx.info.update(frames=live.frames, crossings=sum(live.crossings.values()), dropped=live.stream.dropped + rec.dropped)
                ctx.say(f"  {time.monotonic() - t0:5.0f} s  frames {live.frames}  dropped {live.stream.dropped + rec.dropped}  "
                        f"crossings {sum(live.crossings.values())}  clock offset {'-' if off is None else format(off, '.3f')}")
        except KeyboardInterrupt:
            pass
    finally:
        src.stop()
        if live is not None and live_started:
            live.stop()
        if rec is not None:
            rec.close()
        if link is not None:
            link.close()
    frames = live.frames if live else 0
    crossings = sum(live.crossings.values()) if live else 0
    ctx.say(f"saved {out}  ({frames} frames, {crossings} crossings)")
    return {"out": str(out), "frames": frames, "crossings": crossings}


# ----------------------------------------------------------------------------- map

def do_map(args: argparse.Namespace, ctx: Ctx) -> dict[str, Any]:
    from .drive import DiscoveryDriver, DriveConfig, MapAborted
    from .live import Live, MotionMeter
    from .recorder import Recorder, RecordingReader, load_inputs

    if args.recording or args.video:  # offline: rebuild from a recording
        if args.recording:
            reader = RecordingReader(args.recording)
            marks = reader.marks("empty_start"), reader.marks("empty_end")
            erange = (marks[0][0]["t"], marks[1][0]["t"]) if marks[0] and marks[1] else None
            return {"model": build_and_report(args, reader.frames, reader.inputs(), erange, ctx), "map": args.out}
        from .sources import FileSource
        if not args.inputs:
            raise TracklocError("--video needs --inputs FILE.jsonl (the crossings)")
        return {"model": build_and_report(args, FileSource(args.video).frames, load_inputs(args.inputs), None, ctx), "map": args.out}

    if not args.racelab_url and ctx.connect is None:
        raise TracklocError("`map` drives a car: give --racelab-url (or rebuild offline with --recording DIR)")
    laps = args.laps or (4 if args.lanes == 2 else 2)
    rec_dir = map_recording_dir(args)
    src = open_source(args, ctx)
    rec = live = link = None
    live_started = False
    drv = None
    try:
        try:
            info = src.info()
            rec = Recorder(rec_dir, fps=info["fps"], size=(info["width"], info["height"]), codec=args.codec,
                           meta={"command": "map", "args": vars(args), "info": info})
            live = Live(recorder=rec, meter=MotionMeter())
            link = connect_link(args, ctx)
            link.raw = rec.log_raw
            link.start_inputs(live.on_input)
            src.start(live.on_frame)
            live.start()
            live_started = True
            if args.background == "empty":
                ctx.say("\nREMOVE EVERY CAR from the track and keep your hands out of the view.")
                ctx.info["phase"] = "clear"
                for k in range(int(ctx.empty_countdown_s), 0, -1):
                    ctx.say(f"  capturing the empty track in {k}...")
                    ctx.stop.wait(1.0)
                rec.mark("empty_start")
                n0 = live.frames
                deadline = time.monotonic() + 30.0
                while live.frames < n0 + args.empty_frames + 3:
                    if ctx.stop.wait(0.05):
                        break
                    if time.monotonic() > deadline:
                        raise TracklocError("no frames from the camera while capturing the empty track")
                rec.mark("empty_end")
                ctx.say(f"empty track captured ({args.empty_frames} frames).")
                ctx.say(f"NOW PUT ONE CAR (slot {args.slot + 1}) on the track, just behind the finish line.")
                ctx.info["phase"] = "place"
                if args.yes or not ctx.interactive:
                    ctx.stop.wait(ctx.empty_place_s)
                else:
                    input("Press Enter when the car is in place... ")
                ctx.info["phase"] = "drive"
            if ctx.stop.is_set():  # nothing has moved yet
                raise MapAborted("stopped by the user before the car was started")

            def crossings() -> int:
                n = live.crossing_count(args.slot)
                ctx.info["crossings"] = n
                return n

            ctx.info.update(need=laps + 1, throttle=args.throttle)
            drv = DiscoveryDriver(link, args.slot, DriveConfig(throttle=args.throttle, throttle_cap=args.cap, laps=laps,
                                                               lanes=args.lanes, stall_s=args.stall_s),
                                  progress=live.progress, crossings=crossings)
            ctx.say(f"driving slot {args.slot + 1} at throttle {args.throttle}/15 for {laps} laps (Ctrl-C hands the car back)...")
            result = drv.run(ctx.stop)
            ctx.say(f"discovery lap done: {result['crossings']} crossings, throttle {result['throttle']}"
                    + (f", {result['bumps']} stall bump(s)" if result["bumps"] else ""))
            for n in result["notes"]:
                ctx.say("  note: " + n)
        except MapAborted as e:
            ctx.say(f"\nABORTED: {e}")
            for n in (drv.log if drv is not None else []):
                ctx.say("   " + n)
            raise
        except KeyboardInterrupt:
            ctx.say("\ninterrupted: the car was handed back (throttle 0).")
            raise
        finally:
            src.stop()
            if live is not None and live_started:
                live.stop()
            if rec is not None:
                rec.close()
            if link is not None:
                link.close()
    except (MapAborted, KeyboardInterrupt):
        if rec is not None:
            ctx.say(f"the recording so far is in {rec_dir}; rebuild with: map --recording {rec_dir} --slot {args.slot}")
        raise
    reader = RecordingReader(rec_dir)
    marks = reader.marks("empty_start"), reader.marks("empty_end")
    erange = (marks[0][0]["t"], marks[1][0]["t"]) if marks[0] and marks[1] else None
    model = build_and_report(args, reader.frames, reader.inputs(), erange, ctx)
    ctx.say(f"recording kept in {rec_dir} (rebuild with other parameters: map --recording {rec_dir} --slot {args.slot})")
    return {"model": model, "map": args.out, "recording": str(rec_dir), "drive": result}


# ----------------------------------------------------------------------------- identify

def do_identify(args: argparse.Namespace, ctx: Ctx) -> dict[str, Any]:
    from .identify import FrameTap, RollCallAborted
    from .pipeline import Pipeline, PipelineConfig

    if args.video:
        raise TracklocError("`identify` moves cars: it needs a live camera")
    roll_setup(args)
    tap = FrameTap()
    src = open_source(args, ctx)
    src.start(tap.on_frame)
    link = None
    try:
        model, bg, _ = prepare(args.map, wait_frame(tap, ctx), True, say=ctx.say)
        pipe = Pipeline(model, PipelineConfig(), background=bg)
        link = connect_link(args, ctx)
        return {"results": roll_call(args, link, pipe, tap, ctx)}
    except RollCallAborted as e:
        ctx.say(f"\nABORTED: {e}")
        raise
    except KeyboardInterrupt:
        ctx.say("\ninterrupted: the car was handed back (throttle 0, unclaimed).")
        raise
    finally:
        src.stop()
        if link:
            link.close()


# ----------------------------------------------------------------------------- run

def do_run(args: argparse.Namespace, ctx: Ctx) -> dict[str, Any]:
    from .debug import Overlay
    from .live import Live
    from .pipeline import Pipeline, PipelineConfig, merge, run_stream
    from .recorder import JsonlSink, Recorder, load_inputs
    from .sources import FileSource

    cfg = PipelineConfig(latency_s=args.latency, expected_slots=slots_of(args.slots))
    have_link = bool(args.racelab_url) or ctx.connect is not None
    if args.identify and (args.video or args.inputs or args.sink or not have_link):
        raise TracklocError("--identify moves the cars through RaceLab: it needs the live camera and --racelab-url (not --video, --inputs or --sink)")
    if args.identify:
        roll_setup(args)  # --slots, --throttle, --cap, --pulse-s checked before the camera or RaceLab is touched
    if args.video or args.inputs:  # offline
        import cv2

        if not (args.video and args.inputs):
            raise TracklocError("offline `run` needs --video FILE and --inputs FILE.jsonl")
        stamps = Path(args.video).with_name("frames.jsonl")  # a recording's video: use its host-clock times
        times = [json.loads(line)["t"] for line in stamps.read_text().splitlines() if line.strip()] if stamps.exists() else None
        if times is None:
            ctx.say("warning: no frames.jsonl next to the video, frames are timed from 0: the inputs' crossings will not "
                    "line up with them (for a recording, use `replay`)")
        fsrc = FileSource(args.video, times=times)
        first = next(iter(fsrc.frames()))[1]
        model, bg, _ = prepare(args.map, first, not args.no_register, say=ctx.say)
        pipe = Pipeline(model, cfg, background=bg)
        sink = JsonlSink(args.sink or "positions.jsonl")
        h, w = first.shape[:2]
        overlay = Overlay(args.debug, cv2.VideoCapture(args.video).get(cv2.CAP_PROP_FPS) or 30.0, (w, h)) if args.debug else None
        run_stream(pipe, merge(fsrc.frames(), load_inputs(args.inputs)), sink, args.hz,
                   after_frame=(lambda img, p: overlay.write(img, p)) if overlay else None)
        if overlay:
            overlay.close()
        sink.close()
        ctx.say(f"{len(pipe.log)} frames processed; positions in {args.sink or 'positions.jsonl'}")
        return {"frames": len(pipe.log)}

    src = open_source(args, ctx)
    rec = live = link = pipe = None
    sink: Any = None
    live_started = False
    error: BaseException | None = None
    try:
        info = src.info()
        if args.record:
            rec = Recorder(args.record, fps=info["fps"], size=(info["width"], info["height"]), codec=args.codec,
                           meta={"command": "run", "args": vars(args), "info": info})
        live = Live(recorder=rec, defer=True)
        tap = None
        if args.identify:
            from .identify import FrameTap

            tap = FrameTap()
            live.frame_tap = tap.on_frame  # the roll call looks at frames before the session is processed
        src.start(live.on_frame)
        live.start()
        live_started = True
        for _ in range(200):  # the first frame registers the map to the current view
            if live.first_frame is not None:
                break
            if ctx.stop.wait(0.05):
                raise TracklocError("stopped before the first camera frame")
        else:
            raise TracklocError("no frame from the camera in 10 s")
        model, bg, registration = prepare(args.map, live.first_frame, not args.no_register, say=ctx.say)
        if rec is not None:
            rec.meta["registration"] = registration
            rec.meta["map"] = args.map
            rec.meta["latency_s"] = args.latency
        pipe = Pipeline(model, cfg, background=bg)
        live.pipeline = pipe
        if args.debug:
            live.overlay = Overlay(args.debug, info["fps"], (info["width"], info["height"]))
        if args.sink:
            sink = JsonlSink(args.sink)
        else:
            if not have_link:
                raise TracklocError("give --racelab-url to publish to RaceLab (or --sink FILE to write the positions to a file)")
            link = connect_link(args, ctx)
            sink = link
            if rec:
                link.raw = rec.log_raw
        sink.set_track(model.geometry())
        if link:
            link.start_inputs(live.on_input)

        if tap is not None:
            from .identify import RollCallAborted, apply_results

            try:
                results = roll_call(args, link, pipe, tap, ctx)
            except RollCallAborted as e:
                ctx.say(f"\nABORTED: {e}")
                raise
            except KeyboardInterrupt:
                ctx.say("\ninterrupted: the car was handed back (throttle 0, unclaimed).")
                raise
            live.frame_tap = None
            with pipe.lock:
                named = apply_results(pipe.tracker, results)
            ctx.say(f"named by the roll call: slots {[s + 1 for s in named]}; the others wait for a finish-line crossing")
        live.active = True
        ctx.say(f"track '{model.name}' published ({model.length_c:.2f} m, {len(model.lanes)} lane(s)); cars appear once a "
                "finish-line crossing names them" + ("" if not cfg.expected_slots else f" (or at once: expected slots {cfg.expected_slots})"))
        live.start_publisher(sink, args.hz)
        try:
            while live.error is None and not ctx.stop.is_set():
                if ctx.stop.wait(1.0):
                    break
                cars = pipe.positions(time.monotonic())
                ms = sum(pipe.times_ms[-30:]) / max(1, len(pipe.times_ms[-30:]))
                ctx.info.update(frames=live.frames, ms=round(ms, 1), dropped=live.stream.dropped,
                                cars=[{"slot": c["slot"], "s": round(c["s"], 3), "off": bool(c["off"])} for c in cars])
                ctx.say(f"  frames {live.frames} ({ms:.1f} ms/frame)  dropped {live.stream.dropped}  cars " +
                        " ".join(f"#{c['slot'] + 1}:{c['s']:.3f}{'!' if c['off'] else ''}" for c in cars))
        except KeyboardInterrupt:
            pass
    finally:
        src.stop()
        if live is not None and live_started:
            live.stop()
        if rec:
            rec.close()
        if link:
            try:
                link.clear_track()
            finally:
                link.close()
        elif sink is not None and hasattr(sink, "close"):
            sink.close()
    if live is not None and live.error is not None:
        error = live.error
        ctx.say(f"stopped: {error}")
    result: dict[str, Any] = {"error": None if error is None else str(error)}
    if pipe is not None:  # what the CU's crossings say about the camera latency still missing from the frame times
        try:
            from .tracker import estimate_latency

            est = estimate_latency(pipe.tracker.crossings)
            if est["median"] is not None:
                result["latency"] = {"missing_s": est["median"], "try_s": args.latency + est["median"], "n": int(est["n"])}
                ctx.say(f"latency still missing from the frame times: {est['median'] * 1000:+.0f} ms (from {int(est['n'])} crossings); "
                        f"try --latency {args.latency + est['median']:.3f} (and map again with it)")
        except Exception as e:  # an estimate, never a reason to fail a run that went well
            ctx.say(f"(latency estimate failed: {e})")
    return result
