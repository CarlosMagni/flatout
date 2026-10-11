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

## 5. Quit

System → **"Quit RaceLab"**: the bots are cut and handed back to the
controllers. Closing the window is not enough: RaceLab keeps running in the
background.

## Before running bots on the real track

Software drivers need a CU with modified firmware (the `B` command, see
[bot-protocol.md](bot-protocol.md)); this firmware is **not** provided here.
After any flash, do a bench test with the car lifted (throttle 3, 6, 0;
controller ignored while the bot is active; a single lane change per press; STOP
really cuts) before letting a bot run. Without this firmware, RaceLab works in
Classic mode.

## If something goes wrong

- "Bluetooth unavailable": System Settings → Privacy & Security → Bluetooth →
  allow RaceLab, and check that Bluetooth is on.
- Log: System → Log, or the file `~/.racelab/racelab.log`.
- Reinstall: run the installer again (it is safe, it does not touch your
  sessions or settings).

More in the [user guide](user-guide.md).
