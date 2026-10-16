"""The headless environment: action layout, a race to the end, determinism, spacing, risk."""

from __future__ import annotations

from strategist.env import N_ACTIONS, RaceEnv, action_parts, default_action

CORNERS = {"kind": "brain", "pilot": "corners", "lane": {"policy": "keep"},
           "pit": {"policy": "off", "min_laps": 5}}


def test_action_layout():
    assert N_ACTIONS == 12 and default_action() == 1
    assert [action_parts(a) for a in (0, 1, 2, 3, 6, 11)] == [
        (0, 0, 0.0), (0, 0, 0.5), (0, 0, 1.0), (0, 1, 0.0), (1, 0, 0.0), (1, 1, 1.0)]


def test_a_race_runs_to_the_end():
    info = RaceEnv(CORNERS, seed=3, laps=4, opponents=2).run_episode()
    assert info["laps"] == 4 and not info["truncated"]
    assert 1 <= info["position"] <= 3 and info["race_ms"] > 0
    assert info["dry"] is False


def test_same_seed_same_race():
    a = RaceEnv(CORNERS, seed=11, laps=3, opponents=2).run_episode()
    b = RaceEnv(CORNERS, seed=11, laps=3, opponents=2).run_episode()
    assert (a["position"], a["laps"], a["race_ms"]) == (b["position"], b["laps"], b["race_ms"])


def test_two_seeds_give_valid_races():
    a = RaceEnv(CORNERS, seed=11, laps=3, opponents=2).run_episode()
    b = RaceEnv(CORNERS, seed=12, laps=3, opponents=2).run_episode()
    assert a["laps"] == b["laps"] == 3
    assert 1 <= a["position"] <= 3 and 1 <= b["position"] <= 3


def test_pit_requests_are_spaced_by_min_laps():
    env = RaceEnv(CORNERS, seed=7, laps=8, opponents=1, pit_lane=True)
    env.car.fuel = 2.0  # a human would already be pitting
    obs = env.observe()
    info = {}
    for _ in range(3):
        obs, _r, term, trunc, info = env.step(6 + 3 + 1)  # change lane + pit + risk 0.5
        if term or trunc:
            break
    assert info["pit_requests"] == 1  # min_laps = 5: a single request over three laps


def test_the_live_risk_is_applied_in_place():
    env = RaceEnv(seed=1, laps=1, opponents=1)  # default profile: brain + mpc
    env.step(2)  # risk level 1.0
    assert env.supports_risk and env.profile._model.risk == 1.0
    assert env.profile.pilot_params["risk"] == 1.0


def test_a_generated_track_per_seed():
    from racelab.sim.track import track_for
    a = RaceEnv(CORNERS, seed=5, laps=1, opponents=1, track_per_seed=True)
    assert a.sim.track is track_for(5)            # the episode seed picks the track (cached)
    b = RaceEnv(CORNERS, seed=6, laps=1, opponents=1, track_per_seed=True)
    assert b.sim.track is not a.sim.track         # another seed, another layout
    c = RaceEnv(CORNERS, seed=5, laps=1, opponents=1, track_per_seed=True)
    assert c.sim.track is a.sim.track             # deterministic
    d = RaceEnv(CORNERS, seed=5, laps=1, opponents=1)
    assert d.sim.track.geometry()["length_m"][0] == 18.0   # the default track untouched
