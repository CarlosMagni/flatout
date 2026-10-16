"""Python client (`RaceLabClient`, `SyncClient`, `RaceLabEnv`) against a FAKE server.

The fake server mimics the docs/api.md protocol (hello, pushed states, replies with `id`,
`ok: false`, events) without RaceLab or the simulator. It runs in a thread with its
own loop: it serves the asyncio client (test loop, `asyncio.run`) as well as
`SyncClient` and `RaceLabEnv` (blocking).
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from typing import Any

import pytest
from aiohttp import WSMsgType, web

from racelab.client import (
    OBS_FIELDS,
    Event,
    RaceLabClient,
    RaceLabEnv,
    RaceLabError,
    SyncClient,
)

REF_MS = 300  # reference lap of the environment tests (ms)


class FakeError(Exception):
    """Becomes an `ok: false` reply."""


class _Conn:
    def __init__(self, ws: web.WebSocketResponse, agent: str) -> None:
        self.ws = ws
        self.agent = agent
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def pump(self) -> None:
        while True:
            await self.ws.send_json(await self.queue.get())


class FakeServer:
    """Fake RaceLab server. Tests drive it with `run(fn)` (executes `fn` in the server's
    loop), `emit(...)` (event) and by modifying `state` / `car` / `bot` via `run`.
    With `simulate=True`, the car advances with the throttle (lap = `lap_s` at throttle 15) and emits
    `crossing`/`lap`; the 1st pass is the reference (no `lap`), as on RaceLab."""

    def __init__(self, deadman_ms: int = 600, lap_s: float = 0.1) -> None:
        self.meta = {"safety": {"deadman_ms": deadman_ms}, "version": "test"}
        self.car: dict[str, Any] = {"slot": 0, "laps": 0, "last_ms": None, "best_ms": None,
                                    "since_cross_ms": None, "fuel": 15, "pit": False}
        self.bot: dict[str, Any] = {"slot": 0, "active": False, "driver": None, "throttle": None,
                                    "gate": None, "agent": None, "lane_pending": False}
        self.state: dict[str, Any] = {
            "t": 0.0, "mode": "advanced", "estop": False,
            "session": {"state": "idle", "type": "practice"}, "cu": {"phase": 0},
            "link": {"state": "connected"}, "cars": [self.car], "bots": [self.bot],
        }
        self.requests: list[dict[str, Any]] = []
        self.connector_calls: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[dict[str, str]] = []
        self.hang_ops: set[str] = set()  # ops with no reply
        self.push_states = True
        self.simulate = False
        self.lap_s = lap_s
        self.lap_log: list[int] = []
        self._conns: list[_Conn] = []
        self._progress = 0.0
        self._last_cross: float | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._serve()), daemon=True)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> FakeServer:
        self._thread.start()
        assert self._ready.wait(5), "fake server not started"
        return self

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)

    async def _serve(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        app = web.Application()
        app.router.add_get("/ws", self._ws)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.url = f"ws://127.0.0.1:{runner.addresses[0][1]}/ws"
        ticker = asyncio.create_task(self._tick())
        self._ready.set()
        await self._stop.wait()
        ticker.cancel()
        await self._close_all()
        await runner.cleanup()

    async def _close_all(self) -> None:
        for conn in list(self._conns):
            await conn.ws.close()

    # ------------------------------------------------------------ test side

    def run(self, fn: Any) -> Any:
        """Execute `fn()` in the server's loop (safe access to `state`, `car`, `bot`)."""

        async def call() -> Any:
            return fn()

        return asyncio.run_coroutine_threadsafe(call(), self.loop).result(5)

    def emit(self, name: str, **data: Any) -> None:
        self.run(lambda: self._broadcast({"type": "event", "name": name, "data": data,
                                          "t": time.monotonic()}))

    def drop_clients(self) -> None:
        asyncio.run_coroutine_threadsafe(self._close_all(), self.loop).result(5)

    def ops(self, *names: str) -> list[tuple[str, dict[str, Any]]]:
        return [(r["op"], r["args"]) for r in list(self.requests) if not names or r["op"] in names]

    def wait_for(self, predicate: Any, timeout: float = 2.0) -> None:
        end = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < end, "condition not reached"
            time.sleep(0.01)

    # ------------------------------------------------------------ protocol

    def _broadcast(self, msg: dict[str, Any]) -> None:
        for conn in self._conns:
            conn.queue.put_nowait(msg)

    async def _tick(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            if self.simulate:
                self._advance(now, now - last)
            last = now
            self.state["t"] = now
            if self.push_states:
                self._broadcast({"type": "state", "data": copy.deepcopy(self.state)})

    def _advance(self, now: float, dt: float) -> None:
        if self.state["session"]["state"] == "running" and self.bot["active"] and not self.bot["gate"]:
            self._progress += dt * (self.bot["throttle"] or 0) / 15 / self.lap_s
            if self._progress >= 1:
                self._progress -= 1
                self._cross(now)
        if self._last_cross is not None:
            self.car["since_cross_ms"] = round((now - self._last_cross) * 1000)

    def _cross(self, now: float) -> None:
        self._broadcast({"type": "event", "name": "crossing", "t": now,
                         "data": {"slot": 0, "sector": 1, "cu_ts": 0}})
        if self._last_cross is not None:
            ms = round((now - self._last_cross) * 1000)
            self.lap_log.append(ms)
            self.car.update(laps=self.car["laps"] + 1, last_ms=ms)
            self._broadcast({"type": "event", "name": "lap", "t": now, "data": {
                "slot": 0, "lap": self.car["laps"], "ms": ms, "neutralized": False}})
        self._last_cross = now

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.queries.append(dict(request.query))
        conn = _Conn(ws, request.query.get("agent", "ui-1"))
        self._conns.append(conn)
        pump = asyncio.create_task(conn.pump())
        await ws.send_json({"type": "hello", "client": conn.agent, "meta": self.meta,
                            "state": copy.deepcopy(self.state), "log": []})
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                req = json.loads(msg.data)
                rid, op = req.pop("id"), req.pop("op")
                self.requests.append({"op": op, "args": req, "agent": conn.agent, "t": time.monotonic()})
                if op in self.hang_ops:
                    continue
                try:
                    result = self._handle(op, req, conn.agent)
                    reply = {"type": "reply", "id": rid, "ok": True, "result": result}
                except FakeError as e:
                    reply = {"type": "reply", "id": rid, "ok": False, "error": str(e)}
                conn.queue.put_nowait(reply)
        finally:
            pump.cancel()
            self._conns.remove(conn)
        return ws

    def _handle(self, op: str, args: dict[str, Any], agent: str) -> Any:
        session = self.state["session"]
        if op == "meta":
            return self.meta
        if op == "state":
            return self.state
        if op == "race.setup":
            session.update(state="grid", type=args["settings"]["type"])
            return dict(session)
        if op == "race.start":
            session["state"] = "running"
        elif op == "race.stop":
            session["state"] = "finished"
        elif op in ("race.pause", "race.resume", "estop"):
            if op == "estop":
                self.state["estop"] = True
        elif op == "bot.claim":
            if args["slot"] != 0 and args["slot"] != 1:
                raise FakeError("invalid bot slot (0..5)")
            self.bot.update(active=True, driver="api", agent=agent, throttle=0)
        elif op == "bot.act":
            for action in args.get("actions") or [args]:
                if self.bot["agent"] != agent:
                    raise FakeError(f"slot {action['slot']} not claimed by this agent (bot.claim first)")
                if action.get("throttle") is not None:
                    self.bot["throttle"] = action["throttle"]
        elif op == "bot.unclaim":
            self.bot.update(active=False, driver=None, agent=None, throttle=None)
        elif op in ("connector.set", "connector.clear"):
            self.connector_calls.append((op, args))
        else:
            raise FakeError(f"unknown command: {op}")
        return None


@pytest.fixture
def fake():
    server = FakeServer().start()
    yield server
    server.stop()


def run(coro: Any, timeout: float = 8.0) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout))


