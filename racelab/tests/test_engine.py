"""RaceEngine: laps, lights start, pauses, finish, standings, sessions."""

from __future__ import annotations

import json
import random
import re

import pytest

from racelab.config import DriverConfig, RaceConfig, default_drivers
from racelab.events import (
    BUTTON_START,
    FUEL_MODE,
    LAP_COUNTER_MODE,
    PIT_LANE_MODE,
    StatusMsg,
    TimerMsg,
)
from racelab.race import (
    SESSION_TYPES,
    RaceEngine,
    RecordingActions,
    SessionSettings,
    TowerManager,
)

START = ("press", BUTTON_START)
B = 10_000  # the CU timestamps in the tests start at 10 s, after the GO


class Harness:
    """Engine + hand-driven clock. Convention: host_t (s) = CU timestamp (ms) / 1000."""

    def __init__(self, drivers: int | list[DriverConfig] = 3, *, cfg: RaceConfig | None = None, tower: bool = False):
        self.now = 0.0
        self.actions = RecordingActions()
        self.events: list[tuple[str, dict]] = []
        self.cfg = cfg or RaceConfig()
        if isinstance(drivers, int):
            drivers = [DriverConfig(i, f"P{i}", f"#00000{i}") for i in range(drivers)]
        self.tower = TowerManager(self.actions) if tower else None
        self.engine = RaceEngine(
            drivers,
            self.cfg,
            self.actions,
            tower=self.tower,
            on_event=lambda name, data: self.events.append((name, data)),
            clock=lambda: self.now,
            wall_clock=lambda: 1_790_000_000.0,
        )

    # inputs --------------------------------------------------------------
    def configure(self, **settings) -> None:
        self.engine.configure(SessionSettings(**settings))

    def status(self, phase: int = 0, *, at: float | None = None, fuel=None, pit=None, mode=FUEL_MODE | PIT_LANE_MODE):
        if at is not None:
            self.now = at
        self.engine.on_status(
            StatusMsg(
                fuel=tuple(fuel or (15,) * 8),
                start=phase,
                mode=mode,
                pit=tuple(pit or (False,) * 8),
                display=6,
                host_t=self.now,
            )
        )

    def cross(self, slot: int, ts: int, sector: int = 1, *, host: float | None = None) -> None:
        self.now = max(self.now, ts / 1000) if host is None else host
        self.engine.on_timer(TimerMsg(address=slot, timestamp=ts, sector=sector, host_t=self.now))

    def tick(self, at: float) -> None:
        self.now = at
        self.engine.tick(at)

    # scenarios -----------------------------------------------------------
    def light_start(self) -> None:
        """start() then the CU sequence 0 -> 1 -> 2..6 -> 7 -> 0; GO at t = 7.0 s."""
        self.now = 0.0
        self.engine.start()
        self.status(1, at=0.2)
        self.tick(1.0)  # automatic 2nd press (countdown)
        for i, phase in enumerate(range(2, 7)):
            self.status(phase, at=2.0 + i)
        self.status(7, at=7.0)
        self.status(0, at=7.5)

    def plain_start(self, at: float = 0.0) -> None:
        self.now = at
        self.engine.start()

    # reading -------------------------------------------------------------
    def named(self, name: str) -> list[dict]:
        return [data for n, data in self.events if n == name]

    def states(self) -> list[str]:
        return [d["state"] for d in self.named("session")]

    def snap(self) -> dict:
        return self.engine.snapshot()

    def car(self, slot: int) -> dict:
        return next(c for c in self.snap()["cars"] if c["slot"] == slot)

    def order(self) -> list[int]:
        return [c["slot"] for c in self.snap()["cars"] if c["position"] is not None]


def race3(laps: int = 3, **cfg) -> Harness:
    """3 drivers, lights race, GO already passed."""
    h = Harness(3, cfg=RaceConfig(**cfg))
    h.configure(type="race", laps=laps)
    h.light_start()
    return h


# ====================================================================== sessions without lights


def test_practice_lap_counting_dedupe_and_debounce():
    h = Harness(2)
    h.configure(type="practice")
    assert h.engine.state == "grid"
    h.plain_start()
    assert h.engine.state == "running"

    h.cross(0, B)  # reference pass: not a lap
    assert h.car(0)["laps"] == 0 and h.named("lap") == []
    assert h.named("crossing") == [{"slot": 0, "sector": 1, "cu_ts": B}]

    h.cross(0, B + 5300)
    (lap,) = h.named("lap")
    assert lap == {
        "slot": 0, "lap": 1, "ms": 5300, "neutralized": False,
        "personal_best": True, "session_best": True, "position": 1,
    }  # fmt: skip

    h.cross(0, B + 5300)  # exact duplicate (same address, same timestamp)
    h.cross(0, B + 5900)  # bounce: 600 ms < min_lap_ms
    assert h.car(0)["laps"] == 1 and len(h.named("lap")) == 1

    h.cross(0, B + 10100)  # the ignored bounce did not move the reference
    assert h.named("lap")[-1]["ms"] == 4800
    car = h.car(0)
    assert (car["laps"], car["last_ms"], car["best_ms"], car["best_lap"]) == (2, 4800, 4800, 2)
    assert car["recent"] == [5300, 4800] and car["last_pb"] and car["last_sb"]
    assert h.snap()["session"]["best"] == {"slot": 0, "ms": 4800, "lap": 2}


def test_best_lap_flags_personal_vs_session_best():
    h = Harness(2)
    h.configure(type="practice")
    h.plain_start()
    h.cross(0, B)
    h.cross(1, B + 100)
    h.cross(0, B + 5000)  # P0: 5000, record
    h.cross(1, B + 5300)  # P1: 5200, PB but not a record
    h.cross(0, B + 10500)  # P0: 5500, no PB
    laps = h.named("lap")
    assert [(l["slot"], l["ms"], l["personal_best"], l["session_best"]) for l in laps] == [
        (0, 5000, True, True),
        (1, 5200, True, False),
        (0, 5500, False, False),
    ]
    assert h.car(0)["last_pb"] is False and h.car(1)["last_pb"] is True


def test_cu_timestamp_32_bit_wrap():
    h = Harness(2)
    h.configure(type="practice")
    h.plain_start()
    wrap = 2**32
    h.cross(0, wrap - 3000, host=1.0)
    h.cross(0, 2400, host=6.4)  # the CU counter wrapped
    assert h.named("lap")[0]["ms"] == 5400
    assert h.engine.laps(0)[0]["cu_ts"] == 2400
    h.cross(0, 7900, host=11.9)
    assert h.named("lap")[1]["ms"] == 5500


def test_race_order_survives_the_wrap():
    h = race3(laps=10)
    wrap = 2**32
    h.cross(0, wrap - 4000, host=10.0)  # references before the CU zero
    h.cross(1, wrap - 3800, host=10.2)
    h.cross(0, wrap + 1000 - wrap, host=15.0)  # P0: 5000, after the zero
    h.cross(1, 1500, host=15.5)  # P1: 5300
    assert h.order() == [0, 1]
    car1 = h.car(1)
    assert (car1["gap_ms"], car1["interval_ms"]) == (500, 500)
    h.cross(1, 6500, host=20.5)  # P1 completes its 2nd lap before P0
    h.cross(0, 6600, host=20.6)
    assert h.order() == [1, 0]  # P1: 2 laps at 6500, P0: 2 laps at 6600


def test_crossings_ignored_outside_counting_states():
    h = Harness(2)
    h.cross(0, B)  # idle
    h.configure(type="practice")
    h.cross(0, B + 100)  # grid
    h.plain_start()
    assert h.car(0)["laps"] == 0 and h.named("crossing") == []
    h.cross(0, B + 200)  # reference
    h.cross(0, B + 5200)
    h.engine.stop()
    h.cross(0, B + 10200)  # finished
    assert h.car(0)["laps"] == 1


def test_race_cfg_min_lap_is_read_live():
    h = Harness(1, cfg=RaceConfig(min_lap_ms=100))
    h.configure(type="practice")
    h.plain_start()
    h.cross(0, B)
    h.cross(0, B + 500)  # 500 ms >= 100 ms: counted
    assert h.car(0)["laps"] == 1
    h.cfg.min_lap_ms = 2000
    h.cross(0, B + 1000)  # 500 ms < 2000 ms
    assert h.car(0)["laps"] == 1


def test_grid_behind_line_first_crossing_is_the_reference():
    h = Harness(1, cfg=RaceConfig(grid_behind_line=True))
    h.configure(type="practice")
    h.plain_start(at=5.0)
    h.cross(0, B)
    assert h.car(0)["laps"] == 0 and h.car(0)["position"] == 1
    h.cross(0, B + 6000)
    assert h.car(0)["laps"] == 1 and h.car(0)["best_ms"] == 6000


