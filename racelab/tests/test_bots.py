"""BotManager: safety gates (lights, session, E-STOP, dead-man, reconnection) and profiles."""

from __future__ import annotations

import pytest

from racelab.bots.manager import BotManager
from racelab.bots.profiles import Context, MapProfile, TargetLapProfile, profile_from_dict
from racelab.config import SafetyConfig
from racelab.events import LinkState


class FakeLink:
    def __init__(self):
        self.sent: dict[int, int | None] = {}
        self.lanes: list[int] = []
        self.estopped = False
        self.resumed = 0

    def set_bot(self, slot, value):
        self.sent[slot] = value

    def lane(self, slot):
        self.lanes.append(slot)

    def estop(self):
        self.estopped = True

    def clear_estop(self):
        self.estopped = False

    def resume_bots(self):
        self.resumed += 1


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def snap(state="running", phase=0, cars=None):
    return {"session": {"state": state}, "cu": {"phase": phase}, "cars": cars or []}


@pytest.fixture
def env():
    link, clock = FakeLink(), Clock()
    bots = BotManager(link, SafetyConfig(deadman_ms=500, max_throttle=13), clock=clock)
    bots.on_link(LinkState("connected", "sim", bots_supported=True))
    return bots, link, clock


def test_manual_throttle_and_cap(env):
    bots, link, _ = env
    bots.take(0, "manual")
    bots.set_manual(0, 9)
    bots.tick(snap())
    assert link.sent[0] == 9
    bots.set_manual(0, 15)
    assert link.sent[0] == 13  # max_throttle ceiling
    with pytest.raises(ValueError):
        bots.set_manual(0, 16)
    bots.release(0)
    assert link.sent[0] is None


def test_lights_gate_everyone(env):
    bots, link, _ = env
    bots.take(1, "manual")
    bots.set_manual(1, 8)
    for phase in (1, 3, 6, 8):
        bots.tick(snap("starting", phase))
        assert link.sent[1] == 0 and bots.slots[1].gate == "lights"
    bots.tick(snap("running", 7))
    assert link.sent[1] == 8


def test_profile_only_runs_in_session(env):
    bots, link, clock = env
    bots.set_profile(2, {"kind": "constant", "value": 7, "ramp_s": 0})
    bots.take(2, "profile")
    bots.tick(snap("grid"))
    assert link.sent[2] == 0 and bots.slots[2].gate == "session"
    bots.tick(snap("running"))
    assert link.sent[2] == 7
    bots.tick(snap("running", cars=[{"slot": 2, "finished": True}]))
    assert link.sent[2] == 0 and bots.slots[2].gate == "finished"


def test_api_deadman_and_agent_loss(env):
    bots, link, clock = env
    bots.api_claim(3, "rl")
    with pytest.raises(ValueError):
        bots.api_claim(3, "other")
    bots.api_action(3, "rl", throttle=10)
    bots.tick(snap())
    assert link.sent[3] == 10
    clock.t += 0.6  # more than 500 ms without an action
    bots.tick(snap())
    assert link.sent[3] == 0 and bots.slots[3].gate == "deadman"
    bots.api_action(3, "rl", throttle=6)
    bots.tick(snap())
    assert link.sent[3] == 6
    with pytest.raises(ValueError):
        bots.api_action(3, "intruder", throttle=15)
    bots.agent_gone("rl")
    bots.tick(snap())
    assert link.sent[3] == 0 and bots.slots[3].driver == "api"


def test_api_free_drives_outside_session(env):
    bots, link, _ = env
    bots.api_claim(4, "bench", free=True)
    bots.api_action(4, "bench", throttle=5)
    bots.tick(snap("idle"))
    assert link.sent[4] == 5


def test_estop(env):
    bots, link, _ = env
    bots.take(0, "manual")
    bots.set_manual(0, 9)
    bots.estop()
    assert link.estopped
    bots.tick(snap())
    assert link.sent[0] == 0 and bots.slots[0].manual == 0
    bots.clear_estop()
    bots.tick(snap())
    assert link.sent[0] == 0  # no automatic throttle resume
    bots.set_manual(0, 4)
    assert link.sent[0] == 4


def test_reconnect_resets_throttle_before_resume(env):
    bots, link, _ = env
    bots.take(0, "manual")
    bots.set_manual(0, 9)
    bots.tick(snap())
    bots.on_link(LinkState("lost", "sim"))
    bots.tick(snap())
    assert link.sent[0] == 0 and bots.slots[0].gate == "link"
    bots.on_link(LinkState("connected", "sim", bots_supported=True))
    bots.tick(snap())
    assert link.resumed == 1
    assert bots.slots[0].manual == 0 and link.sent[0] == 0


