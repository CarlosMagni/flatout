#!/usr/bin/env python3
"""Pilot benchmark on the virtual CU — corners vs mpc, on demand and reproducible.

Phase-1 (pilot) diagnosis, 2026-10-16, by the phase-1 bench review. It reuses the
tests' harness (`racelab/tests/test_corners.py::drive`: virtual CU, 50 ms ticks,
the throttle reaches the car `delay_steps` = 2 ticks late) — this script only makes
it runnable outside pytest and prints the grid used for the diagnosis.

    python racelab/bench_pilots.py                 # 20 laps, full grid
    python racelab/bench_pilots.py --laps 30 --only a_lat

Needs the dev extra (pytest) to import the harness:

    pip install -e racelab[dev]

Findings (20 laps, default_track, delay 100 ms; see the branch's commit message):

| config                                   | best  | last-5 | off |
| corners (defaults)                       | 6600  |  6620  |  0  |
| mpc (defaults)                           | 7250  |  7310  |  0  |
| mpc tuned (4it/96, h2.5, sigma .02, r1)  | 6950  |  7010  |  0  |
| mpc tuned, a_lat 18                      | 5800  |  5840  |  0  |
| corners a_lat 18 (margin 5%)             | 5550  |  5550  |  0  |

- the default `a_lat` (10) is 2x under this sim's real limit (`A_MAX = 20 m/s^2`
  in `racelab/sim/track.py`): with the defaults, BOTH profiles are far from the
  car's potential. `a_lat` is the biggest single lever for the mpc.
- at MATCHED grip (a_lat 18 both sides) `corners` still wins by about 0.25 s/lap.
  Suspects in `mpc.py`: the terminal braking check (`BRAKE_REACH` 0.85), a risk
  penalty that bites at any `risk`, slow grip learning, and the first laps while
  the estimators settle (8.6 s on lap 1 at defaults).
- the roadmap's lap times (corners 5.7 s / mpc 6.2 s) did not reproduce with the
  test harness defaults (6.6 s / 7.3 s): which settings produce them?
- NOT measured here: the real car (system identification comes from the field
  recordings); the sim's own `A_MAX` may itself be optimistic vs a real Carrera.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # the repository root
for _p in (ROOT / "racelab" / "src", ROOT / "carreralib" / "src", ROOT / "racelab"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from tests.test_corners import drive
except ImportError as e:  # the harness imports pytest
    sys.exit(f"this benchmark reuses the test harness; install the dev extra first ({e})")

from racelab.bots.mpc import MPCProfile  # noqa: E402
from racelab.bots.profiles import CornersProfile  # noqa: E402

# The grid of the phase-1 diagnosis. The last two rows match the grip assumption.
GRID = [
    ("corners (defaults)", lambda: CornersProfile()),
    ("mpc (defaults)", lambda: MPCProfile()),
    ("mpc tuned (4it/96, h2.5, sigma.02, risk 1)",
     lambda: MPCProfile(iters=4, samples=96, horizon_s=2.5, sigma=0.02, risk=1.0)),
    ("mpc tuned, a_lat 18",
     lambda: MPCProfile(iters=4, samples=96, horizon_s=2.5, sigma=0.02, risk=1.0, a_lat=18.0)),
    ("corners a_lat 18 (margin 5%)", lambda: CornersProfile(a_lat=18.0)),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--laps", type=int, default=20, help="laps per row (default 20)")
    ap.add_argument("--only", default=None, help="keep the rows whose label contains this")
    args = ap.parse_args()

    rows = [(name, make) for name, make in GRID if not args.only or args.only in name]
    if not rows:
        sys.exit(f"no row matches {args.only!r}")
    print(f"virtual CU, {args.laps} laps per row; best and mean of the last 5 laps in ms")
    print(f"{'config':<46} {'best':>6} {'last5':>7} {'off':>4}")
    for name, make in rows:
        times, offs, _top = drive(make(), args.laps)
        print(f"{name:<46} {min(times):>6} {sum(times[-5:]) / 5:>7.0f} {offs:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