def test_grid_not_behind_line_first_crossing_is_a_neutralized_lap_timed_on_host_clock():
    h = Harness(1, cfg=RaceConfig(grid_behind_line=False))
    h.configure(type="practice")
    h.plain_start(at=5.0)
    h.cross(0, B, host=11.2)  # 6.2 s after the start
    (lap,) = h.named("lap")
    assert lap["lap"] == 1 and lap["ms"] == 6200 and lap["neutralized"] is True
    assert lap["personal_best"] is False
    assert h.car(0)["laps"] == 1 and h.car(0)["best_ms"] is None
    h.cross(0, B + 5000, host=16.2)  # next lap: exact CU time
    assert h.named("lap")[1]["ms"] == 5000 and h.named("lap")[1]["personal_best"] is True


def test_sector_splits_are_cumulative_from_the_lap_start():
    h = Harness(1)
    h.configure(type="practice")
    h.plain_start()
    h.cross(0, B + 1500, sector=2)  # no reference lap yet: ignored
    assert h.named("sector") == []
    h.cross(0, B)
    assert h.car(0)["sectors"] == [None, None, None]
    h.cross(0, B + 2500, sector=2)
    h.cross(0, B + 4200, sector=3)
    assert h.named("sector") == [
        {"slot": 0, "sector": 2, "ms": 2500},
        {"slot": 0, "sector": 3, "ms": 4200},
    ]
    assert h.car(0)["sectors"] == [None, 2500, 4200]  # the lap is not completed yet
    assert h.car(0)["laps"] == 0  # sectors 2-3 do not count a lap
    h.cross(0, B + 5000)
    assert h.car(0)["sectors"] == [5000, None, None]  # new lap: S1 = the lap that just finished
    h.cross(0, B + 7400, sector=2)
    assert h.car(0)["sectors"] == [5000, 2400, None]
    h.cross(0, B + 7400, sector=2)  # duplicate
    assert len(h.named("sector")) == 3


def test_since_cross_ms_uses_host_time():
    h = Harness(1)
    h.configure(type="practice")
    h.plain_start()
    assert h.car(0)["since_cross_ms"] is None
    h.cross(0, B)
    h.now = 11.83
    assert h.car(0)["since_cross_ms"] == 1830


# ============================================================ lights start


def test_race_start_sequence_follows_the_cu_phase():
    h = Harness(3)
    h.configure(type="race", laps=3)
    h.now = 0.0
    h.engine.start()
    assert h.engine.state == "starting"
    assert h.actions.of("press") == [(BUTTON_START,)]  # phase 0: arms the CU
    h.status(1, at=0.2)
    h.tick(0.5)
    assert len(h.actions.of("press")) == 1  # not yet 0.8 s of arming
    h.tick(1.0)  # automatic 2nd press
    assert len(h.actions.of("press")) == 2
    for i, phase in enumerate(range(2, 7)):
        h.status(phase, at=2.0 + i)
    assert h.engine.state == "starting"
    assert h.snap()["cu"]["phase"] == 6 and h.snap()["cu"]["lights"] == 5
    h.cross(0, 6500)  # during the countdown: ignored
    assert h.named("crossing") == []
    h.status(7, at=7.0)  # GO
    assert h.engine.state == "running"
    assert h.snap()["session"]["flag"] == "green"
    h.status(0, at=7.5)
    assert h.engine.state == "running"
    assert h.states() == ["grid", "starting", "running"]
    assert [p["phase"] for p in h.named("phase")] == [1, 2, 3, 4, 5, 6, 7, 0]
    assert h.named("phase")[0] == {"phase": 1, "lights": 5, "blink": False}
    h.now = 17.0
    assert h.snap()["session"]["elapsed_s"] == 10.0  # counted from the GO


def test_go_is_also_detected_when_phase_goes_straight_to_zero():
    h = Harness(2)
    h.configure(type="race", laps=3)
    h.engine.start()
    h.status(1, at=0.1)
    h.status(6, at=1.0)
    h.status(0, at=2.0)
    assert h.engine.state == "running"
    h.now = 5.0
    assert h.snap()["session"]["elapsed_s"] == 3.0


def test_cu_disarmed_during_start_is_not_a_go():
    h = Harness(2)
    h.configure(type="race", laps=3)
    h.engine.start()
    h.status(1, at=0.2)
    h.status(0, at=0.5)  # armed then released (1 -> 0): not a start
    assert h.engine.state == "starting"
    h.tick(2.0)  # the CU stayed at 0: arm it again
    assert len(h.actions.of("press")) == 2
    h.status(1, at=2.2)
    h.status(7, at=2.5)  # 1 -> 7 does not exist on the CU: not a start either
    assert h.engine.state == "starting"


def test_auto_countdown_presses_again_while_armed_and_rate_limits():
    h = Harness(2)
    h.configure(type="race", laps=3)
    h.now = 0.0
    h.engine.start()
    h.status(1, at=0.1)
    for t in (0.2, 0.5, 0.85):
        h.tick(t)
    assert len(h.actions.of("press")) == 1  # < 0.8 s of arming
    h.tick(0.95)
    assert len(h.actions.of("press")) == 2
    h.tick(1.5)
    h.tick(2.9)  # the CU does not react: at most one re-press every 2 s
    assert len(h.actions.of("press")) == 2
    h.tick(3.0)
    assert len(h.actions.of("press")) == 3
    h.status(2, at=3.1)  # the CU started the countdown: stop
    h.tick(10.0)
    assert len(h.actions.of("press")) == 3


def test_auto_countdown_can_be_disabled():
    h = Harness(2, cfg=RaceConfig(auto_countdown=False))
    h.configure(type="race", laps=3)
    h.engine.start()
    h.status(1, at=0.1)
    h.tick(5.0)
    assert len(h.actions.of("press")) == 1  # it is up to the operator to press the CU


def test_start_with_cu_already_armed_does_not_press_immediately():
    h = Harness(2)
    h.status(1, at=-10.0)  # CU armed before start()
    h.configure(type="race", laps=3)
    h.now = 0.0
    h.engine.start()
    assert h.actions.of("press") == []
    h.tick(0.7)
    assert h.actions.of("press") == []
    h.tick(0.85)
    assert len(h.actions.of("press")) == 1


def test_start_presses_again_when_cu_does_not_react():
    h = Harness(2)
    h.configure(type="race", laps=3)
    h.now = 0.0
    h.engine.start()
    assert len(h.actions.of("press")) == 1
    h.tick(1.4)
    assert len(h.actions.of("press")) == 1
    h.tick(1.6)  # phase still 0: the CU did not react
    assert len(h.actions.of("press")) == 2
    h.tick(3.0)
    assert len(h.actions.of("press")) == 2  # 2 s spacing
    h.tick(3.7)
    assert len(h.actions.of("press")) == 3
    h.status(1, at=3.8)  # it finally reacts
    h.tick(4.0)
    assert len(h.actions.of("press")) == 3


def test_false_start_returns_to_grid_and_can_restart():
    h = Harness(2)
    h.configure(type="race", laps=3)
    h.now = 0.0
    h.engine.start()
    h.status(1, at=0.2)
    h.status(2, at=1.0)
    h.status(3, at=2.0)
    h.status(8, at=2.5)  # false start
    assert h.engine.state == "grid"
    assert h.named("false_start") == [{}]
    assert h.snap()["session"]["false_start"] is True
    h.status(9, at=3.0)  # phase 8 -> 9 is not a 2nd false start
    assert len(h.named("false_start")) == 1
    h.status(1, at=5.5)  # the CU re-arms
    h.now = 6.0
    h.actions.clear()
    h.engine.start()
    assert h.engine.state == "starting"
    assert h.snap()["session"]["false_start"] is False
    assert h.actions.of("press") == []  # already armed
    h.tick(6.9)
    assert len(h.actions.of("press")) == 1
    for i, phase in enumerate(range(2, 7)):
        h.status(phase, at=7.0 + i)
    h.status(7, at=12.0)
    assert h.engine.state == "running"


def test_start_without_lights_releases_an_armed_cu_without_pausing():
    h = Harness(2)
    h.status(1, at=-1.0)  # CU armed
    h.configure(type="practice")
    h.plain_start(at=0.0)
    assert h.engine.state == "running"
    assert h.actions.of("press") == [(BUTTON_START,)]
    for i, phase in enumerate(range(2, 7)):  # the CU countdown is not a pause
        h.status(phase, at=1.0 + i)
        assert h.engine.state == "running"
    h.status(7, at=6.0)
    h.status(0, at=6.5)
    h.status(1, at=20.0)  # ... but a later Start press is
    assert h.engine.state == "paused"


# ================================================================== pauses and resume


