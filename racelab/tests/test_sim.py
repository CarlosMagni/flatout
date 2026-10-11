"""CU simulator tests: protocol, bot firmware, physics, start, link."""

from __future__ import annotations

import itertools
import time

import pytest
from carreralib import ControlUnit, protocol
from carreralib import connection as cn

from racelab.config import SimConfig
from racelab.sim import SimConnection, Simulator, physics
from racelab.sim.track import LAP_LENGTH, default_track

Status, Timer = ControlUnit.Status, ControlUnit.Timer


def make_sim(**overrides) -> Simulator:
    cfg = dict(latency_ms=0.0, jitter_ms=0.0, seed=1, humans=[], duplicate_rate=0.0)
    cfg.update(overrides)
    return Simulator(SimConfig(**cfg), realtime=False)


def make_cu(sim: Simulator, timeout: float = 0.01) -> ControlUnit:
    return ControlUnit(sim.connect(timeout))


def run(sim: Simulator, seconds: float, dt: float = 0.05):
    """Advance the simulation, yielding the state after each step."""
    for _ in range(round(seconds / dt)):
        sim.step(dt)
        yield sim.snapshot()


def drain_timers(cu: ControlUnit) -> list[Timer]:
    events = []
    while isinstance(msg := cu.poll(), Timer):
        events.append(msg)
    return events


def poll_status(cu: ControlUnit) -> Status:
    while isinstance(msg := cu.poll(), Timer):
        pass
    return msg


def lap_times_ms(sim: Simulator, cu: ControlUnit, slot: int, seconds: float) -> list[int]:
    stamps = []
    for _ in run(sim, seconds):
        stamps += [e.timestamp for e in drain_timers(cu) if e.address == slot]
    return [b - a for a, b in zip(stamps, stamps[1:])]


# --- carreralib protocol ------------------------------------------------------


def test_version_and_status_parse_with_real_carreralib():
    cu = make_cu(make_sim())
    assert cu.version() == "5339"
    status = cu.poll()
    assert isinstance(status, Status)
    assert status.fuel == (15,) * 8
    assert status.start == 0
    assert status.mode == Status.FUEL_MODE | Status.PIT_LANE_MODE
    assert status.pit == (False,) * 8
    assert status.display == 6


def test_status_mode_bits_follow_config():
    status = make_cu(make_sim(fuel_mode=False, pit_lane=False)).poll()
    assert status.mode == 0


def test_raw_frames_are_valid_protocol():
    conn = make_sim().connect(0.01)
    conn.send(b"0")
    version = conn.recv()
    assert version == protocol.pack("c4sC", b"0", b"5339")
    conn.send(b"?")
    assert protocol.unpack("2x8YYYBYC", conn.recv())[:8] == (15,) * 8


def test_timer_event_parses_with_address_timestamp_sector():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(2, 8)
    for _ in run(sim, 20):
        msg = cu.poll()
        if isinstance(msg, Timer):
            break
    assert msg == Timer(address=2, timestamp=msg.timestamp, sector=1)
    # the timer counts in ms of simulated time and the pass happens at the earliest after leaving the grid
    assert 0 < msg.timestamp < sim.t * 1000


def test_poll_returns_pending_timer_before_status():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 8)
    cu.setbot(1, 8)
    for _ in run(sim, 20):
        pass
    first, second = cu.poll(), cu.poll()
    assert isinstance(first, Timer) and isinstance(second, Timer)
    assert first.timestamp <= second.timestamp  # oldest first
    while isinstance(cu.poll(), Timer):
        pass
    assert isinstance(cu.poll(), Status)


def test_ignore_mask_suppresses_timer_events():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 8)
    cu.setbot(1, 8)
    cu.ignore(0b01)
    for _ in run(sim, 20):
        pass
    assert {e.address for e in drain_timers(cu)} == {1}
    cu.ignore(0)
    for _ in run(sim, 20):
        pass
    assert {e.address for e in drain_timers(cu)} == {0, 1}


def test_reset_clears_pending_events():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 8)
    for _ in run(sim, 15):
        pass
    assert isinstance(cu.poll(), Timer)
    cu.reset()
    assert isinstance(cu.poll(), Status)


