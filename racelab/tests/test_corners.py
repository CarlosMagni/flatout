"""Corner speed profile ("corners"): drives from the published positions and the track shape.

The learning tests run on the virtual CU (sim physics, with a control delay); the safety tests
go through the BotManager gates (E-STOP, link loss, session, exceptions)."""

from __future__ import annotations

import math

import pytest

from racelab.bots import profiles
from racelab.bots.manager import BotManager
from racelab.bots.profiles import (
    PROFILE_LABELS,
    PROFILES,
    Context,
    CornersProfile,
    F_MAX,
    F_MIN,
    FALLBACK_HOLD_S,
    Profile,
    curvature,
    lane_curvature,
    profile_from_dict,
)
from racelab.config import SafetyConfig
from racelab.events import LinkState
from racelab.sim import physics
from racelab.sim.track import A_MAX, default_track

from .test_bots import Clock, FakeLink, snap
from .test_sim import make_cu, make_sim

GEO = default_track().geometry()
FRESH = {"s": 0.05, "lane": 0, "v": 3.0, "off": False, "age_ms": 0}


def drive(profile, laps=20, *, slot=0, delay_steps=2, with_v=True):
    """Run `slot` of the virtual CU under `profile`. The throttle reaches the car `delay_steps`
    ticks (50 ms) late. Returns (lap times in ms, number of off tracks, highest throttle sent)."""
    sim = make_sim(fuel_mode=False)
    cu = make_cu(sim)
    cu.setbot(slot, 0)
    car = sim.cars[slot]
    queue = [0] * delay_steps
    times, offs, top, was_off, last_laps, last_t = [], 0, 0, False, 0, None
    while len(times) < laps and sim.t < 900:
        c = sim.snapshot()["cars"][slot]
        pos = {"s": c["s"], "lane": c["lane"], "v": c["v"] if with_v else None, "off": c["off"], "age_ms": 0}
        value = profile.output(Context(now=sim.t, since_cross_s=None, laps=c["laps"], pos=pos, geometry=GEO))
        top = max(top, value)
        queue.append(value)
        cu.setbot(slot, queue.pop(0))
        sim.step(0.05)
        offs += car.off and not was_off
        was_off = car.off
        if car.laps > last_laps:
            last_laps = car.laps
            if last_t is not None:
                times.append(round((sim.t - last_t) * 1000))
                profile.on_lap(len(times), times[-1], False)
            last_t = sim.t
    return times, offs, top


def at_corner(p: CornersProfile, c: int, **extra) -> dict:
    """Position in the middle of corner `c` (the profile must have been used once)."""
    geo = p._geo
    span = p._spans[c]
    i = (span.start + span.stop) // 2 % geo["n"]
    return {"s": (i / geo["n"] - geo["finish"]) % 1.0, "lane": 0, "v": 2.0, "off": False,
            "age_ms": 0, **extra}


def use_once(p: CornersProfile) -> None:
    """Build the tables, with the car on the start straight."""
    p.output(Context(now=0.0, since_cross_s=None, laps=0, pos={**FRESH, "s": 0.0}, geometry=GEO))


# ------------------------------------------------------------------ the model


def test_defaults_match_the_virtual_car():
    p = CornersProfile()
    assert (p.v_max, p.tau_acc, p.tau_brake) == (physics.VMAX, physics.TAU_ACC, physics.TAU_BRAKE)
    assert profiles.V_MAX == physics.VMAX


def test_curvature_gives_the_sims_critical_speed():
    """Scale (length_m), smoothing and lane side all at once: sqrt(a / kappa) vs the sim's vc."""
    track, geo = default_track(), curvature(GEO)
    n = geo["n"]
    for lane in (0, 1):
        k = lane_curvature(geo["kappa"], geo["lane_off"], lane)
        errors = []
        for i in range(n):
            vc = track.lanes[lane].vc[int(i / n * track.n)]
            if vc < 8:
                errors.append(abs(math.sqrt(A_MAX / k[i]) - vc) / vc)
        assert len(errors) > 50
        assert sum(errors) / len(errors) < 0.06 and max(errors) < 0.2


@pytest.mark.parametrize("length", [None, 0, -3, "18", float("nan"), [18.0, None], []])
def test_unusable_length_means_no_curvature(length):
    assert curvature({**GEO, "length_m": length}) is None


def test_length_as_a_single_number_is_the_centre_line():
    k = curvature({**GEO, "length_m": 18.377})
    assert k["length"] == pytest.approx(18.377)


