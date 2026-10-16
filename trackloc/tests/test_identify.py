"""The roll call (`identify`): which car is which slot, with a fake CU and a synthetic camera on ONE fake clock.

The world is the synthetic scene of `trackloc.synth` with parked cars that really obey the throttle they are
given (first-order motor, RaceLab's dead-man switch included), so the roll call is tested closed-loop: no
hardware, no real time. Safety first: every way a pulse can end must leave the slot at throttle 0 and unclaimed.
"""

from __future__ import annotations

import copy
import time

import numpy as np
import pytest

pytest.importorskip("cv2")

from racelab.sim.physics import TAU_ACC, TAU_BRAKE, VMAX  # noqa: E402
from trackloc import bench  # noqa: E402
from trackloc.appearance import distance  # noqa: E402
from trackloc.drive import REFRESH_S  # noqa: E402
from trackloc.identify import RollCall, RollCallAborted, RollConfig, apply_results, format_results  # noqa: E402
from trackloc.pipeline import Pipeline, PipelineConfig  # noqa: E402
from trackloc.synth import CameraSpec, Scenario, Session, SynthCar, T0  # noqa: E402
from trackloc.tracker import wrap  # noqa: E402

CAM = CameraSpec(width=640, height=360)
PARKED = {0: (0, 0.10), 1: (1, 0.22), 2: (0, 0.45)}  # slot -> (lane, u0): away from the bridge and the finish line
DEADMAN_S = 0.6


@pytest.fixture(scope="module")
def model():
    return bench.map_from(bench.discovery(CAM, noise=1.0))


@pytest.fixture(scope="module")
def template():
    return Session(Scenario(cars=[SynthCar(s, "bot", ln, u0, []) for s, (ln, u0) in PARKED.items()], duration=24.0,
                            camera=CAM, noise=1.0))


class Clock:
    def __init__(self):
        self.t = T0
        self.hooks = []

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt
        for h in self.hooks:
            h(self.t)


class World:
    """Fake CU + camera. Cars obey the throttle sent to a CLAIMED slot (cut to 0 after 0.6 s of silence)."""

    def __init__(self, template, clock, cars=PARKED, *, drags=None):
        self.clock, self.cars = clock, dict(cars)
        self.sess = copy.copy(template)
        self.sess.state = {s: {k: v.copy() for k, v in template.state[s].items()} for s in self.cars}
        self.cir = self.sess.circuit
        self.s = {s: self.cir.s_of_u(ln, u0) for s, (ln, u0) in self.cars.items()}
        self.v = dict.fromkeys(self.cars, 0.0)
        self.claimed: set[int] = set()
        self.thr: dict[int, int] = {}
        self.last_act: dict[int, float] = {}
        self.drags = drags or {}  # slot -> other slots that move whenever it is driven (a bumped neighbour...)
        self.i = 0
        self.max_thr = 0
        clock.hooks.append(self.advance)

    def driven(self, slot):
        out = {slot}
        for other in self.drags.get(slot, ()):
            out.add(other)
        return out

    def advance(self, now):
        i_end = int((now - T0) / self.sess.DT)
        while self.i < i_end:
            self.i += 1
            ti = T0 + self.i * self.sess.DT
            for slot in self.cars:
                drivers = [d for d in self.claimed if slot in self.driven(d) and ti - self.last_act.get(d, -9) <= DEADMAN_S]
                thr = max((self.thr.get(d, 0) for d in drivers), default=0)
                target = 0.7 * VMAX * thr / 15
                tau = TAU_ACC if target >= self.v[slot] else TAU_BRAKE
                self.v[slot] += (target - self.v[slot]) * min(1.0, self.sess.DT / (tau * 1.15))
                self.s[slot] += self.v[slot] * self.sess.DT
                ln = self.cars[slot][0]
                self.sess.state[slot]["u"][self.i] = self.cir.u_of_s(ln, self.s[slot])

    # ---- the camera (feed)
    def latest(self):
        self.advance(self.clock.t)
        k = int((self.clock.t - T0 - 0.5 / CAM.fps - self.sess.sc.latency_s) * CAM.fps)
        if k < 0:
            return None
        return self.sess.frame_arrival(k) - 0.0, self.sess.render(k)[1]

    def truth(self, slot, model):
        return bench.map_truth_x(model, self.sess, slot, self.clock.t)


