"""Race brain (profile kind "brain"): pilot + lane planner + pit planner + arbiter.

The behaviour tests run the BotManager on the virtual CU's physics, ticked at 20 Hz on the simulated
clock (lane requests reach the car one tick late); the failure paths go through the manager's
gates and guard, like test_corners.py."""

from __future__ import annotations

import sys
import types

import pytest

from racelab.bots import brain
from racelab.bots.brain import PILOTS, BrainProfile, Pilot, load_pilot, pilots_meta
from racelab.bots.manager import BotManager
from racelab.bots.profiles import PROFILE_LABELS, PROFILES, ConstantProfile, profile_from_dict
from racelab.config import SafetyConfig
from racelab.events import LinkState

from .test_bots import Clock, FakeLink
from .test_sim import make_cu, make_sim

CHANGERS = (0.385, 0.60)  # the virtual track's (sim/track.py)
PIT_IN, PIT_OUT = 0.87, 0.97


class SimLink:
    """The manager's link, straight into the virtual CU's bot firmware (B commands). A lane request
    reaches the car `delay` ticks late, like the real link and Bluetooth."""

    def __init__(self, cu, delay=1):
        self.cu, self.delay = cu, delay
        self.queue: list[list[int]] = []
        self.estopped = False

    def set_bot(self, slot, value):
        if value is None:
            self.cu.botrelease(slot)
        else:
            self.cu.setbot(slot, value)

    def lane(self, slot):
        self.queue.append([self.delay, slot])

    def flush(self):
        for item in list(self.queue):
            item[0] -= 1
            if item[0] < 0:
                self.cu.botlane(item[1])
                self.queue.remove(item)

    def estop(self):
        self.estopped = True

    def clear_estop(self):
        self.estopped = False

    def resume_bots(self):
        pass


class Race:
    """A BotManager driving the virtual CU. The engine's cars[] / cu / session and the positions are
    built from the simulator's ground truth; slot 0 runs `profile`."""

    def __init__(self, profile, *, fuel_mode=True, pit_lane=True, laps_target=None, tank=15, fuel=15.0,
                 lane=None, publish=(0,), delay=1):
        self.sim = make_sim(fuel_mode=fuel_mode, pit_lane=pit_lane)
        self.link = SimLink(make_cu(self.sim), delay)
        self.bots = BotManager(self.link, SafetyConfig(deadman_ms=500, max_throttle=15), clock=lambda: self.sim.t)
        self.bots.on_link(LinkState("connected", "sim", bots_supported=True))
        self.bots.set_profile(0, profile)
        self.bots.take(0, "profile")
        self.car = self.sim.cars[0]
        self.car.tank, self.car.fuel = tank, fuel
        if lane is not None:
            self.car.lane = lane
        self.publish, self.laps_target, self.state = publish, laps_target, "running"
        self.stale = False
        self.geo = self.sim.track_geometry()
        self.laps, self.crossings, self.last_cross = 0, 0, None
        self.requests: list[dict] = []  # {t, s, pit, lap}
        self.flips: list[dict] = []  # lane changes: {t, s, to}
        self.visits: list[dict] = []  # pit visits: {lap, fuel_in, fuel_out, t_in, t_out}
        self.lap_ms: list[int] = []
        self.min_fuel = self.car.fuel

    @property
    def brain(self) -> dict:
        return self.bots.snapshot()[0]["brain"]

    def tick(self):
        snap = self.sim.snapshot()
        cars = [{"slot": c["slot"], "laps": self.laps if c["slot"] == 0 else 0,
                 "fuel": min(15, int(self.sim.cars[c["slot"]].fuel)), "pit": c["pit"]} for c in snap["cars"]]
        engine = {"session": {"state": self.state, "laps": self.laps_target, "remaining_s": None},
                  "cu": {"phase": 0, "fuel_mode": self.sim.cfg.fuel_mode, "pit_lane": self.sim.cfg.pit_lane},
                  "cars": cars}
        positions = {"source": "sim", "age_ms": 0, "stale": self.stale,
                     "cars": [{k: c[k] for k in ("slot", "s", "lane", "v", "off")}
                              for c in snap["cars"] if c["slot"] in self.publish]}
        asked = len(self.link.queue)
        self.bots.tick(engine, positions, self.geo)
        if len(self.link.queue) > asked:
            self.requests.append({"t": self.sim.t, "s": snap["cars"][0]["s"], "pit": self.car.in_pit,
                                  "lap": self.laps})
        self.link.flush()
        lane, pit = self.car.lane, self.car.in_pit
        self.sim.step(0.05)
        self.min_fuel = min(self.min_fuel, self.car.fuel)
        if self.car.lane != lane:
            self.flips.append({"t": self.sim.t, "s": self.sim.snapshot()["cars"][0]["s"], "to": self.car.lane})
        if self.car.in_pit and not pit:
            self.visits.append({"lap": self.laps, "fuel_in": self.car.fuel, "t_in": self.sim.t})
        elif pit and not self.car.in_pit:
            self.visits[-1].update(fuel_out=self.car.fuel, t_out=self.sim.t)
        if self.car.laps > self.crossings:  # the first crossing (from the grid) is the reference
            self.crossings = self.car.laps
            if self.last_cross is not None:
                self.laps += 1
                self.lap_ms.append(round((self.sim.t - self.last_cross) * 1000))
                self.bots.on_engine_event("lap", {"slot": 0, "lap": self.laps, "ms": self.lap_ms[-1],
                                                  "neutralized": False})
            self.last_cross = self.sim.t

    def run(self, until, max_s=900.0):
        while not until(self):
            assert self.sim.t < max_s, "the race did not get there"
            self.tick()
        return self


