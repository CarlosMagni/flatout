"""Vision tracker: Kalman filters, association, CU-anchored identity, off-track (pure stdlib).

A 1-D world (a loop of `LENGTH` m) with cars of known motion, detections with noise and
dropouts, crossings delivered late: nothing here needs OpenCV or numpy.
"""

from __future__ import annotations

import math
import random

import pytest

from racelab.sim.physics import TAU_ACC, TAU_BRAKE, VMAX
from trackloc.tracker import (
    Detection,
    SpeedPrior,
    Tracker,
    TrackerConfig,
    TrackerMap,
    assign,
    estimate_latency,
    wrap,
)

LENGTH = 6.0
DT = 1 / 30


class World:
    """Cars on a loop. Bots: first-order motor with a TRUE gain different from the model's."""

    def __init__(self, seed=1, true_gain=0.7, tau_factor=1.15):
        self.rng = random.Random(seed)
        self.t = 0.0
        self.cars = {}
        self.true_gain = true_gain
        self.tau_factor = tau_factor

    def add(self, slot, x, lane=0, thr=8, human=False, gain=None):
        self.cars[slot] = {"x": x, "v": 0.0, "lane": lane, "thr": thr, "human": human,
                           "gain": gain or self.true_gain, "off": False, "crossed": []}

    def step(self, dt=DT):
        events = []
        for slot, c in self.cars.items():
            if c["off"]:
                continue
            target = c["gain"] * VMAX * c["thr"] / 15 * 1.0
            tau = (TAU_ACC if target >= c["v"] else TAU_BRAKE) * self.tau_factor
            c["v"] += (target - c["v"]) * (1 - math.exp(-dt / tau))
            old = c["x"]
            c["x"] += c["v"] * dt
            if c["x"] >= LENGTH:
                c["x"] -= LENGTH
                frac = c["x"] / c["v"] if c["v"] else 0.0
                events.append((slot, self.t + dt - frac))
        self.t += dt
        return events

    def detections(self, blind=(), drop=0.03, noise=0.012, lane_of=None):
        out = []
        for slot, c in self.cars.items():
            if self.rng.random() < drop or any(a <= c["x"] <= b for a, b in blind):
                continue
            out.append(Detection(self.t, c["lane"], (c["x"] + self.rng.gauss(0, noise)) % LENGTH, area=100))
        return out


def run_world(tracker, world, seconds, *, blind=(), event_delay=0.05, applied=True, hooks=None,
              drop=0.03):
    """Drive the tracker with the world; returns the (t, {slot: error}) series."""
    errors = []
    pending = []
    n = round(seconds / DT)
    for k in range(n):
        for slot, t_cross in world.step():
            pending.append((t_cross + event_delay, slot, t_cross))
        if applied:
            for slot, c in world.cars.items():
                if not c["human"]:
                    tracker.set_applied(slot, c["thr"])
        for item in [p for p in pending if p[0] <= world.t]:
            pending.remove(item)
            tracker.add_anchor(item[1], 1, item[2])
        tracker.update(world.t, world.detections(blind=blind, drop=drop))
        if hooks:
            hooks(k, world, tracker)
        res = {}
        for tr in tracker.tracks:
            if tr.slot is not None and tr.slot in world.cars:
                res[tr.slot] = wrap(tr.x - world.cars[tr.slot]["x"], LENGTH)
        errors.append((world.t, res))
    return errors


def make_tracker(**kw):
    tmap = kw.pop("tmap", None) or TrackerMap(length=LENGTH)
    return Tracker(tmap, **kw)


def test_wrap_and_assign():
    assert wrap(5.9, 6.0) == pytest.approx(-0.1)
    assert wrap(-3.2, 6.0) == pytest.approx(2.8)
    inf = math.inf
    # globally optimal, not greedy: row 0 prefers col 0 but row 1 needs it
    assert assign([[1.0, 2.0], [1.5, inf]], unassigned=10) == [1, 0]
    assert assign([[inf, inf]], unassigned=3) == [None]
    assert assign([], 1) == []
    big = [[float(abs(i - j)) for j in range(20)] for i in range(5)]
    assert assign(big, 50) == [0, 1, 2, 3, 4]  # more than 12 columns


def test_bot_tracking_error_and_gain_learning():
    """A bot whose true gain is 0.7 of the model: the filter learns it and the error stays small."""
    w = World(true_gain=0.7)
    w.add(0, 0.5, thr=8)
    tr = make_tracker(expected_slots=(0,))
    err = run_world(tr, w, 12.0)
    late = [abs(e[1][0]) for e in err if e[0] > 4 and 0 in e[1]]
    assert late and math.sqrt(sum(e * e for e in late) / len(late)) < 0.02
    car = next(t for t in tr.tracks if t.slot == 0)
    assert car.g == pytest.approx(0.7, abs=0.08)  # learned online, not assumed


