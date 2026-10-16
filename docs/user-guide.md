# RaceLab user guide

The race app for Carrera Digital 124/132 tracks: timing, sessions, position
tower, and **software drivers** ("bots") on the CU as it is — no firmware
update (the stock firmware's `C` command, see [bot-protocol.md](bot-protocol.md)).
A WebSocket/REST API lets an agent (RL) drive one or several of the 6 cars,
without a controller.

Two modes, same interface, switched at the top of the screen:

- **Normal**: the race is run as with the official app, everyone with a
  controller.
- **With bots**: the computer can drive any of the 6 cars (autopilot, manual
  fader, or an agent through the API), next to the hand controllers; also "Beat
  the bot" and "AI training" sessions and a diagnostic console. Available on a
  CU with firmware V339 (the current one) or V338, or one flashed with the bot
  module. An older CU can be updated to the stock V339 from System → Advanced
  options.

Screens: **Race**, **Session**, **History** and **System**.

## Installation

Python 3.11 or newer is required. The simplest way is the installer at the
root of the project (details in [getting-started.md](getting-started.md)):

| System | How |
|---|---|
| macOS | double-click `Install RaceLab.command`, then open `RaceLab.app` |
| Windows | double-click `Install RaceLab.bat`, then the **RaceLab** shortcut |
| Linux | `python3 install.py` |

It creates a virtual environment in `.venv` and installs `carreralib/` and
`racelab/` into it. To do the same by hand:

```bash
cd carrera-app
python3 -m venv .venv
.venv/bin/pip install -e carreralib/ -e "racelab[dev]"     # Windows: .venv\Scripts\pip
```

## Running

On the first launch, the welcome screen offers the AppConnect Bluetooth, the
virtual CU or a serial cable; the choice is remembered
(`<data_dir>/settings.json`) and changed afterwards in System → Connection,
without a restart.

From the command line (options are never remembered):

```bash
.venv/bin/racelab                      # last chosen CU (or the welcome screen)
.venv/bin/racelab --window             # in a dedicated window (Chrome/Brave/Edge)
.venv/bin/racelab --sim                # virtual CU
.venv/bin/racelab --sim --stock-firmware   # virtual CU with the stock firmware (bots through C)
.venv/bin/racelab --ble [ADDRESS]      # AppConnect (without an address: search)
.venv/bin/racelab --serial /dev/tty.usbserial-XXXX
.venv/bin/racelab --classic            # Normal mode (no bots)
.venv/bin/racelab --scan               # list the AppConnects and serial adapters in range
```

Other options: `--advanced` (with bots), `-c FILE` (TOML), `--host`, `--port`,
`--no-browser`, `--data-dir DIR`, `-v`; see `racelab --help`.

Only one RaceLab runs at a time: starting it again just opens the window.
To stop it: System → "Quit RaceLab" (bots are cut and handed back to the
controllers), or Ctrl+C in the terminal.

Bluetooth on macOS: the first search triggers a permission request for
RaceLab (or for Terminal). Without the permission, macOS kills the process
that touches Bluetooth; RaceLab therefore does this first access in a separate
process ([`racelab/src/racelab/cu/blescan.py`](../racelab/src/racelab/cu/blescan.py))
and shows what to do instead of stopping.

The UI is served on <http://localhost:8130>. To use it from a tablet, set
`[app] host = "0.0.0.0"` in `racelab.toml` (and ideally an `[api] token`), and open
it by the computer's IP address (`http://192.168.1.20:8130`). A name such as
`http://pit-pc.local:8130` is refused (403, protection against DNS rebinding) until it
is listed in `[api] allowed_hosts`.
All the configuration is documented in
[`racelab/racelab.example.toml`](../racelab/racelab.example.toml).

## The Race screen

- Session bar: type, leader's lap or time remaining, **CU start lights** (the
  real state, read from the status), actions (Start / Pause / Resume /
  Finish). `Space` = main action.
