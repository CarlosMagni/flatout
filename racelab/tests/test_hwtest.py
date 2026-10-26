"""Hardware test bench in the app, on the virtual stock CU: what the CU really receives (C frames, the
heartbeat pause on the wire), STOP over the WebSocket, and how the app wires the bench in.

The bench's own rules (refusals, limits, always ending at 0 and released, the report) are unit
tests: test_hwtest_bench.py.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from racelab.app import RaceLabApp
from racelab.errors import OpError

from .test_api import make_config, op, run, started, stopped, wait_until


async def stock(tmp_path, **kw):
    app, client = await started(make_config(tmp_path, bot_firmware=False, **kw))
    assert app.link_info["bot_transport"] == "C"
    return app, client


def record_ctl(app):
    """Every C frame the virtual CU receives."""
    frames: list[bytes] = []
    orig = app.sim._cmd_ctl

    def rec(frame):
        frames.append(bytes(frame))
        return orig(frame)

    app.sim._cmd_ctl = rec
    return frames


def history(frames, slot):
    """Throttle characters of one slot over time, repeats removed ('P' = held at 0, '0' = handed back)."""
    out = ""
    for f in frames:
        c = chr(f[1 + 2 * slot])
        if not out or out[-1] != c:
            out += c
    return out


async def refused(app, op, match, **args):
    with pytest.raises(OpError, match=match):
        await app.handle(op, args, "ui")


async def at_rest(app, slot):
    assert await wait_until(lambda: app.sim.host_throttle(slot) is None, 2)
    assert app.bots.slots[slot].driver is None and app.hwtest.busy is None
    assert app.snapshot()["hwtest"] is None and not app.link.hb_paused


# ------------------------------------------------------------ latency

def test_bench_stats_and_c_ack(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)
            r = await app.handle("link.bench", {"n": 60}, "ui")
            assert r["n"] == 60 and r["answered"] + r["lost"] == 60 and r["answered"] > 0
            assert r["min_ms"] <= r["median_ms"] <= r["p95_ms"] <= r["max_ms"] and r["jitter_ms"] >= 0
            assert sum(b["count"] for b in r["histogram"]) == r["answered"]
            assert r["c_ack"]["n"] > 0 and r["bot_transport"] == "C" and r["transport"] == "sim"
            # idle: the only frames the bench added are "every slot on its controller"
            assert frames and all(f[1:13] == b"0" * 12 for f in frames)
            assert app.hwtest.busy is None
            await refused(app, "link.bench", "1..300", n=0)
            await refused(app, "link.bench", "1..300", n=301)
            await refused(app, "link.bench", "integer", n="5")
        finally:
            await stopped(app, client)

    run(main())


def test_bench_with_an_active_bot_adds_no_frame(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            app.bots.take(2, "manual")
            app.bots.set_manual(2, 5)
            assert await wait_until(lambda: app.sim.host_throttle(2) == 5)
            frames = record_ctl(app)
            r = await app.handle("link.bench", {"n": 30}, "ui")
            assert r["answered"] > 0
            assert all(chr(f[1 + 2 * 2]) == "5" for f in frames), "a bench frame must never change a bot"
            app.bots.release(2)
        finally:
            await stopped(app, client)

    run(main())


# ------------------------------------------------------------ pulse

def test_pulse_ends_at_zero_and_released(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)
            r = await app.handle("test.pulse", {"slot": 1, "throttle": 4, "ms": 300}, "ui")
            assert r["acked"] is True and r["applied_peak"] == 4 and r["aborted"] is None
            assert r["held_ms"] >= 280 and r["zeroed"] is True and r["released"] is True
            assert await wait_until(lambda: history(frames, 1).endswith("4P0"), 2), history(frames, 1)  # 4, then a 0 on the wire, then handed back
            await at_rest(app, 1)
        finally:
            await stopped(app, client)

    run(main())


def test_pulse_stops_on_estop_and_still_hands_back(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)
            task = asyncio.create_task(app.handle("test.pulse", {"slot": 1, "throttle": 4, "ms": 2000}, "ui"))
            assert await wait_until(lambda: app.sim.host_throttle(1) == 4)
            t0 = time.monotonic()
            await app.handle("estop", {}, "ui")
            r = await task
            assert r["aborted"] == "estop" and time.monotonic() - t0 < 1.0 and r["held_ms"] < 1500
            assert "P" in history(frames, 1)[history(frames, 1).index("4"):]
            await at_rest(app, 1)
            await app.handle("estop.clear", {}, "ui")
        finally:
            await stopped(app, client)

    run(main())


def test_ws_stop_is_not_queued_behind_a_pulse_and_client_loss_releases(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            ws = await client.ws_connect("/ws?agent=ui&state_hz=5")
            await ws.receive_json()
            await ws.send_json({"id": 1, "op": "test.pulse", "slot": 1, "throttle": 4, "ms": 2000})
            assert await wait_until(lambda: app.sim.host_throttle(1) == 4)
            await ws.send_json({"id": 2, "op": "estop"})
            order = {}
            while len(order) < 2:
                msg = await asyncio.wait_for(ws.receive_json(), 5)
                if msg["type"] == "reply":
                    order[msg["id"]] = msg
            assert list(order) == [2, 1], "STOP must be answered before the pulse ends"
            assert order[1]["ok"] and order[1]["result"]["aborted"] == "estop"
            await app.handle("estop.clear", {}, "ui")
            # the page is closed mid-pulse
            await ws.send_json({"id": 3, "op": "test.pulse", "slot": 1, "throttle": 4, "ms": 2000})
            assert await wait_until(lambda: app.sim.host_throttle(1) == 4)
            await ws.close()
            assert await wait_until(lambda: app.hwtest.busy is None, 3)
            await at_rest(app, 1)
        finally:
            await stopped(app, client)

    run(main())


# ------------------------------------------------------------ lane

def test_lane_request_while_running_and_release(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)
            r = await app.handle("test.lane", {"slot": 2, "throttle": 3, "ms": 400}, "ui")
            assert r["lane_sent"] is True and r["acked"] and r["released"] and r["throttle"] == 3
            assert any(f[2 + 2 * 2] == ord("1") for f in frames), "the lane character was never sent"
            assert app.sim.cars[2].armed is True
            assert await wait_until(lambda: history(frames, 2).endswith("3P0"), 2), history(frames, 2)
            await at_rest(app, 2)
            await refused(app, "test.lane", "1..6", slot=2, throttle=8)
            await refused(app, "test.lane", "200..2000", slot=2, ms=50)
        finally:
            await stopped(app, client)

    run(main())


# ------------------------------------------------------------ hand-back (watchdog)

def test_handback_pause_stops_the_refresh_and_always_ends_at_zero(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)
            task = asyncio.create_task(app.handle("test.handback", {"slot": 1, "throttle": 3, "pause_ms": 1000}, "ui"))
            assert await wait_until(lambda: app.link.hb_paused, 3)
            n = len(frames)
            # the app still wants 3, but nothing refreshes it: the CU hands the slot back by itself
            assert await wait_until(lambda: app.sim.host_throttle(1) is None, 1.5)
            assert app.link.bot_targets()[1] == 3 and len(frames) == n
            assert app.snapshot()["hwtest"]["phase"] == "paused"
            r = await task
            assert r["acked"] and r["paused_ms"] >= 950 and r["aborted"] is None and r["released"]
            assert await wait_until(lambda: history(frames, 1).endswith("3P0"), 2), history(frames, 1)  # after the pause: 0, then handed back, never 3 again
            await at_rest(app, 1)
        finally:
            await stopped(app, client)

    run(main())


# ------------------------------------------------------------ latency statistics

def test_bench_statistics():
    from racelab.cu.bench import histogram, stats

    xs = [0.010, 0.012, 0.011, 0.050, 0.010]
    s = stats(xs)
    assert (s["n"], s["min_ms"], s["max_ms"], s["median_ms"], s["p95_ms"]) == (5, 10.0, 50.0, 11.0, 50.0)
    assert s["jitter_ms"] == pytest.approx((2 + 1 + 39 + 40) / 4, abs=0.01)  # mean |difference| of neighbours
    h = histogram(xs)
    assert len(h) == 10 and sum(b["count"] for b in h) == 5 and h[0]["count"] == 4 and h[-1]["count"] == 1
    assert stats([]) is None and histogram([]) == []
    assert histogram([0.02, 0.02]) == [{"from_ms": 20.0, "to_ms": 20.0, "count": 2}]


# ------------------------------------------------------------ refusals, as the API carries them

def test_refusals_travel_with_their_reason_code(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            args = {"slot": 1, "throttle": 3, "ms": 200}
            none = {"latency": None, "move": None, "handback": None}
            assert (await (await client.get("/api/state")).json())["hwtest_why"] == none
            await app.handle("estop", {}, "ui")
            r = await op(client, "test.pulse", **args)
            assert r["ok"] is False and r["code"] == "estop" and "E-STOP" in r["error"] and r["params"] == {}
            state = await (await client.get("/api/state")).json()
            assert state["hwtest_why"] == {"latency": None, "move": "estop", "handback": "estop"}
            await app.handle("estop.clear", {}, "ui")
            await app.handle("drivers.update", {"drivers": [{"slot": 4, "enabled": False}]}, "ui")
            r = await op(client, "test.pulse", **{**args, "slot": 4})
            assert r["code"] == "slot_disabled" and r["params"]["car"] == 5 and r["params"]["name"]
            ws = await client.ws_connect("/ws?agent=ui&state_hz=5")
            await ws.receive_json()
            await ws.send_json({"id": 7, "op": "test.pulse", **{**args, "slot": 4}})
            while (reply := await asyncio.wait_for(ws.receive_json(), 5))["type"] != "reply":
                pass
            assert reply["id"] == 7 and reply["ok"] is False and reply["code"] == "slot_disabled"
            await ws.close()
            # a refusal about the arguments has no code: there is nothing for a page to word
            r = await op(client, "test.pulse", slot=1, throttle=9, ms=200)
            assert r["ok"] is False and "code" not in r and "params" not in r
            r = await op(client, "bot.take", slot=4, driver="manual")  # the same disabled driver, another page
            assert r["code"] == "slot_disabled"
            await app.handle("mode.set", {"mode": "classic"}, "ui")
            assert (await (await client.get("/api/state")).json())["hwtest_why"]["move"] == "mode"
        finally:
            await stopped(app, client)

    run(main())


def test_handback_is_about_the_c_command_pulse_works_on_b_too(tmp_path):
    async def main():
        cfg = make_config(tmp_path)
        cfg.connection.bot_transport = "B"  # the watchdog is another one (hold at 0)
        app, client = await started(cfg)
        try:
            assert app.link_info["bot_transport"] == "B"
            assert app.hwtest.why() == {"latency": None, "move": None, "handback": "transport"}
            await refused(app, "test.handback", "C command", slot=1)
            r = await app.handle("test.pulse", {"slot": 1, "throttle": 3, "ms": 200}, "ui")
            assert r["acked"] and r["released"]
        finally:
            await stopped(app, client)

    run(main())


# ------------------------------------------------------------ how the app wires the bench in (not started)

def bare_app(tmp_path):
    cfg = make_config(tmp_path)
    cfg.connection.transport = "none"
    return RaceLabApp(cfg)


def test_preconditions_come_in_the_order_the_page_words_them(tmp_path):
    app = bare_app(tmp_path)
    why = app.hwtest.why
    assert why() == {"latency": "link", "move": "link", "handback": "link"}
    app.link_info.update(state="connected")
    assert why() == {"latency": None, "move": "bots", "handback": "bots"}  # not probed yet
    app.link_info.update(bots_supported=True)
    app.engine.state = "running"  # a session, bots off: the session comes first, the mode cannot change meanwhile
    assert why()["move"] == "session"
    app.engine.state = "idle"
    assert why()["move"] == "mode"
    app.mode = "advanced"
    assert why() == {"latency": None, "move": None, "handback": "transport"}
    app.link_info.update(bot_transport="C")
    assert why() == {"latency": None, "move": None, "handback": None}
    app.bots.estopped = True
    assert why() == {"latency": None, "move": "estop", "handback": "estop"}
    app.bots.estopped = False
    app.link_info.update(bot_transport="I")  # through a controller interface: not the C command
    assert why()["handback"] == "transport"


def test_while_a_test_runs_the_app_only_watches_the_link_and_the_session(tmp_path):
    app = bare_app(tmp_path)
    with pytest.raises(OpError) as e:
        app._hw_preconditions("run", None)
    assert e.value.code == "link"
    app.link_info.update(state="connected")  # the CU does not need to take bots for this
    app._hw_preconditions("run", None)
    app.engine.state = "grid"
    app._hw_preconditions("run", None)
    app.engine.state = "starting"
    with pytest.raises(OpError) as e:
        app._hw_preconditions("run", None)
    assert e.value.code == "session"


def test_a_disabled_driver_refuses_its_slot(tmp_path):
    app = bare_app(tmp_path)
    app.link_info.update(state="connected", bots_supported=True, bot_transport="C")
    app.mode = "advanced"
    app.cfg.drivers[4].enabled = False
    app._hw_preconditions("move", None)  # no slot asked about
    app._hw_preconditions("move", 3)
    with pytest.raises(OpError, match="disabled") as e:
        app._hw_preconditions("move", 4)
    assert e.value.code == "slot_disabled" and e.value.params == {"name": app.cfg.drivers[4].name, "car": 5}


def test_the_bench_and_the_update_follow_the_current_link(tmp_path):
    """`_relink` replaces app.link: what holds the link must ask the app each time, never keep the old one."""
    app = bare_app(tmp_path)

    class Link:
        def bot_applied(self):
            return [None, 3, None, None, None, None]

        def firmware_update(self, lines, recovery=False):
            self.flashed = (len(lines), recovery)

    app.link = new = Link()
    assert app.hwtest.link.bot_applied()[1] == 3
    app._flash_cu([b"00", b"11"], True)
    assert new.flashed == (2, True)


def test_snapshot_and_meta_publish_the_bench_and_the_update(tmp_path):
    app = bare_app(tmp_path)
    snap = app.snapshot()
    assert snap["hwtest"] is None and snap["hwtest_why"] == {"latency": "link", "move": "link", "handback": "link"}
    assert snap["firmware"]["state"] == "idle" and snap["firmware"]["stuck"] is False
    assert not any(k.startswith("_") for k in snap["firmware"])
    assert app.meta()["hwtest"] == {"throttle_max": 6, "pulse_ms": [200, 2000], "handback_pause_ms": [1000, 2500],
                                    "bench_max": 300}
    app.hwtest.state = {"kind": "pulse", "slot": 1, "phase": "run"}
    assert app.snapshot()["hwtest"] == {"kind": "pulse", "slot": 1, "phase": "run"}


def test_bot_ops_cannot_touch_the_slot_under_test(tmp_path):
    async def main():
        app = bare_app(tmp_path)
        app.mode = "advanced"
        app.hwtest.state = {"kind": "pulse", "slot": 1, "phase": "run"}
        for name, args in [("bot.throttle", {"slot": 1, "value": 15}), ("bot.release", {"slot": 1}),
                           ("bot.take", {"slot": 1, "driver": "manual"}), ("bot.claim", {"slot": 1}),
                           ("bot.act", {"actions": [{"slot": 1, "throttle": 15}]}),
                           # the bot ops parse the slot with int(): "1" and 1.0 are slot 1 too
                           ("bot.throttle", {"slot": "1", "value": 15}), ("bot.throttle", {"slot": 1.0, "value": 15}),
                           ("bot.act", {"actions": [{"slot": "1", "throttle": 15}]})]:
            with pytest.raises(OpError, match="hardware test"):
                await app.handle(name, args, "ui")
        with pytest.raises(OpError, match="manual"):  # another slot: the op's own business
            await app.handle("bot.throttle", {"slot": 2, "value": 3}, "ui")

    run(main())


def test_report_is_saved_with_the_apps_state(tmp_path):
    app = bare_app(tmp_path)
    app.link_info.update(state="connected", bot_transport="C")
    r = app._op_test_report({"report": {"format": 1, "server": "forged", "name": "../../evil"}}, "ui")
    saved = json.loads((tmp_path / "hardware-tests" / r["name"]).read_text("utf-8"))
    assert saved == r["report"] and saved["format"] == 1
    server = saved["server"]
    assert server["link"]["bot_transport"] == "C" and server["racelab"] and server["mode"] == "classic"
    assert server["safety"]["max_throttle"] == app.cfg.safety.max_throttle and server["saved_at"]
    assert app.log[-1]["key"] == "hw_saved"
    with pytest.raises(OpError, match="object"):
        app._op_test_report({"report": "x"}, "ui")
