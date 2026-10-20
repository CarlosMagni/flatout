# RaceLab

*(This repository is `flatout`; the app it contains is RaceLab.)*

Race management and software drivers for **Carrera Digital 124/132** slot-car
tracks, from a laptop. RaceLab talks to the Control Unit (CU) through the
Carrera AppConnect Bluetooth adapter (item 30369) or a serial cable, runs your
races, and lets software drive the cars — on a stock CU, nothing to flash: a
fader in the UI, a local autopilot, or your own agent (reinforcement learning)
through an API.

> **Status: early but playable.** Everything runs against the built-in
> virtual CU; real-hardware validation is in progress
> (see [Status and roadmap](#status-and-roadmap)).

## Features

- **Race control** — CU start lights, live timing, practice, qualifying, race,
  endurance, elimination; fuel and pit stops, position tower, session history
  with lap charts, a **big-screen view** readable from across the room.
- **Software drivers ("bots")** — on any of the 6 controller slots, with the CU
  as it is (stock firmware V338/V339, see [Bots](#bots-on-a-stock-cu)): manual
  fader, autopilots (constant throttle, target lap time with a learned throttle
  ceiling, throttle map, rival), or an external agent. One click turns them on
  (**With bots** at the top of the screen). Safety first: throttle forced to 0
  during start lights, dead-man for agents, emergency stop, zero throttle on
  reconnection and on exit.
- **API for agents** — JSON over WebSocket and REST, a Python client and a
  [Gymnasium](https://gymnasium.farama.org/)-style environment (`racelab.client`).
- **Track connectors** — any program (e.g. an overhead camera) can publish the
  track shape and car positions; the map and the agents read them in the same
  format as the simulator. [`trackloc/`](trackloc/README.md) is one: a camera
  connector (one phone or webcam, automatic track mapping, car tracking).
- **Virtual CU** — a simulated track, cars and firmware: try everything, write
  and test agents, without hardware.
- **CU firmware flashing** (advanced option, not needed for bots) — the images
  are built from source (stock and bot firmware, identified by SHA-256),
  checked before sending, rollback to stock, recovery mode. See
  [Advanced: flash a CU firmware](#advanced-flash-a-cu-firmware) below.
- **English and French** — the interface is in English, with a French option.
- **Cross-platform** — macOS, Windows, Linux; Python ≥ 3.11.

## Status and roadmap

The long-term goal: **AI drivers on a real track** — your own agent, trained
against the simulator, driving a real slot car.

- **Works today** (virtual CU): the full race manager, software drivers on a
  stock CU, an HTTP/WebSocket API, a Python client and a
  [Gymnasium](https://gymnasium.farama.org/)-style environment — the connectors
  to plug in and train your own agent are already in place.
- **In progress**: the last stretch of real-hardware validation (bots over the
  AppConnect Bluetooth, firmware bench tests), more RL training helpers on top of the API
  (training scenarios, reward shaping), a camera track connector for real car
  positions, and pit stops for AI drivers.

## Bots on a stock CU

The stock CU firmware (V339, the current one, and V338) already has a command, `C`, that
lets the host hold the throttle of any of the 6 controller slots — the CU then
treats it exactly like a hand controller: start lights, false starts, fuel. So
**bots need no firmware update**: connect the CU, click **With bots** at the
top of the screen (or **Turn on bots** on the Race screen), and switch on the
cars the computer should drive. Everything else stays as it was: a car that is
not switched on is driven by its controller.

If the app goes silent (killed, Bluetooth lost), the CU itself gives every car
back to its controller after about 750 ms. The command is specified in
[`docs/bot-protocol.md`](docs/bot-protocol.md). An older CU (V336, V337) does not
have it: update it to the stock V339 first (System → Advanced options). A
hardware-8 CU (STM32) has neither: a Raspberry Pi Pico on the controller socket can
play the controllers instead ([`tools/pico_controllers/`](tools/pico_controllers/README.md),
not yet tried on a hardware-8 CU).

## Advanced: flash a CU firmware

Not needed for bots. RaceLab can still update the CU firmware itself — panel
**System → Advanced options** — over Bluetooth or serial, with confirmation,
progress, recovery mode, and the image's SHA-256 shown before you flash (always
check the hash, not the file name). It is there for tinkerers and for features
to come.

| Image to flash | What it does |
|---|---|
| the **stock firmware** image | a stock CU again — the safe rollback, and the no-risk way to rehearse the update; also brings an older CU to V339 |
| the **bot firmware** image | adds the `B` bot command (bots on a CU where `C` is not available) and a firmware watchdog — and otherwise behaves exactly as before |

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

Bots do not need a custom firmware (see [above](#bots-on-a-stock-cu)): the app
uses the stock firmware's `C` command on a V338/V339 CU, and the bot module's `B`
command on a CU flashed with it. Both are specified in
[`docs/bot-protocol.md`](docs/bot-protocol.md). On a CU that has neither, RaceLab
runs in **Normal** mode only (races with hand controllers).

Everything firmware lives in [`firmware/`](firmware/README.md), as two projects
— the base for going further than the stock CU allows:

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
every failure it can see; if the app is killed or the Bluetooth link never
comes back, the CU takes over: with the stock `C` command every car goes back to
its controller after about 750 ms (a car with no controller plugged in stops),
with the bot firmware (`v339-bot.5`) bot cars are held at throttle 0 after about
a second. Try bots and any firmware with the car lifted first (see the
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
| [`docs/bot-protocol.md`](docs/bot-protocol.md) | the `C` and `B` commands (software drivers) |

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
