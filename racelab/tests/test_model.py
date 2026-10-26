"""BotModel (racelab/drive/model.py): the bot rules the CU link and the controller interfaces share,
checked with times handed in, so a 100 ms heartbeat cannot pass for a 150 ms one."""

from __future__ import annotations

import time

import pytest

from racelab.config import SafetyConfig
from racelab.drive.model import HB_PAUSE_MAX_S, BotModel, Frame

NONE6 = (None,) * 6


def make(heartbeat_s=0.1, min_gap_s=0.02, wake=None, **safety):
    return BotModel(SafetyConfig(**safety), heartbeat_s=heartbeat_s, min_gap_s=min_gap_s, wake=wake)


def send(m, now, **kw):
    """One service cycle: the frame decided at `now`, recorded as sent."""
    frame = m.frame(now, **kw)
    if frame is not None:
        m.sent(frame, now)
    return frame


def started(m, now=0.0):
    """A model whose device has just been opened: the zero frame is out."""
    m.forget()
    assert send(m, now).prev is None
    return now


def test_first_frame_starts_the_bots_from_zero_whatever_was_asked():
    m = make()
    m.set_bot(1, 9)
    m.lane(1)
    f = m.frame(1.0)
    assert f == Frame((None, 0, None, None, None, None), frozenset(), None)
    assert m.frame(1.001).prev is None                      # not recorded until `sent`: still the zero frame
    m.sent(f, 1.0)
    assert m.frame(1.019) is None                           # the change (0 -> 9) waits for the gap
    assert m.frame(1.021).values == (None, 9, None, None, None, None)


def test_a_change_goes_out_at_once_but_not_closer_than_the_min_gap():
    m = make(min_gap_s=0.07)
    t = started(m)
    m.set_bot(0, 5)
    assert send(m, t + 0.069) is None
    assert send(m, t + 0.071).values[0] == 5
    m.set_bot(0, 6)
    m.set_bot(0, 7)                                         # coalesced: only the latest goes out
    assert send(m, t + 0.139) is None
    assert send(m, t + 0.142).values[0] == 7


@pytest.mark.parametrize("heartbeat_s", [0.1, 0.3])
def test_an_unchanged_frame_repeats_every_heartbeat_while_a_bot_is_driven(heartbeat_s):
    m = make(heartbeat_s=heartbeat_s)
    t = started(m)
    m.set_bot(0, 5)
    t += 0.05
    assert send(m, t).values[0] == 5
    assert send(m, t + heartbeat_s - 0.002) is None
    assert send(m, t + heartbeat_s + 0.001) is not None
    t += heartbeat_s + 0.001
    assert send(m, t + heartbeat_s - 0.002) is None
    assert send(m, t + heartbeat_s + 0.001) is not None


def test_nothing_is_repeated_when_no_bot_is_driven():
    m = make()
    t = started(m)
    assert send(m, t + 10) is None
    m.set_bot(0, 5)
    send(m, t + 11)
    m.set_bot(0, None)                                      # handed back: the change goes out once
    assert send(m, t + 11.05).values == NONE6
    assert send(m, t + 20) is None


def test_lane_press_is_held_from_the_press_and_only_for_a_bot():
    m = make(heartbeat_s=10)
    t = started(m)
    m.set_bot(0, 5)
    send(m, t + 0.05)
    m.lane(3)                                               # not a bot: ignored
    assert send(m, t + 1.0) is None
    m.lane(0)
    f = send(m, t + 1.1)
    assert f.lanes == {0}
    assert send(m, t + 1.1 + 0.12 - 0.002) is None   # still held, nothing new to say
    f = send(m, t + 1.1 + 0.12 + 0.001)
    assert f.lanes == frozenset() and f.values[0] == 5


def test_the_hold_counts_from_the_frame_that_carries_the_press():
    """A press right behind a frame waits for the gap; the button is down 120 ms from its own frame."""
    m = make(min_gap_s=0.07, heartbeat_s=10)
    started(m)
    m.set_bot(0, 5)
    send(m, 0.08)
    m.set_bot(0, 6)
    m.lane(0)
    assert send(m, 0.09) is None                            # the press waits for its frame
    f = send(m, 0.151)
    assert f.lanes == {0}
    assert send(m, 0.151 + 0.118) is None                   # still down: 120 ms from this frame
    assert send(m, 0.151 + 0.121).lanes == frozenset()