def test_duplicate_events_are_repeated_once():
    sim = make_sim(duplicate_rate=1.0)
    cu = make_cu(sim)
    cu.setbot(0, 8)
    for _ in run(sim, 12):
        pass
    events = drain_timers(cu)
    assert len(events) >= 2 and len(events) % 2 == 0
    assert events[0] == events[1]


def test_clock_wraps_at_32_bits():
    sim = make_sim()
    cu = make_cu(sim)
    sim.t = (2**32 - 3000) / 1000  # clock wraps 3 s after the start
    cu.setbot(0, 8)
    for _ in run(sim, 20):
        pass
    stamps = [e.timestamp for e in drain_timers(cu)]
    assert stamps[0] > 2**31 and stamps[-1] < 20_000


def test_garbage_frames_never_crash():
    conn = make_sim().connect(0.01)
    for frame in (b"", b"Z", b"J", b"Jxx", b"T", b":", b"=", b"B", b"B0", b"\xff\x00\x7f"):
        conn.send(frame)
        assert conn.recv() == b""  # the real CU would answer `#`
    conn.send(b"0")
    assert conn.recv().startswith(b"0")


def test_bad_checksum_is_tolerated():
    sim = make_sim()
    conn = sim.connect(0.01)
    frame = bytearray(protocol.pack("cBYYC", b"J", 0 | 3 << 5, 5, 2))
    frame[-1] = ord("0") + (frame[-1] - ord("0") + 1) % 16
    conn.send(bytes(frame))
    assert conn.recv() == b"J"
    assert sim.cars[3].speed == 5


def test_recv_times_out_when_nothing_pending():
    conn = make_sim().connect(0.01)
    with pytest.raises(cn.TimeoutError):
        conn.recv()


def test_latency_is_simulated():
    conn = make_sim(latency_ms=30.0).connect(1.0)
    conn.send(b"?")
    t0 = time.monotonic()
    conn.recv()
    assert time.monotonic() - t0 >= 0.025


# --- B command (docs/bot-protocol.md) ------------------------------------------


def test_bot_gas_activates_slot():
    sim = make_sim()
    cu = make_cu(sim)
    assert cu.setbot(0, 7) == b"B"
    assert sim.bot_mask == 0b1 and sim.bot_gas[0] == 7
    assert cu.setbot(1, 10) == b"B"  # ':' = 10
    assert sim.bot_mask == 0b11 and sim.bot_gas[1] == 10
    cu.setbot(5, 15)
    assert sim.bot_gas[5] == 15
    assert [c["controller"] for c in sim.snapshot()["cars"]] == ["bot", "bot", "none", "none", "none", "bot"]


def test_bot_release_clears_mask_and_pending_lane():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 7)
    cu.botlane(0)
    assert sim.lane_pending == 0b1
    assert cu.botrelease(0) == b"B"
    assert sim.bot_mask == 0 and sim.lane_pending == 0


def test_lane_change_refused_when_slot_is_not_a_bot():
    sim = make_sim()
    cu = make_cu(sim)
    assert cu.botlane(1) == b"B"  # recognized, but without effect
    assert sim.lane_pending == 0
    cu.setbot(1, 3)
    cu.botlane(1)
    assert sim.lane_pending == 0b10


def test_bot_out_of_range_values_are_ignored():
    sim = make_sim()
    conn = sim.connect(0.01)
    for frame in (b"B8" + b"5", b"B9" + b"5", b"B0" + bytes([0x30 + 16]), b"B0Z", b"B0 ", b"B" + b" 5"):
        conn.send(frame)
        assert conn.recv() == b"B"
    assert sim.bot_mask == 0 and sim.lane_pending == 0 and sim.bot_gas == [0] * 8


def test_bot_on_ghost_or_pace_slot_sets_mask_without_a_car():
    sim = make_sim()
    conn = sim.connect(0.01)
    conn.send(b"B6" + b"9")
    conn.recv()
    assert sim.bot_mask == 1 << 6 and sim.bot_gas[6] == 9
    assert [c["slot"] for c in sim.snapshot()["cars"]] == list(range(6))
    sim.step(1.0)
    assert all(c["v"] == 0 for c in sim.snapshot()["cars"])