def test_profile_schema():
    assert "corners" in PROFILES and PROFILE_LABELS["corners"] == "Corner speed (map)"
    p = profile_from_dict({"kind": "corners", "a_lat": 9, "cap": 10})
    assert p.to_dict()["a_lat"] == 9 and profile_from_dict(p.to_dict()).cap == 10
    for bad in ({"a_lat": 0}, {"cap": 16}, {"margin_pct": -1}, {"latency_s": float("nan")}, {"cap": True}):
        with pytest.raises(ValueError):
            profile_from_dict({"kind": "corners", **bad})


# ------------------------------------------------------------------ learning on the sim


def test_laps_get_faster_without_runaway():
    p = CornersProfile(a_lat=10, f_max=1.5)  # the virtual CU tolerates more than the real-car default
    times, offs, _ = drive(p, 24)
    assert offs == 0
    assert times[0] > 1.2 * min(times)  # slow first laps (standing start, careful corners)
    assert sum(times[-4:]) / 4 < 0.85 * sum(times[:3]) / 3  # then faster
    assert max(times[-6:]) < 1.03 * min(times[-6:])  # and it settles, no runaway
    assert all(F_MIN <= f <= 1.5 for f in p._f.values()) and len(p._f) >= 5


def test_works_on_the_other_lane_and_without_published_speed():
    times, offs, _ = drive(CornersProfile(a_lat=10, f_max=1.5), 18, slot=1)  # slot 1 starts on lane 1
    assert offs == 0 and sum(times[-3:]) < 0.88 * sum(times[:3])
    times, offs, _ = drive(CornersProfile(a_lat=10, f_max=1.5), 18, with_v=False)  # speed from the positions
    assert offs == 0 and sum(times[-3:]) < 0.88 * sum(times[:3])


def test_too_optimistic_start_loses_a_few_laps_then_settles():
    """a_lat above the car's limit: off tracks lower and freeze the corners; no spiral."""
    p = CornersProfile(a_lat=18, f_max=1.5)
    times, offs, _ = drive(p, 30)
    assert 1 <= offs <= 4
    clean = times[-10:]
    assert max(clean) < 1.05 * min(clean)  # the last laps are clean and steady
    assert p._frozen and all(p._f[k] < 1.5 for k in p._frozen)


def test_cap_is_respected():
    _, _, top = drive(CornersProfile(a_lat=10, cap=7), 4)
    assert top <= 7
    # same in the fallback
    p = CornersProfile(cap=6, start=11, ramp_s=0)
    assert p.output(Context(now=0.0, since_cross_s=None, laps=0)) == 6
    # a fractional cap is never rounded up above itself (output() rounds to the nearest notch)
    p = CornersProfile(cap=7.5, start=11, ramp_s=0)
    assert p.output(Context(now=0.0, since_cross_s=None, laps=0)) == 7
    assert p.output(ctx({**FRESH, "v": 0.0})) == 7
    assert curvature({**GEO, "lane_offset": float("inf")})["lane_off"] == 0.0


def test_reset_forgets_what_was_learned():
    p = CornersProfile(a_lat=10)
    drive(p, 4)
    assert p._f
    p.reset()
    assert not p._f and not p._frozen


# ------------------------------------------------------------------ per-corner factors


def test_off_track_lowers_that_corner_and_freezes_it():
    p = CornersProfile(ramp_s=0)
    use_once(p)
    t = 0.0
    p.output(Context(now=t, since_cross_s=None, laps=0, pos=at_corner(p, 2, off=True), geometry=GEO))
    assert p._f[(0, 2)] == pytest.approx(0.92) and (0, 2) in p._frozen
    assert not [k for k in p._f if k != (0, 2)]  # nothing else moved
    # still off, 1 s later: one incident is counted once; the car is told to stop
    t += 1.0
    assert p.output(Context(now=t, since_cross_s=None, laps=0, pos=at_corner(p, 2, off=True), geometry=GEO)) == 0
    assert p._f[(0, 2)] == pytest.approx(0.92)
    # back on track: clean passes through that corner never raise a frozen factor
    for _ in range(3):
        for c in (1, 2, 3):
            t += 0.1
            p.output(Context(now=t, since_cross_s=None, laps=0, pos=at_corner(p, c, v=9.0), geometry=GEO))
        t += 0.1
        p.output(Context(now=t, since_cross_s=None, laps=0, pos={**FRESH, "s": 0.0}, geometry=GEO))
    assert p._f[(0, 2)] == pytest.approx(0.92)
    # other offs in that corner keep lowering it, down to the floor
    for _ in range(12):
        for off in (True, False):
            t += 0.1
            p.output(Context(now=t, since_cross_s=None, laps=0, pos=at_corner(p, 2, off=off), geometry=GEO))
    assert p._f[(0, 2)] == pytest.approx(0.5)


