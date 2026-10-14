"""The RaceLab adapter: the ONLY module of trackloc that talks to RaceLab.

It turns RaceLab into the three seams of the pipeline:

- inputs: RaceLab events and state become plain dicts on the host clock (`start_inputs`):
  `crossing` {t, slot, sector, cu_ts} (`t` = the server's `time.monotonic()` when it emitted the
  event: same machine, same clock) and `throttle` {t, slot, applied} (on every change of the
  throttle the CU applies to a bot);
- sink: `set_track(geometry)` and `publish(t, cars)` map to `track.set` / `track.positions`;
- control (for `map`): claim / act / unclaim / estop and the session, for `drive.DiscoveryDriver`.

Built on `racelab.client.SyncClient` (a background thread owns the connection), so every method
can be called from any thread. Dependency direction: trackloc imports RaceLab, never the reverse.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from racelab.client import DEFAULT_URL, Event, RaceLabError, SyncClient

__all__ = ["RaceLabLink", "DEFAULT_URL", "RaceLabError"]


class RaceLabLink:
    def __init__(self, url: str = DEFAULT_URL, name: str = "camera", token: str | None = None) -> None:
        self.url, self.name, self.token = url, name, token
        self.client: SyncClient | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._applied: dict[int, int | None] = {}
        self._unsub: list[Callable[[], None]] = []
        self.raw: Callable[[str, str, Any, float | None], None] | None = None  # recorder hook for raw data

    def connect(self) -> RaceLabLink:
        self.client = SyncClient.connect(self.url, agent=self.name, state_hz=20.0, token=self.token)
        return self

    def close(self) -> None:
        self._stop.set()
        for u in self._unsub:
            u()
        if self.client is not None:
            self.client.close()
        if self._thread:
            self._thread.join(3)

    # ------------------------------------------------------------------ inputs

    def start_inputs(self, put: Callable[[dict[str, Any]], None]) -> None:
        """Push `crossing` and `throttle` dicts to `put` (must return fast)."""
        assert self.client is not None
        client = self.client

        def on_event(ev: Event) -> None:
            if self.raw:
                self.raw("event", ev.name, ev.data, ev.t)
            if ev.name == "crossing" and "cu_ts" in ev.data:
                put({"type": "crossing", "t": ev.t, "slot": ev.data["slot"], "sector": ev.data.get("sector", 1),
                     "cu_ts": ev.data["cu_ts"]})

        self._unsub.append(client.on_event("*", on_event))

        def states() -> None:
            while not self._stop.is_set():
                try:
                    st = client.wait_state(timeout=2.0)
                except TimeoutError:
                    continue
                except ConnectionError:
                    return
                if self.raw:
                    self.raw("state", "state", _slim(st), st.get("t"))
                for bot in st.get("bots") or []:
                    slot, applied = bot["slot"], bot.get("applied") if bot.get("active") else None
                    if applied != self._applied.get(slot):  # unknown = a human holds the controller (None)
                        put({"type": "throttle", "t": st.get("t", 0.0), "slot": slot, "applied": applied})
                    self._applied[slot] = applied

        self._thread = threading.Thread(target=states, name="trackloc-state", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ sink

    def set_track(self, geometry: dict[str, Any]) -> None:
        assert self.client is not None
        self.client.set_track(geometry)

    def publish(self, t: float, cars: list[dict[str, Any]]) -> None:
        assert self.client is not None
        self.client.send_positions(cars)

    def clear_track(self) -> None:
        if self.client is not None and not self.client.closed:
            self.client.clear_track()

    # ------------------------------------------------------------------ control (map)

    def ensure_session(self) -> bool:
        """Make sure crossings count: they are only reported while a session is running. Starts a
        practice session if none is; returns True if it did (so `stop_session` ends it)."""
        assert self.client is not None
        state = self.client.state.get("session", {}).get("state", "idle")
        if state in ("running", "finishing", "paused"):
            return False
        if state not in ("idle", "grid", "finished"):
            raise RaceLabError(f"a session is in state {state!r}: end it first")
        self.client.setup(type="practice")
        self.client.start()
        return True

    def stop_session(self) -> None:
        assert self.client is not None
        self.client.stop()

    def claim(self, slot: int) -> None:
        assert self.client is not None
        self.client.claim(slot)

    def act(self, slot: int, throttle: int | None = None, lane: bool = False) -> None:
        assert self.client is not None
        self.client.act(slot, throttle, lane)

    def unclaim(self, slot: int) -> None:
        assert self.client is not None
        self.client.unclaim(slot)

    def estop(self) -> None:
        assert self.client is not None
        self.client.estop()


def _slim(state: dict[str, Any]) -> dict[str, Any]:
    """The part of a state snapshot worth recording (positions of other connectors excluded)."""
    return {"t": state.get("t"), "session": {k: state.get("session", {}).get(k) for k in ("state", "type", "id")},
            "estop": state.get("estop"), "link": {k: state.get("link", {}).get(k) for k in ("state", "transport")},
            "bots": [{k: b.get(k) for k in ("slot", "active", "driver", "throttle", "requested", "applied", "gate", "agent")}
                     for b in state.get("bots") or []],
            "cars": [{k: c.get(k) for k in ("slot", "laps", "last_ms", "best_ms", "since_cross_ms", "controller")}
                     for c in state.get("cars") or []],
            "positions": state.get("positions")}
