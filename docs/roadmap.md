# Roadmap and status

Status on 2026-10-16, branch `vision-connector`. Everything below works on the virtual CU and in
simulation; **nothing has run on real hardware yet** (first real test: see
[hardware-test.md](hardware-test.md)).

## Architecture

```
camera ─► trackloc (localisation) ──track.set / track.positions──► RaceLab
                                                                    │
            race brain (one per bot, bots/brain.py)                 │
              pit planner  ─┐                                       │
              lane planner ─┼─► arbiter ─► (throttle, lane request) │
              pilot        ─┘   pilots: corners | target | mpc      │
            safety gates (always first): STOP, dead-man, link, lights, session
                                                                    │
                       C command over AppConnect Bluetooth (75 ms)  ▼
                                                        Control Unit ─► cars
```

Two tiers:
- **Tier 1, driving well (the pilot)**. `corners` = speed profile from the track shape with
  per-corner grip learning (default). `mpc` = model-predictive pilot, 48 throttle plans over
  1.2 s every decision, delay and top speed estimated online.
- **Tier 2, racing well (strategy)**. Today: rules (lane keep / fastest / overtake / alternate,
  pit auto / now / off). Next: an RL strategist on top of the MPC, using the same interfaces.

## Phases

| Phase | State | What is left |
|---|---|---|
| 0. CU control (`C` command, stock V338/V339) | done in sim; Hardware test bench (System → Hardware test) | validate on the real CU: latency, hand-back (~750 ms), lane bit |
| 0b. Firmware in an emulator | prototype only, `firmware/emu/` untracked, stopped before any result | optional: finish the simavr harness and the differential tests vs the virtual CU |
| 1. Localisation (`trackloc/`) | done in sim: map ±1 %, ~2 cm visible, 0 identity swaps (synthetic benchmark); CLI + `serve` page, preview, roll call | real camera: frame rate, latency, reflections, lighting; tune from Sunday's recordings |
| 2. Pilot v1 | `corners` (default) and `mpc` in the brain's pilot registry. Virtual CU, `python racelab/bench_pilots.py`, 20 laps, 0 off everywhere: defaults corners 6.6 s, mpc 7.3 s; `f_max=1.5` corners 5.7 s, mpc 6.2 s; `a_lat=18` corners 5.55 s, mpc (4 iterations, 96 samples, horizon 2.5 s, sigma 0.02, risk 1) 5.8 s | why mpc trails corners at the same grip (suspects: `BRAKE_REACH`, the risk penalty, slow grip learning, lap 1); calibrate `v_max`, `tau_acc`, `tau_brake`, delay, grip on the real car; choose the default |
| 3. Race brain (rules) | lane and pit planners, manual "pit now" / "change lane" buttons, tested on the virtual CU | real car: does the lane request latch until the next changer? how does the CU refuel? |
| 4. Strategy v2 (RL) | **first strategist beats the rule brain on fresh seeds** (see `strategist/RESULTS.md`) | track randomisation, human-replay opponents, longer training, live use |

The default `a_lat` (10 m/s²) is half the simulator's limit (`A_MAX = 20` in `racelab/sim/track.py`),
and the default `f_max` (1.2) caps the grip learning: both pilots are cautious on purpose until the
real car is measured, and `A_MAX` itself may be optimistic for a real Carrera.

