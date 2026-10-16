"""Connector card: a name, a status and a link to the connector's own page."""

from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from racelab.api.server import create_web_app
from racelab.app import OpError, RaceLabApp
from racelab.config import Config


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 30))


async def started(tmp_path, sim=False):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    if sim:
        cfg.connection.transport = "sim"
        cfg.sim.latency_ms = cfg.sim.jitter_ms = 0
    app = RaceLabApp(cfg)
    client = TestClient(TestServer(create_web_app(app)))
    await client.start_server()
    await app.start()
    return app, client


async def call(ws, rid, op, **args):
    await ws.send_json({"id": rid, "op": op, **args})
    while True:
        msg = await ws.receive_json()
        if msg["type"] == "reply" and msg["id"] == rid:
            return msg


def test_set_replace_clear_and_disconnect(tmp_path):
    async def main():
        app, client = await started(tmp_path)
        events = []
        app.subscribe(lambda m: m.get("name") == "connectors" and events.append(m["data"]["connectors"]))
        try:
            ws = await client.ws_connect("/ws?agent=camera")
            await ws.receive_json()
            r = await call(ws, 1, "connector.set", name=" Camera ", ui_url="http://pi.local:8090/ui",
                           status="tracking 6 cars")
            card = {"agent": "camera", "name": "Camera", "ui_url": "http://pi.local:8090/ui",
                    "status": "tracking 6 cars"}
            assert r["ok"] and r["result"] == card
            state = await (await client.get("/api/state")).json()
            assert state["connectors"] == [card]
            # replaced, not added; optional fields default to null
            await call(ws, 2, "connector.set", name="Camera 2")
            state = await (await client.get("/api/state")).json()
            assert state["connectors"] == [{"agent": "camera", "name": "Camera 2", "ui_url": None, "status": None}]
            # one card per connection
            ws2 = await client.ws_connect("/ws?agent=gates")
            await ws2.receive_json()
            await call(ws2, 1, "connector.set", name="Gates", status="ok")
            assert [c["agent"] for c in app.connectors()] == ["camera", "gates"]
            # clear removes only its own
            assert (await call(ws, 3, "connector.clear"))["ok"]
            assert [c["agent"] for c in app.connectors()] == ["gates"]
            assert (await call(ws, 4, "connector.clear"))["ok"]  # nothing to clear: not an error
            # disconnect removes the card
            await ws2.close()
            end = time.monotonic() + 3
            while app.connectors() and time.monotonic() < end:
                await asyncio.sleep(0.02)
            assert app.connectors() == [] and events[-1] == []
            assert (await (await client.get("/api/state")).json())["connectors"] == []
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_not_refused_with_virtual_cu(tmp_path):
    async def main():
        app, client = await started(tmp_path, sim=True)
        try:
            await app.handle("connector.set", {"name": "Camera"}, "cam")  # the virtual CU does not refuse it
            assert (await (await client.get("/api/state")).json())["connectors"][0]["agent"] == "cam"
            # over REST a card could never be removed (no connection closes): refused, nothing added
            for n in range(3):
                r = await (await client.post("/api/connector.set", json={"name": f"x{n}"},
                                             headers={"X-Agent": f"rest{n}"})).json()
                assert r["ok"] is False and "WebSocket" in r["error"]
            assert len((await (await client.get("/api/state")).json())["connectors"]) == 1
        finally:
            await app.stop()
            await client.close()

    run(main())


@pytest.mark.parametrize("args, message", [
    ({}, "name"),
    ({"name": "  "}, "empty"),
    ({"name": 5}, "text"),
    ({"name": "x" * 41}, "too long"),
    ({"name": "a\nb"}, "control"),
    ({"name": "a", "status": "s" * 81}, "status is too long"),
    ({"name": "a", "status": 3}, "status must be text"),
    ({"name": "a", "ui_url": "http://h/" + "p" * 300}, "ui_url is too long"),
    ({"name": "a", "ui_url": "javascript:alert(1)"}, "http or https"),
    ({"name": "a", "ui_url": "JavaScript://host/%0Aalert(1)"}, "http or https"),
    ({"name": "a", "ui_url": "data:text/html,<script>1</script>"}, "http or https"),
    ({"name": "a", "ui_url": "file:///etc/passwd"}, "http or https"),
    ({"name": "a", "ui_url": "//host/page"}, "http or https"),
    ({"name": "a", "ui_url": "http://"}, "http or https"),
    ({"name": "a", "ui_url": "http://a b/"}, "http or https"),
])
def test_validation(tmp_path, args, message):
    async def main():
        app, _client = await started(tmp_path)
        try:
            with pytest.raises(OpError, match=message):
                await app.handle("connector.set", args, "cam")
            assert app.connectors() == []  # a refused card leaves nothing behind
        finally:
            await app.stop()
            await _client.close()

    run(main())


def test_limits_are_inclusive_and_https_ok(tmp_path):
    async def main():
        app, client = await started(tmp_path)
        try:
            url = "https://h.example/" + "p" * (300 - len("https://h.example/"))
            assert len(url) == 300
            card = await app.handle("connector.set", {"name": "n" * 40, "status": "s" * 80, "ui_url": url}, "cam")
            assert card["ui_url"] == url and len(card["name"]) == 40 and len(card["status"]) == 80
            # a refused replacement keeps the previous card
            with pytest.raises(OpError):
                await app.handle("connector.set", {"name": "x", "ui_url": "javascript:1"}, "cam")
            assert app.connectors()[0]["name"] == "n" * 40
        finally:
            await app.stop()
            await client.close()

    run(main())
