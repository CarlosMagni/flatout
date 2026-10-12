"""CU choice from the UI: starting without a CU, hot switching, remembering, quitting."""

from __future__ import annotations

import asyncio
import json
import time

from aiohttp.test_utils import TestClient, TestServer

from racelab.__main__ import build_config, parse_args
from racelab.api.server import create_web_app
from racelab.app import RaceLabApp
from racelab.config import Config


def fresh_config(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.connection.reconnect_delay = 0.1
    cfg.sim.latency_ms = cfg.sim.jitter_ms = 0
    cfg.sim.humans = []
    return cfg


async def started(cfg):
    app = RaceLabApp(cfg)
    client = TestClient(TestServer(create_web_app(app)))
    await client.start_server()
    await app.start()
    return app, client


async def op(client, name, **args):
    return await (await client.post(f"/api/{name}", json=args)).json()


async def wait_until(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("condition never reached")


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 60))


def test_starts_without_cu_then_connects(tmp_path):
    async def main():
        app, client = await started(fresh_config(tmp_path))
        try:
            state = await (await client.get("/api/state")).json()
            assert state["link"]["transport"] == "none" and state["link"]["state"] == "idle"
            await op(client, "race.setup", settings={"type": "race", "laps": 3})
            refused = await op(client, "race.start")
            assert refused["ok"] is False and "CU" in refused["error"]
            assert (await op(client, "cu.connect", transport="sim"))["ok"]
            await wait_until(lambda: app.link_info["state"] == "connected")
            assert app.link_info["bots_supported"] is True and app.sim is not None
            assert (await (await client.get("/api/meta")).json())["track"]["points"]
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_hot_switch_releases_bots_and_ignores_old_link(tmp_path):
    async def main():
        app, client = await started(fresh_config(tmp_path))
        try:
            await op(client, "cu.connect", transport="sim")
            await wait_until(lambda: app.link_info["state"] == "connected")
            first_sim = app.sim
            await op(client, "bot.take", slot=1, driver="manual")
            await op(client, "bot.throttle", slot=1, value=6)
            await wait_until(lambda: first_sim.host_throttle(1) == 6)
            # switch to a CU on stock firmware
            assert (await op(client, "cu.connect", transport="sim", stock=True))["ok"]
            assert first_sim.host_throttle(1) is None  # bot handed back to the controller on the old CU
            assert app.sim is not first_sim and app.bots.slots[1].driver is None
            await wait_until(lambda: app.link_info["state"] == "connected")
            await asyncio.sleep(0.3)  # the last messages from the old link are ignored
            # a stock V339 still takes software drivers, through its C table
            assert app.link_info["bots_supported"] is True and app.link_info["bot_transport"] == "C"
            assert app.link_info["state"] == "connected"
            # session in progress: change refused
            await op(client, "race.setup", settings={"type": "practice"})
            await op(client, "race.start")
            busy = await op(client, "cu.connect", transport="none")
            assert busy["ok"] is False and "session" in busy["error"]
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_choice_is_remembered_but_cli_override_is_not(tmp_path):
    async def main():
        app, client = await started(fresh_config(tmp_path))
        try:
            await op(client, "cu.connect", transport="sim", stock=True)
        finally:
            await app.stop()
            await client.close()

    run(main())
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["connection_choice"] == {"transport": "sim", "device": "", "stock": True}
    cfg = build_config(parse_args(["--data-dir", str(tmp_path)]))
    assert cfg.connection.transport == "sim" and cfg.sim.bot_firmware is False
    # a launch option applies without overwriting the remembered choice
    cli = build_config(parse_args(["--data-dir", str(tmp_path), "--serial", "/dev/null"]))
    assert cli.connection.transport == "serial"
    assert cli.connection_choice["transport"] == "sim"


def test_quit_from_ui(tmp_path):
    async def main():
        app, client = await started(fresh_config(tmp_path))
        stopped = asyncio.Event()
        app.request_quit = stopped.set
        try:
            assert (await op(client, "app.quit"))["ok"]
            await asyncio.wait_for(stopped.wait(), 2)
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_scan_returns_a_list(tmp_path):
    async def main():
        app, client = await started(fresh_config(tmp_path))
        try:
            r = await op(client, "cu.scan", timeout=2)
            assert r["ok"] and isinstance(r["result"]["devices"], list)
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_advanced_mode_follows_bot_firmware(tmp_path):
    async def main():
        app, client = await started(fresh_config(tmp_path))
        try:
            assert app.mode == "classic"  # no CU: no advanced mode
            r = await op(client, "mode.set", mode="advanced")
            assert r["ok"] is False and "no CU chosen" in r["error"]
            await op(client, "cu.connect", transport="sim")
            await wait_until(lambda: app.mode == "advanced")  # advanced preference + bot module
            await op(client, "bot.take", slot=2, driver="manual")
            # an older CU that cannot take software drivers: automatic return to classic, bots handed back
            app.cfg.sim.firmware = "337"
            await op(client, "cu.connect", transport="sim", stock=True)
            await wait_until(lambda: app.link_info["state"] == "connected")
            assert app.mode == "classic" and app.bots.slots[2].driver is None
            r = await op(client, "mode.set", mode="advanced")
            assert r["ok"] is False and "software drivers" in r["error"]
            state = await (await client.get("/api/state")).json()
            assert state["advanced_unavailable"] and state["mode_preferred"] == "advanced"
            # a CU that can again: bot mode comes back by itself
            app.cfg.sim.firmware = "339"
            await op(client, "cu.connect", transport="sim")
            await wait_until(lambda: app.mode == "advanced")
        finally:
            await app.stop()
            await client.close()
        assert app.link.unsafe_slots() == []  # clean shutdown: no warning

    run(main())


def test_mode_choice_is_remembered(tmp_path):
    async def main():
        app, client = await started(fresh_config(tmp_path))
        try:
            assert (await op(client, "mode.set", mode="classic"))["ok"]
        finally:
            await app.stop()
            await client.close()

    run(main())
    cfg = build_config(parse_args(["--data-dir", str(tmp_path)]))
    assert cfg.app.mode == "classic" and cfg.mode_choice == "classic"
    assert build_config(parse_args(["--data-dir", str(tmp_path), "--advanced"])).app.mode == "advanced"


def test_probe_retries_before_concluding():
    from carreralib.connection import Connection, TimeoutError

    from racelab.cu.unit import RaceControlUnit

    class Flaky(Connection):
        def __init__(self, answers):
            self.answers = answers

        def send(self, buf, offset=0, size=None):
            pass

        def recv(self, maxlength=None):
            answer = self.answers.pop(0)
            if answer is None:
                raise TimeoutError("nothing")
            return answer

    assert RaceControlUnit(Flaky([None, b"B"]), on_message=lambda m: None).probe_bots() is True
    assert RaceControlUnit(Flaky([b"#"]), on_message=lambda m: None).probe_bots() is False
    try:
        RaceControlUnit(Flaky([None, None, None]), on_message=lambda m: None).probe_bots()
        raise AssertionError('three silences must not count as "stock firmware"')
    except TimeoutError:
        pass