class FakeLink:
    """The part of the RaceLab link the roll call uses; records everything with the fake time."""

    def __init__(self, world, **status):
        self.w, self.calls = world, []
        self.state = {"session": "idle", "estop": False, "link": "connected", **status}
        self.fail_act_from = None  # act number from which every act raises (link lost / E-STOP)
        self.acts = 0
        self.on_act = None
        self.fail_claim = {}

    def status(self):
        return dict(self.state)

    def claim(self, slot, free=False):
        self.calls.append((self.w.clock.t, "claim", slot, free))
        if slot in self.fail_claim:
            raise RuntimeError(self.fail_claim[slot])
        self.w.claimed.add(slot)
        self.w.thr[slot] = 0

    def act(self, slot, throttle=None, lane=False):
        self.acts += 1
        if self.fail_act_from is not None and self.acts >= self.fail_act_from:
            self.calls.append((self.w.clock.t, "act-refused", slot, throttle))
            raise ConnectionError("agent not claimed (E-STOP)")
        self.calls.append((self.w.clock.t, "act", slot, throttle))
        self.w.max_thr = max(self.w.max_thr, throttle or 0)
        if slot in self.w.claimed:
            self.w.thr[slot] = throttle
            self.w.last_act[slot] = self.w.clock.t
        if self.on_act:
            self.on_act(self, slot, throttle)

    def unclaim(self, slot):
        self.calls.append((self.w.clock.t, "unclaim", slot))
        self.w.claimed.discard(slot)
        self.w.thr[slot] = 0

    def names(self):
        return [c[1] for c in self.calls]


def make(template, model, slots=(0, 1, 2), cars=PARKED, cfg=None, drags=None, **status):
    clock = Clock()
    world = World(template, clock, cars, drags=drags)
    link = FakeLink(world, **status)
    pipe = Pipeline(model, PipelineConfig(latency_s=bench.LATENCY))
    rc = RollCall(link, pipe, world, slots, cfg or RollConfig(), clock=clock.now, sleep=clock.sleep)
    return rc, link, world, clock, pipe


def handed_back(link, world, slot):
    """The slot ends at throttle 0 and unclaimed, and the zero was sent before the unclaim."""
    mine = [c for c in link.calls if c[2] == slot and c[1] in ("act", "act-refused", "unclaim")]
    return slot not in world.claimed and world.thr.get(slot, 0) == 0 and mine and mine[-1][1] == "unclaim" \
        and any(c[1] in ("act", "act-refused") and c[3] == 0 for c in mine)


# ----------------------------------------------------------------------------- the roll call itself

def test_three_parked_cars_are_named_correctly(template, model):
    rc, link, world, clock, pipe = make(template, model)
    res = rc.run()
    L = model.length_c
    assert [r.identified for r in res] == [True, True, True]
    for r in res:
        assert r.lane == PARKED[r.slot][0]
        assert abs(wrap(r.x - world.truth(r.slot, model), L)) < 0.10  # where the car stands after its pulse
        assert 0.08 < r.moved_m < 1.0 and r.attempts == 1 and r.signature is not None
        assert handed_back(link, world, r.slot)
    assert not world.claimed and world.max_thr <= 4  # default pulse: 4/15, never more
    assert all(c[3] is True for c in link.calls if c[1] == "claim")  # outside a session: free=True
    sigs = [r.signature for r in res]
    assert min(distance(sigs[i], sigs[j]) for i in range(3) for j in range(i + 1, 3)) > 0.3  # red, blue, yellow
    assert "slot 1: identified" in format_results(res)


