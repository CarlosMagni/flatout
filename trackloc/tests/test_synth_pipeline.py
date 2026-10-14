"""The vision pipeline on synthetic video with ground truth (oblique camera, bridge, hand, noise,
brightness drift, camera latency, late CU events).

Spec checks: (a) map recovery and metric length, (b) tracker error, (c) identity, (d) lane handedness,
(e) off-track, (f) live == replay. Camera/marker/scene are the same across the sessions of a module.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

from trackloc import bench  # noqa: E402
from trackloc.mapper import MapConfig, MapError, build_map  # noqa: E402
from trackloc.pipeline import Pipeline, PipelineConfig, run_stream  # noqa: E402
from trackloc.synth import CameraSpec, SynthCar, T0  # noqa: E402
from trackloc.tracker import wrap  # noqa: E402


@pytest.fixture(scope="module")
def disc():
    return bench.discovery()


@pytest.fixture(scope="module")
def model(disc):
    return bench.map_from(disc)


@pytest.fixture(scope="module")
def race_sess():
    return bench.race(duration=36.0, hands=(12.0, 26.0))


@pytest.fixture(scope="module")
def race_pipe(model, race_sess):
    return bench.run_race(model, race_sess)


# ----------------------------------------------------------------------------- (a) the map

def test_a_map_recovers_lanes_and_metric_length(disc, model):
    err = bench.map_errors(model, disc)
    assert abs(err["centre_length_err_pct"]) < 3 and abs(err["lane0_length_err_pct"]) < 3 and abs(err["lane1_length_err_pct"]) < 3
    assert abs(err["pitch_err_pct"]) < 15
    assert err["map_s_rms_m"] < 0.05  # positions along the lap, not just the total
    r = model.report
    assert r["marker_found"] and r["metric"] == "marker" and r["lanes_found"] == 2
    assert any(abs(c - 0.32) < 0.05 for c in r["changers"])  # the changer of the synthetic circuit is at 0.30-0.34
    cir = disc.circuit
    bridge = 0.62  # the bridge hides u = 0.62 .. 0.62 + 0.5 m
    assert any(a - 0.02 <= bridge + 0.04 and b + 0.02 >= bridge + 0.04 for a, b in model.blind), model.blind
    assert 0.55 < model.gain_hint < 0.85  # the discovery lap measured the car's real speed vs the motor model


@pytest.mark.parametrize("elevation", [40.0, 70.0])
def test_a_map_length_holds_at_other_camera_elevations(elevation):
    sess = bench.discovery(CameraSpec(elevation_deg=elevation))
    err = bench.map_errors(bench.map_from(sess), sess)
    assert abs(err["centre_length_err_pct"]) < 3 and err["map_s_rms_m"] < 0.06


def test_a_the_motor_model_length_check_is_only_reported_never_trusted(model):
    r = model.report
    # the synthetic car has 70 % of the model's gain: the check is off by ~-30 % and says so
    assert r["model_discrepancy_pct"] < -15 and "UNCALIBRATED" not in r["model_note"] and "guess" in r["model_note"]
    assert abs(r["length_marker_m"] / model.length_c - 1) < 0.05  # the marker length is what is used


def test_a_without_a_marker_the_length_comes_from_the_model_and_a_known_length_fixes_it(disc):
    sess = bench.discovery(marker_size=0.15)
    inputs = [m for m in sess.inputs() if m["type"] == "crossing"]
    # hide the marker by painting over it in every frame
    cir = sess.circuit
    cam = sess.cam
    c = cam.project(np.array([[sess.sc.marker_xy[0], sess.sc.marker_xy[1], 0.0]]))[0].astype(int)

    def frames():
        for t, img in sess.frames():
            img = img.copy()
            img[max(c[1] - 40, 0):c[1] + 40, max(c[0] - 40, 0):c[0] + 40] = 120
            yield t, img

    blind = build_map(frames, inputs, MapConfig(throttle=4, slot=0, latency_s=bench.LATENCY))
    assert blind.report["metric"] == "model" and not blind.report["marker_found"]
    assert blind.H is None and abs(blind.report["model_discrepancy_pct"]) < 10  # time x model speed, by construction
    true_len = cir.lane_len[0]
    known = build_map(frames, inputs, MapConfig(throttle=4, slot=0, latency_s=bench.LATENCY, track_length_m=true_len))
    assert known.report["metric"] == "user"
    assert abs(next(ln.length for ln in known.lanes if ln.lane_id == 0) / true_len - 1) < 1e-6
    assert abs(known.length_c / cir.length - 1) < 0.03


def test_a_background_empty_builds_the_same_map():
    """`--background empty`: ~30 frames of the empty track, then the car is put on it."""
    sess = bench.discovery(empty_prefix=3.0)
    inputs = [m for m in sess.inputs() if m["type"] == "crossing"]
    frames = lambda: sess.frames()  # noqa: E731
    cfg = dict(throttle=4, slot=0, latency_s=bench.LATENCY)
    empty = build_map(frames, inputs, MapConfig(background="empty", **cfg), empty_range=(T0 + 0.3, T0 + 2.6))
    err = bench.map_errors(empty, bench.discovery())
    assert abs(err["centre_length_err_pct"]) < 3 and abs(err["lane0_length_err_pct"]) < 3 and err["map_s_rms_m"] < 0.05
    # the background really is the empty track: no car in it (the discovery car stands at u0 = 0.93 from t = 0)
    sc = bench.discovery(empty_prefix=3.0).sc
    bg = empty.background.astype(np.int16)
    car_px = sess.cam.project(np.array([[*sess.circuit.point(0.93, sess.circuit.pitch / 2)[0], 0.016]]))[0].astype(int)
    patch = bg[car_px[1] - 3:car_px[1] + 4, car_px[0] - 3:car_px[0] + 4].mean(axis=(0, 1))
    table = sess.static[car_px[1] - 3:car_px[1] + 4, car_px[0] - 3:car_px[0] + 4].astype(np.int16).mean(axis=(0, 1))
    assert np.abs(patch - table).max() < 12 and sc.empty_prefix_s == 3.0
    with pytest.raises(MapError, match="no frame"):
        build_map(frames, inputs, MapConfig(background="empty", **cfg), empty_range=(T0 + 900, T0 + 901))


def test_a_not_enough_laps_gives_a_clear_error(disc):
    one = [m for m in disc.inputs() if m["type"] == "crossing"][:1]
    with pytest.raises(MapError, match="at least 2"):
        build_map(lambda: disc.frames(stop=60), one, MapConfig(slot=0))


# ----------------------------------------------------------------------------- (b) accuracy, (c) identity

def test_b_tracker_error_visible_and_after_occlusion(race_pipe, race_sess):
    sc = bench.score(race_pipe, race_sess)
    assert sc["samples"] > 2000
    assert sc["rms_visible_m"] < 0.03, sc  # < 3 cm in the visible zones (map error included)
    assert sc["rms_near_blind_m"] < 0.10, sc  # < 10 cm around the 0.5 m bridge
    assert sc["rms_visible_vs_map_m"] < 0.02  # what the filter adds on top of the map itself
    assert sc["ms_per_frame"] < 15


def test_c_identity_no_swaps_three_cars_bridge_and_hands(race_pipe, race_sess):
    sc = bench.score(race_pipe, race_sess)
    assert sc["labelled"] == [0, 1, 2]
    assert sc["wrong_identity_samples"] == 0 and sc["false_off_samples"] == 0
    assert race_pipe.tracker.stats["anchored"] >= 20


def test_c_identity_two_cars_side_by_side_crossing_the_line_together():
    """Adjacent lanes, same speed, same start: both blobs touch in the image all the time and the two CU
    crossings are a few ms apart. Identity must hold (the CU names them, the lane separates them)."""
    # the outer lane is longer: a higher gain gives the same lap time, so they cross the line together every lap
    cars = [SynthCar(0, "bot", 0, 0.0, [(0.5, 9)], gain=0.72), SynthCar(1, "bot", 1, 0.0, [(0.5, 9)], gain=0.72 * 6.37 / 5.74)]
    disc = bench.discovery()
    model = bench.map_from(disc)
    sess = bench.race(cars=cars, duration=30.0, hands=(15.0,), seed=21)
    pipe = bench.run_race(model, sess)
    sc = bench.score(pipe, sess)
    gaps = [abs(a - b) for (a, s1, _), (b, s2, _) in zip(sess.cross[:-1], sess.cross[1:]) if s1 != s2]
    assert min(gaps) < 0.1  # the scenario really has crossings less than 100 ms apart
    assert sc["wrong_identity_samples"] == 0 and sc["labelled"] == [0, 1] and sc["rms_visible_m"] < 0.04, sc


# ----------------------------------------------------------------------------- (d) handedness

def test_d_lane_handedness_end_to_end(model, race_pipe, race_sess):
    # lane 0 is the LEFT lane in the driving direction: the inner lane of this counter-clockwise circuit
    lens = {ln.lane_id: ln.length for ln in model.lanes}
    assert lens[0] < lens[1]
    cir = race_sess.circuit
    assert lens[0] == pytest.approx(cir.lane_len[0], rel=0.03) and lens[1] == pytest.approx(cir.lane_len[1], rel=0.03)
    # and the cars are published on the right lane
    wrong = total = 0
    for rec in race_pipe.log:
        if rec["t"] - T0 < 10:
            continue
        for c in rec["cars"]:
            total += 1
            wrong += c["lane"] != race_sess.at(c["slot"], rec["t"] - T0)["lane"]
    assert total > 500 and wrong == 0


def test_d_a_mirrored_camera_swaps_the_lanes_and_mirror_fixes_it():
    import cv2

    from trackloc.sources import transform

    sess = bench.discovery()
    inputs = [m for m in sess.inputs() if m["type"] == "crossing"]
    cfg = MapConfig(throttle=4, slot=0, latency_s=bench.LATENCY)
    flipped = build_map(lambda: ((t, cv2.flip(i, 1)) for t, i in sess.frames()), inputs, cfg)
    lens = {ln.lane_id: ln.length for ln in flipped.lanes}
    assert lens[0] > lens[1]  # a mirrored image reads the lanes the wrong way round...
    fixed = build_map(lambda: ((t, transform(cv2.flip(i, 1), mirror=True)) for t, i in sess.frames()), inputs, cfg)
    lens = {ln.lane_id: ln.length for ln in fixed.lanes}
    assert lens[0] < lens[1]  # ...and --mirror puts it right


# ----------------------------------------------------------------------------- (e) off-track

def test_e_a_car_stopping_off_the_corridor_is_flagged_then_cleared(model):
    cars = [SynthCar(0, "bot", 0, 0.0, [(0.5, 9)], gain=0.72), SynthCar(2, "bot", 0, 0.5, [(0.5, 9)], gain=0.72)]
    sess = bench.race(cars=cars, duration=34.0, hands=(), seed=31, off_events=[(2, 16.0, 24.0)])
    pipe = bench.run_race(model, sess)
    L = model.length_c
    first_off = last_off = None
    stuck_x = sess.at(2, 17.0)["x"]
    for rec in pipe.log:
        t = rec["t"] - T0
        for c in rec["cars"]:
            if c["slot"] == 2 and c["off"]:
                first_off = t if first_off is None else first_off
                last_off = t
                assert abs(wrap(c["s"] * L - stuck_x, L)) < 0.15  # held where it left the track
            if c["slot"] == 0:
                assert not c["off"]  # the other car is never flagged
    assert first_off is not None and 16.0 <= first_off <= 17.5  # within ~1 s of leaving the corridor
    assert last_off is not None and 23.8 <= last_off <= 25.5  # cleared when it is back on the track
    final = [c for c in pipe.log[-1]["cars"] if c["slot"] == 2]
    assert final and not final[0]["off"]


# ----------------------------------------------------------------------------- latency, bumped camera

def test_latency_is_estimated_from_the_cu_crossings(model, race_sess):
    pipe = bench.run_race(model, race_sess, latency=0.0)
    est = pipe.latency_estimate()
    # injected 60 ms; the CU->host offset absorbs the smallest Bluetooth delay (a few tens of ms): same sign, same order
    assert est["n"] >= 15 and 0.02 < est["median"] < 0.075
    before = abs(bench.score(pipe, race_sess)["mean_visible_m"])
    fixed = bench.run_race(model, race_sess, latency=est["median"])
    assert abs(bench.score(fixed, race_sess)["mean_visible_m"]) < 0.5 * before and before > 0.08


def test_a_bumped_camera_is_registered_and_the_map_still_works(disc, model):
    from trackloc.geometry import register

    cam2 = CameraSpec(target=(1.0, 0.5), yaw_deg=27.0, distance=2.55)  # pushed a few centimetres, turned 2 degrees
    sess = bench.race(cam2, duration=30.0, hands=(), seed=41)
    first = next(iter(sess.frames()))[1]
    W, cc = register(model.background, first)
    assert cc > 0.85 and np.hypot(W[0, 2], W[1, 2]) > 5  # it really moved
    import cv2

    pipe = Pipeline(model.warped(W), PipelineConfig(latency_s=bench.LATENCY),
                    background=cv2.warpPerspective(model.background, W, (first.shape[1], first.shape[0]),
                                                   borderMode=cv2.BORDER_REPLICATE))
    run_stream(pipe, sess.stream())
    sc = bench.score(pipe, sess, settle=10.0)
    assert sc["labelled"] == [0, 1, 2] and sc["wrong_identity_samples"] == 0 and sc["rms_visible_m"] < 0.05, sc


def test_a_brightness_drift_does_not_create_phantom_cars(model):
    sess = bench.race(duration=24.0, cars=[SynthCar(0, "bot", 0, 0.0, [(0.5, 9)], gain=0.72)], hands=(), drift=0.12)
    pipe = bench.run_race(model, sess)
    assert pipe.tracker.stats["spawned"] <= 2 and bench.score(pipe, sess)["wrong_identity_samples"] == 0