def test_classic_mode_refuses_bots(env):
    bots, _, _ = env
    bots.enabled = False
    with pytest.raises(ValueError):
        bots.take(0, "manual")


def test_stock_firmware_refuses_bots(env):
    bots, _, _ = env
    bots.on_link(LinkState("connected", "sim", bots_supported=False))
    with pytest.raises(ValueError):
        bots.take(0, "manual")


def test_target_lap_profile_converges():
    p = TargetLapProfile(target_ms=6000, start=8, ramp_s=0)
    ctx = Context(now=0, since_cross_s=1.0, laps=0)
    p.output(ctx)
    p.on_lap(1, 7000, False)  # too slow -> more throttle
    assert p.output(ctx) == 9
    p.on_lap(2, 5000, False)  # too fast -> less
    assert p.output(ctx) == 8
    p.on_lap(3, 9000, True)  # neutralized lap: ignored
    assert p.output(ctx) == 8


def test_target_lap_profile_learns_a_ceiling():
    """Unreachable target: the bot does not run away, it learns the throttle the track can take."""
    p = TargetLapProfile(target_ms=4000, start=10, ramp_s=0, max_step=1.0, recover_laps=3)
    ctx = Context(now=0, since_cross_s=1.0, laps=0)
    p.output(ctx)
    p.on_lap(1, 6500, False)  # clean but slow: +1
    assert p.output(ctx) == 11
    p.on_lap(2, 9200, False)  # crash (> 1.25 × 6.5 s): ceiling 10
    assert p.output(ctx) == 10
    for lap in range(3, 5):
        p.on_lap(lap, 6600, False)  # too slow, but capped
        assert p.output(ctx) == 10
    p.on_lap(5, 6600, False)  # 3 clean laps: the ceiling goes back up by half a notch (10.5)
    assert p.output(ctx) == 11  # the bot tries the next notch up
    p.on_lap(6, 9000, False)  # new crash: ceiling 9.5
    assert p.output(ctx) == 10


def test_map_profile_follows_lap_fraction():
    p = MapProfile(points=[[0, 12], [0.5, 5]], ref_lap_ms=6000, ramp_s=0)
    assert p.output(Context(now=0, since_cross_s=1.0, laps=1)) == 12
    assert p.output(Context(now=0, since_cross_s=4.0, laps=1)) == 5
    assert p.output(Context(now=0, since_cross_s=9.0, laps=1)) == 5  # > 1.3 laps: cautious


def test_profile_lane_every_and_ramp(env):
    bots, link, clock = env
    bots.set_profile(5, {"kind": "constant", "value": 10, "ramp_s": 1.0, "lane_every": 2})
    bots.take(5, "profile")
    bots.tick(snap())
    assert link.sent[5] == 0  # start of the ramp
    clock.t += 0.5
    bots.tick(snap())
    assert link.sent[5] == 5
    clock.t += 1
    bots.tick(snap())
    assert link.sent[5] == 10
    bots.on_engine_event("lap", {"slot": 5, "lap": 2, "ms": 6000})
    bots.tick(snap())
    assert link.lanes == [5]


def test_profile_from_dict_validation():
    with pytest.raises(ValueError):
        profile_from_dict({"kind": "nope"})
    with pytest.raises(ValueError):
        profile_from_dict({"kind": "constant", "values": 3})
    assert profile_from_dict({"kind": "rival", "rival_slot": 2}).to_dict()["rival_slot"] == 2


def test_estop_drops_api_claims(env):
    bots, link, _ = env
    bots.api_claim(2, "rl")
    bots.api_action(2, "rl", throttle=8)
    bots.estop()
    bots.clear_estop()
    with pytest.raises(ValueError):
        bots.api_action(2, "rl", throttle=8)  # must claim again with bot.claim
    bots.api_claim(2, "rl")
    bots.api_action(2, "rl", throttle=8)
    bots.tick(snap())
    assert link.sent[2] == 8


def test_empty_tank_laps_do_not_teach_profiles(env):
    bots, link, clock = env
    bots.set_profile(0, {"kind": "target", "target_ms": 6000, "start": 8, "ramp_s": 0})
    bots.take(0, "profile")
    empty = snap(cars=[{"slot": 0, "fuel": 0}])
    empty["cu"]["fuel_mode"] = True
    bots.tick(empty)
    bots.on_engine_event("lap", {"slot": 0, "lap": 1, "ms": 18000})
    bots.tick(empty)
    assert link.sent[0] == 8  # no runaway on an empty-tank lap
    assert bots.snapshot()[0]["fuel_empty"] is True