@pytest.mark.parametrize("latency", [0.15, 0.25])
def test_a_slow_camera_does_not_make_the_next_slot_read_the_last_cars_coasting_as_a_mover(model, latency):
    """Continuity Camera's latency is 100 ms or more: the car pulsed before must have stopped when the next one is looked at."""
    slow = Session(Scenario(cars=[SynthCar(s, "bot", ln, u0, []) for s, (ln, u0) in PARKED.items()], duration=24.0,
                            camera=CAM, noise=1.0, latency_s=latency))
    rc, link, world, clock, pipe = make(slow, model)
    res = rc.run()
    assert [(r.identified, r.attempts) for r in res] == [(True, 1)] * 3


def test_the_throttle_is_refreshed_faster_than_the_deadman_switch(template, model):
    rc, link, world, clock, pipe = make(template, model, slots=(0,))
    assert rc.run()[0].identified  # a missed refresh would have stopped the car (the world cuts it after 0.6 s)
    acts = [c[0] for c in link.calls if c[1] == "act" and c[3] == 4]
    gaps = [b - a for a, b in zip(acts, acts[1:])]
    assert len(acts) >= 2 and max(gaps) <= REFRESH_S + 1e-6 < DEADMAN_S


def test_names_and_colours_reach_the_tracker_and_name_the_parked_cars_at_once(template, model):
    rc, link, world, clock, pipe = make(template, model)
    res = rc.run()
    live = Pipeline(model, PipelineConfig(latency_s=bench.LATENCY))  # a fresh session, cars parked where they ended
    assert apply_results(live.tracker, res) == [0, 1, 2]
    for k in range(14):
        clock.sleep(1 / 30)
        t, img = world.latest()
        live.on_frame(t - 0.5 / CAM.fps, img)
    named = {tr.slot: tr for tr in live.tracker.tracks if tr.slot is not None}
    assert set(named) == {0, 1, 2} and live.tracker.stats["named"] == 3  # no finish-line crossing was needed
    L = model.length_c
    for slot, tr in named.items():
        assert abs(wrap(tr.x - world.truth(slot, model), L)) < 0.12
        assert tr.sig is not None and distance(tr.sig, res[slot].signature) < 0.3  # its own colours, measured live
        assert all(distance(tr.sig, o.signature) > distance(tr.sig, res[slot].signature) for o in res if o.slot != slot)
    assert {c["slot"] for c in live.positions(clock.t)} == {0, 1, 2}  # published


def test_a_slot_with_no_car_is_unidentified_and_the_others_are_fine(template, model):
    cars = {s: PARKED[s] for s in (0, 2)}
    rc, link, world, clock, pipe = make(template, model, cars=cars)
    res = {r.slot: r for r in rc.run()}
    assert res[0].identified and res[2].identified and not res[1].identified
    assert "no car moved" in res[1].note and res[1].attempts == 2  # retried once
    thr1 = [c[3] for c in link.calls if c[1] == "act" and c[2] == 1 and c[3]]
    assert thr1 and set(thr1) == {4, 5}  # the retry went one step up, no more
    assert handed_back(link, world, 1) and not world.claimed
    assert "NOT identified" in format_results(list(res.values()))


def test_the_retry_never_goes_above_the_cap(template, model):
    cars = {s: PARKED[s] for s in (0, 2)}
    rc, link, world, clock, pipe = make(template, model, slots=(1,), cars=cars, cfg=RollConfig(throttle=4, cap=4, retries=2))
    assert not rc.run()[0].identified
    assert world.max_thr == 4
    with pytest.raises(ValueError):
        RollConfig(throttle=9, cap=8)
    with pytest.raises(ValueError):
        RollConfig(pulse_s=30.0)  # a long pulse would drive the car round the track


def test_two_cars_moving_in_one_pulse_are_not_guessed(template, model):
    rc, link, world, clock, pipe = make(template, model, slots=(0,), drags={0: (2,)})  # a neighbour is dragged along
    r = rc.run()[0]
    assert not r.identified and "more than one" in r.note and r.attempts == 2
    assert handed_back(link, world, 0)