def test_bot_prediction_beats_constant_velocity_through_occlusion():
    """Throttle drops while the car is hidden: the motor model follows, constant velocity cannot."""
    def scenario(applied):
        w = World(true_gain=0.8, seed=3)
        w.add(0, 0.0, thr=12)
        tr = Tracker(TrackerMap(length=LENGTH), TrackerConfig(use_speed_prior=applied), expected_slots=(0,))
        last_hidden_error = None
        for _ in range(round(8 / DT)):
            w.step()
            if abs(w.t - 5.0) < DT / 2:
                w.cars[0]["thr"] = 7
            if applied:
                tr.set_applied(0, w.cars[0]["thr"])
            hidden = 5.0 <= w.t < 5.5
            tr.update(w.t, [] if hidden else w.detections(drop=0))
            car = next((t for t in tr.tracks if t.slot == 0), None)
            if hidden and car:
                last_hidden_error = abs(wrap(car.x - w.cars[0]["x"], LENGTH))
        return last_hidden_error

    with_model, without = scenario(True), scenario(False)
    assert with_model is not None and without is not None
    assert with_model < 0.10  # spec: < 10 cm after a 0.5 s occlusion
    assert with_model < without / 2


def test_human_speed_prior_learns_the_lap():
    prior = SpeedPrior(LENGTH, 30)
    assert prior.at(1.0) is None
    for _ in range(3):
        prior.learn(1.0, 2.0)
    assert prior.at(1.0) == pytest.approx(2.0, abs=0.01) and prior.at(4.0) is None


def test_identity_from_crossings_three_cars_no_swap():
    """Three cars (two bots, one human) in two lanes: labels come from the CU crossings only."""
    w = World(seed=5)
    w.add(0, 0.2, lane=0, thr=11)  # two cars one behind the other in lane 0 (same speed)...
    w.add(1, 0.4, lane=1, thr=9)  # ...and a slower one in lane 1 that they pass side by side
    w.add(2, 3.2, lane=0, thr=11, human=True)
    tr = make_tracker()
    swaps = []
    state = {"labelled_at": None}

    def hook(k, world, tracker):
        for t in tracker.tracks:
            if t.slot is not None and abs(wrap(t.x - world.cars[t.slot]["x"], LENGTH)) > 0.4:
                swaps.append((world.t, t.slot))
        if state["labelled_at"] is None and len([t for t in tracker.tracks if t.slot is not None]) == 3:
            state["labelled_at"] = world.t

    run_world(tr, w, 40.0, hooks=hook)
    assert state["labelled_at"] is not None and state["labelled_at"] < 8.0
    assert not swaps
    assert tr.stats["anchored"] >= 15


def test_swapped_labels_are_repaired_at_the_next_crossing():
    w = World(seed=7)
    w.add(0, 0.2, lane=0, thr=9)
    w.add(1, 3.2, lane=0, thr=9)
    tr = make_tracker()
    run_world(tr, w, 10.0)
    t0, t1 = (next(t for t in tr.tracks if t.slot == s) for s in (0, 1))
    t0.slot, t1.slot = 1, 0  # force a swap, as a bad association could
    seen = []

    def hook(k, world, tracker):
        wrong = [t for t in tracker.tracks if t.slot is not None
                 and abs(wrap(t.x - world.cars[t.slot]["x"], LENGTH)) > 0.5]
        seen.append(bool(wrong))

    run_world_continue(tr, w, 8.0, hook)
    assert seen[0] and not any(seen[-30:])  # wrong at first, right again after a lap
    assert tr.stats["relabels"] >= 1


def run_world_continue(tracker, world, seconds, hook):
    n = round(seconds / DT)
    pending = []
    for k in range(n):
        for slot, t_cross in world.step():
            pending.append((t_cross + 0.05, slot, t_cross))
        for slot, c in world.cars.items():
            tracker.set_applied(slot, c["thr"])
        for item in [p for p in pending if p[0] <= world.t]:
            pending.remove(item)
            tracker.add_anchor(item[1], 1, item[2])
        tracker.update(world.t, world.detections())
        hook(k, world, tracker)


def test_late_crossing_is_evaluated_at_crossing_time():
    """A crossing that arrives 150 ms late must still pick the right car (positions at its time)."""
    w = World(seed=9)
    w.add(0, 0.2, thr=12)
    w.add(1, 3.1, thr=12)
    tr = make_tracker()
    run_world(tr, w, 12.0, event_delay=0.15)
    assert {t.slot for t in tr.tracks if t.slot is not None} == {0, 1}
    assert tr.stats["ambiguous"] == 0