def brain_profile(lane=None, pit=None, **kw):
    return {"kind": "brain", "lane": lane or {}, "pit": pit or {"policy": "off"}, **kw}


def near_changer(s, tol=0.02):
    return any(abs(s - c) < tol for c in CHANGERS)


# ------------------------------------------------------------------ schema, registry


def test_profile_round_trip_and_validation():
    assert PROFILES["brain"] is BrainProfile and PROFILE_LABELS["brain"] == "Race brain"
    p = profile_from_dict(brain_profile({"policy": "alternate", "every": 2},
                                        {"policy": "auto", "reserve_laps": 3, "zone": [0.8, 0.95]},
                                        pilot_params={"cap": 7}))
    d = p.to_dict()
    assert d["kind"] == "brain" and d["pilot"] == "corners" and d["pilot_params"]["cap"] == 7
    assert d["lane"]["every"] == 2 and d["lane"]["d_max_m"] == 2.0 and d["pit"]["zone"] == [0.8, 0.95]
    assert profile_from_dict(d).to_dict() == d  # full round trip
    assert profile_from_dict({"kind": "brain"}).to_dict()["pit"]["policy"] == "auto"
    for bad in ({"pilot": "nope"}, {"pilot_params": {"nope": 1}}, {"pilot_params": {"cap": 99}},
                {"lane": {"policy": "random"}}, {"lane": {"every": 0}}, {"lane": {"every": 1.5}},
                {"lane": {"d_min_m": 2.0, "d_max_m": 1.0}}, {"lane": {"nope": 1}}, {"lane": []},
                {"pit": {"policy": "sometimes"}}, {"pit": {"reserve_laps": float("nan")}},
                {"pit": {"throttle": 0}}, {"pit": {"throttle": True}}, {"pit": {"zone": [0.9, 0.9]}},
                {"pit": {"zone": [0.9]}}, {"pit": {"lane": True}}, {"pit": {"lane": 2}}, {"nope": 1}):
        with pytest.raises(ValueError):
            profile_from_dict({"kind": "brain", **bad})


