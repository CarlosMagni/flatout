# Results — the first RL strategist (run5, 2026-10-16)

The first trained strategist (PPO on the headless `RaceEnv`, weights `out/run5/policy.npz`)
against the baselines on **12 fresh seeds** (500000-500011), 26 laps, 4 cars (3 simulated
humans), every arm driving the same pilot (mpc, `a_lat` 18 — calibrated for this simulator):

| arm | position | win | race | dry | offs | pits |
|---|---|---|---|---|---|---|
| **strategist** | **1.58** | **58 %** | **162.8 s** | 42 % | 0.00 | 1.0 |
| rule brain (lane fastest, pit auto) | 2.92 | 0 % | 169.6 s | 0 % | 0.00 | 1.0 |
| random | 3.92 | 0 % | 186.0 s | 0 % | 0.67 | 4.6 |

**The strategist beats the spec's baseline (the rule brain): −1.34 position, +58 points of
wins, 6.8 s a race faster, same pilot.** Ablations say where the remaining value leaks —
the map for the next training round:

| ablation (action dimension frozen) | position | win | dry |
|---|---|---|---|
| lane fixed to *stay* | **1.33** | **75 %** | 42 % |
| risk fixed to 0.5 | 1.67 | 58 % | **0 %** |
| pit fixed to *never* | 4.00 | 0 % | 100 % |

- **The lane decisions are net negative on this track**: forcing *stay* is the best arm of
  all. Lane 0 is simply the better lane here; the policy should learn to keep lane more
  often (more updates, or a leaner lane action);
- **risk 1 buys pace but burns fuel**: with risk fixed at 0.5 the car ends 0 % dry at
  nearly the same position. The risk-fuel coupling is real and learnable — more updates.

## The pit window (one race, fresh seed, scripted pit)

| pit at lap | 10 | 14 | 16 | 18 | **20** | 22 | 24 |
|---|---|---|---|---|---|---|---|
| total reward | 9.16 | 10.50 | 11.83 | 13.16 | **14.50** | 11.78 | 11.68 |
| race | 163.6 s | 163.6 | 163.5 | 163.5 | **163.3** | 184.8 | 206.6 |

The optimal stop (the last safe lap, ~20 of 26) beats an early one by ~5 reward points, and
the dry crawl by ~21-43 s of race time. The rule brain's auto pit (169.6 s) is beaten by
good manual timing (163.3 s). The signal is strong and smooth — the early runs just could
not hear it.

## Training history (the road here)

| run | config | outcome |
|---|---|---|
| run2 | 8-lap races (tank never empty) | learned lane/risk fast (pos 3.97 → 1.09, 94 % wins) — but the guard truncated 26-lap races: invalid, kept as the pipeline proof |
| run3 | 26 laps, correct guard, cold start, dry −0.5 | dry 75 %: the pit signal drowned in the dense ~10-point reward |
| run4 | warm start, dry −2.0, fresh seeds | dry ~50 % on training seeds, late-pit overfit on fresh ones |
| **run5** | warm, dry −2.0, 48 races/update, 45 updates | dry 4-12 % on training seeds; **beats the rule brain on fresh seeds** |

## Reproduce

```bash
PYTHONPATH=src:../racelab/src:../carreralib/src ../.venv/bin/python -m strategist.train \
    --updates 45 --workers 8 --episodes-per-worker 6 --laps 26 --opponents 3 --pilot mpc \
    --pilot-params '{"a_lat": 18.0}' --seed 13 --out out/run5
PYTHONPATH=src:../racelab/src:../carreralib/src ../.venv/bin/python -m strategist.evaluate \
    --policy out/run5/policy.npz --baseline both --races 12 --pilot-params '{"a_lat": 18.0}'
```

## Limits, next steps

- Nothing has run on real hardware; the simulator's grip (`a_lat` 18 vs the cautious 10)
  must come from the track's system identification (the field recordings).
- **Track randomisation**: the policy overfits one layout today (that is why *stay* wins);
  a parameterisable `racelab.sim` track is the next structural step.
- **Human-replay opponents** from the field recordings; live validation of `live.py`.
- The risk-fuel coupling and the lane tendency: 2-4× more updates, or trim the action space
  where the ablations say it is dead weight.