def test_a_slot_that_cannot_be_claimed_is_skipped_and_nothing_is_driven(template, model):
    rc, link, world, clock, pipe = make(template, model, slots=(1, 0))
    link.fail_claim[1] = "slot 1 is claimed by another agent"
    res = rc.run()
    assert not res[0].identified and "cannot claim" in res[0].note and res[1].identified
    assert not any(c[2] == 1 and c[1] == "act" and c[3] for c in link.calls)  # slot 1 never got a throttle


# ----------------------------------------------------------------------------- safety: failure paths

def test_refused_while_a_session_is_running(template, model):
    for state in ("running", "finishing", "paused", "grid"):
        rc, link, world, clock, pipe = make(template, model, session=state)
        with pytest.raises(RollCallAborted, match="session"):
            rc.run()
        assert link.calls == []  # nothing claimed, nothing sent


def test_refused_while_estop_is_on_or_the_link_is_down(template, model):
    rc, link, world, clock, pipe = make(template, model, estop=True)
    with pytest.raises(RollCallAborted, match="E-STOP"):
        rc.run()
    rc, link, world, clock, pipe = make(template, model, link="disconnected")
    with pytest.raises(RollCallAborted, match="link"):
        rc.run()
    assert link.calls == []


def test_a_session_starting_mid_pulse_stops_everything_and_hands_the_car_back(template, model):
    rc, link, world, clock, pipe = make(template, model)

    def start_session(lk, slot, thr):
        if lk.acts == 1:
            lk.state["session"] = "running"

    link.on_act = start_session
    with pytest.raises(RollCallAborted, match="session"):
        rc.run()
    assert handed_back(link, world, 0) and link.names().count("claim") == 1  # slot 1, 2 never touched


def test_estop_mid_pulse_aborts_and_never_claims_again(template, model):
    rc, link, world, clock, pipe = make(template, model)
    link.fail_act_from = 3  # E-STOP: every act is refused from now on (the zero and the unclaim too)
    with pytest.raises(RollCallAborted, match="taken from us"):
        rc.run()
    assert link.names().count("claim") == 1  # never a second claim
    assert ("act-refused", 0, 0) in [(c[1], c[2], c[3] if len(c) > 3 else None) for c in link.calls]  # the zero was still tried
    assert link.names()[-1] == "unclaim"  # and so was the unclaim
    assert handed_back(link, world, 0)


def test_an_exception_mid_pulse_still_zeroes_and_unclaims(template, model):
    rc, link, world, clock, pipe = make(template, model)
    calls = {"n": 0}
    real = world.latest

    def broken():
        calls["n"] += 1
        if link.w.claimed and clock.t > T0 + 0.05 and any(c[1] == "act" and c[3] == 4 for c in link.calls):
            raise RuntimeError("camera crashed")
        return real()

    rc.feed = type("Feed", (), {"latest": staticmethod(broken)})()
    with pytest.raises(RollCallAborted, match="camera crashed"):
        rc.run()
    assert handed_back(link, world, 0) and not world.claimed
    zeros = [c for c in link.calls if c[1] == "act" and c[3] == 0]
    assert zeros  # the throttle was explicitly zeroed, not left to the dead-man switch


def test_ctrl_c_mid_pulse_hands_the_car_back(template, model):
    rc, link, world, clock, pipe = make(template, model)
    real_sleep = clock.sleep

    def sleep(dt):
        real_sleep(dt)
        if link.w.claimed and link.acts >= 2:
            raise KeyboardInterrupt

    rc.sleep = sleep
    with pytest.raises(KeyboardInterrupt):
        rc.run()
    assert handed_back(link, world, 0) and not world.claimed