- Live ranking: laps, last lap (green = personal best, purple = best lap of
  the session), best lap, gap, interval, fuel (15 steps, like the Driver
  Display), pit stops.
- **Bots**: in Normal mode, the card "Race against the computer" → **Turn on
  bots** (or **With bots** at the top). Then one switch per car: on, the
  computer drives it; off, its controller does. How it drives: **Auto**
  (autopilot, the default: constant throttle, target lap time, throttle map or
  rival — **Settings**; it waits for the race to start), **Manual** (0–15 fader
  with a safety detent above 12) or **API** (an external agent). The Lane button
  = lane change at the next switch. **Back to normal** hands every car back.
- The autopilots do not handle pit stops yet: in the CU's fuel mode, a bot
  runs dry (flagged as out of fuel; bring it in with Lane at the pit
  entrance, or turn off the CU's fuel mode for long sessions with bots).
- `STOP` (or `Esc`): emergency stop, throttle 0 on every bot. It can only be
  cleared with the mouse.
- **Big screen**: a view of the ranking readable from across the room.

## The other screens

- **Session**: prepare a session. Pick the type (Practice, Qualifying, Race,
  Endurance, Elimination; with bots also "Beat the bot" and "AI
  training"), set its parameters (laps, duration…) and the drivers, then
  "Put on grid". The rules of each type are in
  [architecture.md](architecture.md#session-types-racemodespy).
- **History**: the recorded sessions, with their results and lap charts.
- **System**: the panels Connection (with the bots' status: "Ready · CU as it
  is" on a stock V338/V339), CU buttons (the CU's physical buttons), Console (raw
  frames, with bots), Garage (programs a car's speed, brake and tank, outside a
  session), API, **Advanced options** (CU firmware update, folded: not needed
  for bots) and Log, plus "Quit RaceLab".

## Bot safety — what the app guarantees

RaceLab reaches the cars with the stock firmware's `C` command (one frame for
the 6 slots) on a V338/V339 CU, or with the bot module's `B` command on a CU flashed
with it; the choice is automatic (`bot_transport` in the configuration). It
follows the rules of [bot-protocol.md](bot-protocol.md):

| Situation | `C` (stock CU) | `B` (bot module) |
|---|---|---|
| connection (first) | a frame that hands back every slot (`00`) | probe `B0A`, then `B<s>A` on the 6 slots (a previous crash may have left a bot active) |
| active bot | the 6-slot frame on every change, resent every 300 ms | its throttle, at most 1 change per slot per rail cycle, resent every 500 ms |
| lights on / false start, session stopped, car finished | throttle 0 | throttle 0 |
| AI agent silent > 600 ms or disconnected | throttle 0 (the bot keeps the slot) | same |
| E-STOP | throttle 0 (`P`) on every bot, before any other frame | `B<s>0` on every bot, before any other frame |
| link lost then recovered | throttle 0 on every bot **before** any other command, faders back to 0 | same |
| shutdown (Ctrl+C, SIGTERM, Quit) | a frame that hands back every slot | throttle 0 then `B<s>A` on each bot |
| never | `G` (bootloader), `E`/`F` (firmware blocks) outside a firmware update | same |

**If the app goes silent** (process killed, Mac shut down, Bluetooth lost for
good), the CU takes over by itself: with `C`, about 750 ms after the last frame
every car is back on its controller (a car with no controller plugged in
stops); with the bot module (`v339-bot.5`), about 1 s after the last command the
bot cars are held at throttle 0. Still: keep a hand near the track's power
supply during the first tests.

## Before the first real test: the bench

Before racing against bots on a real track — and after every firmware flash —
check with the **car lifted**, wheels in the air:

1. Car 1 switched on, **Manual**, fader at 3: the wheels turn slowly; at 6,
   faster; at 0, they stop.
2. Throttle 1 = the minimum, not a cut-off; the slot's physical controller is
   ignored while the bot is active, and takes over again afterwards.
3. Lane button over a switch: a single change per press.
4. STOP (or Esc): the car really stops.

5. Quit RaceLab (or cut the Bluetooth): within a second the car is back on its
   controller (`C`) or stopped (`B`).

If the real throttle does not follow the setpoint, or the switch is inverted,
please open an issue with the CU version shown in System → Connection.

## Updating the CU firmware

**Not needed for bots** on a V338/V339 CU. Panel **System → Advanced options** (folded
by default; it opens by itself during an update or a recovery): the app lists the repository's `.HMF` images
and the ones you imported (import button), then flashes the CU through the AppConnect or the serial
cable, using the procedure of `carreralib.fw` (`GB`, then one `.HMF` line per
`E` command, split into 18-byte `F` blocks over BLE). The frames are described
in [protocol.md](protocol.md).

- Only outside a session, with the CU connected; confirm by typing `FLASH`.
- Before `GB`: bots at throttle 0 then handed back to the controllers, command
  queue emptied; during the flash nothing else is sent to the CU and the app
  refuses any other command (orange banner on every screen).
- After the last line, the app waits for the restart, reads the version again
  and probes the bot module: "bots module" after a bot image, "stock firmware"
  after the stock one = the flash worked.
- If interrupted (button, link lost, app closed): the CU stays in its factory
  bootloader, which is never rewritten. **Recovery mode** = send the image
  again from the first line without going through `GB` again.
- Duration: about 2 to 3 min over BLE for an image of roughly 900 lines, about
  1 min over serial.
- The panel lists the images built from source (`firmware/build/`, with their tag,
  e.g. `v339-bot.5`) and the ones you imported. If they are not built yet, **Build the
  images** builds them on this computer (`firmware/build.py`; needs Docker, a few minutes
  the first time). Images are identified by the
  SHA-256 of the file, never by its name: known builds are labelled (recommended,
  stock, superseded), known-defective bot builds are shown in red and **cannot be
  flashed**, anything else is shown as an unknown image.
- The `.HMF` is encoded by Carrera: the app checks its structure (hexadecimal
  lines in quotes, consistent lengths), not its content. To go back to the
  original firmware, flash `v339-stock` (built with the others, `firmware/build/v339-stock.hmf`).
- The flash can be tested without risk with `racelab --sim --stock-firmware`:
  the virtual CU simulates the bootloader and restarts on the flashed image.

## RL agent

See [api.md](api.md) (protocol) and [`racelab/examples/`](../racelab/examples)
(Python client, Gymnasium environment, basic agents). In short:

```python
from racelab.client import RaceLabClient

async with RaceLabClient(agent="my-agent") as c:
    await c.claim(0, free=True)    # slot 1, driving outside a session allowed
    await c.act(0, throttle=6)     # resend within 600 ms (dead-man switch)
```

Car positions: `state["positions"]` (same format everywhere). With the virtual
CU they come from the simulator; on the real track, from a **track
connector** (camera, estimation…) that also declares the shape of the
circuit — see [api.md](api.md) and
[`racelab/examples/track_connector.py`](../racelab/examples/track_connector.py).
Without a connector: no map, and the agent only has line crossings and lap
times.

## Development

```bash
cd racelab && ../.venv/bin/python -m pytest -q            # app: link, bots, simulator, engine, API
cd carreralib && ../.venv/bin/python -m pytest tests -q   # carreralib: B commands, BLE link
```

- [architecture.md](architecture.md): contracts between modules (CU link,
  simulator, race engine, bots, API).
- `racelab/src/racelab/cu/`: CU link (single thread, priority queue, bot watchdog)
- `racelab/src/racelab/sim/`: virtual CU + track + physics
- `racelab/src/racelab/race/`: race engine, modes, position tower, recording
- `racelab/src/racelab/bots/`: software drivers and profiles
- `racelab/src/racelab/api/`: HTTP/WebSocket server; `racelab/src/racelab/web/`: the UI (JS, no build step)
- `carreralib/` was extended: `setbot` / `botlane` / `botrelease`, and a BLE
  link that reports a drop instead of staying silent.