def test_a_press_is_not_lost_to_a_long_gap():
    """With a gap longer than the hold, the press still gets a frame to ride."""
    m = make(min_gap_s=0.15, heartbeat_s=10)
    started(m)
    m.set_bot(0, 5)
    send(m, 0.16)
    m.lane(0)
    assert send(m, 0.17) is None
    assert send(m, 0.32).lanes == {0}
    assert send(m, 0.32 + 0.16).lanes == frozenset()


def test_a_press_before_a_reconnection_does_not_fire_after_it():
    m = make()
    t = started(m)
    m.set_bot(0, 5)
    m.lane(0)
    m.forget()
    send(m, t + 1.0)
    f = send(m, t + 1.1)
    assert f.values[0] == 5 and f.lanes == frozenset()


def test_a_press_on_a_slot_handed_back_meanwhile_is_dropped():
    m = make()
    t = started(m)
    m.set_bot(0, 5)
    send(m, t + 0.05)
    m.lane(0)
    m.set_bot(0, None)
    f = send(m, t + 0.1)
    assert f.values == NONE6 and f.lanes == frozenset()


def test_estop_goes_out_first_and_holds_every_bot_at_zero():
    m = make(min_gap_s=0.5)
    t = started(m)
    m.set_bot(0, 7)
    m.set_bot(4, 3)
    send(m, t + 0.51)
    m.set_bot(0, 9)
    assert send(m, t + 0.52) is None                        # the gap holds a plain change back
    m.estop()
    f = send(m, t + 0.53)                                   # ... not an E-STOP
    assert f.values == (0, None, None, None, 0, None)
    assert m.holds == {"estop"}
    m.set_bot(0, 9)                                         # refused while it is engaged
    assert m.targets()[0] == 0
    m.clear_estop()
    m.set_bot(0, 9)
    assert m.targets()[0] == 9


def test_estop_with_no_bot_still_sends_a_frame_and_holds():
    m = make()
    t = started(m)
    m.estop()
    assert send(m, t + 0.001) is not None
    assert m.holds == {"estop"}


def test_hold_at_zero_holds_only_when_a_bot_was_driven_and_resume_lifts_it():
    m = make()
    m.hold_at_zero()
    assert m.holds == frozenset()
    m.set_bot(2, 8)
    m.hold_at_zero()
    assert m.holds == {"reconnect"} and m.targets()[2] == 0
    m.set_bot(2, 8)                                         # the app insists: 0
    assert m.targets()[2] == 0
    m.estop()
    m.resume()
    assert m.holds == {"estop"}
    m.clear_estop()
    m.set_bot(2, 8)
    assert m.targets()[2] == 8


def test_clear_drops_everything_pending_but_keeps_an_estop():
    m = make()
    m.set_bot(0, 5)
    m.lane(0)
    m.hold_at_zero()
    m.estop()
    m.clear()
    assert m.targets() == [None] * 6 and m.holds == {"estop"}
    assert m.take() == (True, set(), [None] * 6)             # the press went, the E-STOP flag did not


def test_take_hands_over_the_estop_and_the_presses_once():
    m = make()
    m.set_bot(0, 5)
    m.set_bot(1, 6)
    m.lane(0)
    m.estop()
    assert m.take() == (True, {0}, [0, 0, None, None, None, None])
    assert m.take() == (False, set(), [0, 0, None, None, None, None])


def test_forget_makes_the_next_frame_a_zero_frame_again():
    m = make()
    t = started(m)
    m.set_bot(0, 5)
    send(m, t + 0.05)
    m.lane(0)
    send(m, t + 0.1)
    m.forget()
    f = send(m, t + 0.2)
    assert f == Frame((0, None, None, None, None, None), frozenset(), None)


# ------------------------------------------------------------ the heartbeat pause (hand-back test)

