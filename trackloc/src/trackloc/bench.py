"""Synthetic scenarios with ground truth, scoring, the benchmark and the `synth` recordings.

Shared by the tests and by `python -m trackloc bench`: the numbers quoted in the docs come from here.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np

from .geometry import MapModel
from .mapper import MapConfig, build_map
from .pipeline import Pipeline, PipelineConfig, run_stream
from .synth import CameraSpec, Scenario, Session, SynthCar, T0
from .tracker import wrap

LATENCY = 0.06  # the camera latency injected in the synthetic sessions (s)


def discovery(camera: CameraSpec | None = None, marker_size: float = 0.15, seed: int = 3, duration: float = 37.0,
              throttle: int = 4, two_lanes: bool = True, empty_prefix: float = 0.0, **kw: Any) -> Session:
    """One bot at a low constant throttle: lap 1 lane A, a lane change armed after crossing 2 (lap 2 hybrid,
    lap 3 lane B), again after crossing 4 (lap 4 hybrid): what `map` does. With `empty_prefix` the session
    starts with that many seconds of the empty track (the car is put on it afterwards)."""
    p = empty_prefix
    changes = [(0, 10.0 + p), (0, 26.0 + p)] if two_lanes else []
    return Session(Scenario(cars=[SynthCar(0, "bot", 0, 0.93, [(0.5 + p, throttle)], gain=0.7)], duration=duration + p,
                            changes=changes, seed=seed, camera=camera or CameraSpec(), marker_size=marker_size,
                            empty_prefix_s=p, **kw))


def map_from(sess: Session, throttle: int = 4, **kw: Any) -> MapModel:
    cfg = MapConfig(throttle=throttle, slot=0, latency_s=kw.pop("latency_s", LATENCY), marker_size=sess.sc.marker_size, **kw)
    return build_map(lambda: sess.frames(), [m for m in sess.inputs() if m["type"] == "crossing"], cfg)


def race(camera: CameraSpec | None = None, seed: int = 11, duration: float = 45.0, marker_size: float = 0.15,
         hands: tuple[float, ...] = (12.0, 27.0), **kw: Any) -> Session:
    """Two bots one behind the other in lane 0 (same speed) and a human driver in lane 1, a bridge, hands."""
    cars = kw.pop("cars", None) or [
        SynthCar(0, "bot", 0, 0.00, [(0.5, 9)], gain=0.72), SynthCar(2, "bot", 0, 0.50, [(0.5, 9)], gain=0.72),
        SynthCar(1, "human", 1, 0.25)]
    return Session(Scenario(cars=cars, duration=duration, hands=list(hands), seed=seed, camera=camera or CameraSpec(),
                            marker_size=marker_size, **kw))


def map_truth_x(model: MapModel, sess: Session, slot: int, host_t: float) -> float:
    """Where the MAP says the car is (GT pixel -> map lap fraction, metres): the map-consistent truth."""
    st = sess.state[slot]
    i = int((host_t - T0) / sess.DT)
    p, _ = sess.circuit.point(float(st["u"][i]), float(st["lat"][i]))
    pix = sess.cam.project(np.array([[p[0], p[1], 0.016]]))[0]
    _, c, _, _ = model.project(pix)[0]
    return c * model.length_c


def score(pipe: Pipeline, sess: Session, settle: float = 8.0) -> dict[str, Any]:
    """Compare the pipeline's per-frame output with the ground truth."""
    L = pipe.model.length_c
    blind = [(a * L, b * L) for a, b in pipe.model.blind]
    vis, occ, vis_map, wrong_id, off_false = [], [], [], 0, 0
    labelled_since: dict[int, float] = {}
    for rec in pipe.log:
        t = rec["t"]
        if t - T0 < settle:
            continue
        for c in rec["cars"]:
            gt = sess.at(c["slot"], t - T0)
            e = wrap(c["s"] * L - gt["x"], L)
            near_blind = any(a - 0.3 <= gt["x"] <= b + 0.3 for a, b in blind)
            (occ if near_blind else vis).append(e)
            if not near_blind:
                vis_map.append(wrap(c["s"] * L - map_truth_x(pipe.model, sess, c["slot"], t), L))
            if abs(e) > 0.5:
                wrong_id += 1
            if c["off"] and not gt["off"]:
                off_false += 1
    rms = lambda v: float(np.sqrt(np.mean(np.square(v)))) if len(v) else float("nan")  # noqa: E731
    return {"rms_visible_m": rms(vis), "mean_visible_m": float(np.mean(vis)) if vis else float("nan"),
            "rms_visible_vs_map_m": rms(vis_map), "rms_near_blind_m": rms(occ), "max_abs_m": float(np.max(np.abs(vis + occ))),
            "wrong_identity_samples": wrong_id, "false_off_samples": off_false,
            "labelled": sorted({c["slot"] for c in pipe.log[-1]["cars"]}) if pipe.log else [],
            "ms_per_frame": float(np.mean(pipe.times_ms)) if pipe.times_ms else float("nan"),
            "samples": len(vis) + len(occ)}


def run_race(model: MapModel, sess: Session, latency: float = LATENCY, **cfg: Any) -> Pipeline:
    pipe = Pipeline(model, PipelineConfig(latency_s=latency, **cfg))
    run_stream(pipe, sess.stream())
    return pipe


