# RaceLab agents: examples

An agent (script, bot, reinforcement learning) drives a car through RaceLab's
WebSocket API ([`docs/api.md`](../../docs/api.md)). The Python client lives in
[`src/racelab/client/`](../src/racelab/client): `RaceLabClient` (asyncio),
`SyncClient` (blocking) and `RaceLabEnv` (Gymnasium interface).

## Getting started

```bash
cd racelab
pip install -e ".[rl]"        # `[rl]` = gymnasium + numpy, optional (without them: plain lists and minimal spaces)
racelab --sim --no-browser    # server + virtual CU on ws://localhost:8130/ws
```

Without `--no-browser`, the web UI opens on http://localhost:8130, where you can
see the agent's car. Advanced mode (the default) is required for driving.

In another terminal:

```bash
python examples/rl_random_agent.py --episodes 3 --laps 2   # random throttle, through RaceLabEnv
python examples/pace_agent.py --target 7.5                 # holds a lap time, through RaceLabClient
```

`pace_agent.py`: a proportional controller on the `lap` events, an `act` loop
every 0.2 s, throttle 0 on exit (Ctrl+C included). `rl_random_agent.py`: a
skeleton `reset()` / `step()` loop to replace with a policy.
[`track_connector.py`](track_connector.py) is a track connector: it publishes a
track shape and car positions estimated from the CU's lap times (see
[`docs/api.md`](../../docs/api.md#track-connector-track-shape--positions)).

## The client in a few lines

```python
from racelab.client import RaceLabClient, SyncClient, RaceLabEnv

async with RaceLabClient("ws://localhost:8130/ws", agent="rl-01") as c:
    await c.claim(0)                       # claim car 0
    await c.act(0, throttle=6)             # throttle 0..15; repeat at least every ~300 ms
    state = c.state                        # latest state (cars, bots, session, link, sim…)
    async for ev in c.events("lap"):       # or c.on_event("lap", callback)
        print(ev.data["ms"])

with SyncClient(agent="rl-01") as c:       # same API, without asyncio (notebooks)
    c.claim(0); c.act(0, 6)

env = RaceLabEnv(slot=0, episode_laps=5)   # Gymnasium: obs, info = env.reset() ; env.step(action)
```

`RaceLabEnv` in brief (details in the docstring of `racelab/client/env.py`):

| | |
|---|---|
| action | `Discrete(16)`: the throttle 0..15 (`allow_lane=True`: `Discrete(32)`, `+16` = change lane) |
| observation | 10 floats in [0, 3], only what the real CU provides: time since the last line crossing, last and best lap (÷ reference lap `ref_lap_ms`), throttle, fuel, pit stops, laps in the episode, lane change in progress, `gate` (throttle forced to 0), suspected derailment |
| reward | `ref_lap_ms / lap_time` per completed lap; +0.01 × throttle/15 per step (except on suspected derailment or forced throttle); −1 on each suspected derailment. Replaceable: `reward_fn=` |
| end of episode | `terminated`: `episode_laps` timed laps; `truncated`: `max_steps`, session no longer running, or E-STOP |
| rate | `control_hz=13.3` (75 ms, one rails cycle of the CU) |

Rule of thumb: set `ref_lap_ms` to a good lap time of the track (6000 ms for the
simulator). It normalises the observation and the derailment threshold (1.5 × this time).

## Safety

- **Dead-man switch**: with no `act` for `deadman_ms` (600 ms), the server forces
  the throttle to 0. An agent loop must therefore act at least every ~300 ms.
  Neural-network updates belong between episodes (the car is stopped then): if they
  take more than 600 ms in the middle of an episode, the car stops.
- **`keepalive=True`** (`RaceLabClient`, off by default) refreshes the dead-man switch
  every 0.25 s while the agent is thinking, but only if `act()` was called less than
  2 × `deadman_ms` ago: a frozen loop still cuts the car (at most 1.8 s after its
  last action). It never resends a throttle.
- **Gates (`gate`)**: the throttle is forced to 0 on E-STOP (`estop`), lost CU link
  (`link`), start lights (`lights`), a session that is not running (`session`), a car
  that has finished (`finished`), a silent agent (`deadman`). Bots only run in a
  `running` / `finishing` session, except with `claim(slot, free=True)` (test bench,
  no laps counted). `state["bots"][i]["gate"]` gives the current reason.
- **E-STOP**: `client.estop()` sets every bot to 0. The client does not offer
  `estop.clear` (a human decision, a button in the UI). `RaceLabEnv` truncates the
  episode under E-STOP and `reset()` waits for it to be cleared.
- **Disconnection**: the server sets the throttle of a disconnecting agent to 0.
  `close()` does not hand the car back to the controller; `close(unclaim=True)` (and
  `RaceLabEnv.close()`) does so after a throttle 0.
- Ctrl+C: throttle 0, then close (see `pace_agent.py`). An E-STOP is not needed.

## From the simulator to the real track

1. `racelab` (without `--sim`) with the CU over Bluetooth, bot firmware flashed,
   Advanced mode. Same URL, same code: the client does not change.
2. **Positions**: `RaceLabEnv(positions=True)` adds position, lane, speed and
   off-track, read from `state["positions"]`. With the virtual CU they come from the
   simulator; on the real track you need a **track connector** (camera…, see
   [`docs/api.md`](../../docs/api.md)). Without a connector, or if its positions are
   stale, these components are 0 and `pos_ok` = 0: an agent that uses them must be
   able to drive without.
3. Recalibrate `ref_lap_ms` on the real track.
4. A real derailment lasts until a human puts the car back; in the simulator the car
   restarts by itself after 2 to 4 s. Plan for someone at the trackside, and for short
   episodes (`episode_laps`, `max_steps`).
5. Bluetooth latency and jitter are higher than in simulation: watch
   `info["overrun_ms"]`.
6. Start with a low throttle (the global `[safety] max_throttle` in the TOML file
   caps the throttle).