def test_pause_holds_the_idle_heartbeat_but_not_changes_or_estop():
    m = make()
    t = time.monotonic()
    started(m, t)
    m.set_bot(1, 4)
    send(m, t + 0.05)
    m.pause_heartbeat(2.0)
    assert send(m, t + 1.0) is None                         # an unchanged frame is not repeated
    m.set_bot(1, 6)
    assert send(m, t + 1.1).values[1] == 6                  # a change goes out
    m.estop()
    assert send(m, t + 1.2).values[1] == 0
    assert m.hb_pause_until == 0.0                          # and the pause is gone
    assert send(m, t + 1.2 + 0.11).values[1] == 0           # heartbeat is back


def test_a_pause_that_runs_out_zeroes_the_bots_and_never_resumes_them():
    m = make()
    t = time.monotonic()
    started(m, t)
    m.set_bot(1, 4)
    send(m, t + 0.05)
    m.pause_heartbeat(0.5)
    assert send(m, t + 0.4) is None
    f = send(m, t + 0.7)
    assert f.values[1] == 0 and m.targets()[1] == 0 and m.hb_pause_until == 0.0


def test_pause_is_capped_and_resume_ends_it():
    m = make()
    m.pause_heartbeat(60)
    assert m.hb_pause_until - time.monotonic() <= HB_PAUSE_MAX_S
    m.resume_heartbeat()
    assert m.hb_pause_until == 0.0


def test_link_loss_and_open_end_a_pause():
    m = make()
    m.set_bot(0, 4)
    m.pause_heartbeat(2.0)
    m.hold_at_zero()
    assert m.hb_pause_until == 0.0
    m.pause_heartbeat(2.0)
    m.forget()
    assert m.hb_pause_until == 0.0


def test_a_probe_sends_an_unchanged_frame_only_while_idle_and_once_the_gap_allows():
    m = make()
    t = started(m)
    assert send(m, t + 0.01, probe=True) is None
    assert send(m, t + 0.021, probe=True) is not None
    m.set_bot(0, 5)
    send(m, t + 0.1)
    assert send(m, t + 0.12, probe=True) is None            # a bot is driven: heartbeat rules, not the probe


# ------------------------------------------------------------ the rest

def test_throttle_is_clamped_and_slots_validated():
    m = make(max_throttle=10)
    m.set_bot(0, 99)
    m.set_bot(1, -4)
    m.set_bot(2, 7.9)
    assert m.targets()[:3] == [10, 0, 7]
    for bad in (-1, 6):
        with pytest.raises(ValueError):
            m.set_bot(bad, 1)
        with pytest.raises(ValueError):
            m.lane(bad)


def test_wake_is_called_for_what_the_app_asks_not_for_lifting_a_hold():
    woken = []
    m = make(wake=lambda: woken.append(1))
    for call in (lambda: m.set_bot(0, 1), lambda: m.lane(0), m.estop,
                 lambda: m.pause_heartbeat(1), m.resume_heartbeat):
        woken.clear()
        call()
        assert woken, call
    woken.clear()
    m.clear_estop()
    m.resume()
    assert not woken


def test_final_frame_hands_back_or_holds_at_zero():
    m = make()
    t = started(m)
    m.set_bot(1, 6)
    send(m, t + 0.05)
    assert m.final_frame() == Frame(NONE6, frozenset(), ((None, 6, None, None, None, None), frozenset()))
    hold = make(release_on_exit=False)
    hold.set_bot(1, 6)
    hold.set_bot(3, None)
    assert hold.final_frame().values == (None, 0, None, None, None, None)


def test_changes_are_what_the_recording_needs():
    z = (None,) * 6
    first = Frame((None, 0, None, None, None, None), frozenset(), None)
    assert first.changes() == [(1, 0, False)]               # only what is driven
    assert Frame(z, frozenset(), None).changes() == []
    prev = ((None, 7, None, None, None, None), frozenset())
    assert Frame((None, 7, None, None, None, None), frozenset(), prev).changes() == []     # a heartbeat
    assert Frame((None, 7, None, None, None, None), frozenset({1}), prev).changes() == [(1, 7, True)]
    assert Frame((None, 9, None, None, None, None), frozenset(), prev).changes() == [(1, 9, False)]
    assert Frame(z, frozenset(), prev).changes() == [(1, None, False)]                      # handed back, once
    assert Frame(z, frozenset(), (z, frozenset())).changes() == []