def test_a_camera_that_stalls_aborts_and_hands_the_car_back(template, model):
    rc, link, world, clock, pipe = make(template, model)
    frozen = world.latest
    state = {"stalled": False}

    def stalling():
        if link.w.claimed:
            state["stalled"] = True
        return None if state["stalled"] else frozen()

    rc.feed = type("Feed", (), {"latest": staticmethod(stalling)})()
    with pytest.raises(RollCallAborted, match="no new frame"):
        rc.run()
    assert handed_back(link, world, 0)


def test_a_failing_zero_does_not_skip_the_unclaim(template, model):
    rc, link, world, clock, pipe = make(template, model, slots=(0,))
    real_act = link.act

    def act(slot, throttle=None, lane=False):
        if throttle == 0:
            raise ConnectionError("gone")
        return real_act(slot, throttle, lane)

    link.act = act
    with pytest.raises(RollCallAborted, match="taken from us"):  # the zero after the pulse is refused: abort...
        rc.run()
    assert link.names()[-1] == "unclaim" and any("throttle 0 failed" in line for line in rc.log)  # ...and still unclaim


def test_refused_on_a_real_racelab_while_a_session_is_running_and_after_an_estop(racelab_factory):
    """The same refusal against the real RaceLab app and link (virtual CU): nothing is claimed, nothing is driven."""
    from types import SimpleNamespace

    from trackloc.link_racelab import RaceLabLink

    server = racelab_factory("sim")
    link = RaceLabLink(server.url, "camera").connect()
    dummy = SimpleNamespace(model=SimpleNamespace(length_c=6.0), detector=None)
    try:
        assert link.ensure_session() is True  # a practice session is running
        deadline = time.monotonic() + 5
        while link.status()["session"] not in ("running", "finishing") and time.monotonic() < deadline:
            time.sleep(0.05)
        with pytest.raises(RollCallAborted, match="session"):
            RollCall(link, dummy, None, (0,)).run()
        assert server.call(lambda: server.app.bots.slots[0].driver) is None  # slot 1 was never claimed
        link.stop_session()
        deadline = time.monotonic() + 5
        while link.status()["session"] in ("running", "finishing") and time.monotonic() < deadline:
            time.sleep(0.05)
        server.call(lambda: server.app.bots.estop())
        deadline = time.monotonic() + 5
        while not link.status()["estop"] and time.monotonic() < deadline:
            time.sleep(0.05)
        with pytest.raises(RollCallAborted, match="E-STOP"):
            RollCall(link, dummy, None, (0,)).run()
    finally:
        link.close()


# ----------------------------------------------------------------------------- the command line

def test_cli_defaults_and_refusals():
    from trackloc.__main__ import build_parser, main

    a = build_parser().parse_args(["identify", "--camera", "1", "--map", "m.json", "--slots", "0,1,2"])
    assert (a.throttle, a.cap, a.pulse_s) == (4, 8, 0.5)
    r = build_parser().parse_args(["run", "--camera", "1", "--map", "m.json", "--identify"])
    assert r.identify and r.throttle == 4
    with pytest.raises(SystemExit, match="--racelab-url"):  # it moves cars through RaceLab: never offline
        main(["run", "--camera", "1", "--map", "m.json", "--identify", "--sink", "out.jsonl"])
    with pytest.raises(SystemExit, match="live camera"):
        main(["identify", "--video", "v.mp4", "--map", "m.json", "--slots", "0"])
    # bad settings are refused before a camera or RaceLab is opened (a SystemExit later would skip the clean-up)
    with pytest.raises(SystemExit, match="--slots"):
        main(["run", "--camera", "1", "--map", "m.json", "--identify", "--racelab-url", "ws://127.0.0.1:1/ws"])
    with pytest.raises(SystemExit, match="cap"):
        main(["run", "--camera", "1", "--map", "m.json", "--identify", "--slots", "0", "--throttle", "9", "--cap", "8",
              "--racelab-url", "ws://127.0.0.1:1/ws"])
    with pytest.raises(SystemExit, match="pulse_s"):
        main(["identify", "--camera", "1", "--map", "m.json", "--slots", "0", "--pulse-s", "60"])