def test_pilot_registry_in_meta_and_a_new_pilot_plugs_in(monkeypatch):
    meta = {m["name"]: m for m in pilots_meta()}
    assert list(meta) == ["corners", "target", "mpc"] and meta["mpc"]["available"]  # numpy is a dependency
    assert meta["corners"]["available"] and meta["corners"]["label_key"] == "profile.corners.label"
    assert meta["corners"]["defaults"]["cap"] == 12 and {p["key"] for p in meta["corners"]["params"]} >= {"cap"}
    assert meta["target"]["defaults"]["target_ms"] == 6000 and meta["target"]["reason"] is None
    # the hook: one registry entry, nothing else to change
    monkeypatch.setitem(PILOTS, "steady", Pilot(ConstantProfile, "Steady", "pilot.steady.label",
                                                ({"key": "value", "label_key": "profile.throttle", "min": 0, "max": 15},)))
    p = profile_from_dict(brain_profile(pilot="steady", pilot_params={"value": 6, "ramp_s": 0}))
    assert p.output(brain_ctx()) == 6 and [m["name"] for m in pilots_meta()][-1] == "steady"
    # a pilot in its own module: the module exposes PILOT, the registry line loads it
    module = types.ModuleType("racelab.bots.pilot_fake")
    module.PILOT = Pilot(ConstantProfile, "Fake", "pilot.fake.label")
    monkeypatch.setitem(sys.modules, "racelab.bots.pilot_fake", module)
    assert load_pilot("pilot_fake") is module.PILOT
    # a pilot whose module cannot load is listed, not available, with the reason
    monkeypatch.setitem(PILOTS, "mpc", load_pilot("pilot_that_does_not_exist"))
    mpc = {m["name"]: m for m in pilots_meta()}["mpc"]
    assert mpc["available"] is False and "pilot_that_does_not_exist" in mpc["reason"]
    with pytest.raises(ValueError, match="unavailable"):
        profile_from_dict(brain_profile(pilot="mpc"))


# ------------------------------------------------------------------ lane planner on the sim


def test_alternate_changes_lane_at_the_next_changer_one_request_each():
    r = Race(brain_profile({"policy": "alternate"}), pit_lane=False).run(lambda r: r.laps >= 6)
    assert len(r.flips) >= 5 and all(near_changer(f["s"]) for f in r.flips)
    assert [f["to"] for f in r.flips[:4]] == [1, 0, 1, 0]  # one change per lap, back and forth
    assert len(r.requests) == len(r.flips)  # one request per lane change, none wasted
    for q, f in zip(r.requests, r.flips):  # each one between d_min and d_max before its changer
        assert q["t"] < f["t"] and 0.5 < (f["s"] - q["s"]) % 1.0 * 18 <= 2.1


def test_one_changer_on_the_map_still_gets_one_request_per_pass():
    """A map with a single changer (the brain does not know the second one): every pass gets its
    request, not only the first one."""
    r = Race(brain_profile({"policy": "alternate"}), pit_lane=False)
    r.geo = {**r.geo, "changers": [CHANGERS[0]]}
    r.run(lambda r: r.laps >= 5)
    assert len(r.requests) == len(r.flips) == 4 and all(abs(f["s"] - CHANGERS[0]) < 0.02 for f in r.flips)


def test_alternate_with_the_pit_lane_on_never_asks_before_the_pit_entry():
    r = Race(brain_profile({"policy": "alternate"})).run(lambda r: r.laps >= 6)
    assert not r.visits and len(r.flips) >= 5  # the pit entry never took a lane request
    assert all(not PIT_IN - 0.2 < q["s"] < PIT_IN for q in r.requests)


def test_overtake_on_the_sim_passes_a_parked_car_in_our_lane():
    r = Race(brain_profile({"policy": "overtake"}), pit_lane=False, publish=(0, 3))
    r.sim.cars[3].lane, r.sim.cars[3].s = 0, r.sim.track.arc(0, 0.30)  # parked before the first changer
    r.run(lambda r: r.flips or r.laps >= 2)
    assert r.flips and near_changer(r.flips[0]["s"]) and r.flips[0]["to"] == 1


