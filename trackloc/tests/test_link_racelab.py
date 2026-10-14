"""The RaceLab adapter against a real RaceLab app and API server (no mocks).

- publishing works with `cu.connect transport: none` (the server only refuses `track.*` with the virtual CU);
- crossings arrive as input dicts with `cu_ts` (and sector events would too), throttles on change;
- the whole pipeline publishes end to end through the real server;
- the discovery lap on the virtual CU: normal end, E-STOP mid-lap, lost connection: the car is always handed back.
"""

from __future__ import annotations

import threading
import time

import pytest

from trackloc.drive import DiscoveryDriver, DriveConfig, MapAborted
from trackloc.link_racelab import RaceLabLink

CIRCLE = {"points": [[round(2 + 1.5 * __import__("math").cos(a / 40 * 6.283), 4), round(1.0 + 0.9 * __import__("math").sin(a / 40 * 6.283), 4)]
                     for a in range(40)], "y_axis": "down", "finish": 0.0, "changers": [0.3], "lane_offset": 0.05,
          "name": "Test camera track", "length_m": [8.0, 8.4]}


def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_publishing_works_without_a_cu_and_the_wire_format_is_the_documented_one(racelab_factory):
    server = racelab_factory("none")  # `cu.connect transport: none`
    link = RaceLabLink(server.url, "camera").connect()
    try:
        shape = link.client.set_track(CIRCLE)
        assert shape["source"] == "camera" and shape["finish"] == 0.0 and shape["changers"] == [0.3] and shape["name"] == "Test camera track"
        link.publish(0.0, [{"slot": 0, "s": 0.25, "lane": 1, "v": 2.1, "off": False}, {"slot": 3, "s": 0.9, "lane": 0, "v": 0.0, "off": True}])
        pos = server.call(lambda: server.app.snapshot()["positions"])
        assert pos["source"] == "camera" and not pos["stale"]
        assert pos["cars"][0] == {"slot": 0, "s": 0.25, "lane": 1, "v": 2.1, "off": False} and pos["cars"][1]["off"] is True
        assert server.call(lambda: server.app.meta()["track"]["name"]) == "Test camera track"
    finally:
        link.clear_track()
        link.close()
    assert wait_for(lambda: server.call(lambda: server.app.track_feed.owner) is None)  # the track disappears with us


def test_the_virtual_cu_refuses_a_connector_as_documented(racelab_factory):
    server = racelab_factory("sim")
    link = RaceLabLink(server.url, "camera").connect()
    try:
        with pytest.raises(Exception, match="virtual CU"):
            link.client.set_track(CIRCLE)
    finally:
        link.close()


def test_crossings_and_throttles_arrive_as_plain_input_dicts(racelab_factory):
    server = racelab_factory("sim", time_scale=6.0)
    link = RaceLabLink(server.url, "camera").connect()
    got, raw = [], []
    link.raw = lambda kind, name, data, t: raw.append((kind, name))
    link.start_inputs(got.append)
    try:
        assert link.ensure_session() is True  # crossings only count in a running session: practice has no lights
        link.claim(0)
        link.act(0, throttle=9)
        assert wait_for(lambda: sum(1 for m in got if m["type"] == "crossing") >= 2, 20)
        cross = [m for m in got if m["type"] == "crossing"]
        assert all(set(m) == {"type", "t", "slot", "sector", "cu_ts"} and m["slot"] == 0 and m["sector"] == 1
                   and isinstance(m["cu_ts"], int) and 0 <= m["cu_ts"] < 2 ** 32 for m in cross)
        assert abs(cross[0]["t"] - time.monotonic()) < 30  # the server's time.monotonic(): the same host clock
        thr = [m for m in got if m["type"] == "throttle" and m["slot"] == 0]
        assert thr and thr[0]["applied"] == 9
        link.act(0, throttle=0)
        link.unclaim(0)
        assert wait_for(lambda: [m for m in got if m["type"] == "throttle"][-1]["applied"] in (0, None))
        assert ("event", "crossing") in raw and ("state", "state") in raw
    finally:
        link.stop_session()
        link.close()