def test_external_pause_then_go_resumes_and_neutralizes_laps():
    h = race3(laps=10)
    h.cross(0, B)
    h.cross(0, B + 5000)  # lap 1, normal
    assert h.named("lap")[0]["neutralized"] is False
    h.status(1, at=16.0)  # someone presses Start on the CU
    assert h.engine.state == "paused"
    assert h.named("session")[-1] == {"state": "paused", "prev": "running", "type": "race", "title": "Race · 10 laps"}
    assert h.snap()["session"]["flag"] == "red"
    h.now = 20.0
    assert h.snap()["session"]["elapsed_s"] == 9.0  # 16 - 7: frozen during the pause
    h.cross(0, B + 10000)  # crossing during the pause: counted, neutralized
    assert h.engine.state == "paused"
    assert h.named("lap")[1]["neutralized"] is True and h.car(0)["laps"] == 2
    for i, phase in enumerate(range(2, 7)):
        h.status(phase, at=17.0 + i + 5)  # 22..26
        assert h.engine.state == "paused"
    h.status(7, at=27.0)  # GO: resume
    assert h.engine.state == "running"
    assert h.named("session")[-1]["prev"] == "paused"
    h.cross(0, B + 20_500)  # lap [20.0; 30.5] that covers the pause: neutralized
    h.cross(0, B + 25_500)  # lap entirely after the resume: normal
    laps = h.engine.laps(0)
    assert [l["neutralized"] for l in laps] == [False, True, True, False]
    assert [l["personal_best"] for l in laps] == [True, False, False, False]
    assert h.car(0)["best_ms"] == 5000
    h.now = 40.0
    assert h.snap()["session"]["elapsed_s"] == 40.0 - 7.0 - 11.0  # pause from 16 to 27


def test_pause_and_resume_commands_press_start():
    h = race3(laps=10)
    h.actions.clear()
    h.now = 12.0
    h.engine.pause()
    assert h.engine.state == "paused" and h.actions.of("press") == [(BUTTON_START,)]
    h.status(1, at=12.1)  # the CU confirms: already paused, nothing changes
    assert h.engine.state == "paused" and len(h.named("session")) == 4
    h.now = 15.0
    assert h.snap()["session"]["elapsed_s"] == 5.0
    h.engine.resume()
    assert h.engine.state == "paused"  # the CU counts down, GO to resume
    assert len(h.actions.of("press")) == 2
    for i, phase in enumerate(range(2, 7)):
        h.status(phase, at=16.0 + i)
    h.status(7, at=21.0)
    assert h.engine.state == "running"
    h.now = 30.0
    assert h.snap()["session"]["elapsed_s"] == 30.0 - 7.0 - 9.0  # pause from 12 to 21


def test_resume_presses_again_when_cu_stays_armed():
    h = race3(laps=10)
    h.now = 12.0
    h.engine.pause()
    h.status(1, at=12.1)
    h.now = 20.0
    h.actions.clear()
    h.engine.resume()
    assert h.actions.of("press") == [(BUTTON_START,)]
    h.tick(20.9)  # the CU stayed armed: 2nd press
    assert len(h.actions.of("press")) == 2


def test_resume_does_not_press_during_an_ongoing_countdown():
    h = race3(laps=10)
    h.status(1, at=12.0)  # external pause
    h.status(3, at=14.0)  # someone started the countdown on the CU
    h.actions.clear()
    h.engine.resume()
    assert h.actions.of("press") == []


def test_false_start_during_a_resume_is_shown_and_resume_works_again():
    """A false start in the countdown of a resume: announced, still paused; resuming again clears the
    CU's false-start lights with Start, even when the CU keeps showing them (no re-arm by itself)."""
    h = race3(laps=10)
    h.now = 12.0
    h.engine.pause()
    h.status(1, at=12.1)
    h.now = 20.0
    h.engine.resume()
    h.status(2, at=20.5)
    h.status(8, at=21.0)  # someone throttles during the countdown
    assert h.engine.state == "paused" and h.named("false_start") == [{}]
    assert h.snap()["session"]["false_start"] is True
    h.status(9, at=21.5)
    h.now = 25.0
    h.actions.clear()
    h.engine.resume()
    assert h.actions.of("press") == [(BUTTON_START,)]          # clears the false start
    assert h.snap()["session"]["false_start"] is False
    h.tick(26.6)                                                # the CU still shows it: again
    assert len(h.actions.of("press")) == 2
    h.status(1, at=27.0)                                        # re-armed
    h.tick(29.0)
    assert len(h.actions.of("press")) == 3                      # countdown
    for i, phase in enumerate(range(2, 7)):
        h.status(phase, at=30.0 + i)
    h.status(7, at=35.0)
    assert h.engine.state == "running"


def test_a_short_circuit_in_the_countdown_is_not_a_false_start():
    """Phases 10-11 are the CU's short-circuit show (hardware 8), not a false start (8-9)."""
    h = Harness(2)
    h.configure(type="race", laps=3)
    h.now = 0.0
    h.engine.start()
    h.status(1, at=0.2)
    h.status(2, at=1.0)
    h.status(10, at=1.5)
    assert h.engine.state == "starting" and h.named("false_start") == []
    assert h.named("short_circuit") == [{"phase": 10}]


def test_start_after_a_false_start_clears_the_cu_lights():
    """The CU keeps showing a false start (phase 9) until Start: starting again presses it."""
    h = Harness(2)
    h.configure(type="race", laps=3)
    h.now = 0.0
    h.engine.start()
    h.status(1, at=0.2)
    h.status(2, at=1.0)
    h.status(8, at=1.5)
    h.status(9, at=2.0)
    assert h.engine.state == "grid"
    h.now = 10.0
    h.actions.clear()
    h.engine.start()
    assert h.actions.of("press") == [(BUTTON_START,)]
    h.tick(11.6)
    assert len(h.actions.of("press")) == 2


def test_pause_during_finishing_resumes_to_finishing():
    h = race3(laps=2)
    h.cross(0, B)
    h.cross(1, B + 100)  # P1 took the start: we wait for it
    h.cross(0, B + 5000)
    h.cross(0, B + 10000)  # P0 wins: finishing
    assert h.engine.state == "finishing"
    h.status(1, at=21.0)
    assert h.engine.state == "paused"
    h.status(2, at=22.0)
    h.status(7, at=23.0)
    assert h.engine.state == "finishing"


def test_practice_pause_and_resume_are_immediate():
    h = Harness(1)
    h.configure(type="practice")
    h.plain_start(at=0.0)
    h.now = 10.0
    h.engine.pause()
    assert h.engine.state == "paused" and h.actions.of("press") == [(BUTTON_START,)]
    h.status(1, at=10.2)  # the CU is armed
    h.now = 20.0
    assert h.snap()["session"]["elapsed_s"] == 10.0
    h.engine.resume()
    assert h.engine.state == "running"
    assert len(h.actions.of("press")) == 2  # Start to release the cars
    h.status(2, at=20.2)  # CU countdown: not a pause
    assert h.engine.state == "running"
    h.status(7, at=25.0)
    h.now = 30.0
    assert h.snap()["session"]["elapsed_s"] == 20.0  # 30 - 10 of pause


def test_cannot_resume_or_pause_in_wrong_state():
    h = Harness(1)
    with pytest.raises(ValueError):
        h.engine.pause()
    h.configure(type="practice")
    for command in (h.engine.pause, h.engine.resume):
        with pytest.raises(ValueError):
            command()
    h.plain_start()
    with pytest.raises(ValueError):
        h.engine.resume()


# ===================================================================== full race


def run_race_to_the_end(h: Harness) -> None:
    """3 cars: laps of 5000 / 5200 / 5600 ms, grid staggered by 200 ms."""
    refs = {0: B, 1: B + 200, 2: B + 400}
    lap_ms = {0: 5000, 1: 5200, 2: 5600}
    events = [(refs[s] + n * lap_ms[s], s) for s in refs for n in range(4)]
    for ts, slot in sorted(events):
        h.cross(slot, ts)


def test_race_full_flow_finishing_and_classification():
    h = race3(laps=3)
    run_race_to_the_end(h)
    assert h.states() == ["grid", "starting", "running", "finishing", "finished"]
    assert h.named("finish") == [
        {"slot": 0, "position": 1},
        {"slot": 1, "position": 2},
        {"slot": 2, "position": 3},
    ]
    assert h.named("positions")[0] == {"order": [0]}
    assert h.order() == [0, 1, 2]
    cars = {c["slot"]: c for c in h.snap()["cars"]}
    assert [cars[s]["laps"] for s in (0, 1, 2)] == [3, 3, 3]
    assert [cars[s]["finished"] for s in (0, 1, 2)] == [True, True, True]
    assert (cars[1]["gap_ms"], cars[1]["interval_ms"]) == (800, 800)
    assert (cars[2]["gap_ms"], cars[2]["interval_ms"]) == (2200, 1400)
    assert (cars[0]["gap_ms"], cars[0]["gap_laps"], cars[0]["interval_ms"]) == (None, 0, None)
    assert h.snap()["session"]["flag"] == "checkered"
    # after the finish nothing counts anymore
    h.cross(0, B + 20_000)
    assert h.car(0)["laps"] == 3
    # P0's last lap event is its 3rd lap, position 1
    assert [l for l in h.named("lap") if l["slot"] == 0][-1]["lap"] == 3


