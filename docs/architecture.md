# RaceLab — architecture and contracts

This document is the contract between the modules. Any change to an
interface described here is made here first.

## Overview

```
             I/O thread                                  asyncio loop (20 Hz)
┌──────────────────────────────┐   events    ┌──────────────────────────────────────────┐
│ CULink (cu/link.py)          │ ──────────► │ RaceLabApp (app.py)                      │
│  └ RaceControlUnit (cu/unit) │             │  ├ RaceEngine (race/engine.py)           │
│     └ carreralib Connection  │ ◄────────── │  │   └ TowerManager (race/tower.py)      │
│        ├ BLEConnection       │  commands   │  ├ BotManager (bots/manager.py)          │
│        ├ SerialConnection    │ thread-safe │  ├ SessionRecorder (race/recorder.py)    │
│        └ SimConnection ──────┼──┐          │  └ ApiServer (api/server.py) ── WS/REST  │
└──────────────────────────────┘  │          └──────────────────────────────────────────┘
                                  │                         ▲            ▲
                     ┌────────────▼──────────┐              │            │
                     │ Simulator (sim/)      │          web UI (web/)  RL agents
                     │ virtual CU + track    │                         (client/)
                     └───────────────────────┘
```

- **A single thread talks to the CU** (`CULink`). It owns the carreralib
  connection, polls with `?`, sends queued commands with priorities, and handles
  reconnection, firmware probing, heartbeat and the bot watchdog.
- **All the logic lives in the asyncio loop**: race engine, bots, API. It
  receives the messages of `racelab/events.py` through
  `loop.call_soon_threadsafe`.
- **The simulator is a real `carreralib.connection.Connection`**: with
  `--sim`, the whole path (carreralib → link → engine → API → UI) is exercised
  exactly as with a real CU.

## App modes

The UI calls them **Normal** and **With bots**.

| | Classic ("Normal") | Advanced ("With bots") |
|---|---|---|
| CU | any | one that takes software drivers: stock V338/V339 (`C`) or the bot firmware module (`B`, probed at startup) |
| Sessions | practice, qualifying, race, endurance, elimination | + rival, training |
| Software drivers (`C` / `B`) | never sent (except the probe / reset) | yes, 6 slots |
| Agent API (RL) | read-only | read + drive |
| Raw console | no | yes (letters G/E/F/C/B are refused; the bots have their own commands) |

The link picks the transport (`[connection] bot_transport`, default `auto`): `C` when
the CU's version is `?338` or `?339`, otherwise `B` if the module answers, otherwise none.
It falls back the same way if the CU rejects `C`. Both feed the same per-slot model
(`set_bot` / lane / release), so nothing above the link knows which one is used.

A **controller interface** (`racelab/drive/`, [controller-interface.md](controller-interface.md))
takes the bots instead when one is set (`[interface] port`, next to a real CU): hardware on a
controller socket that plays the controllers, for CUs with neither `C` nor `B`. The bot
manager talks to a proxy, `app.drive`, which is the interface when there is one and the CU
link otherwise; both answer the same calls (`set_bot`, `lane`, `estop`, `clear_estop`,
`resume_bots`). `ControllerInterface` holds the bot model and the safety rules (E-STOP
first, throttle 0 and a hold on a lost interface or CU link, the release frame on exit) and
runs its own thread, never waiting on the CU's polls. A subclass only implements `open`,
`send`, `poll` and `close`: `SerialInterface` speaks the documented wire protocol over any
serial port, with the device's report lines as diagnostics and heartbeat.

Hardware test bench (System tab, ops `link.bench` and `test.*`, see [api.md](api.md)): `cu/bench.py`
holds the latency statistics; `CULink.bench()` samples the poll round trips the link makes anyway
(plus the `C` acknowledgement), and `CULink.pause_heartbeat()` / `resume_heartbeat()` hold the idle
`C` heartbeat for the hand-back test (a deadline in the link thread, 3 s ceiling, cleared by E-STOP,
link loss and expiry). The app owns the safety: a test takes a slot as a manual bot and always zeroes
and releases it in a `finally`. Because these ops wait, the WebSocket answers `link.bench` and
`test.*` in a background task (other ops keep their strict order), so STOP is never queued behind them;
a closed client cancels its tests.

## 1. Link messages (`racelab/events.py`)

`StatusMsg`, `TimerMsg`, `LinkState`, `LinkStats`, `BotSent`, `RawFrame`
(see the file). Addresses are 0..7 (carreralib already removes the +1 of the
wire format). `host_t` = `time.monotonic()` on reception.