def map_errors(model: MapModel, sess: Session) -> dict[str, float]:
    """Length and position errors of a map made from the discovery session `sess`."""
    cir = sess.circuit
    out = {"centre_length_err_pct": 100 * (model.length_c / cir.length - 1)}
    for ln in model.lanes:
        out[f"lane{ln.lane_id}_length_err_pct"] = 100 * (ln.length / cir.lane_len[ln.lane_id] - 1)
    rows = []
    st = sess.state[0]
    for ti in np.arange(2.0, sess.sc.duration - 1, 0.1):
        i = int(ti / sess.DT)
        x_true = float(st["u"][i]) * cir.length
        rows.append(wrap(map_truth_x(model, sess, 0, T0 + ti) - x_true, model.length_c))
    out["map_s_rms_m"] = float(np.sqrt(np.mean(np.square(rows))))
    out["pitch_err_pct"] = 100 * (model.pitch_m / cir.pitch - 1)
    return out


def write_recording(sess: Session, out: Path, codec: str = "ffv1") -> None:
    """Write a session to a folder exactly as `record` does (frames, inputs with arrival order)."""
    from .recorder import Recorder

    rec = Recorder(out, fps=sess.cam.fps, size=(sess.cam.width, sess.cam.height), codec=codec,
                   meta={"synthetic": True, "latency_injected_s": sess.sc.latency_s})
    seq = 0
    for kind, payload in sess.stream():
        seq += 1
        if kind == "frame":
            t, img = payload
            while not rec.log_frame(seq, t, t + 0.5 / sess.cam.fps, img):
                time.sleep(0.002)
        else:
            rec.log_input(seq, {**payload, "recv": payload["t"]})
    rec.close()


def write_synthetic(out: Path, seed: int = 3, elevation: float = 55.0, marker_size: float = 0.15) -> None:
    cam = CameraSpec(elevation_deg=elevation)
    out.mkdir(parents=True, exist_ok=True)
    print(f"writing a synthetic discovery lap and a race to {out} (lossless FFV1, a minute or so)...")
    write_recording(discovery(cam, marker_size, seed), out / "discovery")
    write_recording(race(cam, seed + 8, marker_size=marker_size), out / "race")
    print(f"""done. Try the offline path exactly as on the track:
  python -m trackloc map --recording {out}/discovery --slot 0 --throttle 4 --marker-size {marker_size} --latency {LATENCY} --out {out}/map.json
  python -m trackloc replay {out}/race --map {out}/map.json --latency {LATENCY} --sink {out}/positions.jsonl
(the synthetic camera latency is {LATENCY * 1000:.0f} ms: run replay without --latency to see it estimated)""")


def run_bench(quick: bool = False) -> dict[str, Any]:
    res: dict[str, Any] = {}
    lines = ["trackloc synthetic benchmark (ground truth known; camera latency injected %.0f ms; marker 0.15 m)" % (LATENCY * 1000)]
    configs = [("55 deg, 800x450", CameraSpec())] if quick else [
        ("55 deg, 800x450", CameraSpec()), ("40 deg, 800x450", CameraSpec(elevation_deg=40)),
        ("70 deg, 800x450", CameraSpec(elevation_deg=70)), ("55 deg, k1=-0.05 barrel, no undistortion", CameraSpec(k1=-0.05))]
    for name, cam in configs:
        disc = discovery(cam)
        model = map_from(disc)
        err = map_errors(model, disc)
        res[f"map {name}"] = err
        lines.append(f"[map {name}] centre length {err['centre_length_err_pct']:+.1f} %, lane lengths "
                     + ", ".join(f"{k}={v:+.1f} %" for k, v in err.items() if k.startswith("lane"))
                     + f", position (s) RMS {err['map_s_rms_m'] * 100:.1f} cm, lane spacing {err['pitch_err_pct']:+.1f} %")
        if name.startswith("55 deg, 800"):
            sess = race(cam)
            pipe = run_race(model, sess)
            sc = score(pipe, sess)
            res["race"] = sc
            lines.append(f"[race: 2 bots + 1 human, bridge, 2 hands] s error RMS {sc['rms_visible_m'] * 100:.1f} cm visible "
                         f"({sc['rms_visible_vs_map_m'] * 100:.1f} cm relative to the map itself), {sc['rms_near_blind_m'] * 100:.1f} cm near the bridge; "
                         f"worst {sc['max_abs_m'] * 100:.0f} cm; wrong-identity samples {sc['wrong_identity_samples']}; "
                         f"false off {sc['false_off_samples']}; cars named {sc['labelled']}; {sc['ms_per_frame']:.1f} ms/frame")
            nolat = run_race(model, sess, latency=0.0)
            est = nolat.latency_estimate()
            res["latency"] = {k: v for k, v in est.items()}
            lines.append(f"[latency] injected {LATENCY * 1000:.0f} ms; without --latency the crossings say "
                         f"{(est['median'] or 0) * 1000:+.0f} ms missing (n={est['n']:.0f}; the CU->host clock offset absorbs the BLE delay: "
                         f"expected about {LATENCY * 1000 - 18:.0f}), error without it {score(nolat, sess)['mean_visible_m'] * 100:+.0f} cm")
    if not quick:
        cam = CameraSpec(width=1280, height=720)
        disc = discovery(cam, duration=17.0, two_lanes=False)
        model = map_from(disc, lane=0)
        sess = race(cam, duration=14.0, cars=[SynthCar(0, "bot", 0, 0.0, [(0.5, 9)], gain=0.72)], hands=())
        pipe = run_race(model, sess)
        res["720p_ms_per_frame"] = float(np.mean(pipe.times_ms))
        lines.append(f"[1280x720] pipeline {np.mean(pipe.times_ms):.1f} ms/frame (detect + project + track; capture and encoding excluded)")
    res["text"] = "\n".join(lines)
    return res
