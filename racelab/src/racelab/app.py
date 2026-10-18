"""RaceLabApp: wires together the CU link, the race engine, the bots and the API.

Everything that is not CU I/O runs in the asyncio loop: the link thread's
messages are brought back into it with `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import math
import dataclasses
import json
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .bots.manager import BotManager
from .bots.brain import pilots_meta
from .bots.profiles import PROFILE_LABELS, PROFILES
from .config import Config, apply_connection_choice, save_overrides
from .cu.firmware import FirmwareLibrary, ImageError, build_images, can_build, missing_images
from .cu.link import CULink
from .events import (
    BUTTON_BRAKE,
    BUTTON_CODE,
    BUTTON_FUEL,
    BUTTON_PACE,
    BUTTON_SPEED,
    BUTTON_START,
    BotSent,
    FirmwareProgress,
    LinkState,
    LinkStats,
    RawFrame,
    StatusMsg,
    TimerMsg,
)
from .race.engine import RaceEngine
from .race.modes import SESSION_TYPES, SessionSettings
from .race.recorder import SessionRecorder
from .race.tower import TowerManager
from .track import TrackError, TrackFeed

logger = logging.getLogger(__name__)

TICK_S = 0.05
BUTTONS = {
    "pace": BUTTON_PACE, "start": BUTTON_START, "speed": BUTTON_SPEED,
    "brake": BUTTON_BRAKE, "fuel": BUTTON_FUEL, "code": BUTTON_CODE,
}
IDLE_STATES = ("idle", "grid", "finished")
# car programming (J word, command = word number): docs/protocol.md §3
CAR_WORDS = {"speed": 0, "brake": 1, "fuel": 2}
# the car live on the track: set a level with the CU keys (hardware 8 ignores the J word).
# The manual: one press opens the display (shows the level), each further press steps it
# (levels 1..10, upward, 10 wraps to 1), START validates and writes the car.
LIVE_LEVELS = 10
LIVE_PRESS_GAP_S = 0.25
LIVE_FIELDS = ("speed", "brake", "fuel")


def live_walk_steps(level: int, current: int) -> int:
    """Key presses to walk the level ring (1..10) from `current` up to `level`."""
    if not 1 <= level <= LIVE_LEVELS or not 1 <= current <= LIVE_LEVELS:
        raise ValueError("level and current: 1..10")
    return (level - current) % LIVE_LEVELS
# during a flash, only these commands go through (reads + flash control)
FIRMWARE_SAFE_OPS = frozenset({
    "state", "meta", "log", "laps", "results", "sessions.list", "sessions.get",
    "firmware.images", "firmware.status", "firmware.abort", "firmware.build_status", "cu.scan",
    "track.set", "track.positions", "track.clear", "connector.set", "connector.clear",
})
FIRMWARE_CONFIRM = "FLASH"
# Hardware test bench (System -> Hardware test): limits enforced here, never in the UI
HW_THROTTLE_MAX = 6
HW_PULSE_MS = (200, 2000)
HW_HANDBACK_PAUSE_MS = (1000, 2500)
HW_BENCH_MAX = 300
HW_REPORT_MAX = 200_000  # bytes
# These ops wait (up to a few seconds): the WebSocket answers them in the background so that
# STOP, sent by the same client meanwhile, is never queued behind them.
LONG_OPS = frozenset({"link.bench", "test.pulse", "test.lane", "test.handback"})
CONNECTOR_LIMITS = {"name": 40, "ui_url": 300, "status": 80}
VERIFY_TIMEOUT_S = 45.0  # the CU must answer after its reboot


class OpError(Exception):
    """Command refused: the message is shown as is to the user."""


class _IdleLink:
    """Link when no CU is chosen: sends nothing, refuses whatever would have to go out."""

    state = "idle"
    version = None
    bots_supported = None
    updating = False
    holds = frozenset()
    trace = trace_polls = False

    def start(self) -> None: ...
    def stop(self, timeout: float = 0) -> None: ...
    def set_bot(self, slot, value) -> None: ...
    def lane(self, slot) -> None: ...
    def estop(self) -> None: ...
    def clear_estop(self) -> None: ...
    def resume_bots(self) -> None: ...
    def press(self, button) -> None: ...
    def reset_timer(self) -> None: ...
    def ignore(self, mask) -> None: ...
    def clrpos(self) -> None: ...
    def setpos(self, address, position) -> None: ...
    def setlap(self, lap) -> None: ...
    def bot_targets(self): return [None] * 6
    def bot_applied(self): return [None] * 6
    def unsafe_slots(self): return []
    def firmware_abort(self) -> None: ...
    def resume_heartbeat(self) -> None: ...
    def bench_cancel(self): return None
    bot_transport = None
    hb_paused = False

    def _refuse(self, *args, **kwargs):
        raise ValueError("no CU chosen: choose one in System → Connection")

    setword = send_raw = firmware_update = pause_heartbeat = bench = _refuse


class _LinkProxy:
    """What the engine, the tower and the bots see: always the current link."""

    def __init__(self, app: "RaceLabApp"):
        self._app = app

    def __getattr__(self, name: str):
        return getattr(self._app.link, name)


Broadcast = Callable[[dict[str, Any]], None]


class RaceLabApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.preferred_mode = cfg.app.mode  # what the user wants
        self.mode = "classic"  # what is in effect: advanced only with the bot firmware
        self.started_at = time.time()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sim = None
        self._listeners: set[Broadcast] = set()
        self._raw_listeners: set[Broadcast] = set()
        self._ticker: asyncio.Task | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=200)
        self.link_info: dict[str, Any] = {
            "state": "connecting", "transport": cfg.connection.transport, "device": None,
            "version": None, "bots_supported": None, "bot_transport": None, "bot_module": None,
            "error": None, "rtt_ms": None, "rx_per_s": 0.0, "tx_per_s": 0.0, "bot_tx_per_s": 0.0, "queue": 0,
        }

        self._link_gen = 0
        self.link: Any = _IdleLink()
        self.request_quit: Callable[[], None] | None = None  # set by the launcher
        self._build_link()
        self.cu = _LinkProxy(self)
        self.tower = TowerManager(self.cu, enabled=cfg.tower.enabled,
                                  min_interval_s=cfg.tower.min_interval_ms / 1000)
        self.engine = RaceEngine(cfg.drivers, cfg.race, self.cu, tower=self.tower,
                                 on_event=self._on_engine_event)
        self.bots = BotManager(self.cu, cfg.safety, on_event=self._on_bot_event)
        self.bots.enabled = False
        self.bots.load_profiles(cfg.bots)
        self.recorder = SessionRecorder(cfg.data_path / "sessions")
        self._recording = False
        self.firmware_lib = FirmwareLibrary(cfg.data_path / "firmware")
        self.fw = self._fw_reset()
        self.fw_build: dict[str, Any] = {"state": "idle", "log": ""}
        if self.sim is not None:
            self.sim.bot_images = self.firmware_lib.bot_line_digests()
        self.track_feed = TrackFeed()
        self._connectors: dict[str, dict[str, Any]] = {}  # agent name -> card (one per connection)
        self._hw_busy: str | None = None  # hardware test in progress (one at a time)
        self._hw: dict[str, Any] | None = None  # what the UI shows of it: {kind, slot, phase}

    # ------------------------------------------------------------ transport

    def _build_link(self) -> None:
        """Create the link (and the virtual CU) from cfg.connection; does not start it."""
        self._link_gen += 1
        gen = self._link_gen
        transport, device, opener = self._make_opener()
        self.link_info.update(
            state="idle" if opener is None else "connecting", transport=transport, device=device,
            version=None, bots_supported=None, bot_transport=None, bot_module=None, error=None,
            rtt_ms=None)
        if opener is None:
            self.link = _IdleLink()
            return
        self.link = CULink(opener, transport=transport, device=device,
                           conn_cfg=self.cfg.connection, safety=self.cfg.safety,
                           emit=lambda msg: self._from_link(msg, gen))

    def _make_opener(self):
        c = self.cfg.connection
        if c.transport == "none":
            return "none", None, None
        if c.transport == "sim":
            from .sim import Simulator

            self.sim = Simulator(self.cfg.sim)
            return "sim", "Virtual CU", self.sim.connect
        if c.transport == "serial":
            if not c.device:
                raise ValueError("[connection] device: serial port required (e.g. /dev/tty.usbserial-… or COM3)")
            from carreralib.serial import SerialConnection

            return "serial", c.device, lambda: SerialConnection(c.device, timeout=c.timeout)
        from carreralib.ble import BLEConnection

        from .cu import blescan

        def open_ble():
            blescan.ensure_available()  # otherwise CoreBluetooth could kill the server
            address = c.device or blescan.first_cu()
            self.link_info["device"] = address
            return BLEConnection(address, timeout=c.timeout, connect_timeout=c.connect_timeout)

        if c.transport == "auto" and c.device and len(c.device.split(":")) != 6 \
                and len(c.device.split("-")) != 5:
            from carreralib.serial import SerialConnection

            return "serial", c.device, lambda: SerialConnection(c.device, timeout=c.timeout)
        return "ble", c.device or None, open_ble

    async def set_connection(self, transport: str, device: str = "", *, stock: bool = False) -> None:
        """Switch CU on the fly: bots stopped and handed back on the old one, then a new connection."""
        if transport not in ("none", "ble", "serial", "sim"):
            raise OpError("connection: none, ble, serial or sim")
        if transport == "serial" and not device:
            raise OpError("serial port required")
        if self.engine.state not in IDLE_STATES:
            raise OpError("end the session before changing CU")
        if self.fw["state"] in ("running", "verifying"):
            raise OpError("firmware update in progress")
        self.bots.release_all()
        old_link, old_sim = self.link, self.sim
        self._link_gen += 1  # nothing from the old link is listened to anymore
        await asyncio.get_running_loop().run_in_executor(None, old_link.stop)
        if old_sim is not None:
            old_sim.close()
        self.sim = None
        apply_connection_choice(self.cfg, {"transport": transport, "device": device, "stock": stock})
        self._build_link()
        if self.sim is not None:
            self.sim.bot_images = self.firmware_lib.bot_line_digests()
        self.bots.on_link(LinkState(self.link_info["state"], transport))
        self._apply_mode()
        self.link.trace = bool(self._raw_listeners)
        self.link.start()
        self._save()
        label = {"none": "none", "ble": "Bluetooth AppConnect", "serial": f"serial {device}",
                 "sim": "virtual CU" + (" (stock firmware)" if stock else "")}[transport]
        self._log("info", f"CU selected: {label}")
        if self.sim is not None and self.track_feed.clear():
            self._log("info", "Connector track replaced by the virtual CU's")
        self._event("connection", {"transport": transport, "device": device or None})
        self._event("track", self.track_info())
        self._event("link", dict(self.link_info))

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.link.start()
        self._ticker = asyncio.create_task(self._tick_loop(), name="racelab-tick")
        self._log("info", f"RaceLab {__version__} — {self._mode_label()} mode")

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        if self._recording:
            self._end_recording()
        await asyncio.get_running_loop().run_in_executor(None, self.link.stop)
        unsafe = self.link.unsafe_slots()
        if unsafe:
            logger.error("WARNING: link lost, the CU may still hold a throttle on slots %s", unsafe)
        if self.sim is not None:
            self.sim.close()

    # ------------------------------------------------------------ subscriptions

    def subscribe(self, fn: Broadcast, *, raw: bool = False) -> Callable[[], None]:
        target = self._raw_listeners if raw else self._listeners
        target.add(fn)
        if raw:
            self.link.trace = True
        return lambda: self._unsubscribe(fn, raw)

    def _unsubscribe(self, fn: Broadcast, raw: bool) -> None:
        (self._raw_listeners if raw else self._listeners).discard(fn)
        if raw and not self._raw_listeners:
            self.link.trace = False

    def _broadcast(self, msg: dict[str, Any], *, raw: bool = False) -> None:
        for fn in list(self._raw_listeners if raw else self._listeners):
            try:
                fn(msg)
            except Exception:
                logger.exception("broadcast")

    def _event(self, name: str, data: dict[str, Any]) -> None:
        self._broadcast({"type": "event", "name": name, "data": data, "t": time.monotonic()})

    def _log(self, level: str, text: str) -> None:
        entry = {"t": time.time(), "level": level, "text": text}
        self.log.append(entry)
        getattr(logger, "warning" if level == "warn" else level, logger.info)(text)
        self._event("log", entry)

    # ------------------------------------------------------------ inputs

    def _from_link(self, msg: object, gen: int) -> None:
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._on_link_msg, msg, gen)
            except RuntimeError:
                pass  # loop shutting down

    def _on_link_msg(self, msg: object, gen: int | None = None) -> None:
        if gen is not None and gen != self._link_gen:
            return  # message from a replaced link
        if isinstance(msg, StatusMsg):
            self.engine.on_status(msg)
        elif isinstance(msg, TimerMsg):
            self.engine.on_timer(msg)
        elif isinstance(msg, FirmwareProgress):
            self._on_firmware(msg)
        elif isinstance(msg, LinkState):
            prev = self.link_info["state"]
            self.link_info.update(state=msg.state, version=msg.version, error=msg.error,
                                  bots_supported=msg.bots_supported, bot_transport=msg.bot_transport,
                                  bot_module=msg.bot_module)
            if msg.device:
                self.link_info["device"] = msg.device
            self.bots.on_link(msg)
            self._apply_mode()
            if msg.state == "connected" and self.fw["state"] == "verifying":
                self._firmware_verified(msg)
            if msg.state != prev:
                self._link_log(msg)
            self._event("link", dict(self.link_info))
        elif isinstance(msg, LinkStats):
            self.link_info.update(dataclasses.asdict(msg))
        elif isinstance(msg, BotSent):
            if self._recording:
                self.recorder.record("bot_tx", {"slot": msg.slot, "value": msg.value,
                                                "lane": msg.lane}, msg.host_t)
        elif isinstance(msg, RawFrame):
            self._broadcast({"type": "raw", "dir": msg.direction,
                             "data": msg.data.decode("latin-1"), "t": msg.host_t}, raw=True)

    def _link_log(self, msg: LinkState) -> None:
        if msg.state == "connected":
            how = {"C": "bots ready (the CU as it is, no firmware update needed)",
                   "B": "bots ready (bot firmware module)"}.get(
                msg.bot_transport, "bots unavailable on this CU")
            self._log("info", f"CU connected ({self.link_info['transport']}, v{msg.version}) — {how}")
            if self.preferred_mode == "advanced" and msg.bots_supported is False:
                self._log("warn", "Bots unavailable on this CU: normal mode")
        elif msg.state == "lost":
            self._log("warn", f"CU link lost{f': {msg.error}' if msg.error else ''} — reconnecting…")
        elif msg.state == "updating":
            self._log("warn", "Firmware update: do not power off the CU or the AppConnect")

    def _on_engine_event(self, name: str, data: dict[str, Any]) -> None:
        self.bots.on_engine_event(name, data)
        if name == "session":
            if data["state"] == "grid":
                self._begin_recording()
            elif data["state"] == "finished" and self._recording:
                self._end_recording()
        if self._recording:
            self.recorder.record(name, data, time.monotonic())
        if name == "false_start":
            self._log("warn", "False start!")
        elif name == "finish" and data.get("position") == 1:
            self._log("info", f"Winner: {self._name(data['slot'])}")
        elif name == "elimination":
            self._log("info", f"Eliminated: {self._name(data['slot'])}")
        self._event(name, data)

    def _on_bot_event(self, name: str, data: dict[str, Any]) -> None:
        if name == "agent_lost" and data.get("reason") == "estop":
            self._log("warn", f"E-STOP: agent {data['agent']} must call bot.claim again (slot {data['slot'] + 1})")
        elif name == "agent_lost":
            self._log("warn", f"Agent {data['agent']} disconnected: slot {data['slot'] + 1} at throttle 0")
        elif name == "bots_reset":
            self._log("warn", "Reconnection: bot throttle reset to 0")
        self._event(name, data)

    def _begin_recording(self) -> None:
        if self._recording:
            self._end_recording()
        session = self.engine.snapshot()["session"]
        self.recorder.begin({
            "id": session.get("id"),
            "type": session.get("type"),
            "title": session.get("title"),
            "session": self.engine.settings.to_dict(),
            "mode": self.mode,
            "drivers": [dataclasses.asdict(d) for d in self.cfg.drivers],
            "link": {k: self.link_info.get(k) for k in ("transport", "version", "bots_supported",
                                                         "bot_transport", "bot_module")},
            "started": time.time(),
        })
        self._recording = True

    def _end_recording(self) -> None:
        self._recording = False
        try:
            self.recorder.end(self.engine.results())
        except Exception:
            logger.exception("session recording")

    async def _tick_loop(self) -> None:
        # the state goes out on every tick (20 Hz max); each WS client filters at its own rate
        while True:
            now = time.monotonic()
            try:
                self.engine.tick(now)  # also advances the position tower
                if self.fw["state"] == "verifying" and now - self.fw["_since"] > VERIFY_TIMEOUT_S:
                    self._firmware_failed("the CU does not answer after the update: "
                                          "retry in recovery mode")
                if self.mode == "advanced":
                    self.bots.tick(self.engine.snapshot(), *self._track_inputs())
                if self._listeners:
                    self._broadcast({"type": "state", "data": self.snapshot()})
            except Exception:
                logger.exception("tick")
            await asyncio.sleep(TICK_S)

    # ------------------------------------------------------------ state

    def snapshot(self) -> dict[str, Any]:
        snap = self.engine.snapshot()
        snap["t"] = time.monotonic()
        snap["mode"] = self.mode
        snap["mode_preferred"] = self.preferred_mode
        snap["advanced_unavailable"] = self.advanced_unavailable()
        snap["link"] = dict(self.link_info)
        snap["estop"] = self.bots.estopped
        snap["firmware"] = {k: v for k, v in self.fw.items() if not k.startswith("_")}
        snap["firmware_build"] = self._fw_build_info()
        snap["hwtest"] = dict(self._hw) if self._hw else None
        if self.mode == "advanced":
            snap["bots"] = self.bots.snapshot(self.link.bot_applied())
            by_slot = {b["slot"]: b for b in snap["bots"]}
            for car in snap["cars"]:
                bot = by_slot.get(car["slot"])
                car["controller"] = (
                    "api" if bot and bot["driver"] == "api"
                    else "bot" if bot and bot["active"] else "human")
        else:
            snap["bots"] = []
            for car in snap["cars"]:
                car["controller"] = "human"
        snap["connectors"] = self.connectors()
        snap["positions"] = None
        if self.sim is not None:
            snap["sim"] = self.sim.snapshot()
            snap["positions"] = self._sim_positions(snap["sim"])
        else:
            snap["positions"] = self.track_feed.snapshot()
        return snap

    @staticmethod
    def _sim_positions(sim_snap: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "sim", "age_ms": 0, "stale": False,
            "cars": [{"slot": c["slot"], "s": c["s"], "lane": c["lane"], "v": c["v"], "off": c["off"]}
                     for c in sim_snap["cars"]],
        }

    def _track_inputs(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(positions, track shape) for the software drivers: the virtual CU's or the connector's."""
        if self.sim is not None:
            return self._sim_positions(self.sim.snapshot()), self.sim.track_geometry()
        return self.track_feed.snapshot(), self.track_feed.geometry

    def meta(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "mode": self.mode,
            "session_types": SESSION_TYPES,
            "session": self.engine.settings.to_dict(),
            "profiles": {k: {"label": PROFILE_LABELS[k], "defaults": PROFILES[k]().to_dict()}
                         for k in PROFILES},
            "pilots": pilots_meta(),  # race brain: the driving models, in the UI's selector order
            "buttons": list(BUTTONS),
            "drivers": [dataclasses.asdict(d) for d in self.cfg.drivers],
            "safety": dataclasses.asdict(self.cfg.safety),
            "api": {"state_hz": self.cfg.api.state_hz, "auth": bool(self.cfg.api.token)},
            "track": self.track_geometry(),
            "firmware_confirm": FIRMWARE_CONFIRM,
            "hwtest": {"throttle_max": HW_THROTTLE_MAX, "pulse_ms": list(HW_PULSE_MS),
                       "handback_pause_ms": list(HW_HANDBACK_PAUSE_MS), "bench_max": HW_BENCH_MAX},
            "config_file": self.cfg.source,
            "data_dir": str(self.cfg.data_path),
        }

    def _name(self, slot: int) -> str:
        for d in self.cfg.drivers:
            if d.slot == slot:
                return d.name
        return f"#{slot + 1}"

    def _mode_label(self) -> str:
        # what the UI calls the modes: "classic" is shown as normal, "advanced" as with bots
        return "bots" if self.mode == "advanced" else "normal"

    # ------------------------------------------------------------ commands

    async def handle(self, op: str, args: dict[str, Any], client: str) -> Any:
        """Single entry point for commands (UI, REST, agents)."""
        fn = self._ops.get(op)
        if fn is None:
            raise OpError(f"unknown command: {op}")
        if op.startswith(("bot.", "console.")) and self.mode != "advanced":
            raise OpError("available in advanced mode only")
        if self.fw["state"] in ("running", "verifying") and op not in FIRMWARE_SAFE_OPS:
            raise OpError("firmware update in progress: wait until it finishes")
        if self._hw and self._hw["slot"] is not None and op.startswith("bot."):
            # the 6 cap of a hardware test must not be bypassed from another page
            for a in args.get("actions") or [args]:
                if isinstance(a, dict) and _same_slot(a.get("slot"), self._hw["slot"]):
                    raise OpError(f"slot {self._hw['slot'] + 1} is under a hardware test")
        try:
            result = fn(self, args, client)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except OpError:
            raise
        except (ValueError, KeyError, TypeError) as e:
            raise OpError(_describe(e)) from None

    def client_gone(self, client: str) -> None:
        self.bots.agent_gone(client)
        if self._connectors.pop(client, None) is not None:
            self._log("info", f"Connector \"{client}\" disconnected: card removed")
            self._event("connectors", {"connectors": self.connectors()})
        if self.track_feed.clear(client):
            self._log("info", f"Track connector \"{client}\" disconnected: map removed")
            self._event("track", self.track_info())

    # connector card: a name, a status and a link to the connector's own page (never rendered here)
    def connectors(self) -> list[dict[str, Any]]:
        return [{"agent": agent, **card} for agent, card in self._connectors.items()]

    def _op_connector_set(self, args, client):
        card: dict[str, Any] = {}
        for key, limit in CONNECTOR_LIMITS.items():
            value = args.get(key)
            if value is None and key != "name":
                card[key] = None
                continue
            if not isinstance(value, str):
                raise OpError(f"{key} must be text")
            value = value.strip()
            if key == "name" and not value:
                raise OpError("name must not be empty")
            if len(value) > limit:
                raise OpError(f"{key} is too long (max {limit} characters)")
            if value and any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise OpError(f"{key} must not contain control characters")
            card[key] = value or None
        if card["ui_url"]:
            url = urlsplit(card["ui_url"])
            if url.scheme.lower() not in ("http", "https") or not url.hostname or any(c.isspace() for c in card["ui_url"]):
                raise OpError("ui_url must be an http or https URL")
        self._connectors[client] = card
        self._event("connectors", {"connectors": self.connectors()})
        return {"agent": client, **card}

    def _op_connector_clear(self, args, client):
        if self._connectors.pop(client, None) is not None:
            self._event("connectors", {"connectors": self.connectors()})

    # track: shape + positions (virtual CU or connector)
    def track_geometry(self) -> dict[str, Any] | None:
        if self.sim is not None:
            return {**self.sim.track_geometry(), "source": "sim", "name": "Virtual CU"}
        geo = self.track_feed.geometry
        return {**geo, "source": self.track_feed.owner} if geo else None

    def track_info(self) -> dict[str, Any]:
        return {"geometry": self.track_geometry()}

    def _feed_guard(self) -> None:
        if self.sim is not None:
            raise OpError("the virtual CU already provides the track and the positions")

    def _op_track_set(self, args, client):
        self._feed_guard()
        geometry = args.get("geometry", args)
        try:
            self.track_feed.set_geometry(client, geometry)
        except TrackError as e:
            raise OpError(str(e)) from None
        self._log("info", f"Track provided by \"{client}\" ({len(self.track_feed.geometry['points'])} points)")
        self._event("track", self.track_info())
        return self.track_geometry()

    def _op_track_positions(self, args, client):
        self._feed_guard()
        try:
            self.track_feed.set_positions(client, args.get("cars"))
        except TrackError as e:
            raise OpError(str(e)) from None

    def _op_track_clear(self, args, client):
        if self.track_feed.clear(client):
            self._event("track", self.track_info())

    # race.*
    def _op_setup(self, args, client):
        settings = SessionSettings.from_dict(args.get("settings", args))
        if SESSION_TYPES[settings.type]["advanced_only"] and self.mode != "advanced":
            raise OpError("this session type requires advanced mode")
        self.engine.configure(settings)
        if settings.type == "rival":
            self._setup_rival(settings)
        self.cfg.race.session = settings.to_dict()
        self._save()
        return self.engine.snapshot()["session"]

    def _setup_rival(self, settings: SessionSettings) -> None:
        for s in self.bots.slots:
            if s.driver == "profile" and s.profile.kind == "rival":
                p = s.profile.to_dict()
                p.update(rival_slot=settings.rival_slot if settings.rival_slot is not None else 0,
                         margin_pct=settings.rival_margin_pct)
                self.bots.set_profile(s.slot, p)

    def _op_start(self, args, client):
        if self.link_info["transport"] == "none":
            raise OpError("no CU chosen: choose one before starting")
        self.engine.start()

    def _op_pause(self, args, client):
        self.engine.pause()

    def _op_resume(self, args, client):
        self.engine.resume()

    def _op_stop(self, args, client):
        self.engine.stop()

    def _op_press(self, args, client):
        button = args["button"]
        if button not in BUTTONS:
            raise OpError(f"unknown button: {button}")
        self.link.press(BUTTONS[button])

    # drivers
    def _op_drivers(self, args, client):
        by_slot = {d.slot: d for d in self.cfg.drivers}
        for item in args["drivers"]:
            d = by_slot[int(item["slot"])]
            for key in ("name", "color", "enabled", "car"):
                if key in item:
                    setattr(d, key, item[key])
            d.name = str(d.name).strip()[:24] or f"Driver {d.slot + 1}"
            if not (isinstance(d.color, str) and d.color.startswith("#") and len(d.color) == 7):
                raise OpError("color: #rrggbb format expected")
        self.engine.set_drivers(self.cfg.drivers)
        self._save()
        return [dataclasses.asdict(d) for d in self.cfg.drivers]

    # bots (advanced mode)
    def _require_enabled(self, slot: int) -> None:
        # the CU excludes disabled drivers from timing: a bot would drive there without being counted
        for d in self.cfg.drivers:
            if d.slot == slot and not d.enabled:
                raise OpError(f"{d.name} (slot {slot + 1}) is disabled: enable it in Session → Drivers")

    def _op_bot_take(self, args, client):
        self._require_enabled(int(args["slot"]))
        self.bots.take(int(args["slot"]), args.get("driver", "manual"))

    def _op_bot_release(self, args, client):
        self.bots.release(int(args["slot"]))

    def _op_bot_throttle(self, args, client):
        self.bots.set_manual(int(args["slot"]), args["value"])

    def _op_bot_lane(self, args, client):
        self.bots.lane(int(args["slot"]))

    def _op_bot_brain(self, args, client):
        self.bots.brain_request(int(args["slot"]), str(args.get("request", "")))

    def _op_bot_params(self, args, client):
        return self.bots.set_params(int(args["slot"]), args["params"])

    def _op_bot_profile(self, args, client):
        result = self.bots.set_profile(int(args["slot"]), args["profile"])
        self.cfg.bots = self.bots.dump_profiles()
        self._save()
        return result

    def _op_bot_claim(self, args, client):
        self._require_enabled(int(args["slot"]))
        self.bots.api_claim(int(args["slot"]), client, free=bool(args.get("free", False)))

    def _op_bot_act(self, args, client):
        actions = args.get("actions") or [args]
        for a in actions:
            self.bots.api_action(int(a["slot"]), client, a.get("throttle"), bool(a.get("lane")))

    def _op_episode_reset(self, args, client):
        slot = int(args["slot"])
        owner = self.bots.slots[slot].agent if slot in range(6) else None
        if owner not in (None, client):
            raise OpError(f"slot {slot} driven by agent {owner}")
        return self.engine.reset_episode(slot)

    def _op_bot_unclaim(self, args, client):
        self.bots.api_release(int(args["slot"]), client)

    def _op_estop(self, args, client):
        self.bots.estop()
        self._log("warn", "E-STOP: throttle 0 on all bots")

    def _op_estop_clear(self, args, client):
        self.bots.clear_estop()
        self._log("info", "E-STOP cleared")

    # app
    def advanced_unavailable(self) -> str | None:
        """Why advanced mode is impossible right now (None = possible)."""
        info = self.link_info
        if info["transport"] == "none":
            return "no CU chosen"
        if info["bots_supported"] is True:
            return None
        if info["bots_supported"] is False:
            return f"this CU (firmware {info.get('version') or '?'}) cannot take software drivers"
        return "CU not verified yet: wait for the connection"

    def _apply_mode(self) -> None:
        """Effective mode = preference, except advanced when the CU cannot take software drivers."""
        reason = self.advanced_unavailable()
        mode = "advanced" if self.preferred_mode == "advanced" and reason is None else "classic"
        if mode == self.mode:
            return
        if mode == "classic":
            self.bots.release_all()
        self.mode = mode
        self.bots.enabled = mode == "advanced"
        if mode == "advanced":
            self._log("info", "Bots on")
        elif self.preferred_mode == "advanced" and self.link_info["bots_supported"] is False:
            self._log("warn", f"Normal mode: {reason}")
        self._event("mode", {"mode": mode, "preferred": self.preferred_mode})

    def _op_mode(self, args, client):
        mode = args["mode"]
        if mode not in ("classic", "advanced"):
            raise OpError("mode: classic or advanced")
        if self.engine.state not in IDLE_STATES:
            raise OpError("end the session before changing mode")
        if mode == "advanced":
            reason = self.advanced_unavailable()
            if reason:
                raise OpError(f"advanced mode impossible: {reason}")
        self.preferred_mode = mode
        self.cfg.mode_choice = mode
        self._save()
        self._apply_mode()
        if mode == self.mode == "classic":  # turning bots on already logs "Bots on"
            self._log("info", "Normal mode")
        return self.mode

    def _op_car_program(self, args, client):
        if self.engine.state not in IDLE_STATES:
            raise OpError("car setup is not possible during a session")
        slot = int(args["slot"])
        if slot not in range(6):
            raise OpError("car: slot 1..6")
        done = []
        for key, word in CAR_WORDS.items():
            if key in args and args[key] is not None:
                value = int(args[key])
                if not 0 <= value <= 15:
                    raise OpError(f"{key}: 0..15")
                self.link.setword(word, slot, value, repeat=2)
                done.append(key)
        if not done:
            raise OpError("nothing to program (speed, brake, fuel)")
        self._log("info", f"Car {slot + 1} programmed: {', '.join(f'{k}={args[k]}' for k in done)}")

    async def _op_car_program_live(self, args, client):
        """Set the level of the car ON the track by simulating the CU keys (works on
        hardware 8, where the J word is ignored): one press opens the level display, one
        press per step, START validates and writes the car."""
        if self.engine.state not in IDLE_STATES:
            raise OpError("car setup is not possible during a session")
        field = str(args.get("field", "speed"))
        if field not in LIVE_FIELDS:
            raise OpError("field: speed, brake or fuel")
        try:
            level = int(args["level"])
            current = int(args.get("current", level))
            steps = live_walk_steps(level, current)
        except KeyError:
            raise OpError("level is required (1..10)") from None
        except (TypeError, ValueError):
            raise OpError("level and current: 1..10") from None
        button = BUTTONS[field]
        self.link.press(button)  # open the level display (no step)
        await asyncio.sleep(LIVE_PRESS_GAP_S)
        for _ in range(steps):
            self.link.press(button)
            await asyncio.sleep(LIVE_PRESS_GAP_S)
        self.link.press(BUTTONS["start"])  # validate + transmit to the car on the track
        self._log("info", f"Live program: {field} {current} -> {level} ({steps} steps + START)")

    def _op_tower_reset(self, args, client):
        self.tower.clear()

    async def _op_console(self, args, client):
        frame = str(args["frame"]).strip().strip('"').rstrip("$").encode("latin-1")
        future: asyncio.Future = self.loop.create_future()

        def done(result, error):
            self.loop.call_soon_threadsafe(
                lambda: future.done() or future.set_result((result, error)))

        try:
            self.link.send_raw(frame, done)
        except ValueError as e:
            raise OpError(str(e)) from None
        try:
            result, error = await asyncio.wait_for(future, 3.0)
        except asyncio.TimeoutError:
            raise OpError("no response from the CU") from None
        if error is not None:
            raise OpError(f"{type(error).__name__}: {error}")
        return result.decode("latin-1") if isinstance(result, bytes) else result

    def _op_trace(self, args, client):
        self.link.trace_polls = bool(args.get("polls", False))

    def _op_laps(self, args, client):
        slot = args.get("slot")
        return self.engine.laps(None if slot is None else int(slot))

    def _op_results(self, args, client):
        return self.engine.results()

    def _op_sessions(self, args, client):
        return self.recorder.list()

    def _op_session_get(self, args, client):
        # not `id`: reserved for WebSocket request/reply correlation
        return self.recorder.load(str(args["session_id"]))

    def _op_log(self, args, client):
        return list(self.log)

    def _op_sim_drop(self, args, client):
        if self.sim is None:
            raise OpError("available only with the virtual CU")
        seconds = float(args.get("seconds", 3.0))
        self.sim.drop_link(seconds)
        self._log("warn", f"Virtual CU: simulated link drop ({seconds:.0f} s)")

    # firmware (DFU)
    def _fw_reset(self) -> dict[str, Any]:
        return {"state": "idle", "stage": None, "done": 0, "total": 0, "image": None,
                "recovery": False, "error": None, "started": None, "eta_s": None,
                "result": None, "_since": 0.0}

    def _op_fw_images(self, args, client):
        return [i.info() for i in self.firmware_lib.images()]

    def _op_fw_status(self, args, client):
        return {k: v for k, v in self.fw.items() if not k.startswith("_")}

    def _op_fw_flash(self, args, client):
        if str(args.get("confirm", "")).strip().upper() != FIRMWARE_CONFIRM:
            raise OpError(f"missing confirmation: type {FIRMWARE_CONFIRM}")
        if self.engine.state not in IDLE_STATES:
            raise OpError("end the session before updating the firmware")
        recovery = bool(args.get("recovery", False))
        if not recovery and self.link_info["state"] != "connected":
            raise OpError("CU not connected (if it stayed in update mode: recovery mode)")
        try:
            image = self.firmware_lib.get(str(args["image"]))
        except ImageError as e:
            raise OpError(str(e)) from None
        if image.known and image.known.status == "defective":
            raise OpError(f"image {image.sha256[:8]}… refused: {image.known.warning}")
        self.bots.release_all()
        self.link.firmware_update(image.lines, recovery=recovery)
        self.fw = self._fw_reset()
        self.fw.update(state="running", stage="prepare", total=len(image.lines), image=image.info(),
                       recovery=recovery, started=time.time(), _since=time.monotonic())
        self._log("warn", f"Firmware update: {image.name} ({len(image.lines)} lines"
                          f"{', recovery' if recovery else ''})")
        self._event("firmware", self._op_fw_status(args, client))
        return self._op_fw_status(args, client)

    def _fw_build_info(self) -> dict[str, Any]:
        return {**self.fw_build, "available": can_build(), "missing": missing_images()}

    def _op_fw_build_status(self, args, client):
        return self._fw_build_info()

    def _op_fw_build(self, args, client):
        """Build the firmware images from source in the background (firmware/build.py)."""
        if self.fw["state"] in ("running", "verifying"):
            raise OpError("firmware update in progress")
        if self.fw_build["state"] == "building":
            raise OpError("the images are already being built")
        if not can_build():
            raise OpError("firmware sources not found: run RaceLab from a checkout of the repository")
        self.fw_build = {"state": "building", "log": ""}
        self._event("firmware_build", self._fw_build_info())
        asyncio.get_running_loop().create_task(self._run_fw_build(), name="racelab-fw-build")
        return self._fw_build_info()

    async def _run_fw_build(self) -> None:
        ok, log = await asyncio.get_running_loop().run_in_executor(None, build_images)
        self.fw_build = {"state": "done" if ok else "failed", "log": log[-3000:]}
        if self.sim is not None:
            self.sim.bot_images = self.firmware_lib.bot_line_digests()
        self._event("firmware_build", self._fw_build_info())

    def _op_fw_abort(self, args, client):
        if self.fw["state"] != "running":
            raise OpError("no update in progress")
        self.link.firmware_abort()

    def _on_firmware(self, msg: FirmwareProgress) -> None:
        fw = self.fw
        if fw["state"] not in ("running", "verifying"):
            return
        fw.update(stage=msg.stage, done=msg.done, total=msg.total)
        if msg.stage == "write" and msg.done:
            rate = msg.done / max(0.001, time.monotonic() - fw["_since"])
            fw["eta_s"] = round((msg.total - msg.done) / rate) if rate > 0 else None
        if msg.stage == "reboot":
            fw.update(state="verifying", eta_s=None, _since=time.monotonic())
        elif msg.stage == "failed":
            self._firmware_failed(msg.error or "failed")
            return
        self._event("firmware", self._op_fw_status({}, ""))

    def _firmware_verified(self, st: LinkState) -> None:
        fw = self.fw
        fw.update(state="done", stage="done",
                  result={"version": st.version, "bots_supported": st.bots_supported,
                          "bot_module": st.bot_module})
        bots = {True: "bot module detected", False: "firmware without bot module", None: "not probed"}
        self._log("info", f"Firmware updated — CU v{st.version}, {bots[st.bot_module]}")
        self._event("firmware", self._op_fw_status({}, ""))

    def _firmware_failed(self, error: str) -> None:
        self.fw.update(state="failed", error=error, eta_s=None)
        self._log("error", f"Firmware update: {error}. The CU can still be re-flashed "
                           f"(factory bootloader): retry, in recovery mode if needed.")
        self._event("firmware", self._op_fw_status({}, ""))

    def add_firmware(self, data: bytes, name: str) -> dict[str, Any]:
        try:
            image = self.firmware_lib.add(data, name)
        except ImageError as e:
            raise OpError(str(e)) from None
        self._log("info", f"Firmware image imported: {image.name} ({len(image.lines)} lines)")
        return image.info()

    # ------------------------------------------------------------ hardware test (System -> Hardware test)

    def _hw_phase(self, phase: str, **extra: Any) -> None:
        if self._hw is not None:
            self._hw = {**self._hw, "phase": phase, **extra}
            self._event("hwtest", dict(self._hw))

    def _hw_guard(self, slot: int, *, need_c: bool = False, alone: bool = False) -> None:
        """Every reason a test must not take a slot (the checks are repeated at run time by the hold)."""
        if self._hw_busy:
            raise OpError(f"another hardware test is running ({self._hw_busy})")
        info = self.link_info
        if info["state"] != "connected":
            raise OpError("CU not connected")
        if info["bots_supported"] is not True:
            raise OpError("this CU cannot take software drivers (bots)")
        if self.mode != "advanced":
            raise OpError("switch to With bots first (top bar)")
        if self.engine.state not in IDLE_STATES:
            raise OpError("a session is running: end it before a hardware test")
        if self.bots.estopped:
            raise OpError("E-STOP engaged: clear it first")
        if need_c and info["bot_transport"] != "C":
            raise OpError("this test is about the C command: this CU is driven another way")
        s = self.bots.slots[slot]
        if s.driver == "api":
            raise OpError(f"slot {slot + 1} is driven by an API agent ({s.agent or 'unclaimed'})")
        if s.driver is not None:
            raise OpError(f"slot {slot + 1} is already a bot: release it first")
        self._require_enabled(slot)
        if alone and self.bots.active_slots():
            # the C table is one frame for all slots: a pause would drop every bot, not only the test's
            raise OpError("the hand-back test needs every slot free of bots (release them first)")

    def _hw_abort_reason(self) -> str | None:
        if self.bots.estopped:
            return "estop"
        if self.link_info["state"] != "connected":
            return "link"
        if self.engine.state not in IDLE_STATES:
            return "session"
        return None

    async def _hw_wait_applied(self, slot: int, value: int, timeout: float) -> bool:
        """Until the CU link has put `value` on the wire for the slot."""
        end = time.monotonic() + timeout
        while self.link.bot_applied()[slot] != value:
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.02)
        return True

    async def _hw_hold(self, slot: int, seconds: float) -> dict[str, Any]:
        """Wait `seconds` (server clock), watching E-STOP, link and session; reports what the CU got."""
        t0 = time.monotonic()
        peak, gate, aborted = 0, None, None
        while (left := seconds - (time.monotonic() - t0)) > 0:
            aborted = self._hw_abort_reason()
            if aborted:
                break
            applied = self.link.bot_applied()[slot]
            peak = max(peak, applied or 0)
            gate = self.bots.slots[slot].gate or gate
            await asyncio.sleep(min(0.05, left))
        return {"held_ms": round((time.monotonic() - t0) * 1000), "applied_peak": peak,
                "gate": gate, "aborted": aborted}

    async def _hw_end(self, slot: int) -> dict[str, Any]:
        """Always: throttle 0 on the wire, heartbeat back, slot handed back. Never raises."""
        end = {"zeroed": False, "released": False}
        s = self.bots.slots[slot]
        if s.driver != "manual":
            return end  # somebody else took the slot meanwhile: not ours to touch
        try:
            self.bots.set_manual(slot, 0)
        except Exception:
            logger.exception("hardware test: set_manual(0)")
            try:
                self.link.set_bot(slot, 0)
            except Exception:
                logger.exception("hardware test: set_bot(0)")
        try:
            self.link.resume_heartbeat()  # after the 0 is queued: never a heartbeat at the old throttle
        except Exception:
            logger.exception("hardware test: resume_heartbeat")
        try:
            end["zeroed"] = await self._hw_wait_applied(slot, 0, 0.3)
        except Exception:
            logger.exception("hardware test: wait for 0")
        finally:  # also on a second cancellation (CancelledError) during the wait
            try:
                self.bots.release(slot)
                end["released"] = True
            except Exception:
                logger.exception("hardware test: release")
                try:
                    self.link.set_bot(slot, None)
                except Exception:
                    logger.exception("hardware test: set_bot(None)")
        return end

    async def _hw_run(self, kind: str, slot: int, body, *, need_c: bool = False, alone: bool = False):
        """Take the slot as a manual bot, run `body`, and in all cases zero + release it."""
        self._hw_guard(slot, need_c=need_c, alone=alone)
        self._hw_busy = kind
        self._hw = {"kind": kind, "slot": slot, "phase": "start"}
        self._event("hwtest", dict(self._hw))
        out: dict[str, Any] = {"slot": slot}
        try:
            self.bots.take(slot, "manual")
            self._hw_phase("run")
            out.update(await body())
        finally:
            try:
                out.update(await self._hw_end(slot))
            finally:
                self._hw_busy = None
                self._hw_phase("done", aborted=out.get("aborted"))
                self._hw = None
        self._log("info", f"Hardware test {kind} on slot {slot + 1}: "
                          f"{'aborted (' + out['aborted'] + ')' if out.get('aborted') else 'done'}")
        return out

    def _hw_throttle(self, args, default: int | None = None) -> int:
        """Requested throttle, hard-capped at HW_THROTTLE_MAX (refused above) and at [safety] max_throttle."""
        throttle = min(_hw_int(args, "throttle", 1, HW_THROTTLE_MAX, default=default), HW_THROTTLE_MAX)
        effective = min(throttle, self.cfg.safety.max_throttle)
        if effective < 1:
            raise OpError("[safety] max_throttle is 0: no bot can move")
        return effective

    async def _hw_move(self, slot: int, throttle: int, ms: int, *, lane: bool = False) -> dict[str, Any]:
        """Hold `throttle` for `ms` (clock started once the CU link has put it on the wire)."""
        self.bots.set_manual(slot, throttle)
        if not await self._hw_wait_applied(slot, throttle, 0.6):
            return {"throttle": throttle, "ms": ms, "held_ms": 0, "applied_peak": 0, "acked": False,
                    "gate": self.bots.slots[slot].gate, "aborted": self._hw_abort_reason()}
        if lane:
            self.bots.lane(slot)  # the existing lane path (C: button held ~120 ms; B: one-shot)
        out = await self._hw_hold(slot, ms / 1000)
        out = {"throttle": throttle, "ms": ms, "acked": out["applied_peak"] == throttle, **out}
        return {**out, "lane_sent": out["aborted"] is None} if lane else out

    async def _op_test_pulse(self, args, client):
        slot = _hw_int(args, "slot", 0, 5)
        throttle = self._hw_throttle(args)
        ms = _hw_int(args, "ms", *HW_PULSE_MS)
        return await self._hw_run("pulse", slot, lambda: self._hw_move(slot, throttle, ms))

    async def _op_test_lane(self, args, client):
        """One-shot lane request while the car runs (it applies it at the next lane changer)."""
        slot = _hw_int(args, "slot", 0, 5)
        throttle = self._hw_throttle(args, default=3)
        ms = _hw_int(args, "ms", *HW_PULSE_MS, default=HW_PULSE_MS[1])
        return await self._hw_run("lane", slot, lambda: self._hw_move(slot, throttle, ms, lane=True))

    async def _op_test_handback(self, args, client):
        slot = _hw_int(args, "slot", 0, 5)
        throttle = self._hw_throttle(args, default=3)
        pause_ms = _hw_int(args, "pause_ms", *HW_HANDBACK_PAUSE_MS, default=1500)

        async def body():
            self.bots.set_manual(slot, throttle)
            if not await self._hw_wait_applied(slot, throttle, 0.6):
                return {"throttle": throttle, "pause_ms": pause_ms, "paused_ms": 0, "acked": False,
                        "aborted": self._hw_abort_reason()}
            run_up = await self._hw_hold(slot, 0.5)  # the car picks up speed
            if run_up["aborted"]:
                return {"throttle": throttle, "pause_ms": pause_ms, "paused_ms": 0, "acked": True,
                        "aborted": run_up["aborted"]}
            self._hw_phase("paused", pause_ms=pause_ms)
            self.link.pause_heartbeat(pause_ms / 1000 + 0.25)  # auto-clears, whatever happens here
            out = await self._hw_hold(slot, pause_ms / 1000)
            return {"throttle": throttle, "pause_ms": pause_ms, "paused_ms": out["held_ms"], "acked": True,
                    "gate": out["gate"], "aborted": out["aborted"]}

        return await self._hw_run("handback", slot, body, need_c=True, alone=True)

    async def _op_link_bench(self, args, client):
        n = _hw_int(args, "n", 1, HW_BENCH_MAX, default=100)
        if self._hw_busy:
            raise OpError(f"another hardware test is running ({self._hw_busy})")
        if self.link_info["state"] != "connected":
            raise OpError("CU not connected")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def done(result, error):
            loop.call_soon_threadsafe(lambda: future.done() or future.set_result((result, error)))

        self._hw_busy = "bench"
        self._hw = {"kind": "bench", "slot": None, "phase": "run"}
        timed_out = False
        try:
            try:
                self.link.bench(n, done)
            except (ValueError, RuntimeError) as e:
                raise OpError(str(e)) from None
            try:
                result, error = await asyncio.wait_for(future, _bench_timeout(n))
            except asyncio.TimeoutError:
                # a slow or lossy link is what this test is for: keep what was measured
                partial = self.link.bench_cancel()
                if partial is None or not partial.poll:
                    raise OpError("the CU did not answer in time") from None
                result, error, timed_out = partial.result(), None, True
        finally:
            self.link.bench_cancel()
            self._hw_busy, self._hw = None, None
        if error:
            raise OpError(f"latency test interrupted: {error}")
        result.update(transport=self.link_info["transport"], bot_transport=self.link_info["bot_transport"],
                      timed_out=timed_out)
        return result

    def _op_test_report(self, args, client):
        report = args.get("report")
        if not isinstance(report, dict):
            raise OpError("report: an object is expected")
        if len(json.dumps(report, ensure_ascii=False)) > HW_REPORT_MAX:
            raise OpError("report too large")
        report = {**report, "server": {
            "racelab": __version__, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mode": self.mode,
            "link": {k: self.link_info.get(k) for k in ("state", "transport", "version", "bots_supported",
                                                         "bot_transport", "bot_module", "rtt_ms")},
            "safety": dataclasses.asdict(self.cfg.safety)}}
        folder = self.cfg.data_path / "hardware-tests"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stem = "hwtest-" + time.strftime("%Y%m%d-%H%M%S")
            path, n = folder / f"{stem}.json", 1
            while path.exists():
                n += 1
                path = folder / f"{stem}-{n}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            raise OpError(f"cannot save the report: {e}") from None
        self._log("info", f"Hardware test report saved: {path.name}")
        return {"name": path.name, "path": str(path), "report": report}

    # connection and application
    async def _op_cu_connect(self, args, client):
        await self.set_connection(str(args.get("transport", "")), str(args.get("device") or ""),
                                  stock=bool(args.get("stock", False)))
        return dict(self.link_info)

    async def _op_cu_scan(self, args, client):
        timeout = min(15.0, max(2.0, float(args.get("timeout", 5.0))))
        return await asyncio.get_running_loop().run_in_executor(None, scan_devices, timeout)

    def _op_quit(self, args, client):
        if self.request_quit is None:
            raise OpError("quitting from the interface is unavailable")
        if self.fw["state"] in ("running", "verifying"):
            raise OpError("firmware update in progress: cannot quit now")
        self._log("info", "Quit requested from the interface")
        self.loop.call_later(0.3, self.request_quit)  # time for the reply to go out

    def _save(self) -> None:
        try:
            save_overrides(self.cfg)
        except OSError as e:
            logger.warning("settings not saved: %s", e)

    _ops: dict[str, Callable[..., Any | Awaitable[Any]]] = {
        "state": lambda self, a, c: self.snapshot(),
        "meta": lambda self, a, c: self.meta(),
        "race.setup": _op_setup,
        "race.start": _op_start,
        "race.pause": _op_pause,
        "race.resume": _op_resume,
        "race.stop": _op_stop,
        "cu.press": _op_press,
        "drivers.update": _op_drivers,
        "bot.take": _op_bot_take,
        "bot.release": _op_bot_release,
        "bot.throttle": _op_bot_throttle,
        "bot.lane": _op_bot_lane,
        "bot.profile": _op_bot_profile,
        "bot.brain": _op_bot_brain,
        "bot.params": _op_bot_params,
        "bot.claim": _op_bot_claim,
        "bot.act": _op_bot_act,
        "bot.unclaim": _op_bot_unclaim,
        "episode.reset": _op_episode_reset,
        "estop": _op_estop,
        "estop.clear": _op_estop_clear,
        "mode.set": _op_mode,
        "car.program": _op_car_program,
        "car.program_live": _op_car_program_live,
        "tower.reset": _op_tower_reset,
        "console.send": _op_console,
        "console.trace": _op_trace,
        "laps": _op_laps,
        "results": _op_results,
        "sessions.list": _op_sessions,
        "sessions.get": _op_session_get,
        "log": _op_log,
        "sim.drop_link": _op_sim_drop,
        "connector.set": _op_connector_set,
        "connector.clear": _op_connector_clear,
        "track.set": _op_track_set,
        "track.positions": _op_track_positions,
        "track.clear": _op_track_clear,
        "cu.connect": _op_cu_connect,
        "cu.scan": _op_cu_scan,
        "app.quit": _op_quit,
        "firmware.images": _op_fw_images,
        "firmware.status": _op_fw_status,
        "firmware.flash": _op_fw_flash,
        "firmware.abort": _op_fw_abort,
        "firmware.build": _op_fw_build,
        "firmware.build_status": _op_fw_build_status,
        "link.bench": _op_link_bench,
        "test.pulse": _op_test_pulse,
        "test.lane": _op_test_lane,
        "test.handback": _op_test_handback,
        "test.report": _op_test_report,
    }