def test_race_first_car_to_finish_triggers_finishing_others_finish_at_next_crossing():
    h = race3(laps=2)
    for ts, slot in [(B, 0), (B + 100, 1), (B + 5000, 0), (B + 5300, 1)]:
        h.cross(slot, ts)
    h.cross(0, B + 10_000)  # P0: 2 laps
    assert h.engine.state == "finishing"
    assert h.named("finish") == [{"slot": 0, "position": 1}]
    assert h.car(1)["finished"] is False
    h.cross(1, B + 10_300)
    assert h.named("finish")[-1] == {"slot": 1, "position": 2}
    assert h.engine.state == "finished"  # P2 never took the start: we do not wait for it


def test_lapped_car_finishes_with_fewer_laps():
    h = race3(laps=3)
    events = [(B, 0), (B + 200, 1), (B + 400, 2)]
    events += [(B + 4000 * n, 0) for n in (1, 2, 3)]  # P0: 4 s per lap, finishes at 22.0 s
    events += [(B + 200 + 6500 * n, 1) for n in (1, 2)]  # P1: 6.5 s per lap
    events += [(B + 400 + 14_000, 2)]  # P2: very slow
    for ts, slot in sorted(events):
        h.cross(slot, ts)
    assert h.engine.state == "finished"
    cars = {c["slot"]: c for c in h.snap()["cars"]}
    assert [cars[s]["position"] for s in (0, 1, 2)] == [1, 2, 3]
    assert [cars[s]["laps"] for s in (0, 1, 2)] == [3, 2, 1]  # finished at the next pass, with fewer laps
    assert all(cars[s]["finished"] for s in (0, 1, 2))
    assert (cars[1]["gap_laps"], cars[1]["gap_ms"]) == (1, None)
    assert (cars[2]["gap_laps"], cars[2]["interval_laps"]) == (2, 1)


def test_finishing_timeout_ends_the_session():
    h = race3(laps=2, finish_timeout_s=30.0)
    for ts, slot in [(B, 0), (B + 100, 1), (B + 5000, 0), (B + 5300, 1), (B + 10_000, 0)]:
        h.cross(slot, ts)
    assert h.engine.state == "finishing"  # P1 took the start but does not come back
    h.tick(20.0 + 29.9)
    assert h.engine.state == "finishing"
    h.tick(20.0 + 30.0)
    assert h.engine.state == "finished"
    assert h.named("finish") == [{"slot": 0, "position": 1}]  # P1 did not finish: no event
    assert h.car(1)["finished"] is False


def test_at_the_end_finishers_rank_ahead_of_a_stalled_car_with_the_same_laps():
    h = race3(laps=3, finish_timeout_s=30.0)
    events = [(B, 0), (B + 200, 1), (B + 400, 2)]
    events += [(B + 5000 * n, 0) for n in (1, 2, 3)]  # P0 finishes at 25.0 s
    events += [(B + 200 + 5200 * n, 1) for n in (1, 2)]  # P1: 2 laps then breakdown
    events += [(B + 400 + 8300, 2), (B + 400 + 16_600, 2)]  # P2: slow, 2 laps when the flag falls
    for ts, slot in sorted(events):
        h.cross(slot, ts)
    assert h.engine.state == "finishing"  # P1 has not finished: we wait for it
    assert h.car(2)["finished"] and not h.car(1)["finished"]
    assert h.car(1)["laps"] == h.car(2)["laps"] == 2
    assert h.order() == [0, 1, 2]  # during the race: the 2nd lap time breaks the tie (P1 earlier)
    h.tick(100.0)
    assert h.engine.state == "finished"
    assert h.order() == [0, 2, 1]  # at the end: the finisher ranks ahead of the stalled car
    assert h.named("positions")[-1] == {"order": [0, 2, 1]}


def test_finishing_timeout_does_not_run_while_paused():
    h = race3(laps=2, finish_timeout_s=30.0)
    for ts, slot in [(B, 0), (B + 100, 1), (B + 5000, 0), (B + 5300, 1), (B + 10_000, 0)]:
        h.cross(slot, ts)
    h.status(1, at=25.0)  # external pause 5 s after the flag
    h.tick(200.0)
    assert h.engine.state == "paused"
    h.status(2, at=201.0)
    h.status(7, at=202.0)
    h.tick(202.0 + 24.9)
    assert h.engine.state == "finishing"
    h.tick(202.0 + 25.0)
    assert h.engine.state == "finished"


def test_stop_ends_any_session_and_freezes_elapsed():
    h = race3(laps=10)
    h.now = 17.0
    h.engine.stop()
    assert h.engine.state == "finished"
    h.now = 99.0
    assert h.snap()["session"]["elapsed_s"] == 10.0
    h.engine.stop()  # idempotent
    assert h.states().count("finished") == 1


def test_stop_from_grid_and_starting():
    h = Harness(2)
    with pytest.raises(ValueError):
        h.engine.stop()
    h.configure(type="race", laps=3)
    h.engine.stop()
    assert h.engine.state == "finished" and h.snap()["session"]["elapsed_s"] == 0
    h.configure(type="race", laps=3)
    h.engine.start()
    h.engine.stop()
    assert h.engine.state == "finished"


def test_disabled_driver_does_not_block_the_finish():
    drivers = [DriverConfig(0, "A", "#1"), DriverConfig(1, "B", "#2", enabled=False)]
    h = Harness(drivers)
    h.configure(type="race", laps=2)
    h.light_start()
    h.cross(0, B)
    h.cross(1, B + 100)  # disabled car that is driving: it shows up
    h.cross(0, B + 5000)
    h.cross(0, B + 10_000)
    assert h.engine.state == "finished"
    assert h.car(1)["enabled"] is False


# ============================================================ standings: laps / gaps


def test_gaps_and_intervals_same_lap_and_lapped():
    h = race3(laps=10)
    for ts, slot in [(B, 0), (B + 200, 1), (B + 400, 2)]:
        h.cross(slot, ts)
    for ts, slot in [(B + 5000, 0), (B + 5400, 1), (B + 6000, 2), (B + 10_000, 0), (B + 10_600, 1)]:
        h.cross(slot, ts)
    cars = {c["slot"]: c for c in h.snap()["cars"]}
    assert h.order() == [0, 1, 2]
    assert (cars[1]["laps"], cars[1]["gap_ms"], cars[1]["gap_laps"]) == (2, 600, 0)
    assert (cars[2]["laps"], cars[2]["gap_ms"], cars[2]["gap_laps"]) == (1, None, 1)  # one lap fewer
    assert (cars[2]["interval_ms"], cars[2]["interval_laps"]) == (None, 1)
    h.cross(2, B + 11_600)  # P2 catches up the 2 laps
    cars = {c["slot"]: c for c in h.snap()["cars"]}
    assert (cars[2]["gap_ms"], cars[2]["gap_laps"]) == (1600, 0)
    assert (cars[2]["interval_ms"], cars[2]["interval_laps"]) == (1000, 0)
    h.cross(0, B + 15_000)  # P0: 3 laps, P1 and P2: 2
    cars = {c["slot"]: c for c in h.snap()["cars"]}
    assert (cars[1]["gap_ms"], cars[1]["gap_laps"]) == (None, 1)
    assert (cars[2]["gap_laps"], cars[2]["interval_ms"], cars[2]["interval_laps"]) == (1, 1000, 0)


def test_overtake_changes_order_and_emits_positions_only_on_change():
    h = race3(laps=10)
    h.cross(0, B)
    h.cross(1, B + 200)
    assert [p["order"] for p in h.named("positions")] == [[0], [0, 1]]
    h.cross(0, B + 5000)  # P0: 1 lap, P1: 0 laps -> the order does not change
    assert len(h.named("positions")) == 2
    h.cross(1, B + 4900)  # P1: 1 lap too, but passed 100 ms earlier on the CU clock
    assert h.order() == [1, 0]
    assert h.named("positions")[-1] == {"order": [1, 0]}
    assert h.named("lap")[-1]["position"] == 1


def test_cars_without_reference_have_no_position_and_come_last_by_slot():
    h = race3(laps=10)
    h.cross(2, B)
    snap = h.snap()
    assert [(c["slot"], c["position"]) for c in snap["cars"]] == [(2, 1), (0, None), (1, None)]
    assert h.car(0)["gap_ms"] is None and h.car(0)["gap_laps"] == 0