def test_stock_firmware_rejects_b_commands():
    sim = make_sim(bot_firmware=False)
    cu = make_cu(sim)
    assert cu.setbot(0, 7) == b""
    assert cu.botlane(0) == b""
    assert cu.botrelease(0) == b""
    assert sim.bot_mask == 0 and sim.bot_gas == [0] * 8 and sim.lane_pending == 0
    sim.step(2.0)
    assert sim.snapshot()["cars"][0]["v"] == 0


def test_lane_pending_is_consumed_on_next_physics_step():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 5)
    cu.botlane(0)
    assert not sim.cars[0].armed
    sim.step(0.01)
    assert sim.lane_pending == 0 and sim.cars[0].armed


# --- physics -----------------------------------------------------------------------


def test_snapshot_and_geometry_shapes():
    sim = make_sim()
    snap = sim.snapshot()
    assert set(snap) >= {"t", "phase", "cars"}
    assert [c["slot"] for c in snap["cars"]] == list(range(6))
    car = snap["cars"][0]
    assert set(car) >= {"slot", "s", "lane", "v", "throttle", "off", "pit", "fuel", "controller"}
    assert [c["lane"] for c in snap["cars"]] == [0, 1, 0, 1, 0, 1]
    assert 0 <= car["s"] < 1 and car["fuel"] == 15 and not car["off"]

    geo = sim.track_geometry()
    assert set(geo) >= {"length_m", "points", "lane_offset", "changers", "pit", "finish"}
    assert geo["length_m"][0] == pytest.approx(18.0, abs=0.1)
    assert geo["length_m"][1] > geo["length_m"][0]
    assert 190 <= len(geo["points"]) <= 210
    assert all(0 <= x <= 1 and 0 <= y <= 1 for x, y in geo["points"])
    assert 0 < geo["lane_offset"] < 0.05
    assert geo["pit"][0] > geo["changers"][1] and geo["pit"][1] < 1.0 and geo["finish"] == 0.0


def test_track_straights_have_high_critical_speed_and_hairpin_is_slow():
    track = default_track()
    n = track.n
    for lane in track.lanes:
        for u in (*track.changers, track.pit[0], sum(track.pit) / 2, track.pit[1]):
            window = lane.vc[int((u - 0.01) * n):int((u + 0.01) * n)]
            assert min(window) > physics.VMAX  # the lane changers and the pits are on straights
        assert min(lane.vc) < 0.65 * physics.VMAX  # there are tight corners


@pytest.mark.parametrize("slot", [0, 1])
def test_bot_at_moderate_throttle_completes_plausible_laps(slot):
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(slot, 8)
    laps = lap_times_ms(sim, cu, slot, 45)
    assert len(laps) >= 4
    assert all(3000 <= t <= 12000 for t in laps)
    assert not sim.cars[slot].off


def test_second_lane_is_longer_hence_slower_at_equal_throttle():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 7)
    cu.setbot(1, 7)
    stamps = {0: [], 1: []}
    for _ in run(sim, 40):
        for e in drain_timers(cu):
            if e.address in stamps:
                stamps[e.address].append(e.timestamp)
    lap0 = stamps[0][-1] - stamps[0][-2]
    lap1 = stamps[1][-1] - stamps[1][-2]
    assert lap1 > lap0 * 1.02


def test_full_throttle_deslots_in_hairpin_but_not_on_straights():
    sim = make_sim()
    cu = make_cu(sim)
    track = default_track()
    hairpin = min((i for i in range(track.n) if 0.2 < i / track.n < 0.4),
                  key=lambda i: track.lanes[1].vc[i]) / track.n
    cu.setbot(0, 15)
    assert sim.cars[0].lane == 0
    for snap in run(sim, 30, dt=0.02):
        if snap["cars"][0]["off"]:
            assert snap["cars"][0]["v"] == 0
            assert abs(snap["cars"][0]["s"] - hairpin) < 0.04
            break
    else:
        pytest.fail("the car never broke away at full throttle")


