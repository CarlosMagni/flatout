# RaceLab API

One API for both the web UI and agents (RL, scripts): JSON over WebSocket,
with a REST mirror for simple scripts. By default the server listens on
`127.0.0.1:8130` only; set `[api] token` in the TOML file to require a token.

- Driving (`bot.*`) only exists in **advanced mode** (shown as "With bots" in the UI),
  on a CU that takes software drivers: a stock V338/V339 (`C` command) or one with the bot
  firmware module (`B`), see [bot-protocol.md](bot-protocol.md).
- An agent can only drive the slots it has claimed (`bot.claim`).
- **Dead-man switch**: an agent that sends no action for `[safety] deadman_ms`
  (600 ms by default) has its throttle forced to 0. An agent that disconnects
  has its slots dropped to 0 (the bot keeps control of the slot).
- **E-STOP** (the `STOP` button in the UI): throttle 0 everywhere, and agents lose
  their claims. After it is cleared (by hand, in the UI), an agent has to
  `bot.claim` again: nothing restarts by itself at its old throttle.
- Bots only run during a running session (`running` / `finishing`), never
  during the start lights, never under E-STOP. An agent can pass `free: true`
  to `bot.claim` to drive outside a session (test bench).

## WebSocket `ws://localhost:8130/ws`

Query parameters: `agent=<name>` (the agent's identity, default `ui-N`; if the
name is already taken the server appends `#2`, `#3`…: the name actually used
is `hello.client`),
`state_hz=<n>` (state rate, 0.5..20, default 10), `token=<token>`.

### Messages received

```jsonc
{"type": "hello", "client": "rl-01", "meta": {…}, "state": {…}, "log": […]}  // on connection
{"type": "state", "data": {…}}                         // full state, at state_hz
{"type": "event", "name": "lap", "data": {…}, "t": 1234.5}
{"type": "reply", "id": 7, "ok": true, "result": …}    // reply to a command
{"type": "reply", "id": 8, "ok": false, "error": "slot 2 not claimed by this agent"}
{"type": "raw", "dir": "tx", "data": "B07", "t": …}    // raw frames (console subscription)
```

### Commands sent

`{"id": <n>, "op": "<op>", ...arguments}` — `id` is returned in the reply
(so `id` and `op` are reserved: no argument may use these names).