def test_best_lap_classification_cars_without_best_come_after():
    h = Harness(4)
    h.configure(type="practice")
    h.plain_start()
    for slot, ts in [(0, B), (1, B + 100), (2, B + 200), (3, B + 300)]:
        h.cross(slot, ts)
    h.cross(1, B + 100 + 5100)
    h.cross(0, B + 4900)
    assert h.order() == [0, 1, 2, 3]  # 2 and 3: no lap, by slot
    cars = {c["slot"]: c for c in h.snap()["cars"]}
    assert (cars[0]["best_ms"], cars[0]["gap_ms"]) == (4900, None)
    assert (cars[1]["best_ms"], cars[1]["gap_ms"], cars[1]["interval_ms"]) == (5100, 200, 200)
    assert (cars[1]["gap_laps"], cars[1]["interval_laps"]) == (0, 0)
    assert cars[2]["gap_ms"] is None and cars[2]["position"] == 3
    h.cross(2, B + 200 + 5000)  # P2 moves ahead of P1
    assert h.order() == [0, 2, 1, 3]
    assert h.car(2)["gap_ms"] == 100 and h.car(1)["interval_ms"] == 100


def test_best_lap_tie_goes_to_the_one_who_set_it_first_on_the_cu_clock():
    h = Harness(2)
    h.configure(type="practice")
    h.plain_start()
    h.cross(0, B)
    h.cross(1, B + 100)
    h.cross(1, B + 5100)  # 5000 ms at 15.1 s CU (delivered first)
    h.cross(0, B + 5000)  # 5000 ms at 15.0 s CU
    assert h.order() == [0, 1]


def test_neutralized_lap_never_counts_for_best_lap():
    h = Harness(1)
    h.configure(type="practice")
    h.plain_start(at=0.0)
    h.cross(0, B)
    h.now = 11.0
    h.engine.pause()
    h.now = 13.0
    h.engine.resume()
    h.cross(0, B + 3000, host=13.5)  # lap covering the pause, very short: 3.0 s
    assert h.named("lap")[0]["neutralized"] is True
    assert h.car(0)["best_ms"] is None and h.snap()["session"]["best"] is None
    assert h.car(0)["last_ms"] == 3000 and h.car(0)["laps"] == 1


# ==================================================================== other sessions


def test_endurance_time_up_then_each_car_finishes_at_next_crossing():
    h = Harness(2)
    h.configure(type="endurance", duration_s=60)
    h.light_start()  # GO at 7.0
    h.cross(0, B)
    h.cross(1, B + 200)
    h.cross(0, B + 5000)
    h.tick(66.9)
    assert h.engine.state == "running"
    assert h.snap()["session"]["remaining_s"] == pytest.approx(0.1)
    h.tick(67.0)  # 60 s elapsed
    assert h.engine.state == "finishing"
    s = h.snap()["session"]
    assert (s["remaining_s"], s["flag"], s["duration_s"], s["laps"]) == (0.0, "checkered", 60, None)
    h.cross(0, B + 60_000)
    assert h.named("finish") == [{"slot": 0, "position": 1}]
    h.cross(1, B + 66_000)  # P1: one more lap (counted) then finished
    assert h.named("finish")[-1]["slot"] == 1
    assert h.engine.state == "finished"
    assert h.car(0)["laps"] == 2 and h.car(1)["laps"] == 1


def test_time_up_is_detected_from_the_crossing_even_without_tick():
    h = Harness(1)
    h.configure(type="endurance", duration_s=60)
    h.light_start()
    h.cross(0, B)
    h.cross(0, B + 5000)
    h.cross(0, B + 62_000, host=69.0)  # after 60 s of racing, no tick in between
    assert h.named("lap")[-1]["lap"] == 2
    assert h.car(0)["finished"] is True and h.engine.state == "finished"


def test_crossing_before_the_deadline_delivered_late_does_not_finish_the_car():
    h = Harness(1)
    h.configure(type="endurance", duration_s=60)
    h.light_start()
    h.cross(0, B)
    h.tick(67.5)  # the tick sees the deadline before the 66.5 s pass is delivered
    assert h.engine.state == "finishing"
    h.cross(0, B + 56_500, host=66.5)
    assert h.car(0)["laps"] == 1 and h.car(0)["finished"] is False
    h.cross(0, B + 62_000, host=69.0)
    assert h.car(0)["finished"] is True and h.engine.state == "finished"


def test_endurance_time_up_with_nobody_on_track_finishes_at_once():
    h = Harness(2)
    h.configure(type="endurance", duration_s=30)
    h.light_start()
    h.tick(37.0)
    assert h.engine.state == "finished"


def test_qualifying_time_up_lets_the_lap_in_progress_finish():
    h = Harness(3)
    h.configure(type="qualifying", duration_s=30)
    h.plain_start(at=0.0)
    for ts, slot in [(B, 0), (B + 100, 1), (B + 200, 2)]:
        h.cross(slot, ts)  # P2 only has its reference
    h.cross(0, B + 5000)
    h.cross(1, B + 100 + 5200)
    h.tick(29.9)
    assert h.engine.state == "running"
    h.tick(30.0)
    assert h.engine.state == "finishing"
    assert h.snap()["session"]["remaining_s"] == 0
    h.cross(0, B + 9800, host=31.0)  # lap in progress completed: 4800 ms, best
    assert h.named("finish") == [{"slot": 0, "position": 1}]
    assert h.car(0)["best_ms"] == 4800
    h.cross(1, B + 100 + 10_500, host=32.0)  # 5300 ms, not better than 5200
    assert h.engine.state == "finishing"  # P2 took the start (reference), we wait for it
    h.tick(30.0 + 30.0)
    assert h.engine.state == "finished"
    assert h.order() == [0, 1, 2]
    assert [h.car(s)["best_ms"] for s in (0, 1, 2)] == [4800, 5200, None]
    assert h.car(1)["finished"] is True and h.car(2)["finished"] is False


def test_practice_with_duration_behaves_like_qualifying_and_without_it_never_ends():
    h = Harness(1)
    h.configure(type="practice")
    h.plain_start(at=0.0)
    h.cross(0, B)
    h.tick(10_000.0)
    assert h.engine.state == "running"
    assert h.snap()["session"]["remaining_s"] is None
    h2 = Harness(1)
    h2.configure(type="practice", duration_s=10)
    h2.plain_start(at=0.0)
    assert h2.snap()["session"]["remaining_s"] == 10
    h2.cross(0, B, host=5.0)
    h2.tick(10.0)
    assert h2.engine.state == "finishing"


def test_elimination_sequence_and_ignore_mask():
    drivers = [DriverConfig(i, f"P{i}", "#fff") for i in range(4)] + [
        DriverConfig(6, "Ghost", "#999", enabled=False),
        DriverConfig(7, "Pace", "#777", enabled=False),
    ]
    h = Harness(drivers)
    h.configure(type="elimination", elim_every=2)
    assert h.actions.of("ignore") == [(0xC0,)]  # 6 and 7 disabled
    h.light_start()
    lap_ms = {0: 5000, 1: 5100, 2: 5200, 3: 5300}
    events = [(B + n * lap_ms[s], s) for s in lap_ms for n in range(0, 8)]
    for ts, slot in sorted(events):
        if h.engine.state == "finished":
            break
        h.cross(slot, ts)
    assert h.named("elimination") == [
        {"slot": 3, "lap": 2},
        {"slot": 2, "lap": 4},
        {"slot": 1, "lap": 6},
    ]
    assert h.actions.of("ignore") == [(0xC0,), (0xC0 | 0x08,), (0xC0 | 0x0C,), (0xC0 | 0x0E,)]
    assert h.engine.state == "finished"
    assert h.named("finish") == [{"slot": 0, "position": 1}]  # the last one on track wins
    cars = {c["slot"]: c for c in h.snap()["cars"]}
    assert h.order() == [0, 1, 2, 3]  # eliminated: the last one out ahead
    assert [cars[s]["eliminated"] for s in range(4)] == [False, True, True, True]
    assert cars[0]["finished"] is True
    assert cars[3]["laps"] == 1 and cars[2]["laps"] == 3  # passes of an eliminated car are ignored
    positions = [p["order"] for p in h.named("positions")]
    assert positions[-1] == [0, 1, 2, 3]


def test_elimination_waits_for_a_second_car():
    drivers = [DriverConfig(i, f"P{i}", "#fff") for i in range(2)]
    h = Harness(drivers)
    h.configure(type="elimination", elim_every=1)
    h.light_start()
    h.cross(0, B)
    h.cross(0, B + 5000)  # P0 alone on the track: nobody to eliminate
    assert h.named("elimination") == [] and h.engine.state == "running"
    h.cross(1, B + 5100)
    h.cross(0, B + 10_000)  # leader: 2 laps, P1 (reference only) is out; P0 wins
    assert h.named("elimination") == [{"slot": 1, "lap": 2}]
    assert h.engine.state == "finished"


