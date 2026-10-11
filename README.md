# RaceLab

*(This repository is `flatout`; the app it contains is RaceLab.)*

Race management and software drivers for **Carrera Digital 124/132** slot-car
tracks, from a laptop. RaceLab talks to the Control Unit (CU) through the
Carrera AppConnect Bluetooth adapter (item 30369) or a serial cable, runs your
races, and — with a custom CU firmware — lets software drive the cars: a fader in the UI,
a local autopilot, or your own agent (reinforcement learning) through an API.

> **Status: early but playable.** Everything runs against the built-in
> virtual CU; real-hardware validation is in progress
> (see [Status and roadmap](#status-and-roadmap)).

## Features

- **Race control** — CU start lights, live timing, practice, qualifying, race,
  endurance, elimination; fuel and pit stops, position tower, session history
  with lap charts, a **big-screen view** readable from across the room.
- **Software drivers** (custom firmware required, see below) — on any of the 6
  controller slots: manual fader, autopilots (constant throttle, target lap
  time with a learned throttle ceiling, throttle map, rival), or an external
  agent. Safety first: throttle forced to 0 during start lights, dead-man for
  agents, emergency stop, zero throttle on reconnection and on exit.
- **API for agents** — JSON over WebSocket and REST, a Python client and a
  [Gymnasium](https://gymnasium.farama.org/)-style environment (`racelab.client`).
- **Track connectors** — any program (e.g. an overhead camera) can publish the
  track shape and car positions; the map and the agents read them in the same
  format as the simulator.
- **Virtual CU** — a simulated track, cars and firmware: try everything, write
  and test agents, without hardware.
- **CU firmware flashing** from the app — the images are built from source
  (stock and bot firmware, identified by SHA-256), checked before sending,
  rollback to stock, recovery mode. See
  [Flash a custom CU firmware](#flash-a-custom-cu-firmware-from-the-app) below.
- **English and French** — the interface is in English, with a French option.
- **Cross-platform** — macOS, Windows, Linux; Python ≥ 3.11.

## Status and roadmap

The long-term goal: **AI drivers on a real track** — your own agent, trained
against the simulator, driving a real slot car.

- **Works today** (virtual CU): the full race manager, software drivers with a
  custom firmware, an HTTP/WebSocket API, a Python client and a
  [Gymnasium](https://gymnasium.farama.org/)-style environment — the connectors
  to plug in and train your own agent are already in place.
- **In progress**: the last stretch of real-hardware validation (AppConnect
  Bluetooth, firmware bench tests), more RL training helpers on top of the API
  (training scenarios, reward shaping), a camera track connector for real car
  positions, and pit stops for AI drivers.

## Flash a custom CU firmware from the app

**Software drivers are enabled by the firmware, not the app**: flash a CU
firmware that understands the extra `B` command, and any controller slot can
be driven by the app, an autopilot or your own agent. RaceLab does the update
itself — panel **System → CU firmware** — over Bluetooth or serial, with
confirmation, progress, recovery mode, and the image's SHA-256 shown before
you flash (always check the hash, not the file name).

| Image to flash | What it does |
|---|---|
| the **stock firmware** image | a stock CU again — the safe rollback, and the no-risk way to rehearse the update |
| a **custom firmware** image | unlocks software drivers (bots) on all 6 slots — and otherwise behaves exactly as before: the add-on stays inactive until an app sends the bot command |

- Test the whole flow without risk against the virtual CU:
  `racelab --sim --stock-firmware`.
- On real hardware: bench-test with the car lifted first (see
  [Safety](#safety), and the [user guide](docs/user-guide.md#updating-the-cu-firmware)).
- The images are built from source (`python3 firmware/build.py`, or **Build the images**
  in the app; needs Docker): the stock firmware and the one with software drivers, listed
  with their tag and SHA-256 in [`firmware/manifest.json`](firmware/README.md). The app
  identifies images by SHA-256 only and refuses known-defective builds.

## Install

| System | How |
|---|---|
| macOS | double-click `Install RaceLab.command`, then open `RaceLab.app` |
| Windows | double-click `Install RaceLab.bat`, then the **RaceLab** shortcut (Desktop / Start menu) |
| Linux | `python3 install.py`, then **RaceLab** in your applications menu |

The installer creates a virtual environment in `.venv`, installs RaceLab and a
launcher. It needs Python 3.11+ and an Internet connection, never admin
rights. On first launch, pick your CU — or **Virtual CU** to try without
hardware. Step-by-step guide: [`docs/getting-started.md`](docs/getting-started.md)
([en français](docs/fr/getting-started.md)).

From a terminal:

```bash
python3 -m venv .venv
.venv/bin/pip install -e carreralib/ -e "racelab[dev]"     # Windows: .venv\Scripts\pip
.venv/bin/racelab --sim                                     # Windows: .venv\Scripts\racelab --sim
                                                            # virtual CU, opens http://localhost:8130
```

## Firmware

Software drivers need a CU firmware that understands an extra serial command,
`B` (throttle, lane change, hand back to the controller) — specified in
[`docs/bot-protocol.md`](docs/bot-protocol.md). Without it, RaceLab works in
Classic mode (races with physical controllers); Advanced mode is only enabled
when the CU responds to the `B` command. Everything firmware lives in
[`firmware/`](firmware/README.md), as two projects:

| | **Bot module** — `firmware/bot/` | **V339 in C** — `firmware/reconstruction/` |
|---|---|---|
| What | patches the V339 image: 3 `jmp` hooks + our module (C + a few asm stubs) in free flash | rewrites V339 in C, function by function, until each compiles to the original bytes |
| Output | `v339-bot.N.hmf`, flashed from the app | `v339-stock.hmf`: the V339 image rebuilt from the C, byte-identical to the original |
| Compiler | avr-gcc 5.4.0 (pinned, Docker) | avr-gcc 5.4.0 (pinned, Docker) |
| Command | `python3 firmware/build.py` (builds both) | same |
| Status | done, rebuilt and checked in CI | complete, rebuilt and checked in CI |

The bot image is built on top of the reconstructed stock image: no firmware binary is
stored in the repository, everything is rebuilt from source and checked by SHA-256.

The CU protocol itself is specified in [`docs/protocol.md`](docs/protocol.md),
from public community documentation and our own observations.

## Safety

Slot cars can leave the track at speed. When software drives a car, keep a hand
near the track power supply during first tests. RaceLab cuts bot throttle on
every failure it can see, but if the app is killed or the Bluetooth link never
comes back, the CU keeps the last throttle it received — only the firmware can
fix that. Test any firmware with the car lifted first (see the
[bench test](docs/user-guide.md#before-the-first-real-test-the-bench)).

## Documentation

| File | Content |
|---|---|
| [`docs/getting-started.md`](docs/getting-started.md) | getting started ([FR](docs/fr/getting-started.md)) |
| [`docs/user-guide.md`](docs/user-guide.md) | the app in detail |
| [`docs/api.md`](docs/api.md) | WebSocket / REST API, track connectors |
| [`docs/architecture.md`](docs/architecture.md) | modules and their contracts |
| [`racelab/examples/`](racelab/examples) | agents and a track connector |
| [`docs/protocol.md`](docs/protocol.md) | CU serial / Bluetooth protocol |
| [`docs/bot-protocol.md`](docs/bot-protocol.md) | the `B` command (software drivers) |

An index is in [`docs/README.md`](docs/README.md).

## Layout

```
racelab/      the app: CU link, simulator, race engine, bots, API server, web UI, client
carreralib/   carreralib (MIT, Thomas Kemmer) with our additions: bot commands, sturdier BLE
docs/         user guide, API, architecture, protocols
firmware/     CU firmware built from source: V339 in C, bot module, build.py, manifest (tags, SHA-256)
tools/        check_public.py (pre-publication guard), make_mac_app.py
install.py    cross-platform installer ("Install RaceLab.command" / ".bat" are double-click wrappers)
```

Modules are independent: the race engine, the simulator, the CU link and the
API can be used on their own (see [`docs/architecture.md`](docs/architecture.md)).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Tests run on macOS, Windows and Linux
(GitHub Actions).

## License and credits

MIT — see [`LICENSE`](LICENSE). `carreralib/` is a modified copy of
[carreralib](https://github.com/tkem/carreralib) by Thomas Kemmer (MIT, see
[`carreralib/LICENSE`](carreralib/LICENSE)). Protocol knowledge builds on carreralib and
on the documentation published on slotbaer.de.

RaceLab is not affiliated with or endorsed by Carrera or Stadlbauer Marketing +
Vertrieb GmbH. "Carrera" and "Carrera Digital" are trademarks of their owners,
used here only to say what the software is compatible with.