def test_blind_zone_and_widened_gate_on_exit():
    tmap = TrackerMap(length=LENGTH, blind=((2.0, 2.5),))
    w = World(seed=11, true_gain=0.9)
    w.add(0, 0.5, thr=10)
    tr = make_tracker(tmap=tmap, expected_slots=(0,))
    err = run_world(tr, w, 15.0, blind=((2.0, 2.5),), drop=0.0)
    assert not any(t.off for t in tr.tracks)
    worst = max(abs(e[1][0]) for e in err if e[0] > 5 and 0 in e[1])
    assert worst < 0.10
    assert tr.stats["off_events"] == 0


def test_off_track_when_a_car_stops_off_corridor():
    w = World(seed=13)
    w.add(0, 0.5, thr=9)
    tr = make_tracker(expected_slots=(0,))
    stopped = {}

    def hook(k, world, tracker):
        c = world.cars[0]
        if world.t > 6.0 and not c["off"]:
            c["off"], stopped["x"], stopped["t"] = True, c["x"], world.t

    # the world stops reporting an on-corridor detection and reports an off-corridor one instead
    n = round(12 / DT)
    flagged = None
    for k in range(n):
        w.step()
        tr.set_applied(0, w.cars[0]["thr"])
        c = w.cars[0]
        if c["off"]:
            dets = [Detection(w.t, 0, stopped["x"], area=100, off=True)]
        else:
            dets = w.detections(drop=0)
        tr.update(w.t, dets)
        hook(k, w, tr)
        car = next((t for t in tr.tracks if t.slot == 0), None)
        if car and car.off and flagged is None:
            flagged = w.t
    assert flagged is not None and flagged - stopped["t"] < 1.0
    pos = tr.positions(w.t)[0]
    assert pos["off"] is True and abs(pos["s"] * LENGTH - stopped["x"]) < 0.05


def test_no_false_off_while_a_hand_covers_the_car_or_it_is_missed_briefly():
    w = World(seed=17)
    w.add(0, 0.5, thr=9)
    tr = make_tracker(expected_slots=(0,))
    n = round(14 / DT)
    for k in range(n):
        w.step()
        tr.set_applied(0, w.cars[0]["thr"])
        c = w.cars[0]
        dets = w.detections(drop=0.05)
        if 6.0 <= w.t < 8.0:  # a hand lies over the car for 2 s: one huge blob, no car blob
            dets = [Detection(w.t, 0, c["x"], area=900, occluder=True, extent=0.15)]
        tr.update(w.t, dets)
        assert not any(t.off for t in tr.tracks), w.t
    assert tr.positions(w.t)[0]["off"] is False


def test_lost_car_in_plain_view_is_off_after_a_while():
    w = World(seed=19)
    w.add(0, 0.5, thr=9)
    tr = make_tracker(expected_slots=(0,))
    flagged = None
    for k in range(round(14 / DT)):
        w.step()
        tr.set_applied(0, w.cars[0]["thr"])
        dets = w.detections(drop=0) if w.t < 6.0 else []  # picked off the table: no blob anywhere
        tr.update(w.t, dets)
        car = next((t for t in tr.tracks if t.slot == 0), None)
        if car and car.off and flagged is None:
            flagged = w.t
    assert flagged is not None and 6.9 < flagged < 8.0


def test_only_one_car_expected_is_labelled_without_crossing():
    w = World(seed=23)
    w.add(3, 0.5, thr=9)
    tr = make_tracker(expected_slots=(3,))
    run_world(tr, w, 1.0)
    assert [c["slot"] for c in tr.positions(w.t)] == [3]
    anonymous = make_tracker()  # nothing known: not published before a crossing says who it is
    w2 = World(seed=23)
    w2.add(3, 0.5, thr=9)
    run_world(anonymous, w2, 1.0, event_delay=5.0)
    assert anonymous.positions(w2.t) == []


def test_published_position_is_extrapolated_to_now():
    w = World(seed=29, true_gain=1.0)
    w.add(0, 0.5, thr=15)
    tr = make_tracker(expected_slots=(0,))
    run_world(tr, w, 5.0, drop=0.0)
    car = w.cars[0]
    ahead = tr.positions(w.t + 0.1)[0]["s"] * LENGTH
    now = tr.positions(w.t)[0]["s"] * LENGTH
    assert wrap(ahead - now, LENGTH) == pytest.approx(car["v"] * 0.1, abs=0.04)


def test_latency_estimate_from_crossing_residuals():
    # residual = C - v * dL with dL = 0.12 s and a finish point error C of 6 cm
    entries = [{"matched": True, "labelled": True, "v": v, "resid_m": 0.06 - v * 0.12}
               for v in (1.2, 2.0, 2.8, 3.4, 3.9, 2.4)]
    est = estimate_latency(entries)
    assert est["regression"] == pytest.approx(0.12, abs=1e-6)
    assert 0.05 < est["median"] < 0.12  # slope-only is biased by C
    assert estimate_latency([])["median"] is None