def make_env(fake: FakeServer, **kwargs: Any) -> RaceLabEnv:
    options = dict(slot=0, agent="env", control_hz=100, episode_laps=2, ref_lap_ms=REF_MS,
                   settle_s=0.05, reset_timeout_s=3.0)
    return RaceLabEnv(fake.url, **{**options, **kwargs})


def fresh_state(client: SyncClient) -> None:
    """Wait for a state later than the last modification made on the fake server side."""
    client.wait_state()
    client.wait_state()


# ---------------------------------------------------------------- RaceLabClient


def test_connect_hello_meta(fake):
    async def body():
        client = await RaceLabClient.connect(fake.url, agent="t1", state_hz=15, token="s3cret")
        try:
            assert client.name == "t1"
            assert client.meta["safety"]["deadman_ms"] == 600
            assert client.state["session"]["state"] == "idle"
            assert not client.closed
        finally:
            await client.close()
            await client.close()  # idempotent
        assert client.closed

    run(body())
    assert fake.queries[-1] == {"agent": "t1", "state_hz": "15", "token": "s3cret"}


def test_connect_refused():
    with pytest.raises(ConnectionError, match="unreachable"):
        run(RaceLabClient.connect("ws://127.0.0.1:1/ws"))


def test_request_reply_and_errors(fake):
    async def body():
        async with RaceLabClient(fake.url) as c:
            meta, state = await asyncio.gather(c.request("meta"), c.request("state"))
            assert meta == fake.meta and state["mode"] == "advanced"
            with pytest.raises(RaceLabError, match="unknown command: nope"):
                await c.request("nope")
            with pytest.raises(RaceLabError, match="invalid bot slot"):
                await c.claim(9)
            fake.hang_ops.add("hang")
            with pytest.raises(TimeoutError, match="hang"):
                await c.request("hang", timeout=0.1)
            # the connection stays usable after an error or a timeout
            assert await c.request("meta") == fake.meta

    run(body())