def test_fastest_settles_on_the_shorter_lane():
    """Constant throttle (target pilot pinned at 8): only the lane length differs, lane 1 is ~4 % longer."""
    pinned = {"start": 8, "min_value": 8, "max_value": 8, "ramp_s": 0}
    for start in (0, 1):
        r = Race(brain_profile({"policy": "fastest"}, pilot="target", pilot_params=pinned),
                 pit_lane=False, lane=start).run(lambda r: r.laps >= 10)
        assert r.car.lane == 0 and len(r.flips) <= 2  # timed both lanes once, then stayed


# ------------------------------------------------------------------ overtake, synthetic positions


def positions(*cars, stale=False, age_ms=0):
    return {"source": "test", "age_ms": age_ms, "stale": stale,
            "cars": [{"slot": c[0], "s": c[1], "lane": c[2], "v": c[3], "off": False} for c in cars]}


def geo():
    return make_sim().track_geometry()


@pytest.fixture
def env():
    link, clock = FakeLink(), Clock()
    bots = BotManager(link, SafetyConfig(deadman_ms=500, max_throttle=15), clock=clock)
    bots.on_link(LinkState("connected", "sim", bots_supported=True))
    return bots, link, clock


def drive(bots, profile):
    """Slot 0 on `profile`; a brain's pilot starts without its ramp (the clock stands still here)."""
    if profile["kind"] == "brain":
        profile = {**profile, "pilot_params": {"ramp_s": 0, **profile.get("pilot_params", {})}}
    bots.set_profile(0, profile)
    bots.take(0, "profile")


def engine(state="running", fuel=15, pit=False, fuel_mode=True, pit_lane=True, laps=3):
    return {"session": {"state": state, "laps": None, "remaining_s": None},
            "cu": {"phase": 0, "fuel_mode": fuel_mode, "pit_lane": pit_lane},
            "cars": [{"slot": 0, "laps": laps, "fuel": fuel, "pit": pit}]}


ME = (0, 0.30, 0, 3.0)  # 1.53 m before the first changer, in the request window at 3 m/s


def test_overtake_asks_behind_a_slower_car_only_when_the_other_lane_is_free(env):
    bots, link, _ = env
    drive(bots, brain_profile({"policy": "overtake"}))
    g = geo()
    bots.tick(engine(), positions(ME, (2, 0.33, 0, 1.0)), g)  # 0.54 m ahead, slower, same lane
    assert link.lanes == [0]
    b = bots.snapshot()[0]["brain"]
    assert b["intent_key"] == "brain.intent.overtake" and b["intent_args"] == {"slot": 2} and b["next"] == "lane"
    bots.tick(engine(), positions(ME, (2, 0.33, 0, 1.0)), g)
    assert link.lanes == [0]  # one request per changer
    # same, but a car alongside on the other lane: no request, and it says why
    drive(bots, brain_profile({"policy": "overtake"}))
    link.lanes.clear()
    bots.tick(engine(), positions(ME, (2, 0.33, 0, 1.0), (4, 0.305, 1, 3.0)), g)
    assert link.lanes == [] and bots.snapshot()[0]["brain"]["intent_args"]["why"] == "brain.why.occupied"
    # a car that is not slower, or too far, or in the other lane, is not a reason to change
    for other in ((2, 0.33, 0, 3.0), (2, 0.40, 0, 1.0), (2, 0.33, 1, 1.0)):
        drive(bots, brain_profile({"policy": "overtake"}))
        bots.tick(engine(), positions(ME, other), g)
        assert link.lanes == []


def test_one_request_per_changer_with_old_positions(env):
    """A connector feed 300 ms old: the window uses where the car is now, and the same changer is
    never asked for twice while the car closes in on it."""
    bots, link, _ = env
    drive(bots, brain_profile({"policy": "overtake"}))
    g = geo()
    for s in (0.24, 0.26, 0.28, 0.30, 0.32, 0.34):  # 2.6 m .. 0.8 m before the changer, as seen
        bots.tick(engine(), positions((0, s, 0, 3.0), (2, s + 0.03, 0, 1.0), age_ms=300), g)
    assert link.lanes == [0]


