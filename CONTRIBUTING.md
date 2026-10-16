# Contributing

Thanks for helping! A few rules keep the project safe and publishable.

## Firmware

Everything is in [`firmware/`](firmware/README.md). No firmware image is committed:
`python3 firmware/build.py` builds the stock image from the C reconstruction and the bot
image on top of it, in the pinned toolchain (Docker), and checks both against
`firmware/manifest.json`; CI runs `build.py --check`. `python tools/check_public.py` (also in
CI) refuses any `.hmf`/`.bin`/`.hex` file and disassembly listings.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e carreralib/ -e "racelab[dev]" -e "trackloc[dev]"
cd racelab && ../.venv/bin/python -m pytest -q             # app
cd carreralib && ../.venv/bin/python -m pytest tests -q    # carreralib with our additions
cd trackloc && ../.venv/bin/python -m pytest -q           # camera localisation (imports racelab, never the reverse)
.venv/bin/racelab --sim                                    # try the UI on the virtual CU
```

Everything can be developed against the virtual CU (`--sim`, or
`--sim --stock-firmware` to test bots on a stock CU (`C` command) and firmware updates;
set `[sim] firmware = "337"` to see an older CU that cannot take bots).

## Code

- Python ≥ 3.11, standard library first; new dependencies need a good reason.
- The web UI is plain ES modules and CSS, no build step and no CDN (it must run
  offline next to the track).
- Module contracts are in [`docs/architecture.md`](docs/architecture.md); API
  changes go into [`docs/api.md`](docs/api.md) first.
- Anything that can move a car is safety-critical: add a test for the failure
  path (link loss, agent disconnect, emergency stop) with every change there.
- The UI is English with a French option: user-visible strings need both
  languages.
- Documentation is in English (`docs/`); the French getting-started guide in
  `docs/fr/` should be kept in sync with the English one.

## Hardware reports

Bug reports from real tracks are gold. Please include the CU version (shown in
System → Connection), the transport (AppConnect Bluetooth or serial cable), the
log (System → Log) and what the cars did.
