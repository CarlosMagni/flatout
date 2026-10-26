"""HwTestBench on its own: a fake CU link, the real BotManager, the app's preconditions as a function.

No app, no virtual CU, no server. What the CU really receives (C frames, the heartbeat pause on the
wire) is in test_hwtest.py; here: every refusal and its code, the limits, a test that always ends at
0 and released whatever happens, the latency test's outcomes and the report.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from racelab import hwtest
from racelab.bots.manager import BotManager
from racelab.config import SafetyConfig
from racelab.cu.bench import Bench
from racelab.errors import OpError
from racelab.events import LinkState
from racelab.hwtest import THROTTLE_MAX, HwTestBench

from .test_api import wait_until


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


class FakeLink:
    """The CU link as the bench sees it: what is set is on the wire at once (`frozen`: never)."""

    def __init__(self):
        self.applied = [None] * 6
        self.sent: list[tuple[int, object]] = []
        self.hb_paused = False
        self.pauses: list[float] = []
        self.frozen = False
        self.partial = None
        self.bench_calls = 0

    def set_bot(self, slot, value):
        self.sent.append((slot, value))
        if not self.frozen:
            self.applied[slot] = value

    def bot_applied(self):
        return list(self.applied)

    def lane(self, slot):
        self.sent.append((slot, "lane"))

    def estop(self): ...
    def clear_estop(self): ...
    def resume_bots(self): ...

    def pause_heartbeat(self, seconds):
        self.hb_paused = True
        self.pauses.append(seconds)

    def resume_heartbeat(self):
        self.hb_paused = False

    def bench(self, n, on_done):
        self.bench_calls += 1
        self.on_done = on_done

    def bench_cancel(self):
        partial, self.partial = self.partial, None
        return partial


class Rig:
    def __init__(self, tmp_path):
        self.link = FakeLink()
        self.safety = SafetyConfig()
        self.bots = BotManager(self.link, self.safety)
        self.bots.on_link(LinkState("connected", "sim", bots_supported=True))
        self.refuse: dict[str, str] = {}  # scope -> the code the app would refuse with
        self.asked: list[tuple[str, int | None]] = []
        self.logs: list[tuple[str, dict]] = []
        self.events: list[dict] = []
        self.bench = HwTestBench(
            self.link, self.bots, self.preconditions, safety=self.safety, folder=tmp_path / "hardware-tests",
            log=lambda level, text, key=None, /, **params: self.logs.append((key, params)),
            event=lambda name, data: self.events.append(data))

    def preconditions(self, scope, slot):
        self.asked.append((scope, slot))
        if code := self.refuse.get(scope):
            raise OpError(f"refused: {code}", code)

    def sent_to(self, slot):
        return [v for s, v in self.link.sent if s == slot]

    async def moving(self, slot, value):
        assert await wait_until(lambda: self.link.applied[slot] == value, 2)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


async def refused(call, code=None, match=None, **params):
    with pytest.raises(OpError, match=match) as e:
        await call
    if code is not None:
        assert e.value.code == code
    for k, v in params.items():
        assert e.value.params[k] == v
    return e.value


def at_rest(rig, slot):
    assert rig.bench.busy is None and rig.bench.snapshot() is None
    assert rig.bots.slots[slot].driver is None and rig.link.applied[slot] is None and not rig.link.hb_paused


# ------------------------------------------------------------ refusals

def test_what_each_test_asks_the_app(rig):
    async def main():
        for scope, call in [("move", rig.bench.pulse({"slot": 1, "throttle": 3, "ms": 200})),
                            ("move", rig.bench.lane({"slot": 1})),
                            ("handback", rig.bench.handback({"slot": 1})),
                            ("link", rig.bench.latency({"n": 5}))]:
            rig.refuse = {scope: "why"}
            err = await refused(call, "why")
            assert rig.asked[-1] == (scope, 1 if scope != "link" else None) and str(err) == "refused: why"
        assert rig.link.sent == [] and rig.link.bench_calls == 0 and rig.bots.slots[1].driver is None

    run(main())


def test_why_is_the_codes_the_page_shows(rig):
    assert rig.bench.why() == {"latency": None, "move": None, "handback": None}
    rig.refuse = {"link": "link", "move": "estop", "handback": "transport"}
    assert rig.bench.why() == {"latency": "link", "move": "estop", "handback": "transport"}
    rig.refuse = {}
    rig.bots.take(3, "manual")  # the hand-back test stops every slot's refresh: no bot may be running
    assert rig.bench.why() == {"latency": None, "move": None, "handback": "bots_active"}
    assert ("move", None) in rig.asked  # the page asks about no slot in particular


def test_the_slot_must_be_free(rig):
    async def main():
        rig.bots.take(2, "manual")
        await refused(rig.bench.pulse({"slot": 2, "throttle": 3, "ms": 200}), "slot_bot", "already a bot", car=3)
        rig.bots.release(2)
        rig.bots.take(3, "api")
        await refused(rig.bench.pulse({"slot": 3, "throttle": 3, "ms": 200}), "slot_api", "API agent", car=4)
        rig.bots.api_claim(3, "rl-01", free=True)
        await refused(rig.bench.lane({"slot": 3}), "slot_api", "rl-01", agent="rl-01")
        rig.bots.release(3)
        # a bot elsewhere does not matter to a pulse; the hand-back test wants them all gone
        rig.bots.take(4, "manual")
        await refused(rig.bench.handback({"slot": 1}), "bots_active", "free of bots")
        assert (await rig.bench.pulse({"slot": 1, "throttle": 3, "ms": 200}))["released"] is True
        rig.bots.release(4)

    run(main())


def test_only_one_test_at_a_time(rig):
    async def main():
        rig.bench.busy = "pulse"
        await refused(rig.bench.pulse({"slot": 1, "throttle": 3, "ms": 200}), "busy", "another hardware test", kind="pulse")
        await refused(rig.bench.latency({"n": 5}), "busy", "(pulse)")
        assert rig.bench.why()["move"] is None  # what the page shows of it is its own business

    run(main())


def test_limits_are_refused_not_clamped(rig):
    async def main():
        assert THROTTLE_MAX == 6
        for args, match in [({"throttle": 7, "ms": 300}, "1..6"), ({"throttle": 15, "ms": 300}, "1..6"),
                            ({"throttle": 0, "ms": 300}, "1..6"), ({"throttle": "4", "ms": 300}, "integer"),
                            ({"throttle": True, "ms": 300}, "integer"), ({"throttle": 4.5, "ms": 300}, "integer"),
                            ({"throttle": 4, "ms": 199}, "200..2000"), ({"throttle": 4, "ms": 2001}, "200..2000"),
                            ({"throttle": 4}, "integer"), ({"throttle": 4, "ms": 300, "slot": 6}, "0..5")]:
            await refused(rig.bench.pulse({"slot": 1, **args}), match=match)
        await refused(rig.bench.lane({"slot": 2, "throttle": 8}), match="1..6")
        await refused(rig.bench.lane({"slot": 2, "ms": 50}), match="200..2000")
        await refused(rig.bench.handback({"slot": 1, "pause_ms": 3000}), match="1000..2500")
        await refused(rig.bench.handback({"slot": 1, "throttle": 9}), match="1..6")
        for bad in (float("inf"), float("-inf"), float("nan")):
            await refused(rig.bench.pulse({"slot": 0, "throttle": bad, "ms": 500}), match="integer")
            await refused(rig.bench.pulse({"slot": 0, "throttle": 3, "ms": bad}), match="integer")
        for n in (0, 301, "5"):
            await refused(rig.bench.latency({"n": n}), match="1..300|integer")
        assert rig.link.sent == [] and rig.link.bench_calls == 0

    run(main())


def test_the_configured_ceiling_lowers_a_pulse_never_raises_it(rig):
    async def main():
        rig.safety.max_throttle = 3
        r = await rig.bench.pulse({"slot": 1, "throttle": 6, "ms": 200})
        assert r["throttle"] == 3 and r["applied_peak"] == 3 and r["acked"] is True
        rig.safety.max_throttle = 0
        await refused(rig.bench.pulse({"slot": 1, "throttle": 6, "ms": 200}), "no_throttle", "max_throttle")

    run(main())


# ------------------------------------------------------------ a test always ends at 0, released

def test_pulse_runs_and_hands_the_slot_back(rig):
    async def main():
        r = await rig.bench.pulse({"slot": 1, "throttle": 4, "ms": 300})
        assert r["acked"] is True and r["applied_peak"] == 4 and r["aborted"] is None and r["slot"] == 1
        assert r["held_ms"] >= 280 and r["zeroed"] is True and r["released"] is True
        assert rig.sent_to(1)[-3:] == [4, 0, None]  # 4, then a 0 on the wire, then handed back
        at_rest(rig, 1)
        assert [e["phase"] for e in rig.events] == ["start", "run", "done"]
        assert rig.events[1] == {"kind": "pulse", "slot": 1, "phase": "run"}
        assert rig.logs == [("hw_done", {"kind": "pulse", "car": 2, "reason": None})]

    run(main())


def test_a_slot_that_never_reaches_the_wire_is_reported_not_acked(rig):
    async def main():
        rig.link.frozen = True  # the CU link takes nothing
        r = await rig.bench.pulse({"slot": 1, "throttle": 4, "ms": 200})
        assert r["acked"] is False and r["held_ms"] == 0 and r["applied_peak"] == 0
        assert r["zeroed"] is False and r["released"] is True and rig.sent_to(1)[-2:] == [0, None]

    run(main())


def test_lane_request_while_running(rig):
    async def main():
        r = await rig.bench.lane({"slot": 2, "throttle": 3, "ms": 300})
        assert r["lane_sent"] is True and r["acked"] and r["released"] and r["throttle"] == 3
        assert rig.sent_to(2).count("lane") == 1 and rig.sent_to(2)[-2:] == [0, None]
        at_rest(rig, 2)
        assert rig.logs[-1][1]["kind"] == "lane"

    run(main())


def test_exception_mid_pulse_still_zeroes_and_releases(rig):
    async def main():
        async def boom(slot, seconds):
            await rig.moving(slot, 4)  # truly moving
            raise RuntimeError("boom")

        rig.bench._hold = boom
        with pytest.raises(RuntimeError, match="boom"):
            await rig.bench.pulse({"slot": 1, "throttle": 4, "ms": 1000})
        assert rig.sent_to(1)[-3:] == [4, 0, None]
        at_rest(rig, 1)

    run(main())


def test_the_manager_failing_falls_back_to_the_link(rig):
    async def main():
        def broken(slot, value):
            raise RuntimeError("manager broke")

        rig.bots.set_manual = broken  # even the zeroing through the manager fails
        with pytest.raises(RuntimeError, match="manager broke"):
            await rig.bench.pulse({"slot": 1, "throttle": 4, "ms": 300})
        assert rig.sent_to(1)[-2:] == [0, None]
        at_rest(rig, 1)

    run(main())


def test_a_second_cancellation_still_releases(rig):
    async def main():
        task = asyncio.create_task(rig.bench.pulse({"slot": 1, "throttle": 4, "ms": 2000}))
        await rig.moving(1, 4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rig.sent_to(1)[-3:] == [4, 0, None]
        at_rest(rig, 1)

    run(main())


def test_somebody_elses_slot_is_not_touched_at_the_end(rig):
    async def main():
        async def steal(slot, seconds):
            rig.bots.release(slot)
            rig.bots.take(slot, "api")  # an agent took the slot meanwhile
            return {"held_ms": 0, "applied_peak": 0, "gate": None, "aborted": None}

        rig.bench._hold = steal
        r = await rig.bench.pulse({"slot": 1, "throttle": 4, "ms": 300})
        assert r["zeroed"] is False and r["released"] is False and rig.bots.slots[1].driver == "api"

    run(main())


def test_estop_stops_the_pulse_and_it_still_hands_back(rig):
    async def main():
        task = asyncio.create_task(rig.bench.pulse({"slot": 1, "throttle": 4, "ms": 2000}))
        await rig.moving(1, 4)
        t0 = time.monotonic()
        rig.bots.estop()
        r = await task
        assert r["aborted"] == "estop" and time.monotonic() - t0 < 1.0 and r["held_ms"] < 1500
        assert rig.sent_to(1)[-2:] == [0, None]
        at_rest(rig, 1)
        assert rig.logs[-1] == ("hw_aborted", {"kind": "pulse", "car": 2, "reason": "estop"})

    run(main())


@pytest.mark.parametrize("code", ["link", "session"])
def test_the_link_or_a_session_stops_it_too(rig, code):
    async def main():
        task = asyncio.create_task(rig.bench.pulse({"slot": 1, "throttle": 4, "ms": 2000}))
        await rig.moving(1, 4)
        rig.refuse = {"run": code}  # what the app says while a test runs: link and session only
        r = await task
        assert r["aborted"] == code and rig.sent_to(1)[-2:] == [0, None]
        at_rest(rig, 1)

    run(main())


# ------------------------------------------------------------ hand-back (the heartbeat pause)

def test_handback_pauses_the_refresh_then_ends_at_zero(rig):
    async def main():
        r = await rig.bench.handback({"slot": 1, "throttle": 3, "pause_ms": 1000})
        assert r["acked"] and r["paused_ms"] >= 950 and r["aborted"] is None and r["released"] and r["pause_ms"] == 1000
        assert rig.link.pauses == [1.25]  # a little longer than the pause: it clears itself, whatever happens
        assert any(e["phase"] == "paused" and e["pause_ms"] == 1000 for e in rig.events)
        assert rig.sent_to(1)[-3:] == [3, 0, None]
        at_rest(rig, 1)

    run(main())


def test_the_pause_never_stays_on_after_an_error(rig):
    async def main():
        real = rig.bench._hold

        async def hold(slot, seconds):
            if rig.link.hb_paused:  # the pause is on: blow up
                raise RuntimeError("boom")
            return await real(slot, seconds)

        rig.bench._hold = hold
        with pytest.raises(RuntimeError):
            await rig.bench.handback({"slot": 1, "throttle": 3, "pause_ms": 1000})
        assert not rig.link.hb_paused and rig.sent_to(1)[-3:] == [3, 0, None]
        at_rest(rig, 1)

    run(main())


def test_handback_stopped_before_the_pause(rig):
    async def main():
        task = asyncio.create_task(rig.bench.handback({"slot": 1, "throttle": 3}))
        await rig.moving(1, 3)
        rig.bots.estop()
        r = await task
        assert r["aborted"] == "estop" and r["paused_ms"] == 0 and rig.link.pauses == []
        at_rest(rig, 1)

    run(main())


# ------------------------------------------------------------ latency

def test_latency_returns_what_the_link_measured(rig):
    async def main():
        rig.link.bench = lambda n, on_done: on_done({"n": n, "answered": n, "lost": 0}, None)
        r = await rig.bench.latency({"n": 20})
        assert r == {"n": 20, "answered": 20, "lost": 0, "timed_out": False}
        assert rig.bench.busy is None and rig.bench.snapshot() is None
        assert (await rig.bench.latency({}))["n"] == 100  # the default

    run(main())


def test_latency_while_the_bench_runs_shows_as_a_test_in_progress(rig):
    async def main():
        task = asyncio.create_task(rig.bench.latency({"n": 5}))
        assert await wait_until(lambda: rig.link.bench_calls == 1)
        assert rig.bench.busy == "bench" and rig.bench.snapshot() == {"kind": "bench", "slot": None, "phase": "run"}
        rig.link.on_done({"n": 5}, None)
        await task
        assert rig.bench.busy is None

    run(main())


def test_latency_errors(rig):
    async def main():
        def closed(n, on_done):
            raise ValueError("link closed")

        rig.link.bench = closed
        await refused(rig.bench.latency({"n": 5}), match="link closed")
        assert rig.bench.busy is None
        rig.link.bench = lambda n, on_done: on_done(None, "link lost")
        await refused(rig.bench.latency({"n": 5}), match="interrupted: link lost")
        assert rig.bench.busy is None and rig.bench.snapshot() is None

    run(main())


def test_latency_timeout_keeps_the_partial_data(rig, monkeypatch):
    async def main():
        monkeypatch.setattr(hwtest, "_bench_timeout", lambda n: 0.2)

        def slow(n, on_done):  # 3 slow polls seeded; n=300 is never reached in 0.2 s
            rig.link.partial = Bench(n, 0, on_done)
            rig.link.partial.poll += [0.02, 0.03, 0.4]

        rig.link.bench = slow
        r = await rig.bench.latency({"n": 300})
        assert r["timed_out"] is True and r["answered"] == 3 and r["max_ms"] >= 400.0
        assert rig.bench.busy is None
        rig.link.bench = lambda n, on_done: None  # nothing ever comes back
        await refused(rig.bench.latency({"n": 300}), match="did not answer in time")

    run(main())


# ------------------------------------------------------------ report

SERVER = {"racelab": "0.1", "mode": "advanced", "link": {"bot_transport": "C"}}


def test_report_is_saved_with_what_the_server_knows(rig, tmp_path):
    saved = rig.bench.save_report({"format": 1, "steps": {"pulse": {"ok": True}}, "server": "forged"}, SERVER)
    path = tmp_path / "hardware-tests" / saved["name"]
    assert saved["name"].startswith("hwtest-") and saved["name"].endswith(".json") and path.is_file()
    on_disk = json.loads(path.read_text("utf-8"))
    assert on_disk == saved["report"] and saved["path"] == str(path)
    assert on_disk["steps"]["pulse"]["ok"] is True
    assert on_disk["server"]["link"]["bot_transport"] == "C" and on_disk["server"]["saved_at"]  # not the forged one
    second = rig.bench.save_report({"a": 1}, SERVER)
    assert second["name"] != saved["name"] and len(list(path.parent.glob("*.json"))) == 2
    assert not list(path.parent.glob("*.tmp"))
    assert rig.logs[-1] == ("hw_saved", {"name": second["name"]})


def test_report_refusals(rig, tmp_path):
    with pytest.raises(OpError, match="object"):
        rig.bench.save_report("x", SERVER)
    with pytest.raises(OpError, match="too large"):
        rig.bench.save_report({"blob": "x" * 250_000}, SERVER)
    (tmp_path / "hardware-tests").write_text("a file where the folder should be")
    with pytest.raises(OpError, match="cannot save the report"):
        rig.bench.save_report({"a": 1}, SERVER)


# ------------------------------------------------------------ the other pages

def test_bot_commands_cannot_touch_the_slot_under_test(rig):
    bench = rig.bench
    bench.protect({"slot": 1, "value": 15})  # no test running: nothing to protect
    bench.state = {"kind": "bench", "slot": None, "phase": "run"}
    bench.protect({"slot": 1})  # the latency test holds no slot
    bench.state = {"kind": "pulse", "slot": 1, "phase": "run"}
    for args in [{"slot": 1, "value": 15}, {"slot": "1"}, {"slot": 1.0}, {"actions": [{"slot": 2}, {"slot": "1"}]}]:
        with pytest.raises(OpError, match="slot 2 is under a hardware test"):
            bench.protect(args)
    for args in [{"slot": 2}, {"slot": None}, {"slot": "x"}, {"actions": [{"slot": 3}, "junk"]}, {}]:
        bench.protect(args)  # not the slot under test (an unparsable one is refused by the op itself)


def test_what_the_app_publishes(rig):
    assert rig.bench.limits() == {"throttle_max": 6, "pulse_ms": [200, 2000], "handback_pause_ms": [1000, 2500],
                                  "bench_max": 300}
    rig.bench.state = {"kind": "pulse", "slot": 1, "phase": "run"}
    shown = rig.bench.snapshot()
    shown["phase"] = "changed"
    assert rig.bench.state["phase"] == "run"  # a copy