def test_requests_wait_for_the_window_before_the_changer(env):
    bots, link, _ = env
    drive(bots, brain_profile({"policy": "overtake"}))
    g = geo()
    for s, asked in ((0.20, False), (0.37, False), (0.30, True)):  # 3.3 m: too early; 0.27 m: too late
        bots.tick(engine(), positions((0, s, 0, 3.0), (2, s + 0.03, 0, 1.0)), g)
        assert bool(link.lanes) is asked


# ------------------------------------------------------------------ pit planner on the sim


def test_auto_pit_long_run_never_runs_dry():
    """Double consumption (tank word 0): about 11 laps a tank, 30 laps = 2 or 3 stops."""
    r = Race(brain_profile(pit={"policy": "auto"}), tank=0).run(lambda r: r.laps >= 30)
    assert len(r.visits) >= 2 and r.min_fuel > 0.5  # pitted in time, never empty
    for v in r.visits:
        assert v["fuel_in"] < 5 and v["fuel_out"] > 14.9  # low when it went in, full when it came out
    assert all(b["lap"] - a["lap"] >= 5 for a, b in zip(r.visits, r.visits[1:]))  # min_laps
    assert r.lap_ms[-1] < 1.3 * min(r.lap_ms)  # still lapping at pace after the stops
    assert not any(q["pit"] for q in r.requests)  # never a request from the pit lane
    assert r.brain["fuel_per_lap"] == pytest.approx(1.3, abs=0.25)


def test_auto_pit_skips_the_end_of_a_race_and_still_finishes():
    """The first stop would come at the 9th lap (as above): a 9-lap race (last lap) and a 10-lap race
    (enough fuel to finish) are run without stopping and without running dry; 12 laps need it."""
    for laps, stops in ((9, 0), (10, 0), (12, 1)):
        r = Race(brain_profile(pit={"policy": "auto"}), tank=0, laps_target=laps).run(lambda r: r.laps >= laps)
        assert len(r.visits) == stops and r.min_fuel > 0.3, laps


def test_auto_pit_rules_last_laps_and_min_laps():
    """Short of fuel either way (2 units, 3 per lap): the last lap and a recent stop still say no."""
    p = BrainProfile(pit={"policy": "auto", "reserve_laps": 2, "last_laps": 1, "min_laps": 5})
    p._per_lap, p._fuel = 3.0, 2
    at = lambda **kw: brain_ctx(laps=20, **kw)  # noqa: E731
    assert p._pit_due(at()) and p._pit_due(at(session_laps=32))  # no end in sight, 12 laps to go
    assert not p._pit_due(at(session_laps=21))  # the last lap: never, even if it cannot finish
    p._last_pit_lap = 16
    assert not p._pit_due(at())  # 4 laps after a stop: not again yet
    p._last_pit_lap = 15
    assert p._pit_due(at())  # 5 laps after
    p._per_lap, p._fuel, p._last_pit_lap = 1.0, 2, None
    assert not p._pit_due(at(session_laps=22)) and p._pit_due(at(session_laps=23))  # enough to finish, or not


def test_pit_now_stops_at_the_next_pit_entry_once():
    r = Race(brain_profile(pit={"policy": "now"}), fuel=9.0).run(lambda r: r.laps >= 4)
    assert len(r.visits) == 1 and r.visits[0]["lap"] == 0 and r.visits[0]["fuel_out"] > 14.9
    assert r.brain["next"] is None


def test_pit_request_button_is_timed_by_the_brain():
    r = Race(brain_profile(pit={"policy": "off"}), fuel=9.0).run(lambda r: r.laps >= 1)
    assert not r.visits
    r.bots.brain_request(0, "pit")
    r.run(lambda r: r.visits and "t_out" in r.visits[-1])
    pit_ask = [q for q in r.requests if PIT_IN - 0.15 < q["s"] < PIT_IN]
    assert len(pit_ask) == 1 and r.visits[0]["fuel_out"] > 14.9
    r.run(lambda r: r.laps >= 4)
    assert len(r.visits) == 1  # a one-shot