Research lab: private repo [CarlosMagni/slot-ai](https://github.com/CarlosMagni/slot-ai), its own 2D
simulation of Carrera tracks (numpy + torch, README in French; nothing imports it). One variant per
branch; v3 leaderboard, clean speed in m/s at risk 0 / 0.5 / 1:

| branch | driver | result |
|---|---|---|
| `master` | calibrated controller (reference), PPO v3 | controller 1.66 / 1.77 / 1.86 after ~20 calibration laps; PPO 1.49 / 1.55 / 1.61 |
| `mpc-cem` | sampling MPC (CEM), online estimation | 1.75 / 1.79 / 1.89 after ~12 calibration laps: the best |
| `residual-ppo` | PPO residual on the uncalibrated controller | 1.42 / 1.61 / 1.79 |
| `ppo-gru` | recurrent PPO | 1.19 / 1.30 / 1.43, under-trained |
| `bc-dagger` | imitation + DAgger + PPO | unfinished: trained, not on the leaderboard |

`racelab/src/racelab/bots/mpc.py` is a separate port of the lab's `mpc.py` (branch `mpc-cem`). PPO
learned to drive safely but 10-15 % slower than the controllers: RL goes into strategy, not into the
throttle.

## Strategy v2: the RL strategist (spec)

The pilot keeps the throttle. The strategist only picks among the brain's legal moves, a few times
per lap; the manager's safety gates (STOP, dead-man, link, lights) stay in front of everything it says.

- **Package**: a new add-on next to `trackloc/` (say `strategist/`) that imports `racelab`. RaceLab
  never imports or names it; when RaceLab lacks a hook, add a generic one to RaceLab.
- **Decisions**, at the finish line and before each changer / pit entry:
  - lane: stay, or change at the next changer (`BrainProfile.request("lane")`, op `bot.brain`);
  - pit: at the next pit entry or not (`request("pit")`, with `pit.policy` `off` so the rules do not
    pit on their own);
  - optional, pace: the mpc's `risk` from 3 levels. Gap: `bot.profile` builds a new profile, which
    drops the learned grip and the estimators; a live change needs a generic RaceLab op that updates
    parameters in place.
- **Observation**: laps done and left (or time left), fuel (0-15) and fuel per lap, lane, recent
  clean lap time per lane, gap and speed to the cars ahead and behind, whether the other lane is free
  at the next changer, stops made. Compute it from the snapshot only (`cars[]` fuel and laps,
  positions, `brain.fuel_per_lap`), the same code in the training environment and live: a `/ws`
  client cannot read the brain's internals.
- **Environment**: headless and faster than real time on `racelab.sim` (virtual CU and physics: fuel
  with `EMPTY_SCALE`, pit lane `PIT_SPEED` / `PIT_STOP_S`, lane changers, simulated humans that pit
  under `PIT_FUEL` and change lane with `LANE_CHANGE_PROB`), driving the real `BrainProfile`. The tick
  loop of `drive()` in `racelab/tests/test_corners.py` is the starting point. Randomise the track, the
  opponents' number and pace, the fuel use and the race length.
- **Reward**: the race result (finishing position, laps), minus running dry and deslots; per-lap
  shaping is fine.
- **Algorithm**: PPO with a small MLP (torch or numpy, a dependency of the add-on only); the lab's
  `rl.py` has a vectorised PPO loop to reuse. About one decision per lap keeps episodes short.
- **Baseline to beat**: the rule brain (`lane` `fastest` or `overtake`, `pit` `auto`) on the same seeds;
  report finishing position and race time over N races.
- **Runtime**: the add-on is a client of RaceLab's WebSocket API (`/ws`, see [api.md](api.md)): it
  reads the snapshot and sends `bot.brain`.
- **Tests** (it decides for a moving car): never two pit requests within `pit.min_laps`, nothing sent
  for a stopped or gated slot, the environment deterministic for a seed.

## Next steps, in order

1. **Sunday**: follow [hardware-test.md](hardware-test.md); record everything
   (`python -m trackloc record`, the Hardware test report, the three calibration captures).
2. **System identification** from the recordings: top speed per throttle, motor time constants,
   actuation delay, grip per corner, fuel per lap, pit stop time. Put the values in the pilots'
   defaults and in the simulator.
3. **Pilot**: tune the MPC on the real car (`risk`, `a_lat`, `sigma`) and decide the default.
4. **Brain**: fix whatever the real lane change and refuel show.
5. **Strategy v2**: the race environment, then the RL strategist ([spec](#strategy-v2-the-rl-strategist-spec)).

## Open decisions and known gaps

- UI wording left to the owner: rename STOP to "STOP BOTS", translate backend log lines,
  a bottom tab bar on phones, rename the "Course" tab.
- Escape engages STOP even while typing in a field (deliberate).

## Where things live

| What | Where |
|---|---|
| bot drivers, safety gates | `racelab/src/racelab/bots/manager.py` |
| profiles (`corners`, ...) | `racelab/src/racelab/bots/profiles.py` |
| race brain, pilot registry | `racelab/src/racelab/bots/brain.py` |
| MPC pilot | `racelab/src/racelab/bots/mpc.py` |
| pilot benchmark (virtual CU) | `racelab/bench_pilots.py` |
| research lab (its own sim, PPO, MPC) | [CarlosMagni/slot-ai](https://github.com/CarlosMagni/slot-ai), private |
| hardware test, connector cards, Origin/Host guard | `racelab/src/racelab/app.py`, `cu/bench.py`, `api/server.py`, `web/js/views/hwtest.js` |
| camera localisation | `trackloc/` (its README is the user guide) |
| field test plan | `docs/hardware-test.md`, `docs/fr/hardware-test.md` |
