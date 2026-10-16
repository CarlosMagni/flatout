# strategist — the RL strategist (Strategy v2)

The pilot keeps the throttle (Tier 1); the strategist learns to race (Tier 2). A few times
per lap it picks among the race brain's legal moves — a lane change at the next changer, a
pit stop at the next entry, the MPC pilot's `risk` — and it learns from race results. It is
an **add-on**: it imports `racelab`, and RaceLab never imports or names it. The full design
and the phase table are in [`docs/roadmap.md`](../docs/roadmap.md) ("Strategy v2" section);
this package implements the headless training environment and the first PPO strategist.

## Layout

| file | what |
|---|---|
| `src/strategist/observation.py` | the 20-float observation, computed from the RaceLab snapshot only — the same code in training and live |
| `src/strategist/env.py` | the headless environment: RaceLab's virtual CU and physics, the real race brain on slot 0, simulated humans on the others |
| `src/strategist/policy.py` | the policy (2×64 MLP): numpy inference + checkpoints (`.npz`) — runs without torch |
| `src/strategist/train.py` | PPO (torch in the main process, rollouts in worker processes with the numpy policy) |
| `src/strategist/evaluate.py` | fixed-seed comparisons and ablations: strategist vs rule brain vs random |
| `src/strategist/live.py` | a RaceLab client that runs a checkpoint on the real track |

## Decisions, observations, reward

- **Action** (12 discrete): lane (stay / change at the next changer) × pit (no / yes at the
  next entry) × risk (0 / 0.5 / 1, applied in place through `bot.params` — the learned grip
  and the estimators are kept). Decided at the grid and after each completed lap; the brain
  still times the requests and its safety gates come first.
- **Observation** (20 floats, snapshot-computable): laps done/left, remaining time, fuel and
  fuel per lap, laps of fuel left, lane, last clean lap per lane, gaps and speeds ahead (same
  and other lane) and behind, whether the other lane is free at the next changer, stops, in
  pit, race position, whether a pit stop is allowed (`pit.min_laps` spacing).
- **Reward**: a small pace shaping on each clean lap, then the race result — position and
  laps completed, minus running dry (−0.5) and off-tracks (−0.15 each). The rule brain is
  the baseline to beat; the environment is deterministic per seed.

## Running it

The environment needs the repo's virtualenv with `racelab`'s dependencies (numpy, pytest,
torch — see the root README). From this directory:

```bash
pip install -e .[train,dev]        # or: use the repo venv and the PYTHONPATH below
PYTHONPATH=src:../racelab/src:../carreralib/src ../.venv/bin/python -m strategist.train --quick
PYTHONPATH=src:../racelab/src:../carreralib/src ../.venv/bin/python -m strategist.train \
    --updates 60 --workers 8 --episodes-per-worker 4 --laps 8 --opponents 2 --pilot mpc --out out/run1
PYTHONPATH=src:../racelab/src:../carreralib/src ../.venv/bin/python -m strategist.evaluate \
    --policy out/run1/policy.npz --baseline both --races 12
PYTHONPATH=src:../racelab/src:../carreralib/src ../.venv/bin/python -m strategist.evaluate \
    --policy out/run1/policy.npz --ablate lane        # pit / risk: same idea
# live (car configured as Auto / Race brain in the UI):
PYTHONPATH=src:../racelab/src:../carreralib/src ../.venv/bin/python -m strategist.live \
    --policy out/run1/policy.npz --slot 0 --dry-run
```

Tests: `cd strategist && ../.venv/bin/python -m pytest -q` (the pyproject sets the paths).

## Status, limits

- Nothing here has run on real hardware yet (like everything else on this branch).
- The environment uses RaceLab's single built-in track: **track randomisation** waits for a
  parameterisable `racelab.sim` track; opponents are the sim's simulated humans (skill,
  mistakes, pit under `PIT_FUEL`, lane changes under `LANE_CHANGE_PROB`); **human replay
  ghosts** from field recordings are a natural next opponent.
- One decision per lap in this first version (short episodes, cheap PPO); mid-lap decisions
  before each changer/pit entry are a possible refinement.
- `live.py` is a skeleton: it assumes the car is already configured as the race brain and
  sends `bot.brain` / `bot.params`. The operator's manual requests stay above it (the app).