def test_training_episodes():
    h = Harness(2)
    h.configure(type="training", episode_laps=2)
    assert h.engine.state == "grid"
    h.plain_start()
    assert h.car(0)["episode"] == 1
    h.cross(0, B)
    h.cross(0, B + 5000)
    h.cross(1, B + 100)
    assert h.named("episode_end") == []
    h.cross(0, B + 9800)
    assert h.named("episode_end") == [
        {"slot": 0, "episode": 1, "laps": 2, "best_ms": 4800, "mean_ms": 4900.0, "laps_ms": [5000, 4800]}
    ]
    assert h.car(0)["episode"] == 2 and h.car(1)["episode"] == 1
    assert h.car(0)["laps"] == 2  # the session stats carry on
    h.cross(0, B + 15_000)
    h.cross(0, B + 20_000)
    assert h.named("episode_end")[1] == {
        "slot": 0, "episode": 2, "laps": 2, "best_ms": 5000, "mean_ms": 5100.0, "laps_ms": [5200, 5000],
    }  # fmt: skip
    assert h.car(0)["episode"] == 3
    assert h.engine.state == "running"  # endless


def test_training_reset_episode_skips_the_stopped_lap():
    h = Harness(1)
    h.configure(type="training", episode_laps=2)
    h.plain_start()
    h.cross(0, B)
    h.cross(0, B + 5000)
    assert h.engine.reset_episode(0) == 2  # episode 1 under way -> move on to 2
    assert h.named("episode_reset") == [{"slot": 0, "episode": 2}]
    h.cross(0, B + 14_000)  # lap that includes the stop: outside the episode
    assert h.car(0)["episode_laps"] == 0
    h.cross(0, B + 19_000)
    h.cross(0, B + 24_100)
    assert h.named("episode_end")[-1]["laps_ms"] == [5000, 5100]
    h.engine.stop()
    h.configure(type="practice")
    with pytest.raises(ValueError):
        h.engine.reset_episode(0)


def test_snapshot_episode_is_null_outside_training():
    h = Harness(1)
    h.configure(type="practice")
    assert h.car(0)["episode"] is None


# ============================================================ fuel, pits, ghosts


def test_fuel_pit_events_and_snapshot():
    h = Harness(2, cfg=RaceConfig(fuel_low=3))
    h.configure(type="practice")
    assert h.car(0)["fuel"] is None and h.car(0)["pit"] is False  # nothing received
    h.status(0, at=1.0, fuel=(10, 8, 15, 15, 15, 15, 15, 15))
    assert (h.car(0)["fuel"], h.car(1)["fuel"]) == (10, 8)
    assert h.named("pit_in") == [] and h.named("fuel_low") == []
    h.status(0, at=2.0, fuel=(4, 8, 15, 15, 15, 15, 15, 15))
    assert h.named("fuel_low") == []
    h.status(0, at=3.0, fuel=(3, 8, 15, 15, 15, 15, 15, 15))  # drops below the threshold
    h.status(0, at=4.0, fuel=(2, 8, 15, 15, 15, 15, 15, 15))  # already below: a single alert per drop
    assert h.named("fuel_low") == [{"slot": 0, "fuel": 3}]
    pit = (True,) + (False,) * 7
    h.status(0, at=5.0, fuel=(2, 8, 15, 15, 15, 15, 15, 15), pit=pit)
    assert h.named("pit_in") == [{"slot": 0, "pits": 1}] and h.car(0)["pit"] is True
    h.status(0, at=6.0, fuel=(15, 8, 15, 15, 15, 15, 15, 15), pit=pit)  # refueled
    h.status(0, at=7.0, fuel=(15, 8, 15, 15, 15, 15, 15, 15))
    assert h.named("pit_out") == [{"slot": 0, "pits": 1}] and h.car(0)["pit"] is False
    h.status(0, at=8.0, fuel=(15, 8, 15, 15, 15, 15, 15, 15), pit=pit)
    assert h.named("pit_in")[-1] == {"slot": 0, "pits": 2} and h.car(0)["pits"] == 2
    h.status(0, at=9.0, fuel=(3, 8, 15, 15, 15, 15, 15, 15))  # new drop
    assert len(h.named("fuel_low")) == 2
    h.status(0, at=10.0, fuel=(3, 2, 15, 15, 15, 15, 15, 15))
    assert h.named("fuel_low")[-1] == {"slot": 1, "fuel": 2}


def test_fuel_low_is_muted_when_the_cu_fuel_switch_is_off_and_for_unknown_slots():
    """Fuel is the CU's switch, not a session setting: switched off, no fuel alert."""
    h = Harness(1)
    h.configure(type="practice")
    h.status(0, at=1.0, fuel=(10,) + (15,) * 7, mode=PIT_LANE_MODE)
    h.status(0, at=2.0, fuel=(1,) + (15,) * 7, mode=PIT_LANE_MODE)
    assert h.named("fuel_low") == []
    h.status(0, at=3.0, fuel=(10, 15, 15, 15, 15, 15, 15, 15))
    h.status(0, at=4.0, fuel=(10, 1, 15, 15, 15, 15, 15, 15))  # slot 1: no driver
    assert h.named("fuel_low") == []


def test_cu_mode_bits_and_phase_in_snapshot():
    h = Harness(1)
    assert h.snap()["cu"] == {
        "phase": 0, "lights": 0, "blink": False, "fuel_mode": False, "real_mode": False,
        "pit_lane": False, "lap_counter": False, "display": 6,
    }  # fmt: skip
    h.status(8, at=1.0, mode=PIT_LANE_MODE | LAP_COUNTER_MODE)
    cu = h.snap()["cu"]
    assert (cu["phase"], cu["lights"], cu["blink"]) == (8, 5, True)
    assert (cu["fuel_mode"], cu["pit_lane"], cu["lap_counter"]) == (False, True, True)


def test_ghost_and_pace_car_appear_on_first_crossing():
    h = Harness(6)  # drivers 0..5 only
    h.configure(type="practice")
    h.plain_start()
    assert [c["slot"] for c in h.snap()["cars"]] == [0, 1, 2, 3, 4, 5]
    h.cross(6, B)
    h.cross(6, B + 7000)
    h.cross(7, B + 100)
    slots = [c["slot"] for c in h.snap()["cars"]]
    assert slots == [6, 7, 0, 1, 2, 3, 4, 5]  # 6 has a lap, 7 only the reference, the others have no position
    ghost = h.car(6)
    assert (ghost["name"], ghost["color"], ghost["enabled"], ghost["laps"]) == ("Ghost", "#9b51e0", False, 1)
    assert h.car(7)["name"] == "Pace car"


def test_default_drivers_ignore_mask_and_ghost_still_listed_when_it_crosses():
    h = Harness(default_drivers())
    h.configure(type="practice")
    assert h.actions.of("ignore") == [(0xC0,)]
    h.plain_start()
    assert [c["slot"] for c in h.snap()["cars"]] == [0, 1, 2, 3, 4, 5]
    h.cross(6, B)
    assert h.car(6)["position"] == 1


# =============================================================== position tower


def test_tower_follows_order_and_leader_lap():
    h = Harness(3, tower=True)
    h.configure(type="race", laps=10)
    assert h.actions.calls[:3] == [("reset_timer",), ("ignore", 0), ("clrpos",)]
    h.actions.clear()
    h.light_start()  # the tower is paced by the engine's tick()
    assert h.actions.of("setlap") == [(0,)]  # counter reset to 0
    h.actions.clear()
    h.cross(0, B)
    h.cross(1, B + 200)
    for t in (11.0, 11.2, 11.4, 11.6):
        h.tick(t)
    assert h.actions.of("setpos") == [(0, 1), (1, 2)]
    h.cross(0, B + 5000)
    h.cross(1, B + 5100)
    h.tick(20.0)
    assert h.actions.of("setlap") == [(1,)]  # the leader completed its 1st lap


def test_tower_gets_new_order_after_overtake():
    h = Harness(2, tower=True)
    h.configure(type="race", laps=10)
    h.light_start()
    h.cross(0, B)
    h.cross(1, B + 200)
    h.cross(1, B + 5000)  # P1: lap 1 at 5000, 4800 ms
    h.cross(0, B + 5100)  # P0: lap 1 at 5100
    assert h.order() == [1, 0]
    h.actions.clear()
    for i in range(10):
        h.tick(30.0 + i * 0.2)
    assert h.actions.of("setpos")[-2:] == [(1, 1), (0, 2)]


# ================================================================ commands and states


