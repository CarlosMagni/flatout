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
| 2. Pilot v1 | `corners` (default) and `mpc` in the brain's pilot registry; virtual CU: corners 5.7 s/lap, mpc 6.2 s/lap, 0 off | calibrate `v_max`, `tau_acc`, `tau_brake`, latency, grip on the real car; choose the default |
| 3. Race brain (rules) | lane and pit planners, manual "pit now" / "change lane" buttons, tested on the virtual CU | real car: does the lane request latch until the next changer? how does the CU refuel? |
| 4. Strategy v2 (RL) | not started | fast headless race env on `racelab.sim` (fuel, pit, changers, humans); PPO strategist with per-lap decisions; rule brain as the baseline to beat |

Research lab, outside this repo: `~/Desktop/slot-ai` (local git) and the variant copies
`~/Desktop/slot-ai-{mpc,residual,gru,bc}`. The v3 leaderboard there: MPC 1.75 / 1.79 / 1.89 m/s
at risk 0 / 0.5 / 1, against 1.66 / 1.77 / 1.86 for the calibrated controller, with about 12
calibration laps instead of 20. PPO is safe but 10-15 % slower. The `bc` variant is unfinished.

## Next steps, in order

1. **Sunday**: follow [hardware-test.md](hardware-test.md); record everything
   (`python -m trackloc record`, the Hardware test report, the three calibration captures).
2. **System identification** from the recordings: top speed per throttle, motor time constants,
   actuation delay, grip per corner, fuel per lap, pit stop time. Put the values in the pilots'
   defaults and in the simulator.
3. **Pilot**: tune the MPC on the real car (`risk`, `a_lat`, `sigma`) and decide the default.
4. **Brain**: fix whatever the real lane change and refuel show.
5. **Strategy v2**: the headless race environment, then the RL strategist (step 4 of the table).

## Open decisions and known gaps

- UI wording left to the owner: rename STOP to "STOP BOTS", translate backend log lines,
  a bottom tab bar on phones, rename the "Course" tab.
- Escape engages STOP even while typing in a field (deliberate).
- CI has not run yet with `trackloc` and the numpy dependency.

## Where things live

| What | Where |
|---|---|
| bot drivers, safety gates | `racelab/src/racelab/bots/manager.py` |
| profiles (`corners`, ...) | `racelab/src/racelab/bots/profiles.py` |
| race brain, pilot registry | `racelab/src/racelab/bots/brain.py` |
| MPC pilot | `racelab/src/racelab/bots/mpc.py` |
| hardware test, connector cards, Origin/Host guard | `racelab/src/racelab/app.py`, `cu/bench.py`, `api/server.py`, `web/js/views/hwtest.js` |
| camera localisation | `trackloc/` (its README is the user guide) |
| field test plan | `docs/hardware-test.md`, `docs/fr/hardware-test.md` |