def test_lane_request_button_changes_lane_once():
    r = Race(brain_profile(), pit_lane=False).run(lambda r: r.laps >= 1)
    r.bots.brain_request(0, "lane")
    r.run(lambda r: r.laps >= 4)
    assert len(r.flips) == 1 and near_changer(r.flips[0]["s"]) and len(r.requests) == 1


@pytest.mark.parametrize("side, flips", [(0, 0), (1, 1)])
def test_a_pit_entry_on_one_lane_wins_over_the_lane_planner(side, flips):
    """`pit.lane`: the car gets to the pit side and stays there, even when `alternate` wants the
    other lane at the last changer before the entry (one changer on the map: no second chance)."""
    r = Race(brain_profile({"policy": "alternate"}, {"policy": "off", "lane": side}), fuel=9.0)
    r.geo = {**r.geo, "changers": [CHANGERS[0]]}
    r.run(lambda r: r.laps >= 1)  # on lane 0 at the line: alternate now wants lane 1
    r.bots.brain_request(0, "pit")
    before = len(r.flips)
    r.run(lambda r: r.visits or r.laps >= 3)
    assert r.visits and r.visits[0]["lap"] == 1  # the stop is on this lap, it does not slip
    assert len(r.flips) - before == flips and r.car.lane == side


@pytest.mark.parametrize("fuel_mode, pit_lane, why", [(False, True, "brain.why.fuel_mode"),
                                                       (True, False, "brain.why.pit_lane")])
def test_no_pit_without_the_cu_fuel_mode_and_pit_lane(fuel_mode, pit_lane, why):
    r = Race(brain_profile(pit={"policy": "now"}), fuel_mode=fuel_mode, pit_lane=pit_lane, fuel=9.0)
    r.run(lambda r: r.laps >= 2)
    assert not r.visits and r.brain["pit_why"] == why and r.brain["intent_args"]["why"] == why


def test_pit_zone_override_without_a_pit_on_the_map():
    r = Race(brain_profile(pit={"policy": "now", "zone": [PIT_IN, PIT_OUT]}), fuel=9.0)
    r.geo = {**r.geo, "pit": None}  # a connector that sends no pit zone (a camera)
    r.run(lambda r: r.laps >= 2)
    assert len(r.visits) == 1
    r = Race(brain_profile(pit={"policy": "now"}), fuel=9.0)
    r.geo = {**r.geo, "pit": None}
    r.run(lambda r: r.laps >= 2)
    assert not r.visits and r.brain["pit_why"] == "brain.why.pit_zone"


# ------------------------------------------------------------------ without positions


def test_stale_positions_disable_lane_and_pit_moves_and_the_pilot_falls_back(env):
    bots, link, clock = env
    drive(bots, brain_profile({"policy": "alternate"}, {"policy": "now"},
                              pilot_params={"start": 8, "ramp_s": 0, "cap": 12}))
    g = geo()
    bots.tick(engine(), positions(ME), g)
    bots.on_engine_event("lap", {"slot": 0, "lap": 4, "ms": 6000})  # alternate: a change is due
    clock.t += 1.5  # past the corners pilot's hold on its last throttle
    for feed in (None, positions(ME, stale=True), positions(ME, age_ms=900), positions((3, 0.3, 0, 3.0))):
        for s in (0.30, 0.78):  # in the changer's window, then in the pit entry's
            for c in (feed or {}).get("cars", []):
                c["s"] = s
            bots.tick(engine(), feed, g)
            assert link.lanes == [] and link.sent[0] == 8  # the corners pilot falls back to its pace
            b = bots.snapshot()[0]["brain"]
            assert b["intent_args"]["why"] == "brain.why.positions" and b["next"] == "pit"
    bots.tick(engine(), positions((0, 0.30, 0, None)), g)  # positions without a speed: no timing either
    assert link.lanes == [] and bots.snapshot()[0]["brain"]["lane_why"] == "brain.why.speed"
    bots.tick(engine(), positions(ME), g)  # the positions are back: the planned change goes out
    assert link.lanes == [0] and bots.snapshot()[0]["drive"] == "map"


