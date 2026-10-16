"""Python client for the RaceLab API (JSON WebSocket, see docs/api.md).

`RaceLabClient` is asyncio; `SyncClient` offers the same API in blocking form (notebooks,
simple scripts). Example:

    async with RaceLabClient("ws://localhost:8130/ws", agent="rl-01") as c:
        await c.claim(0)
        await c.act(0, throttle=6)
        async for ev in c.events("lap"):
            print(ev.data["ms"])

Safety: the server forces the throttle to 0 if the agent sends no action for
`deadman_ms` (600 ms by default). An agent loop must therefore call `act()` at
least every ~300 ms, or enable `keepalive` (see `RaceLabClient`).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import threading
import time
from collections.abc import AsyncGenerator, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Self

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_URL = "ws://localhost:8130/ws"
DEFAULT_TIMEOUT_S = 3.0
KEEPALIVE_S = 0.25  # refresh period of the dead-man switch
DEADMAN_MS = 600  # server default, if `meta["safety"]` does not provide it


class RaceLabError(Exception):
    """Command refused by the server (`ok: false`); the message is the server's."""


@dataclass(frozen=True)
class Event:
    """Event pushed by the server (`lap`, `crossing`, `episode_end`, `session`…)."""

    name: str
    data: dict[str, Any]
    t: float  # server clock (s, monotonic)


# (filtered names, queue); empty names = all events
_Subscription = tuple[frozenset[str], asyncio.Queue[Event | None]]


