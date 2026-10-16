# Results — the RL strategist (runs 5-7, 2026-10-16/17)

PPO on the headless `RaceEnv`, checkpoints in `out/run*/policy.npz` (local). Every arm drives
the same pilot with the same parameters; races are 26 laps with 4 cars unless said otherwise.

## Multi-track (run7c) — the headline

One **generated track per episode** (unseen layouts in evaluation), pilot mpc `a_lat 19.5,
cap 15`, warm start from run5, 39 of 70 updates (stopped at the plateau), 4 workers (~50 % CPU).

Training curve, selected updates (training seeds): position 3.10 → 1.5-2.0, wins 27 % → 60-77 %,
dry races 85 % → 23-38 %, off-tracks 0.00.

Fresh tracks, 12 races, seeds 600000-11:

| arm | position | win | race | dry | offs | pits |
|---|---|---|---|---|---|---|
| strategist | 1.83 | 67 % | **128.5 s** | 33 % | 0.00 | 1.0 |
| rule brain (lane fastest, pit auto) | 1.83 | 67 % | 130.8 s | 33 % | 0.00 | 1.0 |
| random | 3.75 | 8 % | 142.9 s | 33 % | 0.00 | 3.6 |
| ablated: never pit | 4.00 | 0 % | 206.6 s | 100 % | 0.00 | 0.0 |
| ablated: risk fixed 0.5 | 1.83 | 67 % | 128.9 s | 42 % | 0.00 | 1.0 |
| ablated: lane fixed to stay | 1.83 | 67 % | 128.5 s | 33 % | 0.00 | 1.0 |

Reading: on unseen tracks the strategist **matches the engineered rules** (and is 2.3 s a race
faster over the distance); the **pit decision is essential** (without it: last, dry every race);
random is far behind. The learned lane choices are inert here (lane-ablated identical) and the
risk dimension moves the dry rate (fixed risk: 42 % instead of 33 %).

## The default track (same era, fresh seeds)

| arm | position | win | race | dry | offs |
|---|---|---|---|---|---|
| strategist | 1.00 | 100 % | 155.0 s | 0 % | 2.17 |
| rule brain | 1.00 | 100 % | 154.8 s | 0 % | 1.08 |
| random | 3.50 | 0 % | 182.5 s | 0 % | 2.58 |

Both dominate; the strategist is sloppier (2.17 off-tracks a race vs 1.08 — its learned risk
runs hotter than the rule's 0.5 and buys nothing where the field is this weak).

## Historical: the first win (run5, one track, `cap` 12 era)

| arm | position | win | race | dry |
|---|---|---|---|---|
| strategist | **1.58** | **58 %** | 162.8 s | 42 % |
| rule brain | 2.92 | 0 % | 169.6 s | 0 % |
| random | 3.92 | 0 % | 186.0 s | 0 % |

and the pit-window economics probe: a stop at the last safe lap scores 14.50, ten laps early
9.16, and a lap too late 11.78 (dry crawl, 21-43 s lost). The training signal was real; the
early full-race runs just could not hear it under a diluted penalty (dry −0.5).

## The `cap` discovery (why the first multi-track run looked dead)

The pilots' target speed tops at `cap/15 × v_max × 0.98`: with the default `cap` 12 that is
**3.29 m/s**, while the simulated humans (no cap) run up to 4.2. On the default twisty track the
cap sufficed; on flowing generated tracks every race ended P4 regardless of the policy — no
learning signal (run6: position flat at 4.00 for 60+ updates). With `cap` 15 the same pilot is
competitive — `corners` even dominates (4.44 s/lap against the humans' ~4.9 on a fresh track) —
and the multi-track training came alive (run7c).

## Honest reading and next steps

The learner works end-to-end: PPO on the headless environment, multi-track generalisation, safe
pit timing learned, rock-solid vs random. But at full pace the simulated humans are **too weak to
discriminate**: any decent driver wins two races out of three, so the strategist converges to
rule-like behaviour instead of beating it. Next levers, in order:

1. **Stronger opponent fields**: drive the other slots with bots (corners/mpc profiles) instead
   of the skill-limited humans → real position pressure;
2. **The risk/fuel coupling** (dry 33-42 % on unseen tracks): more updates, or a pit prior from
   the humans' tank heuristic;
3. **Real-car system identification** (field recordings) to set `a_lat`, `tau`, `v_max` for the
   real track, then re-train and validate `live.py`.

## Reproduce

```bash
PYTHONPATH=src:../racelab/src:../carreralib/src ../.venv/bin/python -m strategist.train \
    --updates 70 --workers 4 --episodes-per-worker 12 --laps 26 --opponents 3 --pilot mpc \
    --pilot-params '{"a_lat": 19.5, "cap": 15.0}' --seed 41 --init out/run5/policy.npz --out out/run7c
PYTHONPATH=src:../racelab/src:../carreralib/src ../.venv/bin/python -m strategist.evaluate \
    --policy out/run7c/policy.npz --baseline both --races 12 --seed0 600000 \
    --pilot-params '{"a_lat": 19.5, "cap": 15.0}'
```
