"""Shared fixtures. Tests that need OpenCV / numpy use `pytest.importorskip` in their own module;
this file imports nothing heavy (the tracker and drive tests run on a bare Python)."""

from __future__ import annotations

import asyncio
import threading

import pytest


class RaceLabThread:
    """A real RaceLab app + API server in a background thread (its own asyncio loop)."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.ready = threading.Event()
        self.url = ""
        self.app = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        from aiohttp import web

        from racelab.api.server import create_web_app
        from racelab.app import RaceLabApp

        self.loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
        self.app = RaceLabApp(self.cfg)
        runner = web.AppRunner(create_web_app(self.app))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"ws://127.0.0.1:{runner.addresses[0][1]}/ws"
        await self.app.start()
        self.ready.set()
        await self._stop.wait()
        await self.app.stop()
        await runner.cleanup()

    def start(self) -> RaceLabThread:
        self._thread.start()
        assert self.ready.wait(15), "RaceLab did not start"
        return self

    def stop(self) -> None:
        assert self.loop is not None and self._stop is not None
        self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(15)

    def call(self, fn):
        """Run `fn()` in the app's loop and return its result (inspect the app safely)."""

        async def wrapper():
            return fn()

        return asyncio.run_coroutine_threadsafe(wrapper(), self.loop).result(5)


@pytest.fixture
def racelab_factory(tmp_path):
    started: list[RaceLabThread] = []

    def make(transport: str = "none", **sim) -> RaceLabThread:
        from racelab.config import Config

        cfg = Config()
        cfg.app.data_dir = str(tmp_path / f"data{len(started)}")
        cfg.connection.transport = transport
        cfg.sim.latency_ms = cfg.sim.jitter_ms = 0
        cfg.sim.humans = []
        cfg.sim.seed = 1
        cfg.safety.deadman_ms = 600
        for k, v in sim.items():
            setattr(cfg.sim, k, v)
        server = RaceLabThread(cfg).start()
        started.append(server)
        return server

    yield make
    for s in started:
        s.stop()