def test_configure_resets_everything_and_announces_session():
    h = Harness(2)
    assert h.engine.state == "idle"
    h.configure(type="practice")
    assert h.engine.state == "grid"
    assert h.actions.calls == [("reset_timer",), ("ignore", 0)]
    assert h.named("session") == [{"state": "grid", "prev": "idle", "type": "practice", "title": "Practice"}]
    h.plain_start()
    h.cross(0, B)
    h.cross(0, B + 5000)
    h.engine.stop()
    assert h.engine.laps()
    h.configure(type="race", laps=5)  # from finished
    assert h.engine.state == "grid" and h.engine.laps() == []
    assert h.named("session")[-1] == {"state": "grid", "prev": "finished", "type": "race", "title": "Race · 5 laps"}
    snap = h.snap()
    assert snap["session"]["best"] is None and snap["session"]["laps"] == 5
    assert all(c["position"] is None and c["laps"] == 0 for c in snap["cars"])
    h.configure(type="practice")  # from grid
    assert h.engine.state == "grid"


def test_session_id_and_title():
    h = Harness(1)
    h.configure(type="race", laps=20)
    s = h.snap()["session"]
    assert re.fullmatch(r"\d{8}-\d{6}-race", s["id"])
    assert s["title"] == "Race · 20 laps"
    first = s["id"]
    h.configure(type="race", laps=20, title="Final")  # same second: distinct id
    assert h.snap()["session"]["id"] != first and h.snap()["session"]["title"] == "Final"
    assert h.engine.settings.title == "Final"
    assert h.snap()["session"]["id"].startswith(first)


def test_commands_are_rejected_in_wrong_states():
    h = Harness(1)
    with pytest.raises(ValueError):
        h.engine.start()  # idle
    h.configure(type="practice")
    with pytest.raises(ValueError):
        h.engine.pause()
    h.plain_start()
    with pytest.raises(ValueError):
        h.engine.start()  # running
    with pytest.raises(ValueError):
        h.configure(type="practice")  # session in progress
    h.engine.pause()
    with pytest.raises(ValueError):
        h.configure(type="practice")
    h.engine.stop()
    h.configure(type="practice")


def test_configure_rejects_invalid_settings_without_touching_the_session():
    h = Harness(1)
    h.configure(type="race", laps=5)
    before = h.snap()["session"]["id"]
    with pytest.raises(ValueError):
        h.configure(type="endurance")  # duration is required
    with pytest.raises(ValueError):
        h.configure(type="nope")
    assert h.engine.state == "grid" and h.snap()["session"]["id"] == before


def test_set_drivers_updates_cars_and_ignore_mask():
    h = Harness(2)
    h.configure(type="practice")
    h.plain_start()
    h.cross(0, B)
    h.actions.clear()
    h.engine.set_drivers([DriverConfig(0, "Alice", "#aaa"), DriverConfig(1, "Bob", "#bbb", enabled=False), DriverConfig(2, "Cy", "#ccc")])
    assert h.actions.of("ignore") == [(0b010,)]
    cars = {c["slot"]: c for c in h.snap()["cars"]}
    assert set(cars) == {0, 2}  # 1 disabled without having driven: removed
    assert cars[0]["name"] == "Alice" and cars[2]["enabled"] is True
    h.engine.set_drivers([DriverConfig(0, "Alice", "#aaa", enabled=False)])
    assert 0 in {c["slot"] for c in h.snap()["cars"]}  # has driven: stays displayed
    with pytest.raises(ValueError):
        h.engine.set_drivers([DriverConfig(9, "X", "#000")])


def test_set_drivers_before_configure_does_not_talk_to_the_cu():
    h = Harness(2)
    h.engine.set_drivers([DriverConfig(0, "Solo", "#000")])
    assert h.actions.calls == []
    assert [c["name"] for c in h.snap()["cars"]] == ["Solo"]


def test_event_callback_errors_do_not_break_the_engine():
    h = Harness(1)

    def broken(name, data):
        raise RuntimeError("broken subscriber")

    h.engine._on_event = broken
    h.configure(type="practice")
    h.plain_start()
    h.cross(0, B)
    h.cross(0, B + 5000)
    assert h.car(0)["laps"] == 1


# ================================================================ snapshot and results

EXAMPLE_SESSION_KEYS = {
    "id", "type", "title", "state", "laps", "duration_s", "elapsed_s", "remaining_s",
    "leader_lap", "best", "flag", "false_start",
}  # fmt: skip
EXAMPLE_CU_KEYS = {"phase", "lights", "blink", "fuel_mode", "real_mode", "pit_lane", "lap_counter", "display"}
EXAMPLE_CAR_KEYS = {
    "slot", "name", "color", "enabled", "position", "laps", "last_ms", "best_ms", "best_lap",
    "gap_ms", "gap_laps", "interval_ms", "interval_laps", "since_cross_ms", "sectors",
    "last_pb", "last_sb", "recent", "fuel", "pit", "pits", "finished", "eliminated", "episode",
}  # fmt: skip


def test_snapshot_is_json_serializable_and_has_every_contract_key_in_every_state():
    h = Harness(3)
    snaps = [h.snap()]  # idle
    h.configure(type="race", laps=2)
    snaps.append(h.snap())  # grid
    h.now = 0.0
    h.engine.start()
    snaps.append(h.snap())  # starting
    for i, phase in enumerate(range(1, 7)):
        h.status(phase, at=1.0 + i)
    h.status(7, at=7.0)
    snaps.append(h.snap())  # running, nobody has crossed the line
    run_race_to_the_end(h)
    snaps.append(h.snap())  # finished
    for snap in snaps:
        json.dumps(snap)
        assert set(snap) == {"session", "cu", "cars"}
        assert EXAMPLE_SESSION_KEYS <= set(snap["session"])
        assert EXAMPLE_CU_KEYS <= set(snap["cu"])
        for car in snap["cars"]:
            assert EXAMPLE_CAR_KEYS <= set(car)
            assert len(car["sectors"]) == 3
    assert snaps[0]["session"]["state"] == "idle" and snaps[0]["session"]["id"] is None
    assert [s["session"]["state"] for s in snaps] == ["idle", "grid", "starting", "running", "finished"]


def test_snapshot_matches_the_architecture_example_shape():
    h = Harness(1)
    h.configure(type="race", laps=20)
    h.light_start()
    h.cross(0, B)
    h.cross(0, B + 5000)
    h.now = 20.0
    snap = h.snap()
    assert snap["session"]["type"] == "race" and snap["session"]["laps"] == 20
    assert snap["session"]["duration_s"] is None and snap["session"]["remaining_s"] is None
    assert snap["session"]["leader_lap"] == 1 and snap["session"]["flag"] == "green"
    assert snap["session"]["best"] == {"slot": 0, "ms": 5000, "lap": 1}
    assert snap["cars"][0] == {
        "slot": 0, "name": "P0", "color": "#000000", "enabled": True, "position": 1,
        "laps": 1, "last_ms": 5000, "best_ms": 5000, "best_lap": 1,
        "gap_ms": None, "gap_laps": 0, "interval_ms": None, "interval_laps": 0,
        "since_cross_ms": 5000, "sectors": [None, None, None],
        "last_pb": True, "last_sb": True, "recent": [5000],
        "fuel": 15, "pit": False, "pits": 0, "finished": False, "eliminated": False, "episode": None, "episode_laps": None,
    }  # fmt: skip


def test_flag_per_state():
    h = Harness(1)
    assert h.snap()["session"]["flag"] == "none"
    h.configure(type="practice")
    assert h.snap()["session"]["flag"] == "none"
    h.plain_start()
    assert h.snap()["session"]["flag"] == "green"
    h.engine.pause()
    assert h.snap()["session"]["flag"] == "red"
    h.engine.stop()
    assert h.snap()["session"]["flag"] == "checkered"


def test_recent_keeps_the_last_five_laps():
    h = Harness(1)
    h.configure(type="practice")
    h.plain_start()
    h.cross(0, B)
    for n in range(1, 8):
        h.cross(0, B + 5000 * n + n * n)
    assert h.car(0)["recent"] == [h.engine.laps(0)[i]["ms"] for i in range(2, 7)]
    assert len(h.car(0)["recent"]) == 5 and h.car(0)["laps"] == 7


def test_laps_and_results():
    h = race3(laps=2)
    run_race_to_the_end(h)
    laps = h.engine.laps()
    assert len(laps) == 6 and all(
        set(l) == {"slot", "lap", "ms", "cu_ts", "t", "neutralized", "personal_best", "session_best"} for l in laps
    )
    assert [l["lap"] for l in h.engine.laps(1)] == [1, 2]
    assert h.engine.laps(5) == []
    assert laps[0]["t"] == pytest.approx(15.0 - 7.0)  # P0 lap 1 at 15.0 s host, GO at 7.0 s
    laps[0]["ms"] = 1  # copy: the engine is not modified
    assert h.engine.laps()[0]["ms"] != 1
    results = h.engine.results()
    json.dumps(results)
    assert set(results) == {"session", "classification", "laps"}
    assert results["session"]["state"] == "finished"
    assert [c["slot"] for c in results["classification"]] == [0, 1, 2]
    assert [c["position"] for c in results["classification"]] == [1, 2, 3]
    assert results["laps"] == h.engine.laps()