def test_deslotted_car_is_put_back_after_a_few_seconds():
    sim = make_sim()
    make_cu(sim).setbot(0, 15)
    states = [snap["cars"][0]["off"] for snap in run(sim, 60, dt=0.02)]
    runs = [(off, len(list(g))) for off, g in itertools.groupby(states)]
    closed = [n * 0.02 for i, (off, n) in enumerate(runs[:-1]) if off]  # completed stops
    assert len(closed) >= 2
    assert all(physics.MARSHAL_S[0] - 0.05 <= t <= physics.MARSHAL_S[1] + 0.05 for t in closed)


def test_j_speed_word_lowers_top_speed():
    def top_speed(program: bool) -> float:
        sim = make_sim()
        cu = make_cu(sim)
        if program:
            cu.setspeed(0, 3)
        cu.setbot(0, 10)
        return max(snap["cars"][0]["v"] for snap in run(sim, 6))

    assert top_speed(True) < 0.75 * top_speed(False)


def test_j_brake_word_lengthens_braking():
    def speed_after_release(brake: int) -> float:
        sim = make_sim()
        cu = make_cu(sim)
        cu.setbrake(0, brake)
        cu.setbot(0, 9)
        for _ in run(sim, 1.0):
            pass
        cu.setbot(0, 0)
        sim.step(0.3)
        return sim.snapshot()["cars"][0]["v"]

    assert speed_after_release(0) > speed_after_release(15)


def test_fuel_is_consumed_in_fuel_mode_only():
    sim = make_sim()
    make_cu(sim).setbot(0, 15)
    for _ in run(sim, 10, dt=0.05):
        pass
    assert sim.snapshot()["cars"][0]["fuel"] < 15

    sim = make_sim(fuel_mode=False)
    make_cu(sim).setbot(0, 15)
    for _ in run(sim, 10):
        pass
    assert sim.snapshot()["cars"][0]["fuel"] == 15


def test_fuel_consumption_is_proportional_to_throttle_and_distance():
    used = {}
    for gas in (4, 8):
        car = make_sim().cars[0]
        distance = 0.0
        for _ in range(500):
            car.step(0.01, gas)
            distance += car.v * 0.01
        used[gas] = (physics.FUEL_MAX - car.fuel) / distance
    assert used[8] == pytest.approx(2 * used[4], rel=0.01)
    assert used[8] == pytest.approx(physics.FUEL_PER_M * 8 / 15, rel=0.01)
    # a full tank lasts FUEL_LAPS laps at race pace
    assert physics.FUEL_PER_M * physics.RACE_PACE * LAP_LENGTH * physics.FUEL_LAPS == pytest.approx(physics.FUEL_MAX)


def test_empty_tank_slows_the_car():
    sim = make_sim()
    cu = make_cu(sim)
    sim.cars[0].fuel = 0.0
    cu.setbot(0, 15)
    top = max(snap["cars"][0]["v"] for snap in run(sim, 6))
    assert 0.9 * 0.35 * physics.VMAX < top < 0.35 * physics.VMAX + 0.01


def test_status_reports_integer_fuel_levels():
    sim = make_sim()
    cu = make_cu(sim)
    sim.cars[3].fuel = 7.9
    assert cu.poll().fuel[3] == 7


def test_lane_change_happens_at_the_next_changer_and_is_continuous():
    sim = make_sim()
    cu = make_cu(sim)
    track = default_track()
    cu.setbot(0, 8)
    cu.botlane(0)
    prev = None
    for snap in run(sim, 12, dt=0.01):
        car = snap["cars"][0]
        if prev and car["lane"] != prev["lane"]:
            assert prev["lane"] == 0 and car["lane"] == 1
            assert abs(car["s"] - track.changers[0]) < 0.01  # first lane changer of the lap
            assert abs(car["s"] - prev["s"]) < 0.01  # same physical position
            break
        prev = car
    else:
        pytest.fail("no lane change")
    assert not sim.cars[0].armed


