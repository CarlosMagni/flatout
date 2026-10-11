"""Track connector: circuit shape and positions supplied from the outside."""

from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from racelab.api.server import create_web_app
from racelab.app import RaceLabApp
from racelab.config import Config
from racelab.track import TrackError, TrackFeed, normalize_geometry, normalize_positions

SQUARE = [[0, 0], [100, 0], [200, 0], [200, 50], [200, 100], [100, 100], [0, 100], [0, 50]]


def test_geometry_is_normalized():
    g = normalize_geometry({"points": SQUARE, "lane_offset": 4, "finish": 0.25, "pit": [0.8, 0.95]})
    xs, ys = [p[0] for p in g["points"]], [p[1] for p in g["points"]]
    assert min(xs) == 0 and max(xs) == 1 and max(ys) == 0.5  # largest dimension = 1
    assert g["lane_offset"] == pytest.approx(0.02) and g["finish"] == 0.25 and g["pit"] == [0.8, 0.95]
    down = normalize_geometry({"points": SQUARE, "y_axis": "down"})  # image frame: y flipped
    assert down["points"][0] == [0.0, 0.5] and g["points"][0] == [0.0, 0.0]


@pytest.mark.parametrize("data, message", [
    ({"points": [[0, 0]] * 3}, "points"),
    ({"points": [[0, 0]] * 10}, "extent"),
    ({"points": [[0, "x"]] * 10}, "numbers"),
    ({"points": SQUARE, "finish": 2}, "finish"),
    ({"points": SQUARE, "y_axis": "left"}, "y_axis"),
    ({"points": SQUARE, "pit": [0.5]}, "pit"),
])
def test_geometry_validation(data, message):
    with pytest.raises(TrackError, match=message):
        normalize_geometry(data)


def test_positions_validation():
    assert normalize_positions([{"slot": 2, "s": 0.5, "lane": 1, "v": 2}])[0]["v"] == 2.0
    for bad in ([{"slot": 9, "s": 0}], [{"slot": 1, "s": 1.5}], [{"slot": 1, "s": 0.1, "lane": 2}],
                [{"slot": 1, "s": 0.1}, {"slot": 1, "s": 0.2}], [{"slot": 1, "s": 0.1, "v": -1}]):
        with pytest.raises(TrackError):
            normalize_positions(bad)


def test_feed_ownership_and_staleness():
    clock = [100.0]
    feed = TrackFeed(clock=lambda: clock[0])
    feed.set_geometry("cam", {"points": SQUARE})
    with pytest.raises(TrackError, match="cam"):
        feed.set_positions("other", [])
    feed.set_positions("cam", [{"slot": 0, "s": 0.1}])
    assert feed.snapshot()["stale"] is False
    clock[0] += 2
    assert feed.snapshot()["stale"] is True
    assert feed.clear("other") is False and feed.clear("cam") is True and feed.snapshot() is None


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 30))


def test_connector_over_websocket(tmp_path):
    async def main():
        cfg = Config()
        cfg.app.data_dir = str(tmp_path)  # no CU: the track comes from the connector
        app = RaceLabApp(cfg)
        client = TestClient(TestServer(create_web_app(app)))
        await client.start_server()
        await app.start()
        events = []
        app.subscribe(lambda m: m.get("name") == "track" and events.append(m["data"]))
        try:
            ws = await client.ws_connect("/ws?agent=camera")
            await ws.receive_json()

            async def call(rid, op, **args):
                await ws.send_json({"id": rid, "op": op, **args})
                while True:
                    msg = await ws.receive_json()
                    if msg["type"] == "reply" and msg["id"] == rid:
                        return msg

            r = await call(1, "track.set", geometry={"points": SQUARE, "y_axis": "down", "name": "My track"})
            assert r["ok"] and r["result"]["source"] == "camera"
            assert (await call(2, "track.positions", cars=[{"slot": 1, "s": 0.4, "lane": 0}]))["ok"]
            state = await (await client.get("/api/state")).json()
            assert state["positions"]["source"] == "camera" and state["positions"]["cars"][0]["s"] == 0.4
            meta = await (await client.get("/api/meta")).json()
            assert meta["track"]["name"] == "My track"
            # another connector cannot take over
            r = await client.post("/api/track.set", json={"geometry": {"points": SQUARE}},
                                  headers={"X-Agent": "intruder"})
            assert (await r.json())["ok"] is False
            await ws.close()
            end = time.monotonic() + 3
            while app.track_feed.owner and time.monotonic() < end:
                await asyncio.sleep(0.02)
            assert app.track_feed.owner is None and events[-1]["geometry"] is None
            state = await (await client.get("/api/state")).json()
            assert state["positions"] is None
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_simulator_keeps_its_own_track(tmp_path):
    async def main():
        cfg = Config()
        cfg.app.data_dir = str(tmp_path)
        cfg.connection.transport = "sim"
        cfg.sim.latency_ms = cfg.sim.jitter_ms = 0
        app = RaceLabApp(cfg)
        client = TestClient(TestServer(create_web_app(app)))
        await client.start_server()
        await app.start()
        try:
            r = await (await client.post("/api/track.set", json={"geometry": {"points": SQUARE}})).json()
            assert r["ok"] is False and "virtual CU" in r["error"]
            state = await (await client.get("/api/state")).json()
            assert state["positions"]["source"] == "sim" and len(state["positions"]["cars"]) == 6
        finally:
            await app.stop()
            await client.close()

    run(main())
