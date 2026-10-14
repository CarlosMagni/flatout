"""Safety of the discovery lap (the only thing in trackloc that can move a car).

Every way the lap can end must leave the car handed back at throttle 0: normal end, exception,
Ctrl-C, stall, E-STOP, lost link, timeout. A fake control and a fake clock: no camera, no RaceLab.
"""

from __future__ import annotations

import threading

import pytest

from trackloc.drive import REFRESH_S, DiscoveryDriver, DriveConfig, MapAborted


class FakeControl:
    def __init__(self, *, session_running=False, fail_act_after=None, fail_claim=False):
        self.calls: list[tuple] = []
        self.session_running = session_running
        self.started_session = False
        self.fail_act_after = fail_act_after
        self.fail_claim = fail_claim
        self.acts = 0

    def ensure_session(self):
        self.calls.append(("ensure_session",))
        if self.session_running:
            return False
        self.started_session = self.session_running = True
        return True

    def stop_session(self):
        self.calls.append(("stop_session",))
        self.session_running = False

    def claim(self, slot):
        self.calls.append(("claim", slot))
        if self.fail_claim:
            raise RuntimeError("slot already claimed")

    def act(self, slot, throttle=None, lane=False):
        self.acts += 1
        if self.fail_act_after is not None and self.acts > self.fail_act_after:
            raise ConnectionError("agent not claimed (E-STOP)")
        self.calls.append(("act", throttle, lane))

    def unclaim(self, slot):
        self.calls.append(("unclaim", slot))

    def estop(self):
        self.calls.append(("estop",))


class Clock:
    def __init__(self):
        self.t = 100.0
        self.hooks = []

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt
        for h in self.hooks:
            h(self.t)


def make(control, cfg=None, *, moving=True, crossing_every=3.0, clock=None, **kw):
    clock = clock or Clock()
    state = {"progress": 0.0, "crossings": 0}
    t0 = clock.t

    def tick(t):
        if moving:
            state["progress"] += 10.0
        state["crossings"] = int((t - t0) / crossing_every) if moving else 0

    clock.hooks.append(tick)
    drv = DiscoveryDriver(control, 2, cfg or DriveConfig(laps=2, lanes=1), progress=lambda: state["progress"],
                          crossings=lambda: state["crossings"], clock=clock.now, sleep=clock.sleep, **kw)
    return drv, clock, state


def released(ctl: FakeControl) -> bool:
    """Handed back: the last throttle sent was 0 and the slot was unclaimed (in that order)."""
    acts = [c for c in ctl.calls if c[0] == "act"]
    names = [c[0] for c in ctl.calls]
    return bool(acts) and acts[-1][1] == 0 and names[-1] in ("unclaim", "stop_session") and "unclaim" in names \
        and names.index("unclaim") > max(i for i, c in enumerate(ctl.calls) if c[0] == "act")


def test_normal_run_zeroes_unclaims_and_ends_its_own_session():
    ctl = FakeControl()
    drv, _, _ = make(ctl)
    out = drv.run()
    assert out["outcome"] == "done" and out["crossings"] >= 3
    assert released(ctl) and ctl.calls[-1] == ("stop_session",) and ctl.started_session


def test_a_session_that_was_already_running_is_left_alone():
    ctl = FakeControl(session_running=True)
    drv, _, _ = make(ctl)
    drv.run()
    assert released(ctl) and ("stop_session",) not in ctl.calls


def test_throttle_is_refreshed_faster_than_the_deadman_switch():
    ctl = FakeControl()
    drv, clock, _ = make(ctl, DriveConfig(laps=3, lanes=1))
    times = []
    orig = ctl.act
    ctl.act = lambda *a, **k: (times.append(clock.t), orig(*a, **k))[1]
    drv.run()
    gaps = [b - a for a, b in zip(times[1:-2], times[2:-1])]  # between the first and the cleanup
    assert gaps and max(gaps) <= REFRESH_S + 0.06 < 0.6  # RaceLab cuts the throttle after 0.6 s of silence


def test_exception_in_the_loop_hands_the_car_back():
    ctl = FakeControl()
    clock = Clock()

    def boom():
        if clock.t > 101.0:
            raise RuntimeError("vision crashed")
        return 0.0

    drv = DiscoveryDriver(ctl, 2, DriveConfig(laps=5, lanes=1), progress=boom, crossings=lambda: 0,
                          clock=clock.now, sleep=clock.sleep)
    with pytest.raises(MapAborted, match="vision crashed"):
        drv.run()
    assert released(ctl) and ctl.calls[-1] == ("stop_session",)