def test_lane_change_request_is_one_shot():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 8)
    cu.botlane(0)
    lanes = {snap["cars"][0]["lane"] for snap in run(sim, 15)}
    assert lanes == {0, 1}
    assert sim.snapshot()["cars"][0]["lane"] == 1  # a single change, no back and forth


def arm_before_pit(sim: Simulator, cu: ControlUnit, seconds: float = 40):
    """Request a lane change just before the pit entry; yields the following states."""
    track = default_track()
    armed = False
    for snap in run(sim, seconds, dt=0.02):
        car = snap["cars"][0]
        if not armed and track.pit[0] - 0.07 < car["s"] < track.pit[0] - 0.03:
            cu.botlane(0)
            armed = True
        yield snap, cu.poll() if armed else None
    assert armed


def test_pit_stop_refuels_and_sets_status_bit():
    sim = make_sim()
    cu = make_cu(sim)
    sim.cars[0].fuel = 2.0
    cu.setbot(0, 8)
    pit_time = 0.0
    seen_bit = False
    min_v_in_pit = 9.0
    for snap, status in arm_before_pit(sim, cu):
        car = snap["cars"][0]
        if car["pit"]:
            pit_time += 0.02
            min_v_in_pit = min(min_v_in_pit, car["v"])
            assert pit_time < 0.5 or car["v"] <= physics.PIT_SPEED + 0.01  # braking at the entry
            if isinstance(status, Status):
                seen_bit = seen_bit or status.pit[0]
    assert seen_bit and min_v_in_pit == 0.0
    assert physics.PIT_STOP_S < pit_time < 12
    car = sim.snapshot()["cars"][0]
    assert not car["pit"] and car["fuel"] > 13
    assert not poll_status(cu).pit[0]


def test_pit_requires_pit_lane_option():
    sim = make_sim(pit_lane=False)
    cu = make_cu(sim)
    cu.setbot(0, 8)
    assert not any(snap["cars"][0]["pit"] for snap, _ in arm_before_pit(sim, cu))


def test_phase_stays_idle_without_start_button():
    sim = make_sim()
    for snap in run(sim, 5):
        assert snap["phase"] == 0


# --- start sequence --------------------------------------------------------------------


def phase_durations(sim: Simulator, seconds: float) -> list[tuple[int, float]]:
    """(phase, duration) of the phases traversed, in 10 ms steps."""
    out: list[list[float]] = [[sim.phase, 0.0]]
    for snap in run(sim, seconds, dt=0.01):
        if snap["phase"] == out[-1][0]:
            out[-1][1] += 0.01
        else:
            out.append([snap["phase"], 0.01])
    return [(int(p), d) for p, d in out]


def test_start_sequence_goes_through_all_phases():
    sim = make_sim(humans=[1])
    cu = make_cu(sim)
    cu.start()
    assert sim.phase == 1
    sim.step(2.0)
    assert sim.phase == 1  # armed: nothing moves until Start is pressed again
    cu.start()
    seq = phase_durations(sim, 8)
    phases = [p for p, _ in seq]
    assert phases == [2, 3, 4, 5, 6, 7, 0]
    durations = dict(seq)
    assert all(0.95 <= durations[p] <= 1.1 for p in (2, 3, 4, 5))
    assert 0.15 <= durations[6] <= 1.3
    assert 0.45 <= durations[7] <= 0.6


def test_cars_do_not_move_until_the_lights_go_out():
    sim = make_sim(humans=[1, 2])
    cu = make_cu(sim)
    cu.setbot(0, 0)
    cu.start()
    cu.start()
    for snap in run(sim, 7, dt=0.02):
        if snap["phase"] in range(1, 7):
            assert all(c["v"] == 0 for c in snap["cars"])
            assert all(c["throttle"] == 0 for c in snap["cars"])
    assert sim.phase in (7, 0)


def test_false_start_when_throttle_is_applied_during_countdown():
    sim = make_sim()
    cu = make_cu(sim)
    cu.start()
    cu.start()
    sim.step(1.5)
    assert sim.phase == 3
    cu.setbot(0, 5)
    sim.step(0.05)
    assert sim.phase == 8
    assert all(c["v"] == 0 for c in sim.snapshot()["cars"])
    seq = phase_durations(sim, 4)
    assert seq[0][0] == 8 and 2.7 < seq[0][1] < 3.3  # false start for ~3 s
    assert seq[1][0] == 1  # then back to the armed state