def test_without_a_map_in_metres_it_says_why(env):
    bots, link, _ = env
    drive(bots, brain_profile({"policy": "alternate"}))
    bots.tick(engine(), positions(ME), {**geo(), "length_m": None})
    assert bots.snapshot()[0]["brain"]["lane_why"] == "brain.why.length"
    bots.tick(engine(), positions(ME), {**geo(), "changers": []})
    assert bots.snapshot()[0]["brain"]["lane_why"] == "brain.why.changers"
    bots.tick(engine(), positions(ME), None)
    assert bots.snapshot()[0]["brain"]["lane_why"] == "brain.why.map" and link.lanes == []


def test_blind_in_the_pit_lane_stops_then_leaves(env):
    """No position inside the pit lane (a camera that does not see it): the CU's pit flag drives it."""
    bots, link, clock = env
    drive(bots, brain_profile(pit={"policy": "auto", "throttle": 3}, pilot_params={"start": 8, "ramp_s": 0}))
    bots.tick(engine(pit=True, fuel=3), None, geo())
    assert link.sent[0] == 3  # in the pit lane: capped
    clock.t += brain.PIT_BLIND_STOP_S
    for fuel in (3, 6, 15):
        bots.tick(engine(pit=True, fuel=fuel), None, geo())
        assert link.sent[0] == (0 if fuel < 15 else 3)  # stopped until the tank is full, then out
    bots.tick(engine(pit=False, fuel=15), None, geo())
    assert link.sent[0] == 8 and link.lanes == []


# ------------------------------------------------------------------ safety


def until_state(name):
    return {
        "lane planned": lambda r: r.brain["next"] == "lane",
        "pit planned": lambda r: r.brain["next"] == "pit",
        "pit lane": lambda r: r.brain["intent_key"] == "brain.intent.pit_lane" and not r.visits[-1].get("t_out")
        and r.car.stop_left == 0 and r.car.v > 0,
        "refuelling": lambda r: r.brain["intent_key"] == "brain.intent.refuel",
    }[name]


GATES = {
    "estop": lambda r: r.bots.estop(),
    "link": lambda r: r.bots.on_link(LinkState("lost", "sim")),
    "session": lambda r: setattr(r, "state", "paused"),
}


@pytest.mark.parametrize("gate", list(GATES))
@pytest.mark.parametrize("state", ["lane planned", "pit planned", "pit lane", "refuelling"])
def test_gates_force_zero_in_every_brain_state(state, gate):
    r = Race(brain_profile({"policy": "alternate"}, {"policy": "now"}), fuel=9.0)
    r.run(until_state(state))
    GATES[gate](r)
    asked = len(r.requests)
    for _ in range(10):
        r.tick()
        assert r.bots.slots[0].output == 0 and r.sim.bot_gas[0] == 0 and r.bots.slots[0].gate == gate
    assert len(r.requests) == asked  # and no lane request either
    if gate == "estop":  # cleared by hand: it drives again, out of the pits and on
        r.bots.clear_estop()
        r.run(lambda r: r.laps >= 3)
        assert r.car.fuel > 0.5 and not r.car.in_pit


def test_a_pilot_failure_stops_the_car(env, monkeypatch):
    bots, link, _ = env
    drive(bots, brain_profile(pilot_params={"start": 8, "ramp_s": 0}))
    bots.tick(engine(), positions(ME), geo())
    assert link.sent[0] > 0
    monkeypatch.setattr(type(bots.slots[0].profile._model), "output", lambda self, ctx: 1 / 0)
    bots.tick(engine(), positions(ME), geo())
    assert link.sent[0] == 0  # the manager's guard