def test_ctrl_c_hands_the_car_back():
    ctl = FakeControl()
    clock = Clock()

    def sleep(dt):
        clock.sleep(dt)
        if clock.t > 101.0:
            raise KeyboardInterrupt

    drv = DiscoveryDriver(ctl, 2, DriveConfig(laps=5, lanes=1), progress=lambda: clock.t * 100, crossings=lambda: 0,
                          clock=clock.now, sleep=sleep)
    with pytest.raises(KeyboardInterrupt):
        drv.run()
    assert released(ctl)


def test_stall_bumps_the_throttle_then_estops_at_the_cap():
    ctl = FakeControl()
    drv, _, _ = make(ctl, DriveConfig(throttle=4, throttle_cap=6, laps=2, lanes=1, stall_s=1.5), moving=False)
    with pytest.raises(MapAborted, match="E-STOP"):
        drv.run()
    throttles = [c[1] for c in ctl.calls if c[0] == "act" and c[1]]
    assert throttles[0] == 4 and 5 in throttles and 6 in throttles and max(throttles) == 6  # never above the cap
    assert ("estop",) in ctl.calls and released(ctl)
    assert drv.bumps == 2


def test_stall_that_clears_after_a_bump_continues_and_finishes():
    ctl = FakeControl()
    clock = Clock()
    state = {"p": 0.0, "n": 0}

    def tick(t):
        if drv.throttle >= 5:  # the car only starts to move at 5
            state["p"] += 10
            state["n"] = int((t - start) / 2.0)

    start = clock.t
    clock.hooks.append(tick)
    drv = DiscoveryDriver(ctl, 2, DriveConfig(throttle=4, laps=2, lanes=1), progress=lambda: state["p"],
                          crossings=lambda: state["n"], clock=clock.now, sleep=clock.sleep)
    out = drv.run()
    assert out["outcome"] == "done" and out["bumps"] >= 1 and released(ctl) and ("estop",) not in ctl.calls


def test_estop_mid_map_aborts_and_never_claims_again():
    ctl = FakeControl(fail_act_after=6)  # act is refused from now on, as after an E-STOP
    drv, _, _ = make(ctl, DriveConfig(laps=5, lanes=1))
    with pytest.raises(MapAborted, match="taken from us"):
        drv.run()
    assert [c for c in ctl.calls if c[0] == "claim"] == [("claim", 2)]  # one claim, never a second
    assert ("unclaim", 2) in ctl.calls  # still handed back


def test_lost_link_aborts_cleanly():
    ctl = FakeControl(fail_act_after=0)
    drv, _, _ = make(ctl)
    with pytest.raises(MapAborted):
        drv.run()
    assert ctl.calls[-1] == ("stop_session",) and ("claim", 2) in ctl.calls


def test_failed_claim_still_ends_the_session_we_started():
    ctl = FakeControl(fail_claim=True)
    drv, _, _ = make(ctl)
    with pytest.raises(MapAborted, match="already claimed"):
        drv.run()
    assert ctl.calls[-1] == ("stop_session",) and not any(c[0] == "act" for c in ctl.calls)


def test_cleanup_failures_do_not_skip_the_other_steps():
    ctl = FakeControl()
    orig_act = ctl.act
    calls = []

    def act(slot, throttle=None, lane=False):
        if throttle == 0:
            calls.append("zero-failed")
            raise ConnectionError("gone")
        return orig_act(slot, throttle, lane)

    ctl.act = act
    drv, _, _ = make(ctl)
    drv.run()
    assert calls == ["zero-failed"] and ("unclaim", 2) in ctl.calls and ctl.calls[-1] == ("stop_session",)


def test_timeout_and_no_crossing_abort():
    ctl = FakeControl()
    drv, _, _ = make(ctl, DriveConfig(laps=9, lanes=1, timeout_s=20.0), crossing_every=100.0)
    with pytest.raises(MapAborted):
        drv.run()
    assert released(ctl)


def test_lane_change_is_armed_once_after_crossings_2_and_4_only_with_two_lanes():
    ctl = FakeControl()
    drv, _, _ = make(ctl, DriveConfig(laps=4, lanes=2))
    drv.run()
    assert sum(1 for c in ctl.calls if c[0] == "act" and c[2]) == 2
    ctl = FakeControl()
    drv, _, _ = make(ctl, DriveConfig(laps=4, lanes=1))
    drv.run()
    assert not any(c[0] == "act" and c[2] for c in ctl.calls)


def test_stop_event_hands_the_car_back():
    ctl = FakeControl()
    stop = threading.Event()
    drv, clock, _ = make(ctl, DriveConfig(laps=50, lanes=1))
    clock.hooks.append(lambda t: stop.set() if t > 101 else None)
    with pytest.raises(MapAborted, match="stopped"):
        drv.run(stop)
    assert released(ctl)