| op | arguments | effect |
|---|---|---|
| `state` | | full state (same content as the `state` messages) |
| `meta` | | session types, bot profiles, drivers, safety settings, track geometry (simulator) |
| `subscribe` | `state_hz?`, `raw?` | change the rate / subscribe to raw frames |
| `race.setup` | `settings: {type, laps, duration_s, elim_every, episode_laps, rival_slot, rival_margin_pct, title}` | new session (state `grid`) |
| `race.start` / `race.pause` / `race.resume` / `race.stop` | | session control (including the CU start lights) |
| `cu.press` | `button: pace\|start\|speed\|brake\|fuel\|code` | simulate a CU button |
| `drivers.update` | `drivers: [{slot, name?, color?, enabled?, car?}]` | drivers |
| `bot.claim` | `slot`, `free?` | claim the slot for this agent (turns it into a bot, throttle 0) |
| `bot.act` | `slot`, `throttle?` (0..15), `lane?` (bool) — or `actions: [{slot, throttle?, lane?}, …]` | agent action; refreshes the dead-man switch (without `throttle` it only refreshes: the throttle is unchanged) |
| `episode.reset` | `slot` | `training` session (AI training): new episode for this slot; the lap in progress (the one containing the stop) does not count. Returns the episode number |
| `bot.unclaim` | `slot` | give the slot back to the physical controller |
| `bot.take` | `slot`, `driver: manual\|profile\|api` | (UI) turn a slot into a bot; `manual` = Manual, `profile` = Auto, `api` = AI |
| `bot.release` | `slot` | give the slot back to the physical controller |
| `bot.throttle` | `slot`, `value` | (UI) throttle of the manual driver |
| `bot.lane` | `slot` | one-shot lane change |
| `bot.profile` | `slot`, `profile: {kind, …}` | autopilot profile (Auto driver) |
| `estop` / `estop.clear` | | emergency stop: throttle 0 on every bot |
| `mode.set` | `mode: classic\|advanced` | outside a session only |
| `car.program` | `slot`, `speed?`, `brake?`, `fuel?` (0..15) | program a car (outside a session) |
| `laps` | `slot?` | all the laps of the session |
| `results` | | ranking + laps |
| `sessions.list` / `sessions.get` | `session_id` | recorded sessions (also `GET /api/sessions/<id>`) |
| `sim.drop_link` | `seconds` | simulator: cut the link (watchdog test) |
| `firmware.images` | | available `.HMF` images (the ones you imported): `{id, name, label, tag, lines, size, sha256, source, status, issue, warning}` — `tag` and `status` (`recommended`, `stock`, `superseded`, `defective`, `unknown`) come from `firmware/manifest.json`, decided by the file's SHA-256 only (never its name) |
| `firmware.build_status` | | `{state, log, available, missing}`: `state` is `idle`, `building`, `done` or `failed`; `missing` lists the manifest tags not built yet |
| `firmware.build` | | starts building the images from source (`firmware/build.py`) in the background; progress in the `firmware_build` event and the state's `firmware_build` |
| `firmware.flash` | `image` (id), `confirm: "FLASH"`, `recovery?` | flash the CU (outside a session; refused for a `defective` image); progress in the `firmware` events and in `state.firmware` |
| `firmware.abort` | | interrupt the flash (the CU stays in its bootloader: run again with `recovery: true`) |
| `firmware.status` | | `{state: idle\|running\|verifying\|done\|failed, stage, done, total, eta_s, error, result}` |
| `track.set` / `track.positions` / `track.clear` | see [Track connector](#track-connector-track-shape--positions) | publish the track shape and car positions |
| `tower.reset` | | clear the position tower display |
| `log` | | the recent log entries (also sent in `hello`) |
| `console.send` | `frame` | (advanced mode) send one raw frame, e.g. `"?"`; returns the CU's answer. `G`, `E`, `F`, `C` and `B` are refused |
| `console.trace` | `polls` (bool) | include the `?` polls in the raw-frame subscription |
| `cu.scan` | `timeout?` (2..15 s, default 5) | `{devices: [{transport: ble\|serial, device, label}], errors: […]}` |
| `cu.connect` | `transport: none\|ble\|serial\|sim`, `device?`, `stock?` | switch CU (outside a session): bots are stopped and handed back on the old one first; the choice is remembered |
| `app.quit` | | stop RaceLab: bots cut and handed back (refused during a firmware update) |

Importing an image: `POST /api/firmware/upload?name=<file>.HMF` with the raw
file as the body. During a flash, every command other than reads and
`firmware.*` is refused.

### State (`state`)

See [architecture.md](architecture.md) §4 for `session`, `cu`, `cars`. In addition:

```jsonc
{
  "t": 1234.56,                 // server clock (s, monotonic)
  "mode": "advanced",
  "estop": false,
  "link": {"state": "connected", "transport": "ble", "version": "5339",
           "bots_supported": true,   // software drivers available on this CU
           "bot_transport": "C",     // "C" (stock CU) | "B" (bot firmware module) | null
           "bot_module": false,      // the bot firmware module answered
           "rtt_ms": 21.4, "rx_per_s": 38.0, "error": null, …},
  "bots": [{"slot": 0, "active": true, "driver": "api", "throttle": 7, "requested": 7,
            "applied": 7, "gate": null, "agent": "rl-01", "api_age_ms": 40,
            "lane_pending": false, "profile": {…}}],
  "cars": [{"slot": 0, …, "controller": "api"}],   // human | bot | api
  "positions": {"source": "sim", "age_ms": 0, "stale": false, "cars": [{"slot": 0, "s": 0.42, …}]},
  "sim": {"t": 12.3, "phase": 0, "cars": [{"slot": 0, "s": 0.42, "lane": 0, "v": 2.1, "off": false, …}]}
}
```

`cars[].since_cross_ms` = time since the last line crossing, computed when the
state is emitted; `null` until the car has crossed the line in the session.
`lane: true` arms a lane change at the next switch (one-shot on the firmware
side); sending it again before the switch changes nothing more.

`positions`: car positions, provided by the virtual CU or by a track connector
(see below); `null` without a source. `sim`: details specific to the simulator
(only present with the virtual CU).

`gate` explains a throttle forced to 0: `estop`, `link`, `lights`, `session`,
`finished`, `agent`, `deadman`. On the real track, `positions` only exists if
a track connector is running: an agent that uses it must be able to drive
without it (`null` or `stale`), from `cars` (line crossings, lap times) and
the clock.

### Events useful to an agent

`crossing` (raw line crossing, `cu_ts` in ms on the CU clock), `lap`,
`session`, `phase`, `episode_end` / `episode_reset` (`training` session),
`bots_reset` (reconnection: throttle set back to 0), `agent_lost`
(disconnection, or `reason: "estop"`), `estop`.

On the real track there is no geometry (`meta.track` is `null`): an agent's
reference lap time has to be calibrated on the first laps (or on
`session.best`).

## Track connector (track shape + positions)

On the real track the CU only reports line crossings. A **connector** (a
camera above the track, an estimator, another program) can provide the shape
of the circuit and the position of the cars; RaceLab publishes them in
`state["positions"]`, in the same format as the virtual CU. An agent written
for the simulator therefore reads the same fields on the real track.

| op | arguments | effect |
|---|---|---|
| `track.set` | `geometry: {points, y_axis?, finish?, pit?, changers?, lane_offset?, name?, length_m?}` | declare the shape; returns the normalised shape |
| `track.positions` | `cars: [{slot, s, lane?, v?, off?}]` | current positions, at the connector's rate (20-60 Hz) |
| `track.clear` | | remove the shape and the positions |

Conventions:

- `points`: closed centre line, in the driving direction, in the connector's
  units (pixels of a top-down image, centimetres…), 8 to 4000 points.
  `y_axis: "down"` for image coordinates. RaceLab maps everything into [0, 1]²,
  y axis pointing up.
- `finish`, `pit: [start, end]`, `changers`: fractions **along the path**,
  starting from the first point.
- `s` of the cars: fraction of a lap **from the finish line** (0 = line), like
  the CU timing. `lane`: 0 = left lane in the driving direction. `v` in m/s,
  `off` = off the track (both optional).
- `lane_offset`: distance between the centre line and a lane, in the units of
  the points.
- One connector at a time: the one that declared the track owns it. **If it
  disconnects, the shape and the positions disappear** (and so does the map in
  the UI); without a shape there is no map, but positions that are sent are
  still published.
- Positions older than one second: `stale: true` (the map greys them out, the
  RL environment ignores them).
- With the virtual CU, the CU itself provides the track: `track.*` is refused.

```jsonc
// state["positions"]
{"source": "camera", "age_ms": 32, "stale": false,
 "cars": [{"slot": 0, "s": 0.42, "lane": 1, "v": 2.3, "off": false}]}
```

A `track` event (`{geometry}`, `null` when the track is removed) is emitted on
every shape change. Full example:
[`racelab/examples/track_connector.py`](../racelab/examples/track_connector.py)
(positions estimated from the CU's lap times; a model for a camera connector).
On the client side: `RaceLabClient.set_track()`, `send_positions()`,
`clear_track()`.

## REST

```bash
curl localhost:8130/api/state
curl -X POST localhost:8130/api/race.setup -d '{"settings": {"type": "training", "episode_laps": 5}}'
curl -X POST localhost:8130/api/race.start
curl -X POST -H 'X-Agent: script' localhost:8130/api/bot.claim -d '{"slot": 0, "free": true}'
curl -X POST -H 'X-Agent: script' localhost:8130/api/bot.act -d '{"slot": 0, "throttle": 6}'
curl -X POST localhost:8130/api/estop
```

Reply: `{"ok": true, "result": …}` or `{"ok": false, "error": "…"}` (HTTP 400).
With REST the agent's identity is the `X-Agent` header; the dead-man switch
applies in the same way (send `bot.act` again at least every 600 ms).

## Python client

See [`racelab/src/racelab/client/`](../racelab/src/racelab/client) and
[`racelab/examples/`](../racelab/examples): `RaceLabClient` (asyncio),
`SyncClient` and `RaceLabEnv` (Gymnasium interface: `reset()` / `step()`).