Start phases (`lights_for_phase`): 0 race/idle, 1 armed (5 red lights),
2..6 countdown (1..5 lights), 7 GO (lights out), 8..9 false start.

## 2. CU actions requested by the logic (`CUActions`)

The engine and the tower never talk to the link directly: they receive an
object implementing this protocol (the app wires it to `CULink`, the tests to
a recorder).

```python
class CUActions(Protocol):
    def press(self, button: int) -> None: ...        # T<n> (events.BUTTON_*)
    def reset_timer(self) -> None: ...               # =10
    def ignore(self, mask: int) -> None: ...         # :<mask> (bit = address excluded from timing)
    def clrpos(self) -> None: ...                    # position tower: clear
    def setpos(self, address: int, position: int) -> None: ...  # J word 6
    def setlap(self, lap: int) -> None: ...          # J words 17/18 (0..255)
```

All these methods are non-blocking (they enqueue).

## 3. Simulator (`racelab/sim/`)

```python
class Simulator:                                  # the virtual CU + the track, persistent
    def __init__(self, cfg: racelab.config.SimConfig, *, realtime: bool = True): ...
    def connect(self) -> SimConnection: ...       # raises carreralib.connection.ConnectionError if the link is cut
    def close(self) -> None: ...                  # stops the physics thread
    def step(self, dt: float) -> None: ...        # advance the physics (used with realtime=False by the tests)
    def drop_link(self, seconds: float) -> None:  # cut the active connection + refuse connect() for `seconds`
    def snapshot(self) -> dict: ...               # ground truth (see below)
    def track_geometry(self) -> dict: ...         # track drawing for the UI
    # "firmware" state that tests can inspect
    bot_mask: int; bot_gas: list[int]; lane_pending: int

class SimConnection(carreralib.connection.Connection):
    send(buf, offset=0, size=None) -> None
    recv(maxlength=None) -> bytes                 # same normalisation as SerialConnection.recv
    close() -> None
```

Replies (identical to what `SerialConnection.recv` returns, hence without `$`):

| Request | Reply |
|---|---|
| `0` | `b"05339"` + checksum (carreralib format `x4sC`) |
| `?` | a pending timer event `b"?" + Y(adr+1) + I(ts) + Y(sector) + C`, otherwise the status `b"?:" + 8Y fuel + Y phase + Y mode + B pitmask + Y display + C` |
| `J…` `T…` `=…` `:…` | echo of the letter (`b"J"`…) |
| `C` + 12 chars + checksum | firmware 338/339 (`[sim] firmware`): echo `b"C"` + the host throttle table of [bot-protocol.md](bot-protocol.md) (expires after 750 ms); other versions: `b""` |
| `B<adr><v>` | bot firmware: echo `b"B"` + the semantics of [bot-protocol.md](bot-protocol.md) (watchdog 1 s); stock firmware: `b""` (the CU answers `#`) |
| anything else | `b""` |

Frames must be parsed without error by `carreralib.ControlUnit.poll()` and
`.version()` (this is tested).

Physics: closed track (spline), 2 lanes of different lengths, lane
switches, pit lane, critical speed in corners (`v² · curvature > a_max` →
off the track, the car restarts after 2-4 s), fuel, J settings (speed,
brake, tank) remembered per car. Start sequence: `T2` at rest → phase 1;
`T2` in phase 1 → 2..6 (≈1 s each) → 7 (after a random delay) → 0. Throttle
> 0 during 2..6 → false start (phase 8 for 3 s, then 1). `T2` during the race
→ phase 1 (cars stopped). In phases 1..6 the cars do not move.

A car's throttle = `bot_gas[adr]` if the `bot_mask` bit is set (bot
firmware), otherwise the controller: a simulated "human" (`cfg.humans`) or 0.

`snapshot()`:
```json
{"t": 12.34, "phase": 0,
 "cars": [{"slot": 0, "s": 0.42, "lane": 0, "v": 2.1, "throttle": 7,
           "off": false, "pit": false, "fuel": 13.2, "controller": "bot"}]}
```
`s` = fraction of a lap, 0..1. `controller` ∈ `bot` | `human` | `none`.

`track_geometry()`:
```json
{"length_m": [18.2, 18.9], "points": [[x, y], ...], "lane_offset": 0.012,
 "changers": [0.18, 0.61], "pit": [0.80, 0.97], "finish": 0.0}
```
`points` = centre line normalised into [0,1]², ~200 points, in the driving direction.

