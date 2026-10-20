# Tools

Standard-library Python scripts, run from the project root.

- `check_public.py`: pre-publication guard. Fails if the repository (tracked files
  and anything `git add -A` would add) holds forbidden material: any firmware image file (they are built from source, never
  committed), disassembly, the official app, or the CU signature outside `firmware/`. Also run by CI: `python tools/check_public.py`.
- `pico_controllers/`: MicroPython program for a Raspberry Pi Pico on the CU's controller
  socket that plays the 6 controllers (bots on any CU, hardware 8 included), and its checks.
  See [its README](pico_controllers/README.md).
- `make_mac_app.py`: builds `RaceLab.app`, the macOS double-click launcher (needs
  `iconutil` and `plutil`). `install.py` calls it on macOS; rerunning replaces the app.
