"""RaceLabApp: wires together the CU link, the race engine, the bots and the API.

Everything that is not CU I/O runs in the asyncio loop: the link thread's
messages are brought back into it with `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from . import __version__, update
from .bots.manager import BotManager
from .bots.brain import pilots_meta
from .bots.profiles import PROFILE_LABELS, PROFILES
from .config import Config, apply_connection_choice, save_overrides
from .cu.firmware import FirmwareLibrary, FirmwareUpdate, ImageError, build_images, can_build, missing_images
from .cu.link import CULink
from .drive import ControllerInterface, SerialInterface, bluetooth, interface_ports
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
    InterfaceState,
    RawFrame,
    StatusMsg,
    TimerMsg,
)
from .errors import OpError
from .hwtest import HwTestBench
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
    "firmware.images", "firmware.status", "firmware.abort", "firmware.build_status", "cu.scan", "interface.scan", "interface.bt_scan",
    "track.set", "track.positions", "track.clear", "connector.set", "connector.clear",
})
FIRMWARE_CONFIRM = "FLASH"
# These ops wait (up to a few seconds): the WebSocket answers them in the background so that
# STOP, sent by the same client meanwhile, is never queued behind them.
LONG_OPS = frozenset({"link.bench", "test.pulse", "test.lane", "test.handback", "interface.bt_scan",
                      "interface.pair", "update.check", "update.apply"})
UPDATE_ERRORS = (OSError, RuntimeError, ValueError, subprocess.SubprocessError)  # offline, no git, GitHub down...
UPDATE_EVERY_S = 6 * 3600  # an app left running checks again this often
CONNECTOR_LIMITS = {"name": 40, "ui_url": 300, "status": 80}


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


class _DriveProxy:
    """What the bots see: the controller interface when one is set, else the CU link (C or B)."""

    def __init__(self, app: "RaceLabApp"):
        self._app = app

    def __getattr__(self, name: str):
        return getattr(self._app.interface or self._app.link, name)


class _LinkProxy:
    """What the engine and the tower see: always the current CU link."""

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
            # the controller interface (racelab/drive/), when one drives the bots instead of the CU
            "interface": None, "interface_kind": None, "interface_device": None, "interface_error": None,
            "interface_info": {},
        }
        self._cu_bots: tuple[bool | None, str | None] = (None, None)  # what the CU link itself offers

        self._link_gen = 0
        self._iface_gen = 0
        self.link: Any = _IdleLink()
        self.interface: ControllerInterface | None = None
        self.request_quit: Callable[[], None] | None = None  # set by the launcher
        self.restart = False  # the launcher starts RaceLab again once it has stopped (after an update)
        self.update_info: dict[str, Any] | None = None  # the last update check
        self._build_link()
        self._build_interface()
        self.cu = _LinkProxy(self)
        self.drive = _DriveProxy(self)
        self.tower = TowerManager(self.cu, enabled=cfg.tower.enabled,
                                  min_interval_s=cfg.tower.min_interval_ms / 1000)
        self.engine = RaceEngine(cfg.drivers, cfg.race, self.cu, tower=self.tower,
                                 on_event=self._on_engine_event)
        self.bots = BotManager(self.drive, cfg.safety, on_event=self._on_bot_event)
        self.bots.enabled = False
        self.bots.load_profiles(cfg.bots)
        self.recorder = SessionRecorder(cfg.data_path / "sessions")
        self._recording = False
        self.firmware_lib = FirmwareLibrary(cfg.data_path / "firmware")
        self.fw = FirmwareUpdate(self.firmware_lib, self._flash_cu, self._log, self._event)
        self.fw_build: dict[str, Any] = {"state": "idle", "log": ""}
        if self.sim is not None:
            self.sim.bot_images = self.firmware_lib.bot_line_digests()
        self.track_feed = TrackFeed()
        self._connectors: dict[str, dict[str, Any]] = {}  # agent name -> card (one per connection)
        self.hwtest = HwTestBench(self.cu, self.bots, self._hw_preconditions, safety=cfg.safety,
                                  folder=cfg.data_path / "hardware-tests", log=self._log, event=self._event)

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
        self._cu_bots = (None, None)
        if opener is None:
            self.link = _IdleLink()
            return
        self.link = CULink(opener, transport=transport, device=device,
                           conn_cfg=self.cfg.connection, safety=self.cfg.safety,
                           emit=lambda msg: self._from_link(msg, gen))

    def _build_interface(self) -> None:
        """The controller interface from cfg.interface, if any (docs/controller-interface.md); does
        not start it. Next to a real CU only: the virtual CU drives its own cars."""
        self._iface_gen += 1
        gen = self._iface_gen
        port = self.cfg.interface.port
        on = bool(port) and self.link_info["transport"] in ("ble", "serial")
        self.interface = SerialInterface(
            port, self.cfg.safety, emit=lambda msg: self._from_link(msg, gen, "interface"),
            baud=self.cfg.interface.baud) if on else None
        self.link_info.update(interface="connecting" if on else None, interface_kind="serial" if on else None,
                              interface_device=port or None, interface_error=None, interface_info={})
        self._sync_bots_info()

    def _sync_bots_info(self) -> None:
        """Bots available, and how: through the controller interface when one is set, else the CU's own."""
        if self.interface is not None:
            self.link_info.update(bots_supported=True, bot_transport="I")
        else:
            self.link_info.update(bots_supported=self._cu_bots[0], bot_transport=self._cu_bots[1])

    def _bots_link_changed(self) -> None:
        self.bots.on_link(LinkState(self.link_info["state"], self.link_info["transport"] or "none",
                                    bots_supported=self.link_info["bots_supported"]))
        self._apply_mode()

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
        self._check_relink("changing CU")
        await self._relink(lambda: apply_connection_choice(
            self.cfg, {"transport": transport, "device": device, "stock": stock}))
        label = {"none": "none", "ble": "Bluetooth AppConnect", "serial": f"serial {device}",
                 "sim": "virtual CU" + (" (stock firmware)" if stock else "")}[transport]
        self._log("info", f"CU selected: {label}",
                  "cu_selected." + transport + ("_stock" if transport == "sim" and stock else ""), device=device)
        if self.sim is not None and self.track_feed.clear():
            self._log("info", "Connector track replaced by the virtual CU's", "track_replaced_sim")
        self._event("connection", {"transport": transport, "device": device or None})
        self._event("track", self.track_info())
        self._event("link", dict(self.link_info))

    async def set_interface(self, port: str) -> None:
        """Choose (or remove, "") the controller interface's port. Only the interface restarts: the
        CU link carries on."""
        self._check_relink("changing the controller interface")
        self.bots.release_all()
        await self._stop_interface()
        self.cfg.interface.port = self.cfg.interface_choice = port
        self._build_interface()
        self._bots_link_changed()
        if self.interface is not None:
            self.interface.start()
        self._save()
        if port:
            self._log("info", f"Controller interface: {port}", "interface_set", port=port)
        else:
            self._log("info", "Controller interface removed", "interface_removed")
        self._event("link", dict(self.link_info))

    async def _stop_interface(self) -> None:
        old, self.interface = self.interface, None
        self._iface_gen += 1  # nothing from the old interface is listened to anymore
        if old is not None:
            await asyncio.get_running_loop().run_in_executor(None, old.stop)

    def _check_relink(self, what: str) -> None:
        if self.engine.state not in IDLE_STATES:
            raise OpError(f"end the session before {what}")
        if self.fw.busy:
            raise OpError("firmware update in progress")

    async def _relink(self, apply: Callable[[], None]) -> None:
        """Bots stopped and handed back on the old link, `apply` the new settings, new link."""
        self.bots.release_all()
        old_link, old_sim = self.link, self.sim
        self._link_gen += 1  # nothing from the old link is listened to anymore
        await self._stop_interface()
        await asyncio.get_running_loop().run_in_executor(None, old_link.stop)
        if old_sim is not None:
            old_sim.close()
        self.sim = None
        apply()
        self._build_link()
        self._build_interface()  # on a real CU only
        if self.sim is not None:
            self.sim.bot_images = self.firmware_lib.bot_line_digests()
        self._bots_link_changed()
        self.link.trace = bool(self._raw_listeners)
        self.link.start()
        if self.interface is not None:
            self.interface.start()
        self._save()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.link.start()
        if self.interface is not None:
            self.interface.start()
        self._ticker = asyncio.create_task(self._tick_loop(), name="racelab-tick")
        self._log("info", f"RaceLab {__version__} — {self._mode_label()} mode",
                  f"started.{self._mode_label()}", version=__version__)

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        if self._recording:
            self._end_recording()
        await self._stop_interface()  # its release frame first
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

    def _log(self, level: str, text: str, key: str | None = None, /, **params: Any) -> None:
        """`text` (English) goes to the log file and the API; `key` + `params` let the UI show
        the entry in its own language ("srv.<key>" in web/js/i18n.js, `text` when it has no such key)."""
        entry = {"t": time.time(), "level": level, "text": text}
        if key:
            entry.update(key=key, params=params)
        self.log.append(entry)
        getattr(logger, "warning" if level == "warn" else level, logger.info)(text)
        self._event("log", entry)

    # ------------------------------------------------------------ inputs

    def _from_link(self, msg: object, gen: int, source: str = "link") -> None:
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._on_link_msg, msg, gen, source)
            except RuntimeError:
                pass  # loop shutting down

    def _on_link_msg(self, msg: object, gen: int | None = None, source: str = "link") -> None:
        if gen is not None and gen != (self._iface_gen if source == "interface" else self._link_gen):
            return  # message from a replaced link or interface
        if isinstance(msg, StatusMsg):
            self.engine.on_status(msg)
        elif isinstance(msg, TimerMsg):
            self.engine.on_timer(msg)
        elif isinstance(msg, FirmwareProgress):
            self.fw.on_progress(msg)
        elif isinstance(msg, LinkState):
            prev = self.link_info["state"]
            self._cu_bots = (msg.bots_supported, msg.bot_transport)
            self.link_info.update(state=msg.state, version=msg.version, error=msg.error, bot_module=msg.bot_module)
            self._sync_bots_info()
            if msg.device:
                self.link_info["device"] = msg.device
            if self.interface is not None:
                self.interface.on_cu_link(msg.state)  # the CU lost: the race is blind, throttle 0 and hold
                msg = dataclasses.replace(msg, bots_supported=True, bot_transport="I")
            self.bots.on_link(msg)
            self._apply_mode()
            self.fw.on_link(msg)
            if msg.state != prev:
                self._link_log(msg)
            self._event("link", dict(self.link_info))
        elif isinstance(msg, InterfaceState):
            was = self.link_info["interface"]
            self.link_info.update(interface=msg.state, interface_error=msg.error, interface_info=msg.info)
            if msg.state != was:
                if msg.state == "lost":
                    self._log("warn", f"Controller interface lost: {msg.error} — bots held at 0, reconnecting…",
                              "interface_lost", error=msg.error)
                elif msg.state == "connected":
                    self._log("info", f"Controller interface connected ({msg.device})", "interface_connected",
                              device=msg.device)
                    if was == "lost":
                        self.bots.outage()  # docs/bot-protocol.md §1: throttle 0 before resuming
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
                   "B": "bots ready (bot firmware module)",
                   "I": "bots through the controller interface"}.get(
                msg.bot_transport, "bots unavailable on this CU")
            self._log("info", f"CU connected ({self.link_info['transport']}, v{msg.version}) — {how}",
                      "cu_connected." + (msg.bot_transport if msg.bot_transport in ("C", "B", "I") else "none"),
                      transport=self.link_info["transport"], version=msg.version)
            if self.preferred_mode == "advanced" and msg.bots_supported is False:
                self._log("warn", "Bots unavailable on this CU: normal mode", "bots_unavailable")
        elif msg.state == "lost":
            if msg.error:
                self._log("warn", f"CU link lost: {msg.error} — reconnecting…", "cu_lost_error", error=msg.error)
            else:
                self._log("warn", "CU link lost — reconnecting…", "cu_lost")
        elif msg.state == "updating":
            self._log("warn", "Firmware update: do not power off the CU or the AppConnect", "fw_updating")

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
            self._log("warn", "False start!", "false_start")
        elif name == "short_circuit":
            self._log("warn", f"Short circuit: the CU cut the track (phase {data['phase']})", "short_circuit",
                      phase=data["phase"])
        elif name == "finish" and data.get("position") == 1:
            self._log("info", f"Winner: {self._name(data['slot'])}", "winner",
                      name=self._name(data["slot"]), slot=data["slot"])
        elif name == "elimination":
            self._log("info", f"Eliminated: {self._name(data['slot'])}", "eliminated",
                      name=self._name(data["slot"]), slot=data["slot"])
        self._event(name, data)

    def _on_bot_event(self, name: str, data: dict[str, Any]) -> None:
        if name == "agent_lost" and data.get("reason") == "estop":
            self._log("warn", f"E-STOP: agent {data['agent']} must call bot.claim again (slot {data['slot'] + 1})",
                      "agent_estop", agent=data["agent"], car=data["slot"] + 1)
        elif name == "agent_lost":
            self._log("warn", f"Agent {data['agent']} disconnected: slot {data['slot'] + 1} at throttle 0",
                      "agent_lost", agent=data["agent"], car=data["slot"] + 1)
        elif name == "bots_reset":
            self._log("warn", "Reconnection: bot throttle reset to 0", "bots_reset")
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
                self.fw.tick(now)
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
        snap["firmware"] = self.fw.status()
        snap["firmware_build"] = self._fw_build_info()
        snap["hwtest"] = self.hwtest.snapshot()
        snap["hwtest_why"] = self.hwtest.why()
        snap["update"] = self.update_info
        if self.mode == "advanced":
            snap["bots"] = self.bots.snapshot(self.drive.bot_applied())
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
            "hwtest": self.hwtest.limits(),
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
        if self.fw.busy and op not in FIRMWARE_SAFE_OPS:
            raise OpError("firmware update in progress: wait until it finishes")
        if op.startswith("bot."):
            self.hwtest.protect(args)
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
            self._log("info", f"Connector \"{client}\" disconnected: card removed", "connector_gone", client=client)
            self._event("connectors", {"connectors": self.connectors()})
        if self.track_feed.clear(client):
            self._log("info", f"Track connector \"{client}\" disconnected: map removed", "track_connector_gone",
                      client=client)
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
        points = len(self.track_feed.geometry["points"])
        self._log("info", f"Track provided by \"{client}\" ({points} points)", "track_provided",
                  client=client, points=points)
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
                raise OpError(f"{d.name} (slot {slot + 1}) is disabled: enable it in Session → Drivers",
                              "slot_disabled", name=d.name, car=slot + 1)

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
        self._log("warn", "E-STOP: throttle 0 on all bots", "estop")

    def _op_estop_clear(self, args, client):
        self.bots.clear_estop()
        self._log("info", "E-STOP cleared", "estop_cleared")

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
            self._log("info", "Bots on", "bots_on")
        elif self.preferred_mode == "advanced" and self.link_info["bots_supported"] is False:
            # bots_supported is False: the reason is always "this CU cannot take software drivers"
            self._log("warn", f"Normal mode: {reason}", "normal_forced", version=self.link_info.get("version") or "?")
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
            self._log("info", "Normal mode", "normal_mode")
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
        settings = ", ".join(f"{k}={args[k]}" for k in done)
        self._log("info", f"Car {slot + 1} programmed: {settings}", "car_programmed", car=slot + 1, settings=settings)

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
        self._log("info", f"Live program: {field} {current} -> {level} ({steps} steps + START)", "live_program",
                  field=field, current=current, target=level, steps=steps)

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
        self._log("warn", f"Virtual CU: simulated link drop ({seconds:.0f} s)", "sim_drop", seconds=f"{seconds:.0f}")

    def _op_fw_images(self, args, client):
        return [i.info() for i in self.firmware_lib.images()]

    def _op_fw_status(self, args, client):
        return self.fw.status()

    # firmware (DFU): the update itself is cu/firmware.py FirmwareUpdate
    def _flash_cu(self, lines: list[bytes], recovery: bool) -> None:
        self.bots.release_all()  # the update takes the CU: the bots let go of it first
        self.link.firmware_update(lines, recovery=recovery)

    def _op_fw_flash(self, args, client):
        if str(args.get("confirm", "")).strip().upper() != FIRMWARE_CONFIRM:
            raise OpError(f"missing confirmation: type {FIRMWARE_CONFIRM}")
        if self.engine.state not in IDLE_STATES:
            raise OpError("end the session before updating the firmware")
        self.fw.start(str(args["image"]), recovery=bool(args.get("recovery", False)),
                      link_state=self.link_info["state"], version=self.link_info.get("version"))
        return self.fw.status()

    def _fw_build_info(self) -> dict[str, Any]:
        return {**self.fw_build, "available": can_build(), "missing": missing_images()}

    def _op_fw_build_status(self, args, client):
        return self._fw_build_info()

    def _op_fw_build(self, args, client):
        """Build the firmware images from source in the background (firmware/build.py)."""
        if self.fw.busy:
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
        if self.fw.state != "running":
            raise OpError("no update in progress")
        self.link.firmware_abort()

    def add_firmware(self, data: bytes, name: str) -> dict[str, Any]:
        try:
            image = self.firmware_lib.add(data, name)
        except ImageError as e:
            raise OpError(str(e)) from None
        self._log("info", f"Firmware image imported: {image.name} ({len(image.lines)} lines)", "fw_imported",
                  name=image.name, lines=len(image.lines))
        return image.info()

    # ------------------------------------------------------------ hardware test (hwtest.py)

    def _hw_preconditions(self, scope: str, slot: int | None) -> None:
        """What the hardware test bench needs of the app: refuses with the reason's code (the checks
        come in the order the System page words them; the scopes are in hwtest.py)."""
        info = self.link_info
        if info["state"] != "connected":
            raise OpError("CU not connected", "link")
        if scope == "link":
            return
        if scope != "run" and info["bots_supported"] is not True:
            raise OpError("this CU cannot take software drivers (bots)", "bots")
        if self.engine.state not in IDLE_STATES:
            raise OpError("a session is running: end it before a hardware test", "session")
        if scope == "run":
            return
        if self.mode != "advanced":
            raise OpError("switch to With bots first (top bar)", "mode")
        if self.bots.estopped:
            raise OpError("E-STOP engaged: clear it first", "estop")
        if scope == "handback" and info["bot_transport"] != "C":
            raise OpError("this test is about the C command: this CU is driven another way", "transport")
        if slot is not None:
            self._require_enabled(slot)

    async def _op_link_bench(self, args, client):
        result = await self.hwtest.latency(args)
        return {**result, "transport": self.link_info["transport"], "bot_transport": self.link_info["bot_transport"]}

    def _op_test_report(self, args, client):
        return self.hwtest.save_report(args.get("report"), {
            "racelab": __version__, "mode": self.mode,
            "link": {k: self.link_info.get(k) for k in ("state", "transport", "version", "bots_supported",
                                                         "bot_transport", "bot_module", "rtt_ms")},
            "safety": dataclasses.asdict(self.cfg.safety)})

    # connection and application
    async def _op_cu_connect(self, args, client):
        await self.set_connection(str(args.get("transport", "")), str(args.get("device") or ""),
                                  stock=bool(args.get("stock", False)))
        return dict(self.link_info)

    async def _op_cu_scan(self, args, client):
        timeout = min(15.0, max(2.0, float(args.get("timeout", 5.0))))
        return await asyncio.get_running_loop().run_in_executor(None, scan_devices, timeout)

    async def _op_interface_scan(self, args, client):
        try:
            ports = await asyncio.get_running_loop().run_in_executor(None, interface_ports)
        except Exception as e:  # pyserial missing or system error
            raise OpError(f"serial ports: {e}") from None
        return {"ports": ports, "current": self.cfg.interface.port or None,
                "pairing": bluetooth.blueutil() is not None}

    async def _op_interface_bt_scan(self, args, client):
        try:
            devices = await asyncio.get_running_loop().run_in_executor(None, bluetooth.devices)
        except Exception as e:  # blueutil missing, Bluetooth off or not allowed
            raise OpError(f"Bluetooth search: {e}") from None
        return {"devices": devices}

    async def _op_interface_pair(self, args, client):
        """Pair a classic Bluetooth serial module (HC-05) from RaceLab, then use its port."""
        address, name = args.get("address"), args.get("name") or ""
        if not isinstance(address, str) or not 11 <= len(address) <= 17 or not isinstance(name, str):
            raise OpError("address: a Bluetooth address is expected")
        self._check_relink("changing the controller interface")
        try:
            port = await asyncio.get_running_loop().run_in_executor(None, bluetooth.pair, address, name[:60])
        except Exception as e:
            raise OpError(f"pairing {name or address}: {e}") from None
        await self.set_interface(port)  # logs the new port
        return dict(self.link_info)

    async def _op_interface_set(self, args, client):
        port = args.get("port") or ""
        if not isinstance(port, str) or len(port) > 200:
            raise OpError("port: a serial port path is expected")
        await self.set_interface(port)
        return dict(self.link_info)

    async def check_updates(self) -> None:
        """From launch (__main__), then every UPDATE_EVERY_S: a newer RaceLab is announced once in the
        log, and shown on every page (snapshot `update`) until installed. Silent when it cannot tell."""
        announced = None
        while True:
            try:
                self.update_info = await asyncio.get_running_loop().run_in_executor(None, update.check)
            except UPDATE_ERRORS:
                pass
            else:
                if self.update_info["available"] and self.update_info["latest"] != announced:
                    announced = self.update_info["latest"]
                    self._log("info", "A new RaceLab version is available: System → Updates", "update_available")
            await asyncio.sleep(UPDATE_EVERY_S)

    async def _op_update_check(self, args, client):
        try:
            self.update_info = await self.loop.run_in_executor(None, update.check)
        except UPDATE_ERRORS as e:
            raise OpError(f"cannot check for updates: {e}") from None
        return self.update_info

    async def _op_update_apply(self, args, client):
        if self.request_quit is None:
            raise OpError("updating from the interface is unavailable")
        if self.engine.state not in IDLE_STATES:
            raise OpError("end the session before updating")
        if self.fw.busy:
            raise OpError("firmware update in progress")
        self._log("info", "Updating RaceLab…", "update_start")
        self.bots.release_all()  # the CU is let go first, cleanly, not after minutes of download
        await self._stop_interface()
        await self.loop.run_in_executor(None, self.link.stop)
        try:
            sha = await self.loop.run_in_executor(None, update.apply)
        except UPDATE_ERRORS as e:
            await self._relink(lambda: None)  # still the old version: back on the CU
            raise OpError(f"update failed: {e}") from None
        self._log("info", f"Updated to {sha[:7]}: restarting", "update_done", version=sha[:7])
        self.restart = True
        self.loop.call_later(0.5, self.request_quit)  # time for the reply to go out
        return {"version": sha}

    def _op_quit(self, args, client):
        if self.request_quit is None:
            raise OpError("quitting from the interface is unavailable")
        if self.fw.busy:
            raise OpError("firmware update in progress: cannot quit now")
        self._log("info", "Quit requested from the interface", "quit")
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
        "interface.scan": _op_interface_scan,
        "interface.set": _op_interface_set,
        "interface.bt_scan": _op_interface_bt_scan,
        "interface.pair": _op_interface_pair,
        "app.quit": _op_quit,
        "update.check": _op_update_check,
        "update.apply": _op_update_apply,
        "firmware.images": _op_fw_images,
        "firmware.status": _op_fw_status,
        "firmware.flash": _op_fw_flash,
        "firmware.abort": _op_fw_abort,
        "firmware.build": _op_fw_build,
        "firmware.build_status": _op_fw_build_status,
        "link.bench": _op_link_bench,
        "test.pulse": lambda self, a, c: self.hwtest.pulse(a),
        "test.lane": lambda self, a, c: self.hwtest.lane(a),
        "test.handback": lambda self, a, c: self.hwtest.handback(a),
        "test.report": _op_test_report,
    }


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
