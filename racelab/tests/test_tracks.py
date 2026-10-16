"""Generated tracks: `Track(shape_seed=...)` and `track_for` (cached).

The default track stays exactly the old one (same changers, lane 0 exactly LAP_LENGTH);
generated layouts are scaled the same way, keep drivable critical speeds, and put their
lane changers on the straightest stretches, never in the pit zone."""

from __future__ import annotations

import math

import pytest

from racelab.sim.track import A_MAX, CHANGERS, LAP_LENGTH, PIT, Track, default_track, track_for


def test_generated_tracks_are_scaled_and_drivable():
    for seed in range(12):
        t = Track(shape_seed=seed)
        assert t.lanes[0].length == pytest.approx(LAP_LENGTH, abs=0.02)
        assert t.lanes[1].length > t.lanes[0].length
        assert 2.0 < min(t.lanes[0].vc) <= max(t.lanes[0].vc) <= 99.0
        assert all(math.isfinite(v) for v in t.lanes[0].s)


def test_the_changers_sit_on_straights_outside_the_pit_zone():
    for seed in range(12):
        t = Track(shape_seed=seed)
        assert len(t.changers) == 2 and t.changers[0] < t.changers[1]
        for u in t.changers:
            assert not (PIT[0] - 0.02 <= u <= PIT[1] + 0.02)
            i = int(u * t.n)
            window = [t.lanes[0].vc[(i + d) % t.n] for d in range(-36, 37)]
            assert A_MAX / min(window) ** 2 < 1.0  # straighter than a 1 m radius


def test_two_seeds_give_two_layouts():
    assert Track(shape_seed=1).geometry()["points"] != Track(shape_seed=2).geometry()["points"]


def test_track_for_is_cached_and_deterministic():
    t = track_for(7)
    assert t is track_for(7)
    assert t.changers == Track(shape_seed=7).changers


def test_the_default_track_is_unchanged():
    t = default_track()
    assert t.changers == CHANGERS
    assert t.shape_seed is None
    assert t.lanes[0].length == pytest.approx(LAP_LENGTH, abs=1e-9)