def test_the_whole_pipeline_publishes_through_the_real_server(racelab_factory, tmp_path):
    pytest.importorskip("cv2")
    from trackloc import bench
    from trackloc.pipeline import Pipeline, PipelineConfig, run_stream
    from trackloc.synth import CameraSpec

    cam = CameraSpec(width=640, height=360)
    model = bench.map_from(bench.discovery(cam, noise=1.0))
    sess = bench.race(cam, duration=10.0, hands=(), seed=61, noise=1.0)
    server = racelab_factory("none")
    link = RaceLabLink(server.url, "camera").connect()
    try:
        pipe = Pipeline(model, PipelineConfig(latency_s=bench.LATENCY))
        run_stream(pipe, sess.stream(), link, publish_hz=30.0)
        shape = server.call(lambda: server.app.track_feed.geometry)
        assert shape is not None and len(shape["points"]) >= 100 and shape["finish"] == 0.0 and shape["length_m"][0] > 5
        pos = server.call(lambda: server.app.snapshot()["positions"])
        assert pos["source"] == "camera" and {c["slot"] for c in pos["cars"]} == {0, 1, 2}
        last = {c["slot"]: c for c in pipe.log[-1]["cars"]}
        # the server holds the last published frame: s, lane, v are the pipeline's, in the documented units
        # (publication is every other frame at 30 Hz of stream time: allow one frame of motion)
        for c in pos["cars"]:
            assert 0 <= c["s"] < 1 and c["lane"] in (0, 1) and 0 < c["v"] < 5
            assert abs(((c["s"] - last[c["slot"]]["s"]) + 0.5) % 1.0 - 0.5) < 0.02
    finally:
        link.close()


# ----------------------------------------------------------------------------- the discovery lap on the virtual CU

class Counting:
    """Wraps the link: counts claims and keeps a record of what was sent."""

    def __init__(self, link):
        self.link, self.claims, self.acts = link, 0, []

    def claim(self, slot):
        self.claims += 1
        self.link.claim(slot)

    def act(self, slot, throttle=None, lane=False):
        self.acts.append(throttle)
        self.link.act(slot, throttle, lane)

    def __getattr__(self, name):
        return getattr(self.link, name)


def drive_setup(racelab_factory, laps=2):
    server = racelab_factory("sim", time_scale=6.0)
    link = RaceLabLink(server.url, "camera").connect()
    crossings = []
    link.start_inputs(lambda m: crossings.append(m) if m["type"] == "crossing" else None)
    ctl = Counting(link)
    drv = DiscoveryDriver(ctl, 0, DriveConfig(throttle=6, laps=laps, lanes=1, stall_s=30.0),
                          progress=lambda: time.monotonic() * 1000, crossings=lambda: len(crossings))
    return server, link, ctl, drv, crossings


def slot_state(server):
    return server.call(lambda: (server.app.bots.slots[0].driver, server.app.bots.slots[0].api_value,
                                server.app.engine.state, server.app.bots.estopped))


def test_discovery_lap_on_the_virtual_cu_ends_with_the_car_handed_back(racelab_factory):
    server, link, ctl, drv, crossings = drive_setup(racelab_factory)
    try:
        out = drv.run()
        assert out["outcome"] == "done" and len(crossings) >= 3 and ctl.claims == 1
        driver, value, session, estop = slot_state(server)
        assert driver is None and value == 0 and session not in ("running", "finishing") and not estop
        assert ctl.acts[-1] == 0
    finally:
        link.close()


def test_estop_mid_lap_aborts_and_the_car_stays_stopped(racelab_factory):
    server, link, ctl, drv, crossings = drive_setup(racelab_factory, laps=9)
    result = {}

    def run():
        try:
            drv.run()
        except MapAborted as e:
            result["abort"] = str(e)

    th = threading.Thread(target=run)
    th.start()
    try:
        assert wait_for(lambda: len(crossings) >= 1, 20)
        server.call(lambda: server.app.bots.estop())  # the big red button
        th.join(15)
        assert not th.is_alive() and "taken from us" in result["abort"]
        driver, value, session, estop = slot_state(server)
        assert driver is None and value == 0 and estop  # E-STOP stays on: clearing it is a human decision
        assert ctl.claims == 1  # and we never claimed the car again
    finally:
        link.close()


def test_losing_the_connection_mid_lap_cuts_the_throttle_on_the_server(racelab_factory):
    server, link, ctl, drv, crossings = drive_setup(racelab_factory, laps=9)
    th = threading.Thread(target=lambda: _swallow(drv.run))
    th.start()
    try:
        assert wait_for(lambda: len(crossings) >= 1, 20)
        assert slot_state(server)[1] == 6  # driven
        link.client.close()  # the connection dies (no unclaim): RaceLab's own safety must zero the throttle
        th.join(15)
        assert not th.is_alive()
        assert wait_for(lambda: slot_state(server)[1] == 0)
        assert server.call(lambda: server.app.bots.slots[0].agent) is None
    finally:
        link.close()


def _swallow(fn):
    try:
        fn()
    except BaseException:  # noqa: BLE001 - the driver reports its own abort
        pass