def test_connection_lost_fails_pending_and_ends_streams(fake):
    async def body():
        c = await RaceLabClient.connect(fake.url)
        fake.hang_ops.add("hang")
        stream = c.events()
        pending = asyncio.create_task(c.request("hang", timeout=5))
        await asyncio.sleep(0.05)
        fake.drop_clients()
        with pytest.raises(ConnectionError):
            await pending
        assert [e async for e in stream] == []  # the event stream ends
        with pytest.raises(ConnectionError):
            await c.request("meta")
        assert c.closed
        await c.close()

    run(body())


def test_state_updates_and_wait_state(fake):
    async def body():
        async with RaceLabClient(fake.url) as c:
            before = c.state["t"]
            fresh = await c.wait_state(1.0)
            assert fresh["t"] > before
            assert c.state["t"] >= fresh["t"]
            fake.push_states = False
            await asyncio.sleep(0.05)
            with pytest.raises(TimeoutError):
                await c.wait_state(0.1)

    run(body())


def test_events_callbacks_and_stream(fake):
    async def body():
        async with RaceLabClient(fake.url) as c:
            laps: list[Event] = []
            everything: list[Event] = []
            c.on_event("lap", laps.append)
            unsubscribe = c.on_event("*", everything.append)
            c.on_event("lap", lambda ev: 1 / 0)  # a failing callback breaks nothing
            stream = c.events("lap", "session")
            fake.emit("lap", slot=0, ms=6000)
            fake.emit("crossing", slot=0, sector=1)
            fake.emit("session", state="running")
            first, second = await anext(stream), await anext(stream)
            assert (first.name, first.data["ms"]) == ("lap", 6000) and first.t > 0
            assert second.name == "session"  # `crossing` filtered out
            assert [e.name for e in everything] == ["lap", "crossing", "session"]
            assert [e.data["ms"] for e in laps] == [6000]
            unsubscribe()
            other = c.events("lap")
            fake.emit("lap", slot=0, ms=5000)
            assert (await anext(other)).data["ms"] == 5000
            assert len(everything) == 3 and len(laps) == 2

    run(body())


