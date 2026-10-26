"""End to end: aiohttp server + RaceLabApp + virtual CU, over REST and WebSocket."""

from __future__ import annotations

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from racelab.api.server import create_web_app
from racelab.app import RaceLabApp
from racelab.config import Config


def make_config(tmp_path, mode="advanced", bot_firmware=True):
    cfg = Config()
    cfg.app.mode = mode
    cfg.app.data_dir = str(tmp_path)
    cfg.connection.transport = "sim"
    cfg.sim.latency_ms = 0
    cfg.sim.jitter_ms = 0
    cfg.sim.humans = []
    cfg.sim.seed = 1
    cfg.sim.bot_firmware = bot_firmware
    cfg.safety.deadman_ms = 400
    return cfg


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


async def started(cfg):
    app = RaceLabApp(cfg)
    client = TestClient(TestServer(create_web_app(app)))
    await client.start_server()
    await app.start()
    for _ in range(200):
        if app.link_info["state"] == "connected":
            break
        await asyncio.sleep(0.02)
    assert app.link_info["state"] == "connected"
    return app, client


async def stopped(app, client):
    await app.stop()
    await client.close()


async def op(client, name, **args):
    r = await client.post(f"/api/{name}", json=args, headers={"X-Agent": "test"})
    return await r.json()


async def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def test_rest_state_meta_and_errors(tmp_path):
    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            state = await (await client.get("/api/state")).json()
            assert state["link"]["bots_supported"] is True and state["mode"] == "advanced"
            assert len(state["bots"]) == 6 and state["session"]["state"] == "idle"
            meta = await (await client.get("/api/meta")).json()
            assert "race" in meta["session_types"] and meta["track"]["points"]
            bad = await op(client, "bot.throttle", slot=0, value=3)
            assert bad["ok"] is False and "manual" in bad["error"]
            unknown = await op(client, "nope")
            assert unknown["ok"] is False
            await op(client, "drivers.update", drivers=[{"slot": 5, "enabled": False}])
            off = await op(client, "bot.take", slot=5, driver="manual")
            assert off["ok"] is False and "disabled" in off["error"]
            index = await client.get("/")
            assert index.status == 200 and "RaceLab" in await index.text()
            js = await client.get("/ui/js/app.js")
            assert js.status == 200
        finally:
            await stopped(app, client)

    run(main())


def test_stop_never_waits_behind_a_slow_command(tmp_path, monkeypatch):
    """STOP sent right behind a command that waits, on the same WebSocket, is answered first: every
    op that waits is answered in the background (a console command to a silent CU waits 3 s)."""
    from racelab import app as app_mod

    monkeypatch.setattr(app_mod, "scan_devices", lambda timeout: time.sleep(1.5) or {"devices": [], "errors": []})

    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            ws = await client.ws_connect("/ws")
            assert (await ws.receive_json())["type"] == "hello"
            await ws.send_json({"id": 1, "op": "cu.scan"})
            await ws.send_json({"id": 2, "op": "estop"})
            replies = []
            while len(replies) < 2:
                msg = await ws.receive_json()
                if msg["type"] == "reply":
                    replies.append(msg["id"])
            assert replies == [2, 1]
            await ws.close()
        finally:
            await stopped(app, client)

    run(main())


def test_ws_agent_drives_a_car_and_deadman(tmp_path):
    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            ws = await client.ws_connect("/ws?agent=rl&state_hz=20")
            hello = await ws.receive_json()
            assert hello["type"] == "hello" and hello["client"] == "rl"

            async def call(rid, name, **args):
                await ws.send_json({"id": rid, "op": name, **args})
                while True:
                    msg = await ws.receive_json()
                    if msg["type"] == "reply" and msg["id"] == rid:
                        return msg

            async def drive(rid, throttle):
                # keep acting like a real agent loop: one act only holds for deadman_ms
                # (400 ms here), which a slow runner can miss entirely
                for _ in range(100):
                    assert (await call(rid, "bot.act", slot=0, throttle=throttle))["ok"]
                    if app.sim.host_throttle(0) == throttle:
                        return True
                    await asyncio.sleep(0.05)
                return False

            assert (await call(1, "bot.claim", slot=0, free=True))["ok"]
            assert await drive(2, 7)
            # the agent goes silent: dead-man -> throttle 0 on the CU
            assert await wait_until(lambda: app.sim.host_throttle(0) == 0, timeout=3)
            assert app.bots.slots[0].gate == "deadman"
            # priority E-STOP
            assert await drive(3, 9)
            assert (await call(4, "estop"))["ok"]
            assert await wait_until(lambda: app.sim.host_throttle(0) == 0)
            assert (await call(5, "estop.clear"))["ok"]
            # another agent cannot drive this slot
            r = await op(client, "bot.act", slot=0, throttle=15)
            assert r["ok"] is False
            await ws.close()
            # agent disconnects: throttle 0, the bot stays in charge of the slot
            assert await wait_until(lambda: app.bots.slots[0].agent is None)
            assert app.sim.host_throttle(0) == 0
        finally:
            await stopped(app, client)
        # clean shutdown: control handed back to the controller
        assert all(app.sim.host_throttle(slot) is None for slot in range(6))

    run(main())


