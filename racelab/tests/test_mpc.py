"""Model-predictive pilot ("mpc") on the virtual CU: it learns, estimates the delay, stays capped,
falls back without positions, and never moves an off-track car."""

from __future__ import annotations

import pytest

from racelab.bots.mpc import MPCProfile
from racelab.bots.profiles import Context

from .test_corners import FRESH, GEO, drive


def test_parameters_are_validated():
    for bad in ({"risk": 2}, {"horizon_s": 0.1}, {"samples": 4}, {"elite": 48}, {"iters": 0}, {"sigma": 0}):
        with pytest.raises(ValueError):
            MPCProfile(**bad)


def test_it_learns_the_track_without_leaving_it():
    p = MPCProfile(a_lat=10, f_max=1.5)
    times, offs, top = drive(p, 15)
    assert offs == 0 and top <= 12
    assert times[0] > 1.2 * min(times)  # careful first laps, then the corners' grip is learned
    assert p._f and all(f > 1.0 for f in p._f.values())


@pytest.mark.parametrize("delay_steps, expected", [(2, 0.1), (4, 0.2)])
def test_it_estimates_the_actuation_delay(delay_steps, expected):
    p = MPCProfile(a_lat=10)
    drive(p, 4, delay_steps=delay_steps)  # the throttle reaches the car delay_steps x 50 ms late
    assert abs(p.latency_est - expected) <= 0.05


def test_stale_positions_fall_back_and_off_track_is_zero():
    p = MPCProfile(start=8, ramp_s=0)
    assert p.output(Context(now=1.0, since_cross_s=None, laps=0, pos={**FRESH, "age_ms": 900}, geometry=GEO)) == 8
    assert p.source == "pace"
    assert p.output(Context(now=1.1, since_cross_s=None, laps=0, pos={**FRESH, "off": True}, geometry=GEO)) == 0
    assert p.source == "map"


def test_the_cap_is_respected():
    p = MPCProfile(a_lat=10, cap=6)
    _, _, top = drive(p, 3)
    assert top <= 6


def test_the_race_brain_lists_and_drives_with_the_mpc_pilot():
    from racelab.bots import brain
    from racelab.bots.profiles import profile_from_dict

    assert {m["name"]: m["available"] for m in brain.pilots_meta()}["mpc"] is True
    b = profile_from_dict({"kind": "brain", "pilot": "mpc"})
    value = b.output(Context(now=1.0, since_cross_s=None, laps=0, pos=FRESH, geometry=GEO))
    assert 0 <= value <= 15
