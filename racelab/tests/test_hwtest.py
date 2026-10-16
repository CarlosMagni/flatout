"""Hardware test bench: link.bench, test.pulse / lane / handback / report, on the virtual stock CU.

Safety paths first: every refusal, a pulse that ends at 0 and released whatever happens
(exception, cancellation, E-STOP, client gone), and a heartbeat pause that never stays on.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from racelab.app import HW_THROTTLE_MAX, OpError

from .test_api import make_config, run, started, stopped, wait_until


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
    assert app.bots.slots[slot].driver is None and app._hw_busy is None
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
            assert app._hw_busy is None
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


def test_bench_refused_without_link_or_when_busy(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            app._hw_busy = "pulse"
            await refused(app, "link.bench", "another hardware test")
            app._hw_busy = None
            app.link_info["state"] = "lost"
            await refused(app, "link.bench", "not connected")
        finally:
            app.link_info["state"] = "connected"
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


def test_pulse_limits_are_refused_not_clamped(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)
            assert HW_THROTTLE_MAX == 6
            for args, match in [({"throttle": 7, "ms": 300}, "1..6"), ({"throttle": 15, "ms": 300}, "1..6"),
                                ({"throttle": 0, "ms": 300}, "1..6"), ({"throttle": "4", "ms": 300}, "integer"),
                                ({"throttle": True, "ms": 300}, "integer"), ({"throttle": 4.5, "ms": 300}, "integer"),
                                ({"throttle": 4, "ms": 199}, "200..2000"), ({"throttle": 4, "ms": 2001}, "200..2000"),
                                ({"throttle": 4}, "integer"), ({"throttle": 4, "ms": 300, "slot": 6}, "0..5")]:
                await refused(app, "test.pulse", match, **{"slot": 1, **args})
            assert frames == [] and app.sim.host_throttle(1) is None
            # the configured ceiling lowers the pulse, never raises it
            app.cfg.safety.max_throttle = 3
            r = await app.handle("test.pulse", {"slot": 1, "throttle": 6, "ms": 200}, "ui")
            assert r["throttle"] == 3 and r["applied_peak"] == 3
            app.cfg.safety.max_throttle = 0
            await refused(app, "test.pulse", "max_throttle", slot=1, throttle=6, ms=200)
        finally:
            await stopped(app, client)

    run(main())


def test_non_finite_numbers_are_refused_cleanly(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            for bad in (float("inf"), float("-inf"), float("nan")):
                await refused(app, "test.pulse", "integer", slot=0, throttle=bad, ms=500)
                await refused(app, "test.pulse", "integer", slot=0, throttle=3, ms=bad)
            await at_rest(app, 0)
        finally:
            await stopped(app, client)

    run(main())


def test_pulse_refusals(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)
            args = {"slot": 1, "throttle": 3, "ms": 200}
            # a session is running
            await app.handle("race.setup", {"settings": {"type": "race", "laps": 5}}, "ui")
            await app.handle("race.start", {}, "ui")
            assert app.engine.state not in ("idle", "grid", "finished")
            await refused(app, "test.pulse", "session is running", **args)
            await refused(app, "test.lane", "session is running", slot=1, ms=300)
            await refused(app, "test.handback", "session is running", slot=1)
            await app.handle("race.stop", {}, "ui")
            assert await wait_until(lambda: app.engine.state in ("idle", "grid", "finished"))
            # E-STOP
            await app.handle("estop", {}, "ui")
            await refused(app, "test.pulse", "E-STOP", **args)
            await app.handle("estop.clear", {}, "ui")
            # slot owned by an API agent, or already a bot
            app.bots.api_claim(1, "rl-01", free=True)
            await refused(app, "test.pulse", "API agent", **args)
            app.bots.release(1)
            app.bots.take(2, "manual")
            await refused(app, "test.pulse", "already a bot", **{**args, "slot": 2})
            app.bots.release(2)
            # disabled driver
            await app.handle("drivers.update", {"drivers": [{"slot": 4, "enabled": False}]}, "ui")
            await refused(app, "test.pulse", "disabled", **{**args, "slot": 4})
            # another test is running
            app._hw_busy = "bench"
            await refused(app, "test.pulse", "another hardware test", **args)
            app._hw_busy = None
            # nothing moved
            assert app.sim.host_throttle(1) is None and not any(
                chr(f[1 + 2 * s]) not in "0P" for f in frames for s in range(6))
            # classic mode (bots off)
            await app.handle("mode.set", {"mode": "classic"}, "ui")
            await refused(app, "test.pulse", "With bots", **args)
        finally:
            await stopped(app, client)

    run(main())


def test_pulse_refused_on_a_cu_without_bots(tmp_path):
    async def main():
        cfg = make_config(tmp_path, bot_firmware=False)
        cfg.sim.firmware = "337"
        app, client = await started(cfg)
        try:
            assert app.link_info["bots_supported"] is False
            await refused(app, "test.pulse", "cannot take software drivers", slot=1, throttle=3, ms=200)
            await refused(app, "test.lane", "cannot take software drivers", slot=1)
        finally:
            await stopped(app, client)

    run(main())


def test_pulse_exception_mid_pulse_still_zeroes_and_releases(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)

            async def boom(slot, seconds):
                assert await wait_until(lambda: app.sim.host_throttle(slot) == 4)  # truly moving
                raise RuntimeError("boom")

            app._hw_hold = boom
            with pytest.raises(RuntimeError, match="boom"):
                await app.handle("test.pulse", {"slot": 1, "throttle": 4, "ms": 1000}, "ui")
            assert await wait_until(lambda: history(frames, 1).endswith("4P0"), 2), history(frames, 1)
            await at_rest(app, 1)
        finally:
            await stopped(app, client)

    run(main())


def test_pulse_exception_before_the_hold_still_releases(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)

            def broken(slot, value):
                raise RuntimeError("manager broke")

            app.bots.set_manual = broken  # even the zeroing through the manager fails: fall back to the link
            with pytest.raises(RuntimeError, match="manager broke"):
                await app.handle("test.pulse", {"slot": 1, "throttle": 4, "ms": 300}, "ui")
            assert await wait_until(lambda: history(frames, 1).endswith("P0"), 2), history(frames, 1)
            await at_rest(app, 1)
        finally:
            await stopped(app, client)

    run(main())


def test_pulse_cancelled_mid_hold(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)
            task = asyncio.create_task(app.handle("test.pulse", {"slot": 1, "throttle": 4, "ms": 2000}, "ui"))
            assert await wait_until(lambda: app.sim.host_throttle(1) == 4)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await wait_until(lambda: history(frames, 1).endswith("4P0"), 2), history(frames, 1)
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
            assert await wait_until(lambda: app._hw_busy is None, 3)
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


def test_handback_flag_cleared_after_an_error(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            frames = record_ctl(app)
            real = app._hw_hold
            calls = []

            async def hold(slot, seconds):
                calls.append(seconds)
                if app.link.hb_paused:  # the pause is on: blow up
                    raise RuntimeError("boom")
                return await real(slot, seconds)

            app._hw_hold = hold
            with pytest.raises(RuntimeError):
                await app.handle("test.handback", {"slot": 1, "throttle": 3, "pause_ms": 1000}, "ui")
            assert not app.link.hb_paused
            assert await wait_until(lambda: history(frames, 1).endswith("3P0"), 2), history(frames, 1)
            await at_rest(app, 1)
        finally:
            await stopped(app, client)

    run(main())


def test_handback_refusals(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            await refused(app, "test.handback", "1000..2500", slot=1, pause_ms=3000)
            await refused(app, "test.handback", "1..6", slot=1, throttle=9)
            # another bot would lose its slot with the pause: the whole C table stops refreshing
            app.bots.take(3, "manual")
            await refused(app, "test.handback", "free of bots", slot=1)
            app.bots.release(3)
            assert not app.link.hb_paused
        finally:
            await stopped(app, client)
        # B transport: the watchdog is another one (hold at 0), the test is about C
        cfg = make_config(tmp_path)
        cfg.connection.bot_transport = "B"
        app, client = await started(cfg)
        try:
            assert app.link_info["bot_transport"] == "B"
            await refused(app, "test.handback", "C command", slot=1)
            r = await app.handle("test.pulse", {"slot": 1, "throttle": 3, "ms": 200}, "ui")  # pulse works on B too
            assert r["acked"] and r["released"]
        finally:
            await stopped(app, client)

    run(main())


# ------------------------------------------------------------ report

def test_report_is_saved_by_the_server(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            r = await app.handle("test.report", {"report": {"format": 1, "steps": {"pulse": {"ok": True}},
                                                           "server": "forged", "name": "../../evil"}}, "ui")
            path = tmp_path / "hardware-tests" / r["name"]
            assert r["name"].startswith("hwtest-") and r["name"].endswith(".json") and path.is_file()
            saved = json.loads(path.read_text("utf-8"))
            assert saved["steps"]["pulse"]["ok"] is True
            assert saved["server"]["link"]["bot_transport"] == "C" and saved["server"]["racelab"]
            assert saved == r["report"]
            r2 = await app.handle("test.report", {"report": {"a": 1}}, "ui")
            assert r2["name"] != r["name"] and len(list((tmp_path / "hardware-tests").glob("*.json"))) == 2
            await refused(app, "test.report", "object", report="x")
            await refused(app, "test.report", "too large", report={"blob": "x" * 250_000})
            meta = await app.handle("meta", {}, "ui")
            assert meta["hwtest"]["throttle_max"] == 6
        finally:
            await stopped(app, client)

    run(main())


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


def test_bench_timeout_keeps_the_partial_data(tmp_path, monkeypatch):
    from racelab import app as app_module
    from racelab.cu.bench import Bench

    async def main():
        app, client = await stock(tmp_path)
        try:
            monkeypatch.setattr(app_module, "_bench_timeout", lambda n: 0.3)

            def slow_link(n, on_done):  # 3 slow polls seeded; n=300 is never reached in 0.3 s
                bench = Bench(n, 0, on_done)
                bench.poll += [0.02, 0.03, 0.4]
                app.link._bench = bench

            monkeypatch.setattr(app.link, "bench", slow_link)
            r = await app.handle("link.bench", {"n": 300}, "ui")
            assert r["timed_out"] is True and 3 <= r["answered"] < 300 and r["max_ms"] >= 400.0  # the live link adds polls
            assert app._hw_busy is None and app.link._bench is None
        finally:
            await stopped(app, client)

    run(main())


def test_bot_ops_cannot_touch_the_slot_under_test(tmp_path):
    async def main():
        app, client = await stock(tmp_path)
        try:
            task = asyncio.create_task(app.handle("test.pulse", {"slot": 1, "throttle": 4, "ms": 800}, "ui"))
            assert await wait_until(lambda: app.sim.host_throttle(1) == 4)
            for op, args in [("bot.throttle", {"slot": 1, "value": 15}), ("bot.release", {"slot": 1}),
                             ("bot.take", {"slot": 1, "driver": "manual"}), ("bot.claim", {"slot": 1}),
                             ("bot.act", {"actions": [{"slot": 1, "throttle": 15}]}),
                             # the bot ops parse the slot with int(): "1" and 1.0 are slot 1 too
                             ("bot.throttle", {"slot": "1", "value": 15}), ("bot.throttle", {"slot": 1.0, "value": 15}),
                             ("bot.take", {"slot": "1", "driver": "manual"}),
                             ("bot.act", {"actions": [{"slot": "1", "throttle": 15}]})]:
                await refused(app, op, "hardware test", **args)
            assert app.sim.host_throttle(1) == 4
            assert (await task)["aborted"] is None
            await app.handle("bot.take", {"slot": 1, "driver": "manual"}, "ui")  # free again afterwards
            app.bots.release(1)
        finally:
            await stopped(app, client)

    run(main())