def _bench_timeout(n: int) -> float:
    return 15.0 + n * 0.1


def _same_slot(value: Any, slot: int) -> bool:
    """True if `value` designates `slot` the way the bot ops parse it (`int(args["slot"])`: "0" and 0.0
    are slot 0 too); an unparsable value designates nothing and is refused later by the op itself."""
    try:
        return int(value) == slot
    except (TypeError, ValueError, OverflowError):
        return False


def _hw_int(args: dict[str, Any], key: str, lo: int, hi: int, *, default: int | None = None) -> int:
    """Integer argument within [lo, hi]: refused, never clamped (the limits are safety limits)."""
    value = args.get(key, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or int(value) != value):
        raise OpError(f"{key}: an integer {lo}..{hi} is expected")
    if not lo <= value <= hi:
        raise OpError(f"{key}: {lo}..{hi} expected (got {int(value)})")
    return int(value)


def _describe(e: Exception) -> str:
    if isinstance(e, KeyError):
        return f"missing parameter: {e.args[0]}"
    return str(e)


def scan_devices(timeout: float = 5.0) -> dict[str, Any]:
    """AppConnect units visible over Bluetooth + serial ports (thread outside the asyncio loop)."""
    found: list[dict[str, str]] = []
    errors: list[str] = []
    try:
        from serial.tools.list_ports import comports

        for port in comports():
            # only USB-serial adapters (virtual Bluetooth ports have no VID)
            if port.vid is None or "Bluetooth" in port.device:
                continue
            found.append({"transport": "serial", "device": port.device,
                          "label": port.description if port.description not in (None, "n/a") else port.device})
    except Exception as e:  # pyserial missing or system error
        errors.append(f"serial ports: {e}")
    from .cu import blescan

    devices, error = blescan.scan(timeout)  # separate process: see cu/blescan.py
    found += [{"transport": "ble", "device": d["address"], "label": d["name"]} for d in devices]
    if error:
        errors.append(error)
    return {"devices": found, "errors": errors}
