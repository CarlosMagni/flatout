"""python -m trackloc <command> ...   (see trackloc/README.md)

    probe     list the cameras (resolution, measured fps)
    marker    write the ArUco marker to print (DICT_4X4_50, id 0)
    record    record camera + RaceLab events to a folder (the most important one on the first day)
    map       discovery lap: drive a car slowly, record, build the map (or rebuild it from a recording)
    run       live: camera + map -> car positions published to RaceLab
    replay    run the same pipeline offline on a recording (identical to the live session)
    synth     write synthetic recordings (ground truth) to try map/replay before touching the track
    bench     synthetic benchmark: accuracy, identity, latency, speed
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_URL = "ws://localhost:8130/ws"


def _size(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    w, h = text.lower().split("x")
    return int(w), int(h)


def _add_camera(p: argparse.ArgumentParser) -> None:
    p.add_argument("--camera", type=int, default=None, help="camera index (see `probe`)")
    p.add_argument("--video", help="a video file instead of a camera (offline)")
    p.add_argument("--size", help="capture size, e.g. 1280x720")
    p.add_argument("--fps", type=float, default=None, help="requested frame rate (30 or 60)")
    p.add_argument("--ffmpeg", action="store_true", help="capture through ffmpeg (fallback if OpenCV cannot open the camera)")
    p.add_argument("--mirror", action="store_true", help="flip the image left-right (a mirrored image swaps the lanes)")
    p.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270), help="rotate the image")
    p.add_argument("--exposure-offset", type=float, default=None,
                   help="seconds subtracted from the arrival time of a frame (default: half a frame period)")


def _add_link(p: argparse.ArgumentParser) -> None:
    p.add_argument("--racelab-url", default=None, help=f"RaceLab WebSocket, e.g. {DEFAULT_URL}")
    p.add_argument("--token", default=None, help="RaceLab API token, if set")
    p.add_argument("--inputs", help="a JSONL of inputs (crossing/throttle) instead of RaceLab (offline)")
    p.add_argument("--name", default="camera", help="agent name shown as the track source in RaceLab")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m trackloc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="list cameras")
    p.add_argument("--index", type=int, help="only this index")
    p.add_argument("--max", type=int, default=4)

    p = sub.add_parser("marker", help="write the marker to print (PDF at an exact size, and a PNG preview)")
    p.add_argument("-o", "--out", default="trackloc_marker.pdf")
    p.add_argument("--size-mm", type=float, default=150.0, help="side of the black square (A4 holds up to ~190)")
    p.add_argument("--landscape", action="store_true")

    p = sub.add_parser("record", help="record video + timestamps + RaceLab events")
    _add_camera(p)
    _add_link(p)
    p.add_argument("--out", default=None, help="folder (default ~/.racelab/trackloc/<date>-record)")
    p.add_argument("--codec", default="mjpg", choices=("mjpg", "ffv1"), help="mjpg: small; ffv1: lossless (big)")
    p.add_argument("--duration", type=float, default=None, help="seconds (default: until Ctrl-C)")

    p = sub.add_parser("map", help="discovery lap and map")
    _add_camera(p)
    _add_link(p)
    p.add_argument("--slot", type=int, required=True, help="slot 0..5 of the car that drives the discovery lap")
    p.add_argument("--out", default="track_map.json", help="map file to write")
    p.add_argument("--recording", help="rebuild the map from this recording instead of driving (offline)")
    p.add_argument("--throttle", type=int, default=4, help="discovery throttle 0..15")
    p.add_argument("--cap", type=int, default=8, help="a stall never raises the throttle above this")
    p.add_argument("--stall-s", type=float, default=1.5)
    p.add_argument("--laps", type=int, default=None, help="laps to record (default 4 with 2 lanes, 2 with 1)")
    p.add_argument("--lanes", type=int, default=2, choices=(1, 2), help="2: also map the other lane (lane change)")
    p.add_argument("--lane", type=int, default=0, choices=(0, 1), help="lane of the car when only one lane is mapped")
    p.add_argument("--lane-pitch-m", type=float, default=0.10, help="rail-to-rail lane spacing (m), single lane only")
    p.add_argument("--marker-size", type=float, default=0.150, help="side of the printed black square, in metres (measure it!)")
    p.add_argument("--track-length-m", type=float, default=None, help="known length of lane 0 (rescales the metres)")
    p.add_argument("--background", default="median", choices=("median", "empty"),
                   help="median of the frames of the lap, or the average of ~30 frames of the empty track")
    p.add_argument("--empty-frames", type=int, default=30)
    p.add_argument("--latency", type=float, default=0.0, help="camera latency in seconds (from `replay`)")
    p.add_argument("--codec", default="mjpg", choices=("mjpg", "ffv1"))
    p.add_argument("--yes", action="store_true", help="do not wait for Enter (empty background)")

    p = sub.add_parser("run", help="live localisation, published to RaceLab")
    _add_camera(p)
    _add_link(p)
    p.add_argument("--map", required=True, help="map file from `map`")
    p.add_argument("--latency", type=float, default=0.0, help="camera latency in seconds (from `replay`)")
    p.add_argument("--slots", default="", help="cars on the track, e.g. 0,1 (one car: no crossing needed to name it)")
    p.add_argument("--record", default=None, help="also record the session to this folder")
    p.add_argument("--codec", default="mjpg", choices=("mjpg", "ffv1"))
    p.add_argument("--debug", default=None, help="write an overlay video (polylines, detections, tracks)")
    p.add_argument("--no-register", action="store_true", help="do not re-register the map to the current view")
    p.add_argument("--sink", default=None, help="write the positions to this JSONL instead of RaceLab (offline)")
    p.add_argument("--hz", type=float, default=30.0)

    p = sub.add_parser("replay", help="run the pipeline offline on a recording")
    p.add_argument("recording")
    p.add_argument("--map", required=True)
    p.add_argument("--latency", type=float, default=0.0)
    p.add_argument("--slots", default="")
    p.add_argument("--sink", default=None, help="JSONL of what would be published")
    p.add_argument("--debug", default=None)
    p.add_argument("--no-register", action="store_true")

    p = sub.add_parser("synth", help="write synthetic recordings")
    p.add_argument("out")
    p.add_argument("--seed", type=int, default=3)
    p.add_argument("--elevation", type=float, default=55.0)
    p.add_argument("--marker-size", type=float, default=0.15)

    p = sub.add_parser("bench", help="synthetic benchmark")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--json", action="store_true")
    return ap


# ----------------------------------------------------------------------------- helpers

def _data_dir(name: str) -> Path:
    return Path.home() / ".racelab" / "trackloc" / (time.strftime("%Y%m%d-%H%M%S") + "-" + name)


def _make_source(args: argparse.Namespace) -> Any:
    from .sources import CameraSource, FfmpegSource

    if args.camera is None:
        sys.exit("give --camera INDEX (see `python -m trackloc probe`) or --video FILE")
    size = _size(args.size)
    if args.ffmpeg:
        return FfmpegSource(args.camera, size=size or (1280, 720), fps=args.fps or 30.0, mirror=args.mirror,
                            rotate=args.rotate, exposure_offset_s=args.exposure_offset)
    return CameraSource(args.camera, size=size, fps=args.fps, mirror=args.mirror, rotate=args.rotate,
                        exposure_offset_s=args.exposure_offset).open()


def _connect(args: argparse.Namespace) -> Any:
    from .link_racelab import RaceLabLink

    try:
        return RaceLabLink(args.racelab_url, args.name, args.token).connect()
    except (ConnectionError, OSError) as e:
        sys.exit(f"{e}\nStart RaceLab (`racelab`) first, or use --inputs FILE to work offline.")


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


def _prepare(model_path: str, first_frame: Any, register: bool, stored: dict[str, Any] | None = None
             ) -> tuple[Any, Any, dict[str, Any] | None]:
    """Load the map; re-register its background to the current view (or reuse the registration stored by
    a recording: replay then matches the live session exactly); returns (model, background, registration)."""
    import cv2
    import numpy as np

    from .geometry import MapModel, RegistrationError, register as do_register

    model = MapModel.load(model_path)
    if model.background is None:
        sys.exit("the map has no background image next to it (…bg.png): run `map` again")
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
            sys.exit(str(e))
    shift = float(np.hypot(W[0, 2], W[1, 2]))
    print(f"map registered to the current view (correlation {cc:.2f}, shift {shift:.1f} px)" + (" [as stored in the recording]" if stored else ""))
    return (model.warped(W), cv2.warpPerspective(bg, W, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE),
            {"W": W.tolist(), "cc": float(cc)})


def _slots(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split(",") if x.strip() != "")


# ----------------------------------------------------------------------------- commands

def cmd_probe(args: argparse.Namespace) -> int:
    from .sources import PERMISSION_HELP, ffmpeg_devices, probe

    names = ffmpeg_devices()
    if names:
        print("devices (ffmpeg numbering; OpenCV's order is usually the same, `probe` below checks):")
        for i, n in names.items():
            print(f"  [{i}] {n}")
    print("opening the cameras (this turns them on)...")
    found = probe(args.max, args.index)
    ok = False
    for f in found:
        if f["opened"] and f.get("frames_read"):
            ok = True
            print(f"  camera {f['index']}: {f.get('width')}x{f.get('height')} at {f.get('fps')} fps (jitter {f.get('jitter_ms')} ms)"
                  + (f"  [{f['name']}]" if f.get("name") else ""))
        else:
            print(f"  camera {f['index']}: " + ("opened but no frame" if f["opened"] else "does not open"))
    if not ok:
        print("\n" + PERMISSION_HELP)
        return 1
    return 0


def cmd_marker(args: argparse.Namespace) -> int:
    import cv2

    from .geometry import marker_pdf, marker_sheet

    Path(args.out).write_bytes(marker_pdf(args.size_mm, args.landscape))
    png = str(Path(args.out).with_suffix(".png"))
    cv2.imwrite(png, marker_sheet(900))
    print(f"written {args.out} (and {png}, a preview without print scale).\n"
          f"ArUco DICT_4X4_50, id 0; the black square is {args.size_mm:g} mm if printed at 100 % / actual size.\n"
          "Print on matt paper, no 'fit to page', glue it flat on a board, lay it in the camera view on the table\n"
          "(not on a slope). Then MEASURE the side of the black square with a ruler and pass that as\n"
          f"--marker-size METRES (e.g. {args.size_mm / 1000:.3f}). A bigger marker gives a better map (A4: up to ~190 mm).")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from .live import Live
    from .recorder import Recorder

    if args.video:
        sys.exit("`record` records a live camera")
    out = Path(args.out) if args.out else _data_dir("record")
    src = _make_source(args)
    info = src.info()
    rec = Recorder(out, fps=info["fps"], size=(info["width"], info["height"]), codec=args.codec,
                   meta={"camera": args.camera, "args": vars(args), "info": info})
    live = Live(recorder=rec)
    link = None
    if args.racelab_url:
        link = _connect(args)
        link.raw = rec.log_raw
        link.start_inputs(live.on_input)
        print(f"RaceLab connected ({args.racelab_url}): crossings and throttles are recorded")
    else:
        print("no --racelab-url: video only")
    src.start(live.on_frame)
    live.start()
    print(f"recording to {out} ({info['width']}x{info['height']} at {info['fps']:.0f} fps, {args.codec}); Ctrl-C to stop")
    t0 = time.monotonic()
    try:
        while args.duration is None or time.monotonic() - t0 < args.duration:
            time.sleep(2.0)
            off = rec.clock.offset
            print(f"  {time.monotonic() - t0:5.0f} s  frames {live.frames}  dropped {live.stream.dropped + rec.dropped}  "
                  f"crossings {sum(live.crossings.values())}  clock offset {'-' if off is None else format(off, '.3f')}")
    except KeyboardInterrupt:
        pass
    finally:
        src.stop()
        live.stop()
        rec.close()
        if link:
            link.close()
    print(f"saved {out}  ({live.frames} frames, {sum(live.crossings.values())} crossings)")
    return 0


def _build_and_report(args: argparse.Namespace, frames: Any, inputs: Any, empty_range: Any) -> int:
    from .mapper import MapConfig, MapError, build_map

    cfg = MapConfig(marker_size=args.marker_size, throttle=args.throttle, background=args.background,
                    empty_frames=args.empty_frames, lane=args.lane, lane_pitch_m=args.lane_pitch_m,
                    latency_s=args.latency, track_length_m=args.track_length_m, name=args.name, slot=args.slot)
    try:
        model = build_map(frames, inputs, cfg, empty_range=empty_range)
    except MapError as e:
        print(f"cannot build the map: {e}")
        return 2
    model.save(args.out)
    print(format_report(model))
    print(f"\nmap saved: {args.out} (+ .bg.png). Next: python -m trackloc run --camera N --map {args.out} --racelab-url ...")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    from .drive import DiscoveryDriver, DriveConfig, MapAborted
    from .live import Live, MotionMeter
    from .recorder import Recorder, RecordingReader, load_inputs

    if args.recording or args.video:  # offline: rebuild from a recording
        if args.recording:
            reader = RecordingReader(args.recording)
            marks = reader.marks("empty_start"), reader.marks("empty_end")
            erange = (marks[0][0]["t"], marks[1][0]["t"]) if marks[0] and marks[1] else None
            return _build_and_report(args, reader.frames, reader.inputs(), erange)
        from .sources import FileSource
        if not args.inputs:
            sys.exit("--video needs --inputs FILE.jsonl (the crossings)")
        return _build_and_report(args, FileSource(args.video).frames, load_inputs(args.inputs), None)

    if not args.racelab_url:
        sys.exit("`map` drives a car: give --racelab-url (or rebuild offline with --recording DIR)")
    laps = args.laps or (4 if args.lanes == 2 else 2)
    out_dir = Path(args.out).with_suffix("")
    rec_dir = out_dir.parent / (out_dir.name + "_recording")
    src = _make_source(args)
    info = src.info()
    rec = Recorder(rec_dir, fps=info["fps"], size=(info["width"], info["height"]), codec=args.codec,
                   meta={"command": "map", "args": vars(args), "info": info})
    live = Live(recorder=rec, meter=MotionMeter())
    link = _connect(args)
    link.raw = rec.log_raw
    link.start_inputs(live.on_input)
    src.start(live.on_frame)
    live.start()
    result: dict[str, Any] = {}
    interrupted = False
    try:
        if args.background == "empty":
            print("\nREMOVE EVERY CAR from the track and keep your hands out of the view.")
            for k in (3, 2, 1):
                print(f"  capturing the empty track in {k}...", flush=True)
                time.sleep(1.0)
            rec.mark("empty_start")
            n0 = live.frames
            while live.frames < n0 + args.empty_frames + 3:
                time.sleep(0.05)
            rec.mark("empty_end")
            print(f"empty track captured ({args.empty_frames} frames).")
            print(f"NOW PUT ONE CAR (slot {args.slot + 1}) on the track, just behind the finish line.")
            if args.yes or not sys.stdin.isatty():
                time.sleep(6.0)
            else:
                input("Press Enter when the car is in place... ")
        drv = DiscoveryDriver(link, args.slot, DriveConfig(throttle=args.throttle, throttle_cap=args.cap, laps=laps,
                                                           lanes=args.lanes, stall_s=args.stall_s),
                              progress=live.progress, crossings=lambda: live.crossing_count(args.slot))
        print(f"driving slot {args.slot + 1} at throttle {args.throttle}/15 for {laps} laps (Ctrl-C hands the car back)...")
        result = drv.run()
        print(f"discovery lap done: {result['crossings']} crossings, throttle {result['throttle']}"
              + (f", {result['bumps']} stall bump(s)" if result["bumps"] else ""))
        for n in result["notes"]:
            print("  note:", n)
    except MapAborted as e:
        print(f"\nABORTED: {e}")
        for n in drv.log:
            print("  ", n)
        return 3
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted: the car was handed back (throttle 0).")
    finally:
        src.stop()
        live.stop()
        rec.close()
        link.close()
    if interrupted:
        print(f"the recording so far is in {rec_dir}; rebuild with: map --recording {rec_dir} --slot {args.slot}")
        return 130
    reader = RecordingReader(rec_dir)
    marks = reader.marks("empty_start"), reader.marks("empty_end")
    erange = (marks[0][0]["t"], marks[1][0]["t"]) if marks[0] and marks[1] else None
    code = _build_and_report(args, reader.frames, reader.inputs(), erange)
    print(f"recording kept in {rec_dir} (rebuild with other parameters: map --recording {rec_dir} --slot {args.slot})")
    return code


def cmd_run(args: argparse.Namespace) -> int:
    from .debug import Overlay
    from .live import Live
    from .pipeline import Pipeline, PipelineConfig, merge, run_stream
    from .recorder import JsonlSink, Recorder, load_inputs
    from .sources import FileSource

    cfg = PipelineConfig(latency_s=args.latency, expected_slots=_slots(args.slots))
    if args.video or args.inputs:  # offline
        if not (args.video and args.inputs):
            sys.exit("offline `run` needs --video FILE and --inputs FILE.jsonl")
        src = FileSource(args.video)
        first = next(iter(src.frames()))[1]
        model, bg, _ = _prepare(args.map, first, not args.no_register)
        pipe = Pipeline(model, cfg, background=bg)
        sink = JsonlSink(args.sink or "positions.jsonl")
        run_stream(pipe, merge(src.frames(), load_inputs(args.inputs)), sink, args.hz)
        sink.close()
        print(f"{len(pipe.log)} frames processed; positions in {args.sink or 'positions.jsonl'}")
        return 0

    src = _make_source(args)
    info = src.info()
    rec = None
    if args.record:
        rec = Recorder(args.record, fps=info["fps"], size=(info["width"], info["height"]), codec=args.codec,
                       meta={"command": "run", "args": vars(args), "info": info})
    live = Live(recorder=rec, defer=True)
    src.start(live.on_frame)
    live.start()
    for _ in range(200):  # the first frame registers the map to the current view
        if live.first_frame is not None:
            break
        time.sleep(0.05)
    else:
        src.stop()
        sys.exit("no frame from the camera in 10 s")
    model, bg, registration = _prepare(args.map, live.first_frame, not args.no_register)
    if rec is not None:
        rec.meta["registration"] = registration
        rec.meta["map"] = args.map
        rec.meta["latency_s"] = args.latency
    pipe = Pipeline(model, cfg, background=bg)
    live.pipeline = pipe
    if args.debug:
        live.overlay = Overlay(args.debug, info["fps"], (info["width"], info["height"]))
    link = None
    sink: Any
    if args.sink:
        sink = JsonlSink(args.sink)
    else:
        if not args.racelab_url:
            src.stop()
            sys.exit("give --racelab-url to publish to RaceLab (or --sink FILE to write the positions to a file)")
        link = _connect(args)
        sink = link
        if rec:
            link.raw = rec.log_raw
    sink.set_track(model.geometry())
    if link:
        link.start_inputs(live.on_input)
    live.active = True
    print(f"track '{model.name}' published ({model.length_c:.2f} m, {len(model.lanes)} lane(s)); cars appear once a "
          "finish-line crossing names them" + ("" if not cfg.expected_slots else f" (or at once: expected slots {cfg.expected_slots})"))
    live.start_publisher(sink, args.hz)
    try:
        while live.error is None:
            time.sleep(1.0)
            cars = pipe.positions(time.monotonic())
            ms = sum(pipe.times_ms[-30:]) / max(1, len(pipe.times_ms[-30:]))
            print(f"  frames {live.frames} ({ms:.1f} ms/frame)  dropped {live.stream.dropped}  cars " +
                  " ".join(f"#{c['slot'] + 1}:{c['s']:.3f}{'!' if c['off'] else ''}" for c in cars), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        src.stop()
        live.stop()
        if rec:
            rec.close()
        if link:
            try:
                link.clear_track()
            finally:
                link.close()
        elif hasattr(sink, "close"):
            sink.close()
    if live.error is not None:
        print(f"stopped: {live.error}")
        return 1
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .debug import Overlay
    from .pipeline import Pipeline, PipelineConfig, run_stream
    from .recorder import JsonlSink, RecordingReader
    from .tracker import estimate_latency

    reader = RecordingReader(args.recording)
    first = next(iter(reader.frames()))[1]
    model, bg, _ = _prepare(args.map, first, not args.no_register, reader.meta.get("registration"))
    pipe = Pipeline(model, PipelineConfig(latency_s=args.latency, expected_slots=_slots(args.slots)), background=bg)
    h, w = first.shape[:2]
    overlay = Overlay(args.debug, reader.fps, (w, h)) if args.debug else None
    sink = JsonlSink(args.sink) if args.sink else None
    t0 = time.time()
    run_stream(pipe, reader.items(), sink, 30.0, after_frame=(lambda img, p: overlay.write(img, p)) if overlay else None)
    if overlay:
        overlay.close()
    if sink:
        sink.close()
    print(f"{len(pipe.log)} frames in {time.time() - t0:.1f} s ({sum(pipe.times_ms) / max(1, len(pipe.times_ms)):.1f} ms/frame)")
    print(f"identity: {pipe.tracker.stats}")
    cross = [e for e in pipe.tracker.crossings if e.get("matched")]
    if cross:
        print("CU crossings vs the camera (positive residual = the camera sees the car AHEAD of the line at the CU's instant):")
        for e in cross[:12]:
            print(f"  slot {e['slot'] + 1}: residual {e['resid_m'] * 100:+6.1f} cm at {e['v']:.2f} m/s")
        est = estimate_latency(pipe.tracker.crossings)
        if est["median"] is not None:
            print(f"  -> latency still missing from the frame times: {est['median'] * 1000:+.0f} ms (median of residual/speed)"
                  + (f", {est['regression'] * 1000:+.0f} ms by regression (speed spread allows it)" if est["regression"] is not None else "")
                  + f"\n     try --latency {args.latency + est['median']:.3f} (and map again with it: the finish line depends on it)")
    else:
        print("no crossing could be matched to a car: nothing to estimate the latency from")
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    from .bench import write_synthetic

    write_synthetic(Path(args.out), seed=args.seed, elevation=args.elevation, marker_size=args.marker_size)
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import run_bench

    res = run_bench(quick=args.quick)
    print(json.dumps(res, indent=1, default=float) if args.json else res["text"])
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    try:
        return {"probe": cmd_probe, "marker": cmd_marker, "record": cmd_record, "map": cmd_map, "run": cmd_run,
                "replay": cmd_replay, "synth": cmd_synth, "bench": cmd_bench}[args.cmd](args)
    except OSError as e:
        print(e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