def test_practice_session_counts_bot_laps(tmp_path):
    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            assert (await op(client, "race.setup", settings={"type": "practice"}))["ok"]
            assert (await op(client, "race.start"))["ok"]
            assert (await op(client, "bot.profile", slot=1, profile={"kind": "constant", "value": 10, "ramp_s": 0}))["ok"]
            assert (await op(client, "bot.take", slot=1, driver="profile"))["ok"]
            assert await wait_until(lambda: next(c for c in app.engine.snapshot()["cars"] if c["slot"] == 1)["laps"] >= 1, timeout=20)
            car = next(c for c in app.snapshot()["cars"] if c["slot"] == 1)
            assert car["controller"] == "bot" and 4000 < car["last_ms"] < 9000
            assert (await op(client, "race.stop"))["ok"]
            sessions = (await op(client, "sessions.list"))["result"]
            assert sessions and sessions[0]["type"] == "practice"
        finally:
            await stopped(app, client)

    run(main())


def test_link_drop_forces_zero_after_reconnect(tmp_path):
    async def main():
        cfg = make_config(tmp_path)
        cfg.connection.reconnect_delay = 0.1
        cfg.connection.timeout = 0.3
        app, client = await started(cfg)
        try:
            await op(client, "bot.take", slot=2, driver="manual")
            await op(client, "bot.throttle", slot=2, value=8)
            assert await wait_until(lambda: app.sim.host_throttle(2) == 8)
            assert (await op(client, "sim.drop_link", seconds=0.5))["ok"]
            assert await wait_until(lambda: app.link_info["state"] == "lost")
            assert await wait_until(lambda: app.link_info["state"] == "connected", timeout=6)
            # docs/bot-protocol.md §1: throttle 0 forced on reconnection, no resume at 8
            assert await wait_until(lambda: app.sim.host_throttle(2) == 0)
            await asyncio.sleep(0.3)
            assert app.sim.host_throttle(2) == 0 and app.bots.slots[2].manual == 0
            assert "reconnect" not in app.link.holds
            await op(client, "bot.throttle", slot=2, value=5)
            assert await wait_until(lambda: app.sim.host_throttle(2) == 5)
        finally:
            await stopped(app, client)

    run(main())


def test_classic_mode_and_stock_firmware(tmp_path):
    async def main():
        cfg = make_config(tmp_path, mode="classic", bot_firmware=False)
        cfg.sim.firmware = "337"  # an older CU: no proven C table, no bot module -> no software drivers
        app, client = await started(cfg)
        try:
            r = await op(client, "bot.take", slot=0, driver="manual")
            assert r["ok"] is False and "advanced" in r["error"]
            assert app.link_info["bots_supported"] is False
            r = await op(client, "race.setup", settings={"type": "training"})
            assert r["ok"] is False
            assert (await op(client, "race.setup", settings={"type": "race", "laps": 5}))["ok"]
            # this CU cannot take software drivers: bot mode is refused
            r = await op(client, "mode.set", mode="advanced")
            assert r["ok"] is False and "software drivers" in r["error"]
            assert app.mode == "classic"
            r = await op(client, "bot.take", slot=0, driver="manual")
            assert r["ok"] is False
        finally:
            await stopped(app, client)

    run(main())


def test_live_walk_steps():
    from racelab.app import live_walk_steps

    assert live_walk_steps(1, 10) == 1          # 10 wraps to 1 with one press
    assert live_walk_steps(10, 1) == 9          # 1 -> .. -> 10
    assert live_walk_steps(5, 5) == 0           # same level: open + validate only
    assert live_walk_steps(6, 7) == 9           # 7 -> wrap -> 6
    for bad in ((0, 5), (11, 5), (5, 0), (5, 11)):
        try:
            live_walk_steps(*bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} should raise")


def test_car_program_live_presses(tmp_path, monkeypatch):
    import racelab.app as app_mod
    monkeypatch.setattr(app_mod, "LIVE_PRESS_GAP_S", 0.01)

    async def main():
        app, client = await started(make_config(tmp_path, mode="classic"))
        try:
            r = await client.post("/api/car.program_live", json={"field": "speed", "level": 1, "current": 10})
            assert (await r.json())["ok"]
            for _ in range(150):                 # the link sends the queued presses
                if len(app.sim.press_log) >= 3:
                    break
                await asyncio.sleep(0.02)
            # open the display, one step, START validates: 10 -> 1 is a single press
            assert app.sim.press_log[:3] == [5, 5, 2]

            r = await client.post("/api/car.program_live", json={"field": "speed", "level": 5, "current": 1})
            assert (await r.json())["ok"]
            for _ in range(150):
                if len(app.sim.press_log) >= 9:
                    break
                await asyncio.sleep(0.02)
            # 1 -> 5 is four steps: open + 4 presses, then START
            assert app.sim.press_log[3:9] == [5, 5, 5, 5, 5, 2]

            r = await client.post("/api/car.program_live", json={"field": "wings", "level": 3, "current": 3})
            assert "field" in (await r.json())["error"]
            r = await client.post("/api/car.program_live", json={"field": "speed", "level": 12, "current": 10})
            assert (await r.json())["ok"] is False
        finally:
            await app.stop()
            await client.close()

    run(main())