def test_commands_payloads(fake):
    async def body():
        async with RaceLabClient(fake.url, agent="p") as c:
            await c.claim(0)
            await c.claim(1, free=True)
            await c.act(0, 7)
            await c.act(0, lane=True)
            await c.act(0)
            await c.act_many([{"slot": 0, "throttle": 3}, {"slot": 1, "lane": True}])
            await c.unclaim(1)
            await c.estop()
            assert await c.setup(type="training", episode_laps=3) == {"state": "grid", "type": "training"}
            await c.start()
            await c.pause()
            await c.resume()
            await c.stop()

    run(body())
    assert fake.ops() == [
        ("bot.claim", {"slot": 0, "free": False}),
        ("bot.claim", {"slot": 1, "free": True}),
        ("bot.act", {"slot": 0, "throttle": 7}),
        ("bot.act", {"slot": 0, "lane": True}),
        ("bot.act", {"slot": 0}),
        ("bot.act", {"actions": [{"slot": 0, "throttle": 3}, {"slot": 1, "lane": True}]}),
        ("bot.unclaim", {"slot": 1}),
        ("estop", {}),
        ("race.setup", {"settings": {"type": "training", "episode_laps": 3}}),
        ("race.start", {}),
        ("race.pause", {}),
        ("race.resume", {}),
        ("race.stop", {}),
    ]
    assert {r["agent"] for r in fake.requests} == {"p"}


def test_act_requires_claim(fake):
    async def body():
        async with RaceLabClient(fake.url) as c:
            with pytest.raises(RaceLabError, match="not claimed"):
                await c.act(0, 5)

    run(body())


def test_keepalive_is_bounded_by_the_watchdog():
    server = FakeServer(deadman_ms=100).start()  # keepalive every 50 ms, bounded to 2 × 100 ms
    try:
        async def body():
            async with RaceLabClient(server.url, agent="k", keepalive=True) as c:
                await c.claim(0)
                t_act = time.monotonic()
                await c.act(0, 5)
                await asyncio.sleep(0.5)
                refreshes = [r for r in server.requests if r["op"] == "bot.act" and "actions" in r["args"]]
                count = len(refreshes)
                assert count >= 2  # the agent is "thinking": the server receives refreshes
                # never any throttle or lane in a refresh
                assert all(r["args"] == {"actions": [{"slot": 0}]} for r in refreshes)
                assert refreshes[-1]["t"] - t_act < 0.4  # ... but the watchdog stops them quickly
                await asyncio.sleep(0.2)
                assert sum(1 for r in server.requests if r["op"] == "bot.act") == count + 1  # + the act()

        run(body())
    finally:
        server.stop()


def test_no_keepalive_by_default(fake):
    async def body():
        async with RaceLabClient(fake.url) as c:
            await c.claim(0)
            await c.act(0, 5)
            await asyncio.sleep(0.4)

    run(body())
    assert len(fake.ops("bot.act")) == 1


def test_close_unclaim_option(fake):
    async def body(unclaim: bool):
        c = await RaceLabClient.connect(fake.url)
        await c.claim(0)
        await c.act(0, 6)
        await c.close(unclaim=unclaim)

    run(body(False))
    assert fake.ops("bot.unclaim") == []  # default: we close, the server sets the throttle back to 0
    run(body(True))
    assert fake.ops()[-2:] == [("bot.act", {"slot": 0, "throttle": 0}), ("bot.unclaim", {"slot": 0})]


# ---------------------------------------------------------------- SyncClient


def test_sync_client_flow(fake):
    with SyncClient(fake.url, agent="sync") as c:
        assert c.name == "sync" and c.meta["safety"]["deadman_ms"] == 600
        c.claim(0)
        c.act(0, 4)
        assert fake.bot["throttle"] == 4
        assert c.wait_state(1.0)["session"]["state"] == "idle"
        seen: list[Event] = []
        c.on_event("lap", seen.append)
        stream = c.events("lap")
        fake.emit("lap", slot=0, ms=1234)
        assert next(stream).data["ms"] == 1234
        assert [e.data["ms"] for e in seen] == [1234]
        with pytest.raises(RaceLabError, match="unknown command"):
            c.request("nope")
        assert c.setup(type="practice")["state"] == "grid"
    assert c.closed
    with pytest.raises(ConnectionError):
        c.act(0, 1)
    c.close()  # idempotent
    stream.close()


def test_connector_card_calls(fake):
    with SyncClient(fake.url, agent="cam") as c:
        c.set_connector("Camera", ui_url="http://pi:8090/", status="tracking")
        c.set_connector("Gates")
        c.clear_connector()
    assert fake.connector_calls == [
        ("connector.set", {"name": "Camera", "ui_url": "http://pi:8090/", "status": "tracking"}),
        ("connector.set", {"name": "Gates", "ui_url": None, "status": None}),
        ("connector.clear", {}),
    ]

    async def main():
        async with RaceLabClient(fake.url, agent="cam2") as c:
            await c.set_connector("Async", status="ok")
            await c.clear_connector()

    run(main())
    assert fake.connector_calls[-2] == ("connector.set", {"name": "Async", "ui_url": None, "status": "ok"})