## 4. Race engine (`racelab/race/`)

```python
class RaceEngine:
    def __init__(self, drivers: list[DriverConfig], race_cfg: RaceConfig,
                 actions: CUActions, *, tower: TowerManager | None = None,
                 on_event: Callable[[str, dict], None] = lambda n, d: None,
                 clock: Callable[[], float] = time.monotonic): ...
    # inputs
    def on_status(self, msg: StatusMsg) -> None
    def on_timer(self, msg: TimerMsg) -> None
    def tick(self, now: float | None = None) -> None          # 20 Hz
    # commands (ValueError if not allowed in the current state)
    def configure(self, settings: SessionSettings) -> None    # new session -> "grid"
    def start(self) -> None
    def pause(self) -> None
    def resume(self) -> None
    def stop(self) -> None                                    # -> "finished"
    def set_drivers(self, drivers: list[DriverConfig]) -> None
    # reads
    def snapshot(self) -> dict
    def laps(self, slot: int | None = None) -> list[dict]
    def results(self) -> dict
    state: str ; settings: SessionSettings
```

### Session types (`race/modes.py`)

`SessionSettings` (dataclass, serialisable): `type`, `laps`, `duration_s`,
`elim_every`, `episode_laps`, `rival_slot`, `rival_margin_pct`, `fuel`,
`title`. `SESSION_TYPES`: metadata for the UI (label, description,
`advanced_only`, `uses_lights`, relevant parameters).

| type | UI name | lights | end | ranking |
|---|---|---|---|---|
| practice | Practice | no | optional `duration_s` | best lap |
| qualifying | Qualifying | no | `duration_s`, then the lap in progress | best lap |
| race | Race | yes | first to `laps`, then everyone at their next crossing | laps, then time |
| endurance | Endurance | yes | `duration_s`, then everyone at their next crossing | laps, then time |
| elimination | Elimination | yes | every `elim_every` laps of the leader, the last one is out; last car on track wins | laps, then time |
| rival (Advanced) | Beat the bot | yes | like race | laps, then time |
| training (Advanced) | AI training | no | endless, episodes of `episode_laps` laps per slot | best lap |

### States

`idle` → `configure()` → `grid` → `start()` → `starting` (lights) → GO → `running`
→ (`finishing`) → `finished`. `paused` from `running`/`finishing`.

- Session with lights: `start()` presses Start if the phase is 0; if the phase
  stays at 1 for more than 0.8 s and `auto_countdown` is set, it presses again
  (at most every 2 s). GO = transition from 2..6 to 7 or 0. Phase 8/9 → a
  `false_start` event, back to `grid`.
- Session without lights: `start()` → `running` immediately (if the CU is in
  phase 1, press Start to release the cars).
- The CU is the authority: in `running`/`finishing`, a phase 1..6 → `paused`
  (someone pressed Start on the CU, or `pause()`), then GO → resume.
  `pause()` = Start press; `resume()` = Start press.
- Elapsed time = time outside pauses since GO.

### Laps

- Only sector 1 counts a lap; sectors 2-3 = intermediate times.
- Duplicate filtering: the same (address, timestamp) is ignored; a crossing
  within `min_lap_ms` of the previous one is ignored.
- `grid_behind_line`: the first crossing after GO (or after `start()` without
  lights) is the reference (lap 0), not a lap.
- Lap time = difference of CU timestamps, modulo 2³².
- A lap that covers a pause = `neutralized`: counted, but excluded from best laps.
- Crossings are counted in `running`, `finishing` and `paused`
  (neutralized); ignored in `idle`/`grid`/`starting`/`finished`.
- A car with address 6/7 (ghost/pace) appears at its first crossing.

### Events emitted (`on_event(name, data)`)

| name | data |
|---|---|
| `session` | `{state, prev, type}` |
| `phase` | `{phase, lights, blink}` |
| `false_start` | `{}` |
| `crossing` | `{slot, sector, cu_ts}` |
| `lap` | `{slot, lap, ms, neutralized, personal_best, session_best, position}` |
| `sector` | `{slot, sector, ms}` |
| `positions` | `{order: [slot, ...]}` |
| `finish` | `{slot, position}` |
| `elimination` | `{slot, lap}` |
| `pit_in` / `pit_out` | `{slot, pits}` |
| `fuel_low` | `{slot, fuel}` |
| `episode_end` | `{slot, episode, laps, best_ms, mean_ms, laps_ms}` |

