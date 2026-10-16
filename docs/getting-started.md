# Getting started with RaceLab

Français : [docs/fr/getting-started.md](fr/getting-started.md)

## 1. Install (once)

- **macOS**: double-click **`Install RaceLab.command`** (builds `RaceLab.app`).
- **Windows**: double-click **`Install RaceLab.bat`** (Desktop and Start menu shortcuts).
- **Linux**: `python3 install.py` (a "RaceLab" entry in the applications menu).

You need Python 3.11 or newer and an Internet connection (1 to 2 minutes).

If macOS refuses to open the file: right-click → Open → Open. On Windows,
"Windows protected your PC": More info → Run anyway.

## 2. Launch

Double-click **`RaceLab.app`** (macOS) or the **RaceLab** shortcut (Windows,
Linux). RaceLab opens in its own window if Chrome, Brave or Edge is installed,
otherwise in your browser.

Launching RaceLab while it is already running just reopens the window.

## 3. First launch: pick your CU

- **AppConnect Bluetooth** — CU switched on, AppConnect plugged in, the phone
  Carrera app closed (the AppConnect accepts a single connection), then
  "Connect". macOS asks to allow Bluetooth for RaceLab: accept.
- **Serial cable** — a USB-serial adapter plugged directly into the CU; pick the
  port in the list. On Linux, add yourself to the `dialout` group first
  (`sudo usermod -aG dialout $USER`, then log in again).
- **Virtual CU** — to try RaceLab without hardware.

RaceLab remembers the choice. To change it: System → Connection.

## 4. Race

Session → pick the type → "Put on grid" → Race → "Start" (or the Space bar).
STOP (or Esc) cuts every bot.

## 5. Race against the computer (bots)

Race screen → **Turn on bots** (or **With bots** at the top). Switch on the cars
the computer should drive: in **Auto**, a bot waits for the race to start, then
drives on its own. The other cars stay on their controllers. **Back to normal**
hands every car back.

Nothing to install on the CU: bots work with the CU as it is (firmware V339, the
current one).

## 6. Quit

System → **"Quit RaceLab"**: the bots are cut and handed back to the
controllers. Closing the window is not enough: RaceLab keeps running in the
background.

## Before running bots on the real track

The first time, do a bench test with the car lifted: a car switched on in
**Manual**, fader at 3, 6, 0; controller ignored while the bot is active; a
single lane change per press; STOP really cuts. Details in the
[user guide](user-guide.md#before-the-first-real-test-the-bench).

If the app says bots are unavailable, the CU is older than V338: update it to the
stock V339 in System → Advanced options (see the
[user guide](user-guide.md#updating-the-cu-firmware)).

## If something goes wrong

- "Bluetooth unavailable": System Settings → Privacy & Security → Bluetooth →
  allow RaceLab, and check that Bluetooth is on.
- Log: System → Log, or the file `~/.racelab/racelab.log`.
- Reinstall: run the installer again (it is safe, it does not touch your
  sessions or settings).

More in the [user guide](user-guide.md).

## Camera localisation (optional)

The [`trackloc/`](../trackloc/README.md) folder is a camera connector: an iPhone (Continuity
Camera) or a webcam above the track publishes the track shape and the car positions to RaceLab.

```bash
.venv/bin/pip install -e trackloc
python -m trackloc marker      # marker to print at 100 %, then measure the black square
python -m trackloc probe       # available cameras (allow camera access for the terminal)
python -m trackloc map --camera 1 --slot 0 --marker-size 0.150   # automatic discovery lap
python -m trackloc run --camera 1 --map track_map.json --slots 0,1
```

Details (recording, latency, limits) in [`trackloc/README.md`](../trackloc/README.md).