def test_clean_passes_raise_only_corners_that_ran_at_the_target():
    p = CornersProfile(ramp_s=0)
    use_once(p)
    t = 0.0
    # corner 1 driven at the target speed, corner 3 crawled through (e.g. after a restart)
    for c, v in ((1, 9.0), (3, 0.2)):
        t += 0.1
        p.output(Context(now=t, since_cross_s=None, laps=0, pos=at_corner(p, c, v=v), geometry=GEO))
        t += 0.1
        p.output(Context(now=t, since_cross_s=None, laps=0, pos={**FRESH, "s": 0.0}, geometry=GEO))
    assert p._f[(0, 1)] == pytest.approx(1.03) and (0, 3) not in p._f


def test_slow_lap_without_off_flag_blames_the_corner_where_time_was_lost():
    p = CornersProfile(ramp_s=0)
    use_once(p)
    p.on_lap(1, 6000, False)
    t = 0.0
    for _ in range(30):  # a car stuck in corner 4 for 3 s, nobody says it is off
        t += 0.1
        p.output(Context(now=t, since_cross_s=None, laps=1, pos=at_corner(p, 4, v=0.0), geometry=GEO))
    p.on_lap(2, 9500, False)
    assert p._f[(0, 4)] == pytest.approx(0.92) and (0, 4) in p._frozen and len(p._f) == 1
    # a neutralised slow lap (empty tank...) teaches nothing
    p.output(Context(now=t + 0.1, since_cross_s=None, laps=2, pos=at_corner(p, 5, v=0.0), geometry=GEO))
    p.on_lap(3, 20000, True)
    assert len(p._f) == 1


def test_one_incident_is_counted_once_off_then_slow_lap():
    p = CornersProfile(ramp_s=0)
    use_once(p)
    p.on_lap(1, 6000, False)
    p.output(Context(now=1.0, since_cross_s=None, laps=1, pos=at_corner(p, 3, off=True), geometry=GEO))
    p.output(Context(now=2.0, since_cross_s=None, laps=1, pos=at_corner(p, 3, v=0.0), geometry=GEO))
    p.on_lap(2, 12000, False)
    assert p._f[(0, 3)] == pytest.approx(0.92)


# ------------------------------------------------------------------ fallback and failures


def target_pace(**kw):
    return CornersProfile(start=8, ramp_s=0, **kw)


def ctx(pos, geometry=GEO):
    return Context(now=1.0, since_cross_s=None, laps=0, pos=pos, geometry=geometry)


def test_missing_or_stale_inputs_fall_back_to_target_pace():
    p = target_pace()
    assert p.output(ctx(FRESH)) != 8  # driving from the positions (standing start: full cap)
    for context in (
        ctx(None),  # no position for this car
        ctx(FRESH, None),  # no shape
        ctx({**FRESH, "age_ms": 900}),  # stale
        ctx({**FRESH, "s": None}),
        ctx({**FRESH, "s": float("nan")}),
        ctx(FRESH, {**GEO, "length_m": None}),  # unscaled shape (a connector without length_m)
        ctx(FRESH, {"points": []}),
        ctx(FRESH, {"points": [[0, 0]] * 12}),  # no extent
    ):
        assert p.output(context) == 8
    assert p.output(ctx(FRESH)) != 8  # and back to driving when the inputs come back


def test_losing_the_positions_never_jumps_to_a_hotter_throttle(monkeypatch):
    """In a corner at throttle 3, the positions go stale: the lap-average pace (8) would be too hot.
    For FALLBACK_HOLD_S the output stays at most the last driven throttle, then target pace."""
    p = target_pace()
    out = {"v": 3.0}
    monkeypatch.setattr(CornersProfile, "_drive", lambda self, c: out["v"])
    at = lambda t: Context(now=t, since_cross_s=None, laps=0, pos=FRESH, geometry=GEO)
    assert p.output(at(1.0)) == 3
    out["v"] = None  # positions lost
    assert p.output(at(1.2)) == 3 and p.output(at(1.0 + FALLBACK_HOLD_S)) == 3
    assert p.output(at(1.0 + FALLBACK_HOLD_S + 0.1)) == 8  # then the target pace takes over


def test_fallback_keeps_learning_the_target_pace():
    p = target_pace(target_ms=5000)
    p.on_lap(1, 6000, False)  # too slow: more throttle next time
    assert p.output(ctx(None)) > 8


def test_an_exception_in_the_corner_logic_falls_back(monkeypatch):
    p = target_pace()

    def boom(self, c):
        raise RuntimeError("bug")

    monkeypatch.setattr(CornersProfile, "_drive", boom)
    assert p.output(ctx(FRESH)) == 8