### `snapshot()`

```json
{
  "session": {"id": "20261010-171200-race", "type": "race", "title": "Race · 20 laps",
              "state": "running", "laps": 20, "duration_s": null,
              "elapsed_s": 83.2, "remaining_s": null, "leader_lap": 7,
              "best": {"slot": 2, "ms": 5123, "lap": 4},
              "flag": "green", "false_start": false},
  "cu": {"phase": 0, "lights": 0, "blink": false, "fuel_mode": true,
         "real_mode": false, "pit_lane": true, "lap_counter": false, "display": 6},
  "cars": [
    {"slot": 0, "name": "Carlos", "color": "#e5383b", "enabled": true,
     "position": 1, "laps": 7, "last_ms": 5321, "best_ms": 5123, "best_lap": 4,
     "gap_ms": null, "gap_laps": 0, "interval_ms": null, "interval_laps": 0,
     "since_cross_ms": 1830, "sectors": [null, null, null],
     "last_pb": true, "last_sb": false, "recent": [5400, 5390, 5321],
     "fuel": 12, "pit": false, "pits": 1,
     "finished": false, "eliminated": false, "episode": null}
  ]
}
```
`flag`: `green` | `red` (pause) | `checkered` (finishing/finished) | `none`.
`gap_*` to the leader, `interval_*` to the car ahead (ms if on the same lap,
otherwise laps).

### Position tower (`race/tower.py`)

```python
class TowerManager:
    def __init__(self, actions: CUActions, *, enabled=True, min_interval_s=0.15): ...
    def clear(self) -> None
    def update(self, order: list[int], leader_laps: int) -> None   # desired state
    def tick(self, now: float) -> None                            # at most 1 operation per interval
```
Coalescing queue: only the most recent desired state counts; priority goes to
the lap counter, then P1, P2…; only what changed is sent.

### Recording (`race/recorder.py`)

```python
class SessionRecorder:
    def __init__(self, directory: Path): ...
    def begin(self, meta: dict) -> None
    def record(self, name: str, data: dict, t: float) -> None
    def end(self, results: dict) -> Path | None
    def list(self) -> list[dict]          # {id, type, title, started, duration_s, winner, path}
    def load(self, session_id: str) -> dict
```
One JSON file per session in `<data_dir>/sessions/`, written at `end()`
(and every 30 s during the session, with atomic writes).

## 5. Bots (`racelab/bots/`)

`BotManager`: 6 controllers (slots 0..5). A slot is either human (no
`B`), or a bot with a driver: `manual` (UI slider; Manual), `profile` (local
autopilot; Auto), `api` (external agent; AI). Safety:

- effective throttle = 0 on E-STOP, when the session is not running (`gated`),
  when the car has finished or is eliminated, when an API agent has been silent
  for more than `deadman_ms`, when the link is lost;
- `max_throttle` ceiling;
- a profile that raises an exception is stopped (throttle 0), never left at its last value;
- `tick(snapshot, positions, geometry)`: profiles get their own car's `positions` (None when
  missing or older than 500 ms) and the track shape in `Context`, and must work without them
  (the `corners` profile falls back to the target pace);
- `Context` also carries the race state the race brain uses (the other profiles ignore it):
  `fuel` (own `cars[].fuel`), `in_pit` (`cars[].pit`), `fuel_mode` / `pit_lane` (`cu`),
  `session_laps` (the lap count of a lap-target session; once it is `finishing`, the car's current
  lap), `remaining_s` (timed session) and `others` (the other cars' fresh positions);
- the race brain (`bots/brain.py`, profile kind `brain`, see [api.md](api.md#race-brain-profile-brain))
  wraps a pilot from its `PILOTS` registry and adds a lane planner, a pit planner and an arbiter
  behind the gates above. It asks for a lane change through the same path as the other profiles
  (`take_lane_request()` → `link.lane`), only while its car has a non-zero throttle; `brain_request()`
  (op `bot.brain`) is its one-shot test entry. `bots/__init__.py` imports it, which registers the kind;
- on reconnection: throttle 0 on every bot slot (the link does it before any
  other command), then the drivers resume;
- on shutdown: throttle 0 then `A` (if `release_on_exit`).

## 6. API ([`api.md`](api.md))

WebSocket `/ws` + REST `/api/*`, JSON. The web UI and the RL agents use the
same protocol.
