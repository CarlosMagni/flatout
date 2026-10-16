"""Observation: contract, gaps, changer freedom, clean laps, unknown inputs."""

from __future__ import annotations

import numpy as np

from strategist.observation import OBS_DIM, ObsState

LEN = 18.0
GEO = {"length_m": LEN, "changers": [0.385, 0.60], "pit": [0.62, 0.72], "finish": 0.0}


def state(own, others=(), fuel=10, laps=3, pit=False, fpl=1.0, positions=True, target=10):
    cars = [{"slot": 0, "laps": laps, "fuel": fuel, "pit": pit}]
    pos = [{"slot": 0, "s": own[0], "lane": own[1], "v": own[2], "off": False}]
    for slot, s, lane, v in others:
        cars.append({"slot": slot, "laps": laps, "fuel": 10, "pit": False})
        pos.append({"slot": slot, "s": s, "lane": lane, "v": v, "off": False})
    return {"session": {"state": "running", "laps": target, "remaining_s": None},
            "cu": {"phase": 0, "fuel_mode": True, "pit_lane": True},
            "cars": cars,
            "bots": [{"slot": 0, "brain": {"fuel_per_lap": fpl}}],
            "positions": {"stale": not positions, "age_ms": 0, "cars": pos if positions else []}}


def obs_state():
    st = ObsState()
    st.set_slot(0)
    return st


def test_vector_shape_and_dtype():
    v = obs_state().build(state((0.05, 0, 3.0)), GEO)
    assert v.shape == (OBS_DIM,) and v.dtype == np.float32


def test_gaps_ahead_behind_and_lanes():
    v = obs_state().build(state((0.10, 0, 3.0),
                                others=[(1, 0.20, 0, 2.0), (2, 0.30, 1, 2.5), (3, 0.05, 0, 2.2)]), GEO)
    assert abs(v[9] - 0.10 * LEN / 10.0) < 1e-4    # same lane ahead: 1.8 m
    assert abs(v[10] - 2.0 / 5.0) < 1e-4
    assert abs(v[11] - 0.20 * LEN / 10.0) < 1e-4   # other lane ahead: 3.6 m
    assert abs(v[12] - 2.5 / 5.0) < 1e-4
    assert abs(v[13] - 0.05 * LEN / 10.0) < 1e-4   # behind: 0.9 m
    assert abs(v[14] - 2.2 / 5.0) < 1e-4


def test_other_lane_free_at_the_next_changer():
    v = obs_state().build(state((0.35, 0, 3.0), others=[(1, 0.387, 1, 2.0)]), GEO)
    assert v[15] == 0.0                            # a car in the other lane right at the changer
    v = obs_state().build(state((0.35, 0, 3.0)), GEO)
    assert v[15] == 1.0
    v = obs_state().build(state((0.35, 0, 3.0)), None)
    assert v[15] == -1.0                           # no geometry: unknown


def test_stale_positions_are_flagged():
    v = obs_state().build(state((0.1, 0, 3.0), positions=False), GEO)
    assert v[9] == 1.0 and v[10] == -1.0 and v[15] == -1.0


def test_laps_fuel_and_clean_lap_memory():
    st = obs_state()
    st.note(state((0.0, 0, 3.0), laps=0), 0.0)
    st.note(state((0.0, 0, 3.0), laps=1), 6.5)     # one clean lap, lane 0: 6500 ms
    v = st.build(state((0.1, 0, 3.0), laps=1, fuel=7, fpl=1.0), GEO)
    assert abs(v[7] - 6500 / 6000) < 1e-4
    assert abs(v[3] - 7 / 15) < 1e-4
    assert abs(v[4] - 1.0 / 5) < 1e-4
    assert abs(v[5] - min(7 / 9, 2.0)) < 1e-4      # laps of fuel left / laps left


def test_a_lap_with_a_pit_visit_is_not_a_clean_lap():
    st = obs_state()
    st.note(state((0.0, 0, 3.0), laps=0), 0.0)
    st.note(state((0.0, 0, 3.0), laps=0, pit=True), 3.0)
    st.note(state((0.0, 0, 3.0), laps=1, pit=True), 6.5)
    assert st.lane_ms == [None, None]


def test_stops_and_pit_allowed_spacing():
    st = ObsState(pit_min_laps=5)
    st.set_slot(0)
    st.note(state((0.0, 0, 3.0), laps=2, pit=False), 0.0)
    st.note(state((0.0, 0, 3.0), laps=2, pit=True), 0.05)
    assert st.stops == 1 and st.pit_lap == 2
    assert st.build(state((0.1, 0, 3.0), laps=3), GEO)[19] == 0.0
    assert st.build(state((0.1, 0, 3.0), laps=7), GEO)[19] == 1.0
    off = dict(state((0.1, 0, 3.0), laps=7))
    off["cu"] = {"phase": 0, "fuel_mode": False, "pit_lane": True}
    assert st.build(off, GEO)[19] == 0.0