def test_sync_client_connect_failure_releases_thread():
    with pytest.raises(ConnectionError):
        SyncClient.connect("ws://127.0.0.1:1/ws")
    assert not any(t.name == "racelab-sync" and t.is_alive() for t in threading.enumerate())


# ---------------------------------------------------------------- RaceLabEnv


def test_env_episode_with_simulated_laps(fake):
    fake.simulate = True
    with make_env(fake, ref_lap_ms=150) as env:
        assert env.action_space.n == 16 and env.observation_space.shape == (len(OBS_FIELDS),)
        obs, info = env.reset()
        assert len(obs) == len(OBS_FIELDS) and all(0 <= v <= 3 for v in obs)
        assert info["session_state"] == "running" and info["gate"] is None and info["laps"] == 0
        assert fake.ops("race.setup", "race.start", "bot.claim")[:3] == [
            ("race.setup", {"settings": {"type": "training", "episode_laps": 2}}),
            ("race.start", {}),
            ("bot.claim", {"slot": 0, "free": False}),
        ]

        def drive() -> tuple[float, bool, bool, dict]:
            total, terminated, truncated = 0.0, False, False
            for _ in range(1000):
                obs, reward, terminated, truncated, info = env.step(15)
                total += reward
                assert len(obs) == len(OBS_FIELDS)
                if terminated or truncated:
                    break
            return total, terminated, truncated, info

        total, terminated, truncated, info = drive()
        assert terminated and not truncated
        assert info["laps"] == len(info["lap_ms"]) == 2
        assert total > 2 * 1.0  # 2 laps at ~100 ms against a 150 ms reference
        assert fake.ops()[-1] == ("bot.act", {"slot": 0, "throttle": 0})  # throttle 0 at the end of the episode
        with pytest.raises(RuntimeError, match="reset"):
            env.step(0)

        # 2nd episode: the car has already crossed the line, its 1st lap (stop + wait) is ignored
        time.sleep(0.3)
        env.reset()
        total, terminated, truncated, info = drive()
        assert terminated and info["laps"] == 2
        assert max(info["lap_ms"]) < 250
        assert fake.lap_log[-3] > 300  # the ignored lap does exist on the server side
    assert len(fake.ops("race.setup")) == 1  # the session in progress is respected