class RaceLabClient:
    """WebSocket connection to RaceLab: state, events and commands.

    Two ways to get one: `c = await RaceLabClient.connect(...)` (then
    `await c.close()`), or `async with RaceLabClient(...) as c:`.

    Args:
        url: WebSocket address of the server.
        agent: identity of the agent; the server may suffix it (`rl-agent#2`) if it
            is already taken, see `name`.
        state_hz: rate of the state messages (the server clamps it to 0.5..20).
        token: token if `[api] token` is set on the server side.
        timeout: default reply timeout of a command (s).
        keepalive: if true, a background task sends a dead-man refresh
            (`bot.act` without throttle) every 0.25 s for each driven slot. Defaults to
            false: the dead-man switch is the safety net for a stuck agent, and an
            unconditional keepalive would defeat it (the network thread would keep
            refreshing while the agent's loop is frozen). The keepalive is therefore
            bounded: it only refreshes a slot if `act()` was called for it less than
            2 × `deadman_ms` ago. A stuck loop thus cuts the car at most
            3 × `deadman_ms` (1.8 s by default) after its last action instead of 1 ×.
            It never resends the throttle or `lane` (nothing to replay after an E-STOP):
            it only covers the compute pauses of a live agent.
    """

    def __init__(self, url: str = DEFAULT_URL, agent: str = "rl-agent", state_hz: float = 20.0,
                 token: str | None = None, *, timeout: float = DEFAULT_TIMEOUT_S,
                 keepalive: bool = False) -> None:
        self.url = url
        self.agent = agent
        self.state_hz = state_hz
        self.token = token
        self.timeout = timeout
        self.keepalive = keepalive
        self.name = agent  # identity assigned by the server (hello)
        self.meta: dict[str, Any] = {}
        self.log: list[dict[str, Any]] = []
        self.closed = False
        self._state: dict[str, Any] = {}
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._state_waiters: list[asyncio.Future[dict[str, Any]]] = []
        self._callbacks: dict[str, list[Callable[[Event], None]]] = {}
        self._queues: list[_Subscription] = []
        self._claimed: set[int] = set()
        self._last_act: dict[int, float] = {}  # slot -> time of the last explicit act()

    # ------------------------------------------------------------ connection

    @classmethod
    async def connect(cls, url: str = DEFAULT_URL, agent: str = "rl-agent", state_hz: float = 20.0,
                      token: str | None = None, *, timeout: float = DEFAULT_TIMEOUT_S,
                      keepalive: bool = False) -> Self:
        client = cls(url, agent, state_hz, token, timeout=timeout, keepalive=keepalive)
        await client._open()
        return client

    async def __aenter__(self) -> Self:
        await self._open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _open(self) -> None:
        if self._ws is not None:
            return
        params = {"agent": self.agent, "state_hz": f"{self.state_hz:g}"}
        if self.token:
            params["token"] = self.token
        self._http = aiohttp.ClientSession()
        try:
            async with asyncio.timeout(self.timeout):
                self._ws = await self._http.ws_connect(self.url, params=params, heartbeat=10.0)
                hello = await self._ws.receive_json()
            if hello.get("type") != "hello":
                raise RaceLabError(f"unexpected greeting message: {hello.get('type')!r}")
        except BaseException as e:
            if self._ws is not None:
                await self._ws.close()
            await self._http.close()
            self._ws = None
            if isinstance(e, aiohttp.WSServerHandshakeError):
                raise RaceLabError("invalid API token" if e.status == 401
                                   else f"connection refused (HTTP {e.status})") from None
            if isinstance(e, aiohttp.ClientConnectorError):
                raise ConnectionError(f"RaceLab server unreachable ({self.url}): "
                                      "is `racelab --sim` running?") from None
            raise
        self.name = hello["client"]
        self.meta = hello.get("meta") or {}
        self.log = hello.get("log") or []
        self._state = hello.get("state") or {}
        self._reader = asyncio.create_task(self._read(), name="racelab-client-reader")
        if self.keepalive:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="racelab-keepalive")

    async def close(self, *, unclaim: bool = False) -> None:
        """Close the connection. By default the slots are not handed back: the server sets
        their throttle to 0 on disconnect and the slot stays under the bot's control.
        `unclaim=True` first sets the throttle to 0 then hands the claimed slots back to
        the physical controller."""
        if unclaim and not self.closed:
            for slot in sorted(self._claimed):
                with contextlib.suppress(RaceLabError, OSError):
                    await self.act(slot, 0)
                    await self.unclaim(slot)
        self._shutdown()
        if self._ws is not None:
            await self._ws.close()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._http is not None:
            await self._http.close()

    def _shutdown(self) -> None:
        """End of connection (local close or loss): wakes up all waiters."""
        if self.closed:
            return
        self.closed = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        error = ConnectionError("RaceLab connection closed")
        for fut in (*self._pending.values(), *self._state_waiters):
            if not fut.done():
                fut.set_exception(error)
        for _, queue in self._queues:
            queue.put_nowait(None)

    async def _read(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    try:
                        self._dispatch(json.loads(msg.data))
                    except ValueError:
                        logger.warning("non-JSON message ignored")
        finally:
            self._shutdown()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "state":
            self._state = msg["data"]
            waiters, self._state_waiters = self._state_waiters, []
            for fut in waiters:
                if not fut.done():
                    fut.set_result(self._state)
        elif kind == "event":
            self._emit(Event(msg["name"], msg.get("data") or {}, msg.get("t", 0.0)))
        elif kind == "reply":
            fut = self._pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    def _emit(self, event: Event) -> None:
        for callback in (*self._callbacks.get(event.name, ()), *self._callbacks.get("*", ())):
            try:
                callback(event)
            except Exception:
                logger.exception("event callback %s", event.name)
        for names, queue in self._queues:
            if not names or event.name in names:
                queue.put_nowait(event)

    # ------------------------------------------------------------ reading

    @property
    def state(self) -> dict[str, Any]:
        """Last full state received (see docs/architecture.md §4 and docs/api.md); do not modify."""
        return self._state

    async def wait_state(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for the next state message (received after the call) and return it.
        `TimeoutError` after `timeout` s (default: the client's)."""
        if self.closed:
            raise ConnectionError("RaceLab connection closed")
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._state_waiters.append(fut)
        try:
            return await asyncio.wait_for(fut, self.timeout if timeout is None else timeout)
        finally:
            if fut in self._state_waiters:
                self._state_waiters.remove(fut)

    def on_event(self, name: str, callback: Callable[[Event], None]) -> Callable[[], None]:
        """Call `callback(event)` on every `name` event (`"*"` = all). The callback is
        synchronous and must be brief (it runs in the read loop). Returns an
        unsubscribe function."""
        self._callbacks.setdefault(name, []).append(callback)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._callbacks[name].remove(callback)

        return unsubscribe

    def events(self, *names: str) -> AsyncGenerator[Event, None]:
        """Async stream of events (all, or only those in `names`). The subscription
        starts at this call, not at the first iteration; the stream ends when the
        connection closes."""
        entry: _Subscription = (frozenset(names), asyncio.Queue())
        if self.closed:
            entry[1].put_nowait(None)
        self._queues.append(entry)
        return self._drain(entry)

    async def _drain(self, entry: _Subscription) -> AsyncGenerator[Event, None]:
        try:
            while (event := await entry[1].get()) is not None:
                yield event
        finally:
            self._queues.remove(entry)

    # ------------------------------------------------------------ commands

    async def request(self, op: str, /, *, timeout: float | None = None, **args: Any) -> Any:
        """Send `{"id", "op", **args}` and return `result`. `RaceLabError` if the server
        answers `ok: false`, `TimeoutError` if there is no reply, `ConnectionError` if the
        connection is closed."""
        if self.closed or self._ws is None:
            raise ConnectionError("RaceLab connection closed")
        wait = self.timeout if timeout is None else timeout
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._ws.send_json({"id": rid, "op": op, **args})
            reply = await asyncio.wait_for(fut, wait)
        except TimeoutError:
            raise TimeoutError(f"no reply to {op!r} within {wait:g} s") from None
        finally:
            self._pending.pop(rid, None)
        if not reply.get("ok"):
            raise RaceLabError(reply.get("error") or "unknown error")
        return reply.get("result")

    async def claim(self, slot: int, free: bool = False) -> None:
        """Claim the slot (0..5): it becomes a bot, throttle 0. `free=True` allows driving outside
        a session (test bench); otherwise the throttle stays at 0 until a session is running."""
        await self.request("bot.claim", slot=slot, free=free)
        self._claimed.add(slot)

    async def act(self, slot: int, throttle: int | None = None, lane: bool = False) -> None:
        """Agent action: throttle 0..15 and/or lane change request (one-shot).
        Without `throttle`, the throttle is kept and only the dead-man switch is refreshed."""
        args: dict[str, Any] = {"slot": slot}
        if throttle is not None:
            args["throttle"] = throttle
        if lane:
            args["lane"] = True
        await self.request("bot.act", **args)
        self._last_act[slot] = time.monotonic()

    async def act_many(self, actions: Iterable[Mapping[str, Any]]) -> None:
        """Several actions in one message: `[{"slot": 0, "throttle": 7}, {"slot": 1, "lane": True}]`."""
        actions = [dict(a) for a in actions]
        await self.request("bot.act", actions=actions)
        now = time.monotonic()
        for a in actions:
            self._last_act[a["slot"]] = now

    async def unclaim(self, slot: int) -> None:
        """Hand the slot back to the physical controller."""
        await self.request("bot.unclaim", slot=slot)
        self._claimed.discard(slot)
        self._last_act.pop(slot, None)

    async def estop(self) -> None:
        """E-STOP: throttle 0 on all bots. Clearing it (`estop.clear`) is deliberately
        absent from the convenience API: it stays a human decision
        (`request("estop.clear")` if really needed)."""
        await self.request("estop")

    # -- track connector (camera, estimator…): see docs/api.md, "Track connector"
    async def set_track(self, geometry: dict[str, Any]) -> dict[str, Any]:
        """Declare the circuit shape ([x, y] points in its own units, `y_axis`,
        `finish`, `pit`, `changers`, `lane_offset`, `name`); returns the normalized shape."""
        return await self.request("track.set", geometry=geometry)

    async def send_positions(self, cars: list[dict[str, Any]]) -> None:
        """Current positions: `[{slot, s, lane, v?, off?}]`, `s` = lap fraction since the line."""
        await self.request("track.positions", cars=cars)

    async def clear_track(self) -> None:
        await self.request("track.clear")

    # -- connector card: a chip in the RaceLab UI with a link to the connector's own page
    async def set_connector(self, name: str, ui_url: str | None = None, status: str | None = None) -> dict[str, Any]:
        """Show (or replace) this connection's card: `name` (<= 40 chars), `ui_url` (http/https,
        <= 300) opened in a new tab, `status` (<= 80). It disappears when the connection closes."""
        return await self.request("connector.set", name=name, ui_url=ui_url, status=status)

    async def clear_connector(self) -> None:
        await self.request("connector.clear")

    async def setup(self, **settings: Any) -> dict[str, Any]:
        """New session (`grid` state), e.g. `setup(type="training", episode_laps=5)`."""
        return await self.request("race.setup", settings=settings)

    async def start(self) -> None:
        await self.request("race.start")

    async def stop(self) -> None:
        await self.request("race.stop")

    async def pause(self) -> None:
        await self.request("race.pause")

    async def resume(self) -> None:
        await self.request("race.resume")

    # ------------------------------------------------------------ keepalive

    async def _keepalive_loop(self) -> None:
        deadman_s = self.meta.get("safety", {}).get("deadman_ms", DEADMAN_MS) / 1000
        max_age = 2 * deadman_s
        period = min(KEEPALIVE_S, deadman_s / 2)
        assert self._ws is not None
        try:
            while True:
                await asyncio.sleep(period)
                now = time.monotonic()
                actions = [{"slot": s} for s, t in self._last_act.items() if now - t <= max_age]
                if actions:  # reply ignored: with no pending `id`, the reader drops it
                    await self._ws.send_json({"id": next(self._ids), "op": "bot.act", "actions": actions})
        except ConnectionError:
            pass


class SyncClient:
    """Blocking `RaceLabClient`: the asyncio loop runs in a background thread.

    Same methods, same exceptions. `on_event` callbacks run in that thread
    (protect shared structures). `with SyncClient(...) as c:` or
    `c = SyncClient.connect(...)` then `c.close()`.
    """

    def __init__(self, url: str = DEFAULT_URL, agent: str = "rl-agent", state_hz: float = 20.0,
                 token: str | None = None, *, timeout: float = DEFAULT_TIMEOUT_S,
                 keepalive: bool = False) -> None:
        self._client = RaceLabClient(url, agent, state_hz, token, timeout=timeout, keepalive=keepalive)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="racelab-sync", daemon=True)
        self._thread.start()

    @classmethod
    def connect(cls, url: str = DEFAULT_URL, agent: str = "rl-agent", state_hz: float = 20.0,
                token: str | None = None, *, timeout: float = DEFAULT_TIMEOUT_S,
                keepalive: bool = False) -> Self:
        client = cls(url, agent, state_hz, token, timeout=timeout, keepalive=keepalive)
        try:
            client._call(client._client._open())
        except BaseException:
            client.close()
            raise
        return client

    def __enter__(self) -> Self:
        self._call(self._client._open())
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call(self, coro: Any) -> Any:
        if self._loop.is_closed():
            coro.close()
            raise ConnectionError("RaceLab client closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def close(self, *, unclaim: bool = False) -> None:
        """See `RaceLabClient.close`; also stops the thread. Idempotent."""
        if self._loop.is_closed():
            return
        try:
            self._call(self._client.close(unclaim=unclaim))
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._loop.close()

    # reading
    @property
    def name(self) -> str:
        return self._client.name

    @property
    def meta(self) -> dict[str, Any]:
        return self._client.meta

    @property
    def state(self) -> dict[str, Any]:
        return self._client.state

    @property
    def closed(self) -> bool:
        return self._client.closed

    def wait_state(self, timeout: float | None = None) -> dict[str, Any]:
        return self._call(self._client.wait_state(timeout))

    def on_event(self, name: str, callback: Callable[[Event], None]) -> Callable[[], None]:
        return self._client.on_event(name, callback)

    def events(self, *names: str) -> Iterator[Event]:
        """Blocking iterator of events; subscribes at the call and stops when the
        connection closes."""

        async def subscribe() -> AsyncGenerator[Event, None]:
            return self._client.events(*names)

        return self._iterate(self._call(subscribe()))

    def _iterate(self, stream: AsyncGenerator[Event, None]) -> Iterator[Event]:
        async def advance() -> Event:
            return await stream.__anext__()

        try:
            while True:
                try:
                    yield self._call(advance())
                except StopAsyncIteration:
                    return
        finally:
            with contextlib.suppress(ConnectionError):
                self._call(stream.aclose())

    # commands
    def request(self, op: str, /, *, timeout: float | None = None, **args: Any) -> Any:
        return self._call(self._client.request(op, timeout=timeout, **args))

    def claim(self, slot: int, free: bool = False) -> None:
        self._call(self._client.claim(slot, free))

    def act(self, slot: int, throttle: int | None = None, lane: bool = False) -> None:
        self._call(self._client.act(slot, throttle, lane))

    def act_many(self, actions: Iterable[Mapping[str, Any]]) -> None:
        self._call(self._client.act_many(actions))

    def unclaim(self, slot: int) -> None:
        self._call(self._client.unclaim(slot))

    def estop(self) -> None:
        self._call(self._client.estop())

    def set_track(self, geometry: dict[str, Any]) -> dict[str, Any]:
        return self._call(self._client.set_track(geometry))

    def send_positions(self, cars: list[dict[str, Any]]) -> None:
        self._call(self._client.send_positions(cars))

    def clear_track(self) -> None:
        self._call(self._client.clear_track())

    def set_connector(self, name: str, ui_url: str | None = None, status: str | None = None) -> dict[str, Any]:
        return self._call(self._client.set_connector(name, ui_url, status))

    def clear_connector(self) -> None:
        self._call(self._client.clear_connector())

    def setup(self, **settings: Any) -> dict[str, Any]:
        return self._call(self._client.setup(**settings))

    def start(self) -> None:
        self._call(self._client.start())

    def stop(self) -> None:
        self._call(self._client.stop())

    def pause(self) -> None:
        self._call(self._client.pause())

    def resume(self) -> None:
        self._call(self._client.resume())