# ------------------------------------------------------------------ through the BotManager


def good_positions(**car):
    return {"source": "sim", "age_ms": 0, "stale": False,
            "cars": [{"slot": 0, "s": 0.05, "lane": 0, "v": 3.0, "off": False, **car}]}


@pytest.fixture
def env():
    link, clock = FakeLink(), Clock()
    bots = BotManager(link, SafetyConfig(deadman_ms=500, max_throttle=15), clock=clock)
    bots.on_link(LinkState("connected", "sim", bots_supported=True))
    bots.set_profile(0, {"kind": "corners", "ramp_s": 0, "start": 8, "cap": 12})
    bots.take(0, "profile")
    return bots, link, clock


def tick(bots, positions, geometry=GEO, **kw):
    bots.tick(snap(cars=[{"slot": 0}], **kw), positions=positions, geometry=geometry)


def test_manager_drives_from_positions_and_falls_back_when_stale(env):
    bots, link, _ = env
    tick(bots, good_positions())
    assert link.sent[0] not in (0, 8)
    tick(bots, None)
    assert link.sent[0] == 8  # no positions: target pace
    tick(bots, {**good_positions(), "stale": True})
    assert link.sent[0] == 8
    tick(bots, {**good_positions(), "age_ms": 700})
    assert link.sent[0] == 8
    tick(bots, good_positions())
    assert link.sent[0] not in (0, 8)
    bots.tick(snap(cars=[{"slot": 0}]))  # positions no longer given at all
    assert link.sent[0] == 8
    tick(bots, {"source": "x", "age_ms": 0, "stale": False, "cars": [{"slot": 3, "s": 0.1}]})
    assert link.sent[0] == 8  # positions exist but not for this car


def test_manager_caps_and_zeroes_while_the_car_is_off_track(env):
    bots, link, _ = env
    tick(bots, good_positions(v=0.0))
    assert link.sent[0] == 12  # the profile's cap (standing start on the straight)
    bots.set_profile(0, {"kind": "corners", "ramp_s": 0, "cap": 5})
    tick(bots, good_positions(v=0.0))
    assert link.sent[0] <= 5
    tick(bots, good_positions(off=True))
    assert link.sent[0] == 0


def test_manager_estop_zeroes_and_nothing_restarts_by_itself(env):
    bots, link, _ = env
    tick(bots, good_positions(v=0.0))
    assert link.sent[0] > 0
    bots.estop()
    assert link.estopped
    tick(bots, good_positions(v=0.0))
    assert link.sent[0] == 0 and bots.slots[0].gate == "estop"
    bots.clear_estop()
    tick(bots, good_positions(v=0.0))
    assert link.sent[0] > 0  # E-STOP lifted: the autopilot may drive again (profile reset)


def test_manager_link_loss_session_and_finish_gates(env):
    bots, link, _ = env
    tick(bots, good_positions(v=0.0))
    assert link.sent[0] > 0
    bots.on_link(LinkState("lost", "sim"))
    tick(bots, good_positions(v=0.0))
    assert link.sent[0] == 0 and bots.slots[0].gate == "link"
    bots.on_link(LinkState("connected", "sim", bots_supported=True))
    tick(bots, good_positions(v=0.0), state="grid")
    assert link.sent[0] == 0 and bots.slots[0].gate == "session"
    bots.tick(snap(cars=[{"slot": 0, "finished": True}]), positions=good_positions(v=0.0), geometry=GEO)
    assert link.sent[0] == 0 and bots.slots[0].gate == "finished"


def test_manager_a_failing_profile_never_leaves_the_last_throttle(env):
    class Broken(Profile):
        kind = "broken"

        def throttle(self, ctx):
            raise RuntimeError("bug")

    bots, link, _ = env
    tick(bots, good_positions(v=0.0))
    assert link.sent[0] > 0
    bots.slots[0].profile = Broken()
    tick(bots, good_positions(v=0.0))
    assert link.sent[0] == 0


def test_the_default_factor_ceiling_is_the_cautious_real_car_one():
    """Real track: a corner never learns more than F_MAX (1.2) unless asked; 1.5 is a hard ceiling."""
    p = CornersProfile(a_lat=10)
    assert p.f_max == F_MAX == 1.2
    for _ in range(40):
        p._bump((0, 0), 1.10)
    assert p._f[(0, 0)] == pytest.approx(1.2)
    q = CornersProfile(a_lat=10, f_max=9.0)
    for _ in range(40):
        q._bump((0, 0), 1.10)
    assert q._f[(0, 0)] == pytest.approx(1.5)
