"""TowerManager: coalescing queue, priorities, pacing, clearing."""

from __future__ import annotations

from racelab.race import RecordingActions, TowerManager


def make(**kw) -> tuple[TowerManager, RecordingActions]:
    actions = RecordingActions()
    return TowerManager(actions, **kw), actions


def drain(tower: TowerManager, actions: RecordingActions, start: float = 0.0, step: float = 0.15, n: int = 20):
    """Run enough ticks to empty the queue."""
    for i in range(n):
        tower.tick(start + i * step)
    return actions.calls


def test_lap_counter_has_priority_then_positions_in_order():
    tower, actions = make()
    tower.update([2, 0, 1], leader_laps=7)
    assert drain(tower, actions) == [
        ("setlap", 7),
        ("setpos", 2, 1),
        ("setpos", 0, 2),
        ("setpos", 1, 3),
    ]


def test_one_operation_per_interval():
    tower, actions = make(min_interval_s=0.15)
    tower.update([0, 1], 1)
    tower.tick(10.0)
    assert len(actions.calls) == 1
    tower.tick(10.05)
    tower.tick(10.149)
    assert len(actions.calls) == 1
    tower.tick(10.15)
    assert len(actions.calls) == 2
    tower.tick(10.30)
    assert len(actions.calls) == 3
    tower.tick(10.45)
    assert len(actions.calls) == 3  # nothing left to send


def test_coalescing_only_latest_desired_state_is_sent():
    tower, actions = make()
    tower.update([0, 1, 2], 1)
    tower.update([1, 2, 0], 2)  # replaces the desired state before anything is sent
    tower.update([2, 1, 0], 3)
    assert drain(tower, actions) == [
        ("setlap", 3),
        ("setpos", 2, 1),
        ("setpos", 1, 2),
        ("setpos", 0, 3),
    ]


def test_only_differences_are_sent():
    tower, actions = make()
    tower.update([0, 1, 2], 1)
    drain(tower, actions)
    actions.clear()
    tower.update([1, 0, 2], 1)  # 0 and 1 swap places
    assert drain(tower, actions, start=10.0) == [("setpos", 1, 1), ("setpos", 0, 2)]
    actions.clear()
    tower.update([1, 0, 2], 1)  # nothing new
    assert drain(tower, actions, start=20.0) == []


def test_new_lap_counter_goes_before_pending_positions():
    tower, actions = make()
    tower.update([0, 1, 2], 1)
    tower.tick(0.0)  # setlap(1)
    tower.tick(0.15)  # setpos(0, 1)
    tower.update([0, 1, 2], 2)  # the leader completed one more lap
    tower.tick(0.30)
    assert actions.calls[-1] == ("setlap", 2)
    tower.tick(0.45)
    assert actions.calls[-1] == ("setpos", 1, 2)


def test_lap_counter_wraps_like_carreralib():
    tower, actions = make()
    tower.update([0], 253)
    tower.tick(0.0)
    assert actions.calls == [("setlap", 3)]


def test_positions_beyond_eight_are_ignored():
    tower, actions = make()
    tower.update(list(range(8)) + [8, 9], 0)
    calls = drain(tower, actions, n=30)
    assert [c for c in calls if c[0] == "setpos"] == [("setpos", a, a + 1) for a in range(8)]


def test_clear_is_immediate_and_forgets_everything():
    tower, actions = make()
    tower.update([0, 1], 4)
    drain(tower, actions)
    actions.clear()
    tower.clear()
    assert actions.calls == [("clrpos",)]  # without waiting for a tick
    assert drain(tower, actions, start=50.0) == [("clrpos",)]  # desired state forgotten too
    tower.update([1, 0], 4)  # the same counter must be sent again: the sent state is forgotten
    assert drain(tower, actions, start=60.0)[1:] == [("setlap", 4), ("setpos", 1, 1), ("setpos", 0, 2)]


def test_disabled_tower_does_nothing():
    tower, actions = make(enabled=False)
    tower.update([0, 1], 3)
    tower.clear()
    assert drain(tower, actions) == []
