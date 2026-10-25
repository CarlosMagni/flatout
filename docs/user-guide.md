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
.venv/bin/pip install -e carreralib/ -e "racelab[mpc,bt-serial]"  # Windows: .venv\Scripts\pip
```

Optional parts, installed by the installer when they can be (RaceLab runs without them, and
the feature then says it is unavailable): `mpc` (numpy: the race brain's predictive pilot),
`bt-serial` (Bluetooth serial modules on macOS), `dev` (the tests).

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
  computer drives it; off, its controller does. A car you switch on starts
  in **Manual** (throttle 0: it takes the car, nothing moves until you push the
  fader). How it drives: **Auto** (autopilot: constant throttle, target lap time,
  throttle map, rival or corner speed — **Settings**, see
  [Auto driver profiles](#auto-driver-profiles); it waits for the race to start),
  **Manual** (0–15 fader with a safety detent above 12) or **API** (an external agent). The Lane button
  = lane change at the next switch. **Back to normal** hands every car back.
- Pit stops: only the **Race brain** profile goes to refuel by itself (see
  [Race brain](#race-brain)). With the other profiles, in the CU's fuel mode a bot
  runs dry (flagged as out of fuel; bring it in with Lane at the pit entrance,
  or turn off the CU's fuel mode for long sessions with bots).
- `STOP` (or `Esc`): emergency stop, throttle 0 on every bot. It works from every
  screen, also with a drawer or a dialog open (`Esc` closes it and stops). It can
  only be cleared with the mouse; `Esc` while it is on only says so.
- **Big screen**: a view of the ranking readable from across the room.

## Auto driver profiles

The profile of an **Auto** car is chosen in **Settings** (the gear next to the switch):

| Profile | What it does |
|---|---|
| Constant throttle | A fixed throttle. The simplest way to tune a car. |
| Target pace | Adjusts the throttle after each lap to hold a lap time. A lap much longer than the best is read as an off-track moment: the bot lifts off, then slowly climbs back. |
| Throttle map | A throttle per portion of the lap (straights, corners), timed from the last line crossing. |
| Rival | Follows another driver's pace, with a margin. |
| Corner speed (map) | Drives from the car **positions** and the track **shape** (see below). |
| Race brain | Drives with Corner speed or Target pace, and also changes lane and pits for fuel by itself (see [Race brain](#race-brain)). |

**Corner speed (map)** ("AI driver v1") needs the car positions and the track shape. The virtual
CU provides both; on a real track a [track connector](api.md#track-connector-track-shape--positions)
(for example the camera) must be running and must send `length_m` with the shape (the lap
length in metres, or `[lane 0, lane 1]`), otherwise the profile cannot scale the corners. How it
works:

1. From the shape it computes the curvature of the track and, for each lane, a target speed:
   `sqrt(a_lat / curvature)` in corners, reduced by the margin, and a backward pass so that the
   target already drops before the corner (braking distance of the motor).
2. Every 50 ms it predicts the car's speed one latency ahead (0.1 s) with a model of the motor,
   and picks the throttle that reaches the target speed of the road just ahead: it lifts when the
   car is above it. The throttle never exceeds the profile's **cap**.
3. It learns each corner's limit during the session: +3 % on the corner's speed after a clean
   pass at the target, -8 % and frozen after an off track (the car's `off` flag, or a lap much
   slower than the best, blamed on the corner where the most time was lost). Factors stay between
   0.5 and 1.5, and a frozen corner never goes up again. They are forgotten at the next session,
   after an emergency stop or when the profile is saved again.
4. Without fresh positions (older than 500 ms), without a shape, or with a shape that has no
   `length_m`, it holds the **target lap time** instead (shown as "Lap time to hold without
   positions"), so a lost camera never leaves a car without a driver.

Settings: **Grip at start** (m/s², lateral acceleration assumed in corners: lower is more
careful, the learning raises it corner by corner; 8-12 is a good start for the virtual CU),
**Margin** (%, taken off every target speed), **Throttle cap** (the highest throttle the profile
ever sends; the global `max_throttle` of the safety settings still applies on top).

## Race brain

**Race brain** is an Auto profile that adds decisions to a driving profile. In **Settings** you pick:

- **Pilot**: the driving model, *Corner speed (map)* (default) or *Target pace*, with its few settings
  (the list comes from the app, so a new pilot shows up there by itself). Keep the cap low for a first
  real run, as with Corner speed.
- **Lane changes**: *Keeps its lane*; *Fastest lane* (times both lanes on clean laps, then keeps the
  faster one if it is at least 2 % faster); *Overtakes* (changes lane behind a slower car closer than
  the set distance, only if the other lane is free alongside, otherwise like Fastest lane);
  *Alternates lanes* (every N laps, for tests).
- **Pit stops**: *No pit stop*; *Pits when low* (it measures the fuel used per lap and pits when the
  fuel left after the next lap would be under the reserve, 2 laps by default; never in the last lap of
  a race, never when the tank is enough to finish, never twice within 5 laps); *Pits once, at once*.
  In the pit lane it drives at the **pit lane throttle**, stops in the middle of the pit zone until the
  tank is full, then leaves. It needs the CU's **fuel mode** and **pit lane** on.
- **Pit zone**: entry and exit in % of the lap from the line, only if the track map has no pit zone (a
  camera map does not have one yet: park the car at the pit entry and read its `s` in the API state).

Lane changes and pit stops need **live positions** (the virtual CU, or a camera connector with the
cars' speed) and a track map in metres with its lane changers. The brain sends each request between
about 2 m and 1 m before the changer or the pit entry (closer when slower), once per pass, never from
the pit lane; without positions it only drives (like Corner speed without positions) and never guesses.
The car's chip says what it is doing ("Pit stop this lap (fuel 3/15)", "Lane 1 is faster"…) and, when
it cannot, why ("needs live positions (camera)").

For tests, **Pit now** and **Change lane at the next changer** (in the same Settings) send one request
to the running brain, timed like its own. The STOP button, the session state and a lost link still cut
the car whatever the brain plans, also while it is stopped in the pits.

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
| hand-back test (System, Hardware test) | `C` heartbeat paused for up to 3 s on purpose, the CU hands the slot back; the pause ends by itself, on STOP and on link loss, and the bot is zeroed, never resumed | not applicable |
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

## Hardware test (System tab)

System → **Hardware test** validates the stock CU's `C` command on the real track,
step by step. It needs a CU on V338/V339 and **With bots** on; put the car on a
**straight**, clear the track, and keep a hand on **STOP** (the panel has its own
STOP button that stays on screen). Each step gets a chip (To do, Running, PASS,
FAIL, Interrupted, Not run).

1. **Link check**: transport, CU version, bot transport (`C`, `B` or none),
   bots supported. PASS only if the CU is connected and bots go through `C`.
2. **Latency**: times 50, 100 or 300 round trips of the CU poll (nothing that can
   move a car is sent) and shows min, median, p95, max, jitter, a text
   histogram and the acknowledgement time of `C` frames. PASS means no lost
   answer, p95 at most 150 ms and no round trip above 400 ms (an indication:
   a `C` frame must arrive well under the CU's 750 ms limit).
3. **Throttle pulse**: pick the car, a throttle (1 to 6, the server refuses more)
   and a duration (0.2 to 2 s). The server holds the throttle, then forces 0
   and hands the slot back; the page never times it. Answer "did the car move,
   then stop?".
4. **Lane change**: same run, plus one lane request. Put the car just before a lane
   changer (less than 30 cm), on a straight; the run lasts 1.5 s or more (a shorter
   chosen duration is raised to 1.5 s). Answer whether the car changed lane at the
   lane changer.
5. **Hand-back (CU watchdog)**: RaceLab holds a throttle, then stops refreshing the
   `C` frame for 1.5 s. About 0.75 s later the CU must give the slot back to its
   controller, so the car stops (with the controller idle or unplugged). RaceLab
   cannot see this, so you answer yes or no. It then sends 0 and hands the slot back.
   Every other slot must be free of bots, because the `C` frame covers all six.
6. **Report**: **Copy report** collects the steps and your notes into one JSON,
   saves it in the data folder (`hardware-tests/hwtest-<date>.json`) and copies
   it, to share with another tester.

The tests are refused during a session, under STOP, on a CU without bots, on a
slot driven by an API agent or already a bot, and while another test runs. STOP
interrupts any of them: the slot is zeroed and handed back. The same bench runs
against the virtual CU (`racelab --sim --stock-firmware`). The ops behind it
(`link.bench`, `test.*`) are in [api.md](api.md).

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