def test_results_classification_excludes_cars_that_never_started():
    h = Harness(3)
    h.configure(type="practice")
    h.plain_start()
    h.cross(1, B)
    assert [c["slot"] for c in h.engine.results()["classification"]] == [1]


# ================================================================ session settings


def test_session_types_metadata():
    assert list(SESSION_TYPES) == ["practice", "qualifying", "race", "endurance", "elimination", "rival", "training"]
    labels = {k: v["label"] for k, v in SESSION_TYPES.items()}
    assert labels == {
        "practice": "Practice", "qualifying": "Qualifying", "race": "Race",
        "endurance": "Endurance", "elimination": "Elimination", "rival": "Beat the bot",
        "training": "AI training",
    }  # fmt: skip
    fields = set(SessionSettings().to_dict())
    for meta in SESSION_TYPES.values():
        assert set(meta) == {"label", "description", "advanced_only", "uses_lights", "params"}
        assert set(meta["params"]) <= fields and meta["description"].endswith(".")
    assert {k for k, v in SESSION_TYPES.items() if v["advanced_only"]} == {"rival", "training"}
    assert {k for k, v in SESSION_TYPES.items() if v["uses_lights"]} == {"race", "endurance", "elimination", "rival"}


def test_settings_defaults_roundtrip_and_unknown_keys():
    s = SessionSettings()
    assert s.to_dict() == {
        "type": "practice", "laps": 20, "duration_s": None, "elim_every": 3, "episode_laps": 5,
        "rival_slot": None, "rival_margin_pct": 3.0, "title": "",
    }  # fmt: skip
    again = SessionSettings.from_dict({**s.to_dict(), "type": "race", "laps": 12, "bogus": 1})
    assert (again.type, again.laps) == ("race", 12)
    assert SessionSettings.from_dict(again.to_dict()) == again
    assert SessionSettings.from_dict({"type": "race", "laps": 20.0}).laps == 20  # JSON: 20.0
    assert SessionSettings.from_dict({}) == SessionSettings()
    json.dumps(again.to_dict())


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "drag"},
        {"laps": 0},
        {"laps": 10_000},
        {"laps": "12"},
        {"laps": True},
        {"laps": 2.5},
        {"duration_s": 0},
        {"duration_s": -5},
        {"duration_s": "60"},
        {"duration_s": 90_000},
        {"type": "endurance"},
        {"type": "qualifying"},
        {"elim_every": 0},
        {"episode_laps": 0},
        {"rival_slot": 8},
        {"rival_slot": -1},
        {"rival_margin_pct": -21},
        {"rival_margin_pct": 80},
        {"title": "x" * 81},
        {"title": 3},
    ],
)
def test_settings_validation_rejects(bad):
    with pytest.raises(ValueError):
        SessionSettings.from_dict(bad)


def test_settings_from_dict_rejects_non_objects():
    with pytest.raises(ValueError):
        SessionSettings.from_dict([1, 2])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("settings", "title"),
    [
        ({"type": "race", "laps": 20}, "Race · 20 laps"),
        ({"type": "race", "laps": 1}, "Race · 1 lap"),
        ({"type": "endurance", "duration_s": 900}, "Endurance · 15 min"),
        ({"type": "qualifying", "duration_s": 300}, "Qualifying · 5 min"),
        ({"type": "practice"}, "Practice"),
        ({"type": "practice", "duration_s": 90}, "Practice · 1 min 30 s"),
        ({"type": "endurance", "duration_s": 5400}, "Endurance · 1 h 30 min"),
        ({"type": "elimination", "elim_every": 3}, "Elimination · every 3 laps"),
        ({"type": "elimination", "elim_every": 1}, "Elimination · every lap"),
        ({"type": "rival", "laps": 8, "rival_slot": 3}, "Beat the bot · 8 laps"),
        ({"type": "training", "episode_laps": 5}, "AI training · 5-lap episodes"),
    ],
)
def test_default_titles(settings, title):
    assert SessionSettings.from_dict(settings).default_title() == title
    h = Harness(1)
    h.configure(**settings)
    assert h.snap()["session"]["title"] == title


def test_rival_session_behaves_like_a_race():
    h = Harness(2)
    h.configure(type="rival", laps=2, rival_slot=1)
    h.light_start()
    assert h.engine.state == "running"
    h.cross(0, B)
    h.cross(1, B + 100)
    h.cross(0, B + 5000)
    h.cross(0, B + 10_000)
    assert h.engine.state == "finishing" and h.snap()["session"]["laps"] == 2


# ===================================================================== robustness


@pytest.mark.parametrize("kind", list(SESSION_TYPES))
def test_random_message_storm_never_breaks_invariants(kind):
    rng = random.Random(list(SESSION_TYPES).index(kind))
    h = Harness(default_drivers())
    settings = {"type": kind, "laps": 4, "duration_s": 40, "elim_every": 2, "episode_laps": 2}
    h.configure(**settings)
    clock = {slot: (2**32 - 30_000 + slot * 50) % 2**32 for slot in range(8)}  # the CU counter is going to wrap
    valid_states = {"idle", "grid", "starting", "running", "paused", "finishing", "finished"}
    for _ in range(600):
        h.now += rng.random() * 0.9
        roll = rng.random()
        slot = rng.randrange(8)
        if roll < 0.45:
            clock[slot] = (clock[slot] + rng.choice([100, 1500, 2500, 5000, 5200, 6000, -400])) % 2**32
            h.cross(slot, clock[slot], sector=rng.choice([1, 1, 1, 2, 3]), host=h.now)
            if rng.random() < 0.2:  # duplicate
                h.cross(slot, clock[slot], host=h.now)
        elif roll < 0.65:
            h.status(rng.choice([0, 0, 0, 1, 2, 3, 6, 7, 8]), fuel=tuple(rng.randrange(16) for _ in range(8)),
                     pit=tuple(rng.random() < 0.1 for _ in range(8)))  # fmt: skip
        elif roll < 0.85:
            h.tick(h.now)
        else:
            command = rng.choice(["start", "pause", "resume", "stop", "configure"])
            try:
                if command == "configure":
                    h.configure(**settings)
                else:
                    getattr(h.engine, command)()
            except ValueError:
                pass  # command forbidden in this state: that is the contract
        snap = h.snap()
        json.dumps(snap)
        assert snap["session"]["state"] in valid_states
        assert snap["session"]["elapsed_s"] >= 0
        remaining = snap["session"]["remaining_s"]
        assert remaining is None or remaining >= 0
        positions = sorted(c["position"] for c in snap["cars"] if c["position"] is not None)
        assert positions == list(range(1, len(positions) + 1))
        for car in snap["cars"]:
            assert car["laps"] == len(h.engine.laps(car["slot"]))
            assert car["gap_laps"] >= 0 and car["interval_laps"] >= 0
            if car["position"] is None:
                assert car["laps"] == 0
        assert all(l["ms"] > 0 for l in h.engine.laps())


def _running(h) -> None:
    """Drive a harness to `running` through the CU start sequence."""
    h.engine.start()
    h.status(1, at=0.2)
    h.tick(1.0)
    for i, phase in enumerate(range(2, 7)):
        h.status(phase, at=2.0 + i)
    h.status(7, at=3.0)
    h.status(0, at=3.2)


def test_the_end_of_a_session_holds_the_cars_on_the_cu():
    """The practice ends: one Start press arms the CU, the cars do not move."""
    h = Harness(3)
    h.configure(type="race", laps=3)
    _running(h)
    assert h.engine.state == "running"
    base = len(h.actions.of("press"))
    h.engine.stop()
    assert h.engine.state == "finished"
    assert h.actions.of("press")[base:] == [(BUTTON_START,)]


def test_the_end_never_presses_during_the_cu_countdown():
    """Session ends while the CU is counting down: leave the CU alone."""
    h = Harness(3)
    h.configure(type="race", laps=3)
    h.engine.start()
    h.status(1, at=0.2)
    h.tick(1.0)
    for i, phase in enumerate(range(2, 7)):
        h.status(phase, at=2.0 + i)
    base = len(h.actions.of("press"))
    h.engine.stop()
    assert h.engine.state == "finished"
    assert len(h.actions.of("press")) == base


def test_short_circuit_phases_emit_one_event_per_show():
    """The CU's fault show (phases 10 -> 11 -> 0): one event per show, none while it lasts."""
    h = Harness(3)
    h.configure(type="race", laps=3)
    _running(h)
    h.status(10, at=10.0)
    h.status(11, at=11.0)
    h.status(0, at=12.0)
    h.status(10, at=20.0)
    shows = [e for e in h.events if e[0] == "short_circuit"]
    assert len(shows) == 2