def test_throttle_in_phase_one_is_not_a_false_start():
    sim = make_sim()
    cu = make_cu(sim)
    cu.start()
    cu.setbot(0, 5)
    sim.step(1.0)
    assert sim.phase == 1 and sim.snapshot()["cars"][0]["v"] == 0


def test_start_button_during_race_stops_the_cars_in_phase_one():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 8)
    sim.step(3.0)
    assert sim.snapshot()["cars"][0]["v"] > 1
    cu.start()
    assert sim.phase == 1
    sim.step(1.5)
    assert sim.snapshot()["cars"][0]["v"] == 0
    cu.setbot(0, 0)  # like the app: throttle 0 during the countdown
    cu.start()
    sim.step(7)
    assert sim.phase == 0
    cu.setbot(0, 8)
    sim.step(3)
    assert sim.snapshot()["cars"][0]["v"] > 1


def test_pace_button_toggles_speed_cap():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 15)
    cu.press(ControlUnit.PACE_CAR_ESC_BUTTON_ID)
    assert sim.snapshot()["pace"]
    top = max(snap["cars"][0]["v"] for snap in run(sim, 6))
    assert top <= 0.4 * physics.VMAX + 0.01
    cu.press(ControlUnit.PACE_CAR_ESC_BUTTON_ID)
    assert not sim.snapshot()["pace"]


def test_other_buttons_are_echoed_only():
    sim = make_sim()
    cu = make_cu(sim)
    for button in (5, 6, 7, 8):
        assert cu.press(button) == b"T"
    assert sim.phase == 0 and not sim.snapshot()["pace"]


# --- position tower (J words 6, 17, 18) --------------------------------------------------


def test_position_tower_state_is_recorded():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setpos(2, 1)
    cu.setpos(0, 3)
    cu.setlap(0x2B)
    tower = sim.snapshot()["tower"]
    assert tower == {"positions": {2: 1, 0: 3}, "lap": 0x2B}
    cu.clrpos()
    assert sim.snapshot()["tower"]["positions"] == {}


# --- simulated humans ---------------------------------------------------------------------


def start_race(sim: Simulator, cu: ControlUnit) -> None:
    cu.start()
    cu.start()
    sim.step(8)
    assert sim.phase == 0


def test_humans_drive_clean_plausible_laps_after_the_start():
    sim = make_sim(humans=[1, 2], seed=7)
    cu = make_cu(sim)
    start_race(sim, cu)
    assert [c["controller"] for c in sim.snapshot()["cars"]][:3] == ["none", "human", "human"]
    laps1 = lap_times_ms(sim, cu, 1, 60)
    assert len(laps1) >= 6
    ok = [t for t in laps1 if t < 8000]
    assert len(ok) >= len(laps1) - 2  # a few crashes at most
    assert all(3500 <= t for t in laps1)
    assert sim.snapshot()["cars"][0]["v"] == 0  # the slot without a driver stays stopped