def test_a_planner_failure_leaves_the_pilot_alone(env, monkeypatch):
    bots, link, _ = env
    drive(bots, brain_profile({"policy": "alternate"}, {"policy": "now", "throttle": 3},
                              pilot_params={"start": 8, "ramp_s": 0}))
    g = geo()
    bots.on_engine_event("lap", {"slot": 0, "lap": 4, "ms": 6000})
    monkeypatch.setattr(BrainProfile, "_arbiter", lambda self, ctx, value: 1 / 0)
    bots.tick(engine(), None, g)
    assert link.sent[0] == 8 and link.lanes == []  # the pilot drives, nothing else is asked for
    assert bots.snapshot()[0]["brain"]["intent_args"]["why"] == "brain.why.error"
    bots.tick(engine(pit=True, fuel=3), None, g)
    assert link.sent[0] == 3  # in the pit lane the pilot is still capped


# ------------------------------------------------------------------ reading


def test_snapshot_fields():
    r = Race(brain_profile({"policy": "alternate"}, {"policy": "auto"}), tank=0).run(lambda r: r.laps >= 3)
    b = r.brain
    assert set(b) >= {"intent_key", "intent_args", "lane_policy", "pit_policy", "next", "fuel_per_lap"}
    assert (b["lane_policy"], b["pit_policy"], b["pilot"]) == ("alternate", "auto", "corners")
    assert b["fuel_per_lap"] > 0 and r.bots.snapshot()[0]["drive"] == "map"
    r.run(lambda r: r.brain["next"] == "lane")
    assert r.brain["intent_key"] == "brain.intent.alternate" and r.brain["intent_args"] == {"n": 1}
    r.run(lambda r: r.brain["next"] == "pit")
    assert r.brain["intent_key"] == "brain.intent.pit" and r.brain["intent_args"]["fuel"] <= 4
    assert r.bots.snapshot()[1]["brain"] is None  # another kind of profile


def test_brain_request_only_for_a_brain(env):
    bots, _, _ = env
    with pytest.raises(ValueError, match="race brain"):
        bots.brain_request(0, "pit")  # not a bot
    drive(bots, {"kind": "constant", "value": 5})
    with pytest.raises(ValueError, match="race brain"):
        bots.brain_request(0, "pit")
    drive(bots, brain_profile())
    with pytest.raises(ValueError):
        bots.brain_request(0, "nope")


def brain_ctx(**kw):
    from racelab.bots.profiles import Context

    return Context(**{"now": 1.0, "since_cross_s": None, "laps": 0, **kw})


# ------------------------------------------------------------------ live params (bot.params)

def test_live_params_risk_applies_in_place_and_keeps_the_learned_state():
    """The RL strategist changes the MPC's risk between two laps: no profile rebuild, no loss."""
    r = Race(brain_profile(pilot="mpc", pilot_params={"risk": 0.5}))
    r.run(lambda r: r.laps >= 1)
    prof = r.bots.slots[0].profile
    grip = dict(prof._model._f)
    assert r.bots.set_params(0, {"risk": 0.9}) == {"risk": 0.9}
    assert prof._model.risk == 0.9 and dict(prof._model._f) == grip
    assert prof.pilot_params["risk"] == 0.9
    for bad in ({"risk": 2}, {"risk": -1}, {"risk": True}, {"risk": "high"}, {"nope": 1}):
        with pytest.raises(ValueError):
            r.bots.set_params(0, bad)
    assert prof._model.risk == 0.9  # a refused update changes nothing


def test_live_params_are_refused_without_a_brain_or_a_risk():
    with pytest.raises(ValueError):
        Race(brain_profile()).bots.set_params(0, {"risk": 1.0})   # corners pilot: no live risk
    c = Race(brain_profile(pilot="mpc"))
    c.bots.take(0, "manual")
    with pytest.raises(ValueError):
        c.bots.set_params(0, {"risk": 1.0})                        # not a profile driver