def test_env_lap_reward_and_observation(fake):
    with make_env(fake) as env:
        env.reset()
        fake.emit("lap", slot=0, lap=1, ms=REF_MS, neutralized=False)
        fake.emit("lap", slot=1, lap=1, ms=100, neutralized=False)  # other car: ignored
        fake.emit("lap", slot=0, lap=2, ms=100, neutralized=True)  # neutralized: ignored
        time.sleep(0.05)
        obs, reward, terminated, truncated, info = env.step(0)
        assert reward == pytest.approx(1.0)  # ref / ms, throttle 0 so no shaping
        assert (terminated, truncated) == (False, False)
        assert info["laps"] == 1 and info["lap_ms"] == [REF_MS]
        fields = dict(zip(OBS_FIELDS, obs))
        assert fields["last_lap"] == pytest.approx(1.0) and fields["best_lap"] == pytest.approx(1.0)
        assert fields["laps"] == pytest.approx(0.5)  # 1 lap out of episode_laps=2

        _, reward, *_ = env.step(15)  # no lap: only the 0.01 × throttle/15 shaping
        assert reward == pytest.approx(0.01)

        fake.emit("lap", slot=0, lap=3, ms=REF_MS // 2, neutralized=False)
        time.sleep(0.05)
        obs, reward, terminated, truncated, info = env.step(0)
        assert reward == pytest.approx(2.0)
        assert terminated and not truncated and info["lap_ms"] == [REF_MS, REF_MS // 2]
        assert dict(zip(OBS_FIELDS, obs))["best_lap"] == pytest.approx(0.5)


def test_env_ignores_first_lap_when_car_already_crossed(fake):
    fake.run(lambda: fake.car.update(since_cross_ms=100))
    with make_env(fake) as env:
        env.reset()
        fake.emit("lap", slot=0, lap=5, ms=5000, neutralized=False)  # stop lap: ignored
        fake.emit("lap", slot=0, lap=6, ms=REF_MS, neutralized=False)
        time.sleep(0.05)
        _, reward, _, _, info = env.step(0)
        assert info["lap_ms"] == [REF_MS] and reward == pytest.approx(1.0)


def test_env_deslot_suspicion_penalty_once_per_incident(fake):
    suspected = int(1.6 * REF_MS)
    with make_env(fake) as env:
        env.reset()

        def step_with(since_ms: int, throttle: int = 15) -> tuple[Any, float, dict]:
            fake.run(lambda: fake.car.update(since_cross_ms=since_ms))
            fresh_state(env.client)
            obs, reward, _, _, info = env.step(throttle)
            return dict(zip(OBS_FIELDS, obs)), reward, info

        obs, reward, info = step_with(50)
        assert obs["deslot_suspected"] == 0 and reward == pytest.approx(0.01)
        obs, reward, info = step_with(suspected)
        assert obs["deslot_suspected"] == 1 and info["deslot_suspected"]
        assert reward == pytest.approx(-1.0)  # penalty when it first appears, no shaping
        obs, reward, info = step_with(suspected + 50)
        assert obs["deslot_suspected"] == 1 and reward == pytest.approx(0.0)  # same incident
        obs, reward, info = step_with(50)
        assert obs["deslot_suspected"] == 0 and reward == pytest.approx(0.01)
        _, reward, _ = step_with(suspected)
        assert reward == pytest.approx(-1.0)  # new incident


def test_env_no_penalty_while_gated(fake):
    with make_env(fake) as env:
        env.reset()
        fake.run(lambda: (fake.car.update(since_cross_ms=int(2 * REF_MS)), fake.bot.update(gate="deadman")))
        fresh_state(env.client)
        obs, reward, *_ , info = env.step(15)
        assert info["gate"] == "deadman" and dict(zip(OBS_FIELDS, obs))["gate"] == 1
        assert reward == pytest.approx(0.0)  # neither penalty nor shaping: the throttle is forced to 0


def test_env_pre_existing_suspicion_is_not_penalised_at_reset(fake):
    fake.run(lambda: fake.car.update(since_cross_ms=3 * REF_MS))
    with make_env(fake) as env:
        env.reset()
        _, reward, *_ = env.step(15)
        assert reward == pytest.approx(0.0)


def test_env_close_sends_throttle_zero_and_unclaims(fake):
    env = make_env(fake)
    env.reset()
    env.step(9)
    assert fake.bot["throttle"] == 9
    env.close()
    assert fake.ops()[-2:] == [("bot.act", {"slot": 0, "throttle": 0}), ("bot.unclaim", {"slot": 0})]
    assert fake.bot["throttle"] is None and env.client is None
    fake.wait_for(lambda: not fake._conns)  # connection closed
    env.close()  # idempotent


def test_env_reconnects_after_connection_loss(fake):
    with make_env(fake) as env:
        env.reset()
        fake.drop_clients()
        fake.wait_for(lambda: env.client.closed)
        with pytest.raises(ConnectionError):
            env.step(3)
        env.reset()  # new connection, slot claimed again
        *_, terminated, truncated, info = env.step(3)
        assert (terminated, truncated) == (False, False) and info["step"] == 1
    assert len(fake.queries) == 2


def test_env_truncation(fake):
    with make_env(fake, max_steps=3) as env:
        env.reset()
        flags = [env.step(5)[2:4] for _ in range(3)]
        assert flags == [(False, False), (False, False), (False, True)]
        assert fake.ops()[-1] == ("bot.act", {"slot": 0, "throttle": 0})

        env.reset()
        fake.run(lambda: fake.state["session"].update(state="paused"))
        fresh_state(env.client)
        _, _, terminated, truncated, info = env.step(5)
        assert truncated and not terminated and info["session_state"] == "paused"

    with make_env(fake) as env:
        fake.run(lambda: fake.state["session"].update(state="running"))
        env.reset()
        fake.run(lambda: fake.state.update(estop=True))
        fresh_state(env.client)
        _, _, _, truncated, info = env.step(5)
        assert truncated and info["estop"]


def test_env_reset_waits_for_link_and_gate(fake):
    fake.run(lambda: (fake.bot.update(gate="link"), fake.state["link"].update(state="lost")))
    threading.Timer(0.3, lambda: fake.run(
        lambda: (fake.bot.update(gate=None), fake.state["link"].update(state="connected")))).start()
    with make_env(fake) as env:
        start = time.monotonic()
        _, info = env.reset()
        assert time.monotonic() - start >= 0.25
        assert info["gate"] is None and info["link"] == "connected"


def test_env_reset_times_out_when_blocked(fake):
    fake.run(lambda: fake.bot.update(gate="link"))
    with make_env(fake, reset_timeout_s=0.3) as env:
        with pytest.raises(TimeoutError, match="gate link"):
            env.reset()


def test_env_respects_session_choice(fake):
    fake.run(lambda: fake.state["session"].update(state="running", type="race"))
    with make_env(fake) as env:
        env.reset()
    assert fake.ops("race.setup", "race.start") == []  # active session: left untouched

    fake.run(lambda: fake.state["session"].update(state="idle"))
    with make_env(fake, use_training_session=False, reset_timeout_s=0.3) as env:
        with pytest.raises(TimeoutError):
            env.reset()
    assert fake.ops("race.setup") == []  # no session forced


def test_env_overrun_is_reported(fake):
    with make_env(fake, control_hz=100_000) as env:
        env.reset()
        *_, info = env.step(3)
        assert info["overrun_ms"] > 0 and info["step"] == 1
    with make_env(fake, control_hz=50) as env:
        env.reset()
        *_, info = env.step(3)
        assert info["overrun_ms"] == 0


def test_env_positions(fake):
    feed = {"source": "camera", "age_ms": 30, "stale": False,
            "cars": [{"slot": 0, "s": 0.25, "lane": 1, "v": 2.1, "off": True}]}
    fake.run(lambda: fake.state.update(positions=feed))
    with make_env(fake, positions=True) as env:
        obs, _ = env.reset()
        assert len(obs) == 15 and env.observation_space.shape == (15,)
        assert list(obs[10:]) == pytest.approx([0.25, 1.0, 2.1 / 4.2, 1.0, 1.0], rel=1e-5)
    with make_env(fake, agent="env2") as env:  # default: no positions
        obs, _ = env.reset()
        assert len(obs) == len(OBS_FIELDS)

    # stale or missing positions: components at 0 and pos_ok = 0, no error
    fake.run(lambda: fake.state.update(positions={**feed, "stale": True}))
    with make_env(fake, positions=True, agent="env3") as env:
        obs, _ = env.reset()
        assert list(obs[10:]) == [0.0] * 5
    fake.run(lambda: fake.state.update(positions=None))
    with make_env(fake, positions=True, agent="env4") as env:
        obs, _ = env.reset()
        assert obs[14] == 0.0


def test_env_lane_actions_and_validation(fake):
    with make_env(fake, allow_lane=True) as env:
        assert env.action_space.n == 32
        env.reset()
        env.step(16 + 5)
        env.step(5)
        assert fake.ops("bot.act")[-2:] == [("bot.act", {"slot": 0, "throttle": 5, "lane": True}),
                                              ("bot.act", {"slot": 0, "throttle": 5})]
        with pytest.raises(ValueError, match="out of"):
            env.step(32)
    with make_env(fake) as env:  # without allow_lane: only 16 actions
        env.reset()
        with pytest.raises(ValueError):
            env.step(16)


def test_env_pluggable_reward_and_obs(fake):
    with make_env(fake, reward_fn=lambda frame: 42.0 + frame.throttle,
                  obs_fn=lambda frame: [frame.throttle, frame.laps], obs_size=2) as env:
        assert env.observation_space.shape == (2,)
        obs, _ = env.reset()
        assert list(obs) == [0, 0]
        obs, reward, *_ = env.step(7)
        assert list(obs) == [7, 0] and reward == 49.0
    with pytest.raises(ValueError, match="obs_size"):
        RaceLabEnv(obs_fn=lambda frame: [0.0])