def test_humans_are_faster_than_a_moderate_bot():
    sim = make_sim(humans=[1], seed=3)
    cu = make_cu(sim)
    cu.setbot(0, 0)
    start_race(sim, cu)
    cu.setbot(0, 8)
    stamps = {0: [], 1: []}
    for _ in run(sim, 50):
        for e in drain_timers(cu):
            stamps[e.address].append(e.timestamp)

    def median_lap(ts: list[int]) -> int:
        return sorted(b - a for a, b in zip(ts, ts[1:]))[(len(ts) - 1) // 2]

    assert median_lap(stamps[1]) < 0.85 * median_lap(stamps[0])


def test_humans_react_after_lights_not_before():
    sim = make_sim(humans=[1], seed=2)
    cu = make_cu(sim)
    cu.start()
    cu.start()
    first_move = None
    for snap in run(sim, 9, dt=0.01):
        if first_move is None and snap["cars"][1]["v"] > 0:
            first_move = snap["phase"]
    assert first_move in (7, 0)


def test_human_jump_start_triggers_a_false_start(monkeypatch):
    monkeypatch.setattr(physics, "JUMP_START_PROB", 1.0)
    sim = make_sim(humans=[1])
    cu = make_cu(sim)
    cu.start()
    cu.start()
    assert 8 in {snap["phase"] for snap in run(sim, 6)}


def test_humans_pit_when_the_tank_is_low():
    sim = make_sim(humans=[1], seed=4)
    cu = make_cu(sim)
    sim.cars[1].fuel = 2.0
    start_race(sim, cu)
    pitted = any(snap["cars"][1]["pit"] for snap in run(sim, 40))
    assert pitted
    for _ in run(sim, 10):
        pass
    assert sim.snapshot()["cars"][1]["fuel"] > 10


def test_simulation_is_deterministic_for_a_seed():
    def trace(seed: int):
        sim = make_sim(humans=[1, 2], seed=seed, duplicate_rate=0.3)
        cu = make_cu(sim)
        start_race(sim, cu)
        out = []
        for snap in run(sim, 30):
            out.append((snap["cars"][1]["s"], snap["cars"][2]["s"], cu.poll()))
        return out

    assert trace(11) == trace(11)
    assert trace(11) != trace(12)


# --- link: outage, reconnection, new connection --------------------------------------------


def test_drop_link_raises_then_reconnect_keeps_state():
    sim = make_sim()
    cu = make_cu(sim)
    cu.setbot(0, 8)
    sim.step(2.0)
    assert sim.snapshot()["cars"][0]["v"] > 0

    sim.drop_link(0.15)
    with pytest.raises(cn.ConnectionError):
        cu.poll()
    with pytest.raises(cn.ConnectionError):
        sim.connect()
    with pytest.raises(cn.ConnectionError):
        cu.poll()
    sim.step(0.5)  # the track keeps running during the outage

    time.sleep(0.2)
    with pytest.raises(cn.ConnectionError):
        cu.poll()  # the old connection stays dead
    cu2 = make_cu(sim)
    assert cu2.version() == "5339"
    # firmware state and cars preserved: the bot throttle is still on
    assert sim.bot_mask == 1 and sim.bot_gas[0] == 8
    assert sim.snapshot()["cars"][0]["v"] > 0
    cu2.setbot(0, 0)  # the app watchdog forces throttle 0 after reconnection
    sim.step(2.0)
    assert sim.snapshot()["cars"][0]["v"] < 0.05


def test_recv_raises_when_link_drops_between_send_and_recv():
    sim = make_sim()
    conn = sim.connect(0.01)
    conn.send(b"?")
    sim.drop_link(10)
    with pytest.raises(cn.ConnectionError):
        conn.recv()


def test_new_connection_invalidates_the_previous_one():
    sim = make_sim()
    old = sim.connect(0.01)
    new = sim.connect(0.01)
    assert isinstance(old, SimConnection) and isinstance(new, SimConnection)
    with pytest.raises(cn.ConnectionError):
        old.send(b"?")
    new.send(b"0")
    assert new.recv().startswith(b"0")


def test_closed_connection_cannot_be_used():
    sim = make_sim()
    conn = sim.connect(0.01)
    conn.close()
    with pytest.raises(cn.ConnectionError):
        conn.send(b"?")
    sim.connect()  # reconnecting is possible


# --- real time -------------------------------------------------------------------------------


def test_realtime_thread_advances_clock_and_stops_on_close():
    sim = Simulator(SimConfig(latency_ms=0, seed=1, humans=[], time_scale=10.0), realtime=True)
    try:
        cu = ControlUnit(sim.connect(0.2))
        cu.setbot(0, 8)
        time.sleep(0.3)
        snap = sim.snapshot()
        assert snap["t"] > 1.0  # time_scale 10
        assert snap["cars"][0]["v"] > 0
        thread = sim._thread
        assert thread is not None and thread.daemon and thread.is_alive()
    finally:
        sim.close()
    assert not thread.is_alive()
