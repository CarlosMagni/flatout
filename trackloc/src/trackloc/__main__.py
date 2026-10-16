"""python -m trackloc <command> ...   (see trackloc/README.md)

    probe     list the cameras (resolution, measured fps)
    marker    write the ArUco marker to print (DICT_4X4_50, id 0)
    preview   aim the camera: annotated live view in a browser on 127.0.0.1 (or --snapshot FILE), moves nothing
    serve     everything below from ONE local web page (camera picker, preview, record, map, identify, run, stop)
    record    record camera + RaceLab events to a folder (the most important one on the first day)
    map       discovery lap: drive a car slowly, record, build the map (or rebuild it from a recording)
    identify  roll call: pulse each slot's car at a low throttle to see which car is which slot (`run --identify`)
    run       live: camera + map -> car positions published to RaceLab
    replay    run the same pipeline offline on a recording (identical to the live session)
    synth     write synthetic recordings (ground truth) to try map/replay before touching the track
    bench     synthetic benchmark: accuracy, identity, latency, speed
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .actions import Ctx, TracklocError, do_identify, do_map, do_record, do_run, prepare, slots_of

DEFAULT_URL = "ws://localhost:8130/ws"


def _add_camera(p: argparse.ArgumentParser) -> None:
    p.add_argument("--camera", default=None, help="camera: index (see `probe`), part of its name (e.g. iphone), or a "
                   "stream URL (rtsp://..., http://...); omitted: asks which one")
    p.add_argument("--video", help="a video file instead of a camera (offline)")
    p.add_argument("--size", help="capture size, e.g. 1280x720")
    p.add_argument("--fps", type=float, default=None, help="requested frame rate (30 or 60)")
    p.add_argument("--ffmpeg", action="store_true", help="capture through ffmpeg (fallback if OpenCV cannot open the camera)")
    p.add_argument("--mirror", action="store_true", help="flip the image left-right (a mirrored image swaps the lanes)")
    p.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270), help="rotate the image")
    p.add_argument("--exposure-offset", type=float, default=None,
                   help="seconds subtracted from the arrival time of a frame (default: half a frame period)")


def _add_roll_call(p: argparse.ArgumentParser) -> None:
    p.add_argument("--throttle", type=int, default=4, help="roll-call pulse throttle 1..15 (default 4)")
    p.add_argument("--cap", type=int, default=8, help="a roll-call retry never raises the throttle above this")
    p.add_argument("--pulse-s", type=float, default=0.5, help="length of the roll-call pulse in seconds (0.1..2)")


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

    p = sub.add_parser("preview", help="aim the camera: annotated live view in a browser (read-only, 127.0.0.1)")
    _add_camera(p)
    p.add_argument("--map", default=None, help="map file: draws its lane lines and checks the track fills the frame")
    p.add_argument("--port", type=int, default=8140, help="port on 127.0.0.1 (the page is never served on another address)")
    p.add_argument("--snapshot", metavar="FILE", help="write one annotated JPEG per second to FILE instead of serving")
    p.add_argument("--duration", type=float, default=None, help="stop after this many seconds (default: until Ctrl-C)")
    p.add_argument("--lang", choices=("en", "fr"), default=None, help="language of the terminal text and the picture "
                   "(default: from $LANG; the page has an EN/FR switch)")

    p = sub.add_parser("serve", help="the connector's own local web page: preview, record, map, identify, run (127.0.0.1)")
    _add_camera(p)
    p.add_argument("--racelab-url", default="ws://127.0.0.1:8130/ws", help="RaceLab WebSocket (default %(default)s)")
    p.add_argument("--token", default=None, help="RaceLab API token, if set (not the page's own token, which is random per run)")
    p.add_argument("--name", default="camera", help="agent name shown as the track source in RaceLab")
    p.add_argument("--port", type=int, default=8140, help="port on 127.0.0.1 (the page is never served on another address)")
    p.add_argument("--map", default=None, help="an existing map file to start with (otherwise: the map of the last `Map` button)")
    p.add_argument("--out-dir", default=None, help="where maps and recordings go (default ~/.racelab/trackloc/serve)")
    p.add_argument("--lang", choices=("en", "fr"), default=None, help="default language of the page and terminal (the page has an EN/FR switch)")

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

    p = sub.add_parser("identify", help="roll call: which car is which slot (moves each car a little)")
    _add_camera(p)
    _add_link(p)
    p.add_argument("--map", required=True, help="map file from `map`")
    p.add_argument("--slots", required=True, help="slots of the cars on the track, e.g. 0,1,2")
    _add_roll_call(p)

    p = sub.add_parser("run", help="live localisation, published to RaceLab")
    _add_camera(p)
    _add_link(p)
    p.add_argument("--map", required=True, help="map file from `map`")
    p.add_argument("--identify", action="store_true",
                   help="roll call before tracking: pulse each car of --slots to name it (needs RaceLab, no session running)")
    _add_roll_call(p)
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

def _make_source(args: argparse.Namespace) -> Any:
    from .actions import Ctx, open_source

    return open_source(args, Ctx())


def _slots(text: str) -> tuple[int, ...]:
    return slots_of(text)


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
    do_record(args, Ctx())
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    from .drive import MapAborted
    from .mapper import MapError

    try:
        do_map(args, Ctx())
    except MapAborted:
        return 3  # said by do_map
    except MapError:
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import run_serve

    return run_serve(args)


def cmd_preview(args: argparse.Namespace) -> int:
    import asyncio

    from .geometry import MapModel
    from .preview import Preview, checklist_text, feed_video, run_snapshots, say, serve

    lang = args.lang or ("fr" if os.environ.get("LANG", "").lower().startswith("fr") else "en")
    model = None
    if args.map:
        try:
            model = MapModel.load(args.map)
        except (OSError, ValueError, KeyError) as e:
            sys.exit(f"cannot read the map {args.map}: {e}")
    pv = Preview(model, lang)
    stop = threading.Event()
    src = None
    if args.video:  # a video file in place of a camera (a demo, or a test of the page)
        threading.Thread(target=feed_video, args=(pv, args.video, stop), daemon=True, name="trackloc-video").start()
    else:
        src = _make_source(args)
        src.start(lambda t, t_raw, image: pv.feed(image, t_raw))
    threading.Thread(target=pv.run, args=(stop,), daemon=True, name="trackloc-preview").start()
    timer = None
    if args.duration:
        timer = threading.Timer(args.duration, stop.set)
        timer.daemon = True  # Ctrl-C must not wait for it
        timer.start()
    print(checklist_text(lang), flush=True)
    try:
        if args.snapshot:
            print("\n" + say(lang, "snap_writing", path=args.snapshot), flush=True)
            run_snapshots(pv, args.snapshot, stop)
        else:
            def ready(host: str, port: int) -> None:
                print("\n" + say(lang, "open_url", host=host, port=port), flush=True)

            try:
                asyncio.run(serve(pv, args.port, stop, ready))
            except OSError as e:
                sys.exit(f"cannot listen on 127.0.0.1:{args.port}: {e} (another preview running? try --port)")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if timer is not None:
            timer.cancel()
        if src is not None:
            src.stop()
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    from .identify import RollCallAborted

    try:
        do_identify(args, Ctx())
    except RollCallAborted:
        return 3  # said by do_identify
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .identify import RollCallAborted

    try:
        res = do_run(args, Ctx())
    except RollCallAborted:
        return 3
    except KeyboardInterrupt:
        return 130
    return 1 if res.get("error") else 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .debug import Overlay
    from .pipeline import Pipeline, PipelineConfig, run_stream
    from .recorder import JsonlSink, RecordingReader
    from .tracker import estimate_latency

    reader = RecordingReader(args.recording)
    first = next(iter(reader.frames()))[1]
    model, bg, _ = prepare(args.map, first, not args.no_register, reader.meta.get("registration"))
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
        return {"probe": cmd_probe, "marker": cmd_marker, "preview": cmd_preview, "serve": cmd_serve, "record": cmd_record, "map": cmd_map,
                "identify": cmd_identify, "run": cmd_run, "replay": cmd_replay, "synth": cmd_synth,
                "bench": cmd_bench}[args.cmd](args)
    except TracklocError as e:
        sys.exit(str(e))
    except OSError as e:
        print(e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
