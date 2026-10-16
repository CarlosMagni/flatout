"""RaceLab configuration.

Three layers, the last one wins:
1. the defaults in this module;
2. the TOML file (`--config`, else `./racelab.toml`, else
   `~/.racelab/racelab.toml`), see `racelab.example.toml`;
3. the settings changed from the UI (`<data_dir>/settings.json`: drivers,
   session settings, bot profiles).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_COLORS = [
    "#e5383b",  # red
    "#2f80ed",  # blue
    "#f2c94c",  # yellow
    "#27ae60",  # green
    "#f2994a",  # orange
    "#e0e0e0",  # white
    "#9b51e0",  # ghost
    "#828282",  # pace car
]
DEFAULT_NAMES = [f"Driver {i}" for i in range(1, 7)] + ["Ghost", "Pace car"]


@dataclass
class AppConfig:
    mode: str = "advanced"  # "classic" | "advanced"
    host: str = "127.0.0.1"
    port: int = 8130
    open_browser: bool = True
    data_dir: str = "~/.racelab"
    log_level: str = "info"


@dataclass
class ConnectionConfig:
    transport: str = "none"  # "none" (chosen in the UI) | "ble" | "serial" | "sim" | "auto"
    device: str = ""  # BLE address / macOS UUID, or serial port; empty = BLE scan
    timeout: float = 1.0  # max wait for a CU reply (s)
    connect_timeout: float = 20.0
    reconnect_delay: float = 1.5
    max_timeouts: int = 3  # missed replies in a row before declaring the link lost
    # how software drivers reach the cars: "auto" = the stock C table on a V339 CU
    # (no custom firmware needed), else the B command of the bot module; "C" / "B" force one
    bot_transport: str = "auto"


@dataclass
class SafetyConfig:
    heartbeat_ms: int = 500  # resend of the active bots' throttle (docs/bot-protocol.md §1)
    min_bot_interval_ms: int = 70  # ≥ 1 rail cycle (75 ms): changes are coalesced
    deadman_ms: int = 600  # API agent silent for longer -> throttle 0
    release_on_exit: bool = True  # on exit: throttle 0 then hand control back (A)
    release_all_on_connect: bool = True  # first connection: A on 0-5 (clean state)
    confirm_throttle_above: int = 12  # the UI asks for confirmation above this
    max_throttle: int = 15  # global bot ceiling


@dataclass
class RaceConfig:
    min_lap_ms: int = 2000  # debounce: closer crossings are ignored
    grid_behind_line: bool = True  # first crossing after the start = lap 0
    auto_countdown: bool = True  # automatic 2nd Start press if the CU stays armed
    finish_timeout_s: float = 30.0  # max wait for stragglers after the flag
    fuel_low: int = 3  # low-fuel alert threshold (0..15)
    session: dict[str, Any] = field(default_factory=dict)  # last session settings


@dataclass
class TowerConfig:
    enabled: bool = True
    min_interval_ms: int = 150  # ~150 ms per programming operation (docs/protocol.md §6)


@dataclass
class ApiConfig:
    enabled: bool = True
    token: str = ""  # if not empty: required (Authorization: Bearer header / ?token=)
    state_hz: float = 10.0  # broadcast rate of the full state
    # Host header names accepted besides IP literals and "localhost" (DNS-rebinding guard)
    allowed_hosts: list[str] = field(default_factory=list)
    # cross-origin pages allowed to use the API/WebSocket, e.g. "http://dash.example:3000"
    allowed_origins: list[str] = field(default_factory=list)


@dataclass
class SimConfig:
    bot_firmware: bool = True  # False = simulate the stock firmware (no B command)
    firmware: str = "339"  # firmware version the virtual CU reports (C table from 338 on)
    humans: list[int] = field(default_factory=lambda: [1, 2])  # slots held by simulated "humans"
    latency_ms: float = 12.0  # simulated BLE round trip
    jitter_ms: float = 6.0
    time_scale: float = 1.0
    seed: int | None = None
    fuel_mode: bool = True
    pit_lane: bool = True
    duplicate_rate: float = 0.02  # the real CU sometimes repeats a timing event
    track_seed: int | None = None  # generated track (sim/track.track_for); None = the default track


@dataclass
class DriverConfig:
    slot: int
    name: str
    color: str
    enabled: bool = True
    car: str = ""  # car model, free text


def default_drivers() -> list[DriverConfig]:
    return [
        DriverConfig(slot=i, name=DEFAULT_NAMES[i], color=DEFAULT_COLORS[i], enabled=i < 6)
        for i in range(8)
    ]


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    race: RaceConfig = field(default_factory=RaceConfig)
    tower: TowerConfig = field(default_factory=TowerConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    drivers: list[DriverConfig] = field(default_factory=default_drivers)
    bots: dict[str, Any] = field(default_factory=dict)  # profiles per slot ("0".."5")
    source: str | None = None  # loaded TOML file
    # last CU chosen in the UI ({"transport", "device", "stock"}): only this
    # value is saved, never a command-line option
    connection_choice: dict[str, Any] | None = None
    mode_choice: str | None = None  # last mode chosen in the UI (same: never the command line)

    @property
    def data_path(self) -> Path:
        return Path(os.path.expanduser(self.app.data_dir))

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("source", None)
        d.pop("connection_choice", None)
        d.pop("mode_choice", None)
        return d


class ConfigError(ValueError):
    pass


def _merge_section(obj: Any, values: dict[str, Any], where: str) -> None:
    names = {f.name: f for f in dataclasses.fields(obj)}
    for key, value in values.items():
        if key not in names:
            raise ConfigError(f"unknown key [{where}] {key}")
        current = getattr(obj, key)
        if isinstance(current, bool) and not isinstance(value, bool):
            raise ConfigError(f"[{where}] {key} must be true/false")
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"[{where}] {key} must be a number")
            value = type(current)(value) if isinstance(current, float) else value
        setattr(obj, key, value)


def _merge(cfg: Config, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key == "drivers":
            by_slot = {d.slot: d for d in cfg.drivers}
            for item in value:
                slot = int(item.get("slot", -1))
                if slot not in by_slot:
                    raise ConfigError(f"[[drivers]] invalid slot: {slot}")
                _merge_section(by_slot[slot], item, f"drivers.{slot}")
        elif key == "bots":
            cfg.bots.update({str(k): v for k, v in value.items()})
        elif key == "connection_choice":
            if value:
                apply_connection_choice(cfg, value)
        elif key == "mode_choice":
            if value not in ("classic", "advanced"):
                raise ConfigError(f"invalid saved mode: {value!r}")
            cfg.app.mode = cfg.mode_choice = value
        elif hasattr(cfg, key) and dataclasses.is_dataclass(getattr(cfg, key)):
            _merge_section(getattr(cfg, key), value, key)
        else:
            raise ConfigError(f"unknown section [{key}]")


def validate(cfg: Config) -> None:
    if cfg.app.mode not in ("classic", "advanced"):
        raise ConfigError("[app] mode must be 'classic' or 'advanced'")
    if cfg.connection.transport not in ("none", "auto", "ble", "serial", "sim"):
        raise ConfigError("[connection] transport: none | auto | ble | serial | sim")
    if cfg.connection.bot_transport not in ("auto", "C", "B"):
        raise ConfigError("[connection] bot_transport: auto | C | B")
    if not 0 <= cfg.safety.max_throttle <= 15:
        raise ConfigError("[safety] max_throttle must be between 0 and 15")
    if cfg.safety.heartbeat_ms < 100:
        raise ConfigError("[safety] heartbeat_ms too low (min 100)")
    if cfg.safety.min_bot_interval_ms < 20:
        raise ConfigError("[safety] min_bot_interval_ms too low (min 20)")
    for key in ("allowed_hosts", "allowed_origins"):
        value = getattr(cfg.api, key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"[api] {key} must be a list of strings")
    for slot in cfg.sim.humans:
        if slot not in range(6):
            raise ConfigError("[sim] humans: slots 0..5")


def apply_connection_choice(cfg: Config, choice: dict[str, Any]) -> None:
    transport = choice.get("transport")
    if transport not in ("none", "ble", "serial", "sim"):
        raise ConfigError(f"invalid saved CU: {transport!r}")
    cfg.connection.transport = transport
    cfg.connection.device = str(choice.get("device") or "")
    if transport == "sim":
        cfg.sim.bot_firmware = not choice.get("stock", False)
    cfg.connection_choice = {"transport": transport, "device": cfg.connection.device,
                             "stock": bool(choice.get("stock", False))}


def find_config_file(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(os.path.expanduser(explicit))
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p
    for p in (Path("racelab.toml"), Path("~/.racelab/racelab.toml").expanduser()):
        if p.is_file():
            return p
    return None


def load_config(path: str | None = None, *, overrides: bool = True,
                data_dir: str | None = None) -> Config:
    cfg = Config()
    file = find_config_file(path)
    if file is not None:
        with open(file, "rb") as f:
            _merge(cfg, tomllib.load(f))
        cfg.source = str(file)
    if data_dir:
        cfg.app.data_dir = data_dir  # before reading settings.json, which lives there
    if overrides:
        settings = cfg.data_path / "settings.json"
        if settings.is_file():
            try:
                _merge(cfg, json.loads(settings.read_text(encoding="utf-8")))
            except (ValueError, ConfigError) as e:
                logger.warning("settings.json ignored (%s)", e)
    validate(cfg)
    return cfg


def save_overrides(cfg: Config) -> Path:
    """Persist what the UI can change (drivers, session, bots)."""
    data = {
        "drivers": [dataclasses.asdict(d) for d in cfg.drivers],
        "race": {"session": cfg.race.session},
        "bots": cfg.bots,
    }
    if cfg.connection_choice:
        data["connection_choice"] = cfg.connection_choice
    if cfg.mode_choice:
        data["mode_choice"] = cfg.mode_choice
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    path = cfg.data_path / "settings.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path
