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
| `meta` | | session types, bot profiles, race brain pilots (`pilots`), drivers, safety settings, track geometry (simulator) |
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
| `bot.profile` | `slot`, `profile: {kind, …}` | autopilot profile (Auto driver): `constant`, `target`, `rival`, `map`, `corners` or `brain` ([race brain](#race-brain-profile-brain)); the parameters and defaults of each kind are in `meta.profiles` |
| `bot.brain` | `slot`, `request: pit\|lane` | race brain only (refused otherwise): one-shot test request, `pit` = pit at the next pit entry, `lane` = change lane at the next changer; timed by the brain like its own moves, dropped by a new session, an E-STOP or a reconnection |
| `bot.params` | `slot`, `params: {risk}` | race brain only: live parameters applied in place (the MPC's `risk`, 0..1); everything learned is kept, unknown keys are refused |
| `estop` / `estop.clear` | | emergency stop: throttle 0 on every bot |
| `mode.set` | `mode: classic\|advanced` | outside a session only |
| `car.program` | `slot`, `speed?`, `brake?`, `fuel?` (0..15) | program a car (outside a session) |
| `car.program_live` | `field?`, `level`, `current?` (1..10) | set the level of the car on the track with the CU keys (SPEED/BRAKE/FUEL + START; the way that works on hardware 8) |
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
| `connector.set` / `connector.clear` | see [Connector card](#connector-card) | show or remove this connection's card in the UI |
| `tower.reset` | | clear the position tower display |
| `log` | | the recent log entries (also sent in `hello`) |
| `console.send` | `frame` | (advanced mode) send one raw frame, e.g. `"?"`; returns the CU's answer. `G`, `E`, `F`, `C` and `B` are refused |
| `console.trace` | `polls` (bool) | include the `?` polls in the raw-frame subscription |
| `cu.scan` | `timeout?` (2..15 s, default 5) | `{devices: [{transport: ble\|serial, device, label}], errors: […]}` |
| `cu.connect` | `transport: none\|ble\|serial\|sim`, `device?`, `stock?` | switch CU (outside a session): bots are stopped and handed back on the old one first; the choice is remembered |
| `app.quit` | | stop RaceLab: bots cut and handed back (refused during a firmware update) |
| `link.bench` | `n?` (1..300, default 100) | hardware test: times `n` round trips of the CU's poll (nothing that can move a car); the `C` acknowledgement time comes from the frames the link sends, plus an idle "every slot on its controller" frame when no bot is active (never a frame that changes a bot). Returns `{n, answered, lost, min_ms, median_ms, p95_ms, max_ms, mean_ms, jitter_ms, histogram: [{from_ms, to_ms, count}], c_ack: {n, min_ms, median_ms, p95_ms, max_ms, mean_ms, jitter_ms} \| null, duration_ms, timed_out, transport, bot_transport}` (`timed_out`: the wait ended before `n` answers, the partial data is returned). p95 = nearest rank; jitter = mean absolute difference between consecutive samples |
| `test.pulse` | `slot`, `throttle` (1..6), `ms` (200..2000) | hardware test: the server takes the slot as a bot, holds the throttle for `ms` (clock started once the CU link applied it), then forces 0 and hands the slot back, in a `finally` (also on error, cancellation or a closed client). Out-of-range values are refused, never clamped; `[safety] max_throttle` can only lower the throttle. Returns `{slot, throttle, ms, acked, held_ms, applied_peak, gate, aborted: null\|estop\|link\|session, zeroed, released}` |
| `test.lane` | `slot`, `throttle?` (1..6, default 3), `ms?` (200..2000, default 2000) | like `test.pulse`, plus one lane request through the usual lane path once the throttle is applied; adds `lane_sent` |
| `test.handback` | `slot`, `throttle?` (1..6, default 3), `pause_ms?` (1000..2500, default 1500) | C transport only. Holds a throttle, then pauses the `C` heartbeat for `pause_ms` so that the CU's ~750 ms expiry hands the slot back (the app cannot observe it: the tester confirms). Then 0, heartbeat back, hand-back. The pause is a deadline in the link (3 s ceiling): it ends by itself, on E-STOP, on link loss and on any error, and when it ends by itself the bots are zeroed, never resumed. Refused unless every slot is free of bots. Returns `{slot, throttle, pause_ms, paused_ms, acked, aborted, zeroed, released}` |
| `test.report` | `report: {…}` (object, 200 kB max) | saves the report as `<data dir>/hardware-tests/hwtest-<date>.json` (name chosen by the server) after adding a `server` section (version, link, safety settings). Returns `{name, path, report}` |

The `test.*` ops are refused (error reply) during a running session, under E-STOP,
outside With bots mode, on a CU without bot support, on a slot already used as a bot
or owned by an API agent, on a disabled driver, and while another `test.*` op or
`link.bench` runs. `link.bench`, `test.pulse`, `test.lane` and `test.handback` wait for
their end: over the WebSocket they are answered in the background, so `estop` sent
meanwhile is processed at once (it ends the test early with `aborted: "estop"`).
While a `test.*` op runs, `bot.*` ops on its slot are refused (the throttle cap cannot be bypassed from another page). While one runs, `state.hwtest` is `{kind, slot, phase}` (`null` otherwise) and `hwtest`
events carry the phases (`run`, `paused`, `done`).

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
            "lane_pending": false, "profile": {…},
            "drive": null,   // corners profile (or a brain's corners pilot): "map" (from the map) | "pace" (fallback), else null
            "brain": null}], // race brain: {intent_key, intent_args, …}, see below; null for the other profiles
  "cars": [{"slot": 0, …, "controller": "api"}],   // human | bot | api
  "connectors": [{"agent": "camera", "name": "Camera", "ui_url": "http://…", "status": "tracking"}],
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

Autopilot `corners` ("Corner speed (map)") is a built-in Auto profile that drives from
`positions` and the track shape, with the virtual CU or any connector; it falls back to the
`target` behaviour when they are missing or older than 500 ms. Its parameters: `a_lat`
(m/s², start), `margin_pct`, `cap` (highest throttle), `latency_s` (0.1), `v_max`,
`tau_acc`, `tau_brake` (motor model), `f_max` (highest per-corner speed factor learned, default 1.2, hard ceiling 1.5: raise it once the real car's grip is measured), plus the `target` ones used by the fallback. When the positions go stale, the output never exceeds the last position-driven throttle for 1 s before falling back to target pace.

### Race brain (profile `brain`)

"Race brain" ("Cerveau de course") is an Auto profile made of a **pilot** (the throttle), a **lane
planner**, a **pit planner** and a small arbiter ([`bots/brain.py`](../racelab/src/racelab/bots/brain.py)):

```jsonc
{"kind": "brain",
 "pilot": "corners",            // a name from meta.pilots ("corners", "target")
 "pilot_params": {"cap": 8},    // that pilot's own parameters (the profile of the same name)
 "lane": {"policy": "keep",     // keep | fastest | overtake | alternate
          "every": 1,           // alternate: a change every N laps
          "gap_m": 0.8,         // overtake: a slower car ahead in our lane, closer than this
          "clear_m": 0.4,       // a change needs the target lane free this far around the car
          "hysteresis_pct": 2.0, // fastest: the other lane must be this much faster (best clean laps)
          "d_min_m": 0.15, "d_max_m": 2.0, "latency_s": 0.25},  // request timing, see below
 "pit": {"policy": "auto",      // off | auto | now
         "reserve_laps": 2.0,   // auto: pit when the fuel left after the next lap < reserve × fuel per lap
         "last_laps": 1,        // auto: never in the session's last N laps (nor when the fuel is enough to finish)
         "min_laps": 5,         // auto: never two stops within N laps
         "throttle": 4,         // throttle cap in the pit lane
         "max_stop_s": 12.0,    // longest wait at the stop for a full tank
         "zone": null,          // [entry, exit] in lap fractions from the line; null = the track map's `pit`
         "lane": null}}         // lane the pit entry is taken from (0 | 1); null = either
```

- **Pilot**: `meta.pilots` is the registry (`PILOTS` in `brain.py`): `[{name, label, label_key,
  available, reason, params: [{key, label_key, min, max, scale?}], defaults}]`. The UI builds its pilot
  selector and the pilot's few fields from it. A new pilot is one module exposing `PILOT` plus one
  registry line (`"mpc": load_pilot("mpc")` for `bots/mpc.py`); if its module cannot be imported (a missing
  dependency) it is listed with `available: false` and the reason. In the pit lane (the CU's
  `cars[].pit`) the pilot is capped at `pit.throttle`, held at 0 at the stop (the middle of the pit
  zone; without a usable position, 0.5 s after the pit flag) until the tank reads 15 or `max_stop_s`,
  then leaves at the cap.
- **Arbiter**: the manager's gates (E-STOP, link, lights, session, finished) come first and are
  unchanged, then pit (planned or requested) > a requested lane change > overtake > fastest lane >
  alternate > keep.
- **Timing**: a lane request goes out at most once per lane changer / pit entry pass, when that point is
  between `d_min_m + v × latency_s` and `d_max_m` (at least 0.5 m wide) ahead and the previous point is
  0.3 m behind, never in the pit lane. With the CU's pit lane on, the pit entry takes the request like a
  changer: a lane request is never sent with the pit entry next. Fractions: the map's `changers` / `pit`
  are along the drawn path, `pit.zone` and positions' `s` from the finish line.
- **Pit planner**: the fuel per lap is measured from `cars[].fuel` over the session's tanks (the first
  estimate after 2 laps). `now` pits once at the first opportunity, again after a new session, an E-STOP
  or a reconnection. Pit stops need `cu.fuel_mode` and `cu.pit_lane`.
- **Without positions** (missing, older than 500 ms, or without `v`), a map, `length_m`, changers or a pit
  zone, the planners that need them stop and say why; the pilot falls back like `corners` does. The brain
  never guesses a position to trigger a lane change or a pit entry.

`state.bots[].brain` (`null` for the other profiles):

```jsonc
{"intent_key": "brain.intent.pit", "intent_args": {"fuel": 3},   // a UI dictionary key + its values
 "pilot": "corners", "lane_policy": "alternate", "pit_policy": "auto",
 "next": "pit",              // what the next request will be: "pit" | "lane" | null
 "fuel_per_lap": 0.74,       // measured, null until known
 "lane_why": null, "pit_why": "brain.why.positions"}   // why a planner is stopped (dictionary key), else null
```

`intent_args` may carry `why` (a dictionary key, shown after the intent), `slot` (the car being
passed), `lane` (1 or 2), `fuel` (0..15) and `n` (laps).

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
- `length_m`: lap length in metres (a number: the centre line; or `[lane 0, lane 1]`
  like the virtual CU). Optional, but the `corners` autopilot (curvature in metres) needs it:
  without it that profile holds its target lap time instead.
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

### Connector card

Any connector (a camera program, light gates...) can show up in the UI with a link to
**its own page**. RaceLab never renders that page: the UI shows a chip (name, status, an
"Open" link opening a new tab) in the Race view's track panel header and in System.
Independent of `track.*`: a connector without a track can have a card, and it is **not**
refused with the virtual CU.

| op | arguments | effect |
|---|---|---|
| `connector.set` | `name` (1..40 chars), `ui_url?` (http/https only, <= 300 chars), `status?` (<= 80 chars) | set this connection's card (replaces the previous one); returns `{agent, name, ui_url, status}` |
| `connector.clear` | | remove this connection's card |

- The UI adds `lang=en|fr` (its current language) to the `ui_url` query, keeping the
  existing query, so a connector page can follow the UI language.
- One card per agent connection. It is **removed automatically when that agent
  disconnects** (same as the `track.*` ownership).
- Text is trimmed and must not contain control characters. Over-long values and any
  `ui_url` that is not `http://` or `https://` with a host (`javascript:`, `data:`,
  `file:`, relative URLs...) are refused, never truncated; a refused call keeps the
  previous card.
- `state["connectors"]`: `[{agent, name, ui_url, status}]` (empty list when none), and a
  `connectors` event `{connectors: [...]}` on every change.
- Python: `RaceLabClient.set_connector(name, ui_url=None, status=None)` and
  `clear_connector()` (same on `SyncClient`).

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

## Security

The server drives real cars, so any web page open in your browser must not be able to
reach it. Two checks run on every request, before the token:

- **Host** (DNS rebinding): the `Host` header must be an IP address, `localhost`, the
  `[app] host` the server is bound to, or a name in `[api] allowed_hosts`
  (e.g. `["pit-pc.local"]` to reach a LAN machine by name). Otherwise `403`.
- **Origin** (cross-site requests, `/ws` and `/api/*`, GET included): a request that
  carries an `Origin` header (every browser page does) must be same-origin: same
  scheme, host and port as the request's own `Host`. Otherwise `403`. The UI is served
  by the server itself, so it always passes, including as a `RaceLab.app` window and
  from a tablet on the LAN (`[app] host = "0.0.0.0"`, open `http://<LAN IP>:8130`).
  Use the same name in the address bar as in `allowed_hosts`.
- A request **without** `Origin` (the Python client, curl, scripts) is not a browser
  page and is allowed.
- `[api] allowed_origins = ["http://dash.example:3000"]` lists foreign pages that may
  connect anyway (default: none). It is meant for WebSocket clients; no CORS headers are
  sent, so a foreign page cannot read REST replies. Behind a TLS proxy, list the
  `https://` origin here too.

`[api] token` is separate and still applies on top of these checks.

## Python client

See [`racelab/src/racelab/client/`](../racelab/src/racelab/client) and
[`racelab/examples/`](../racelab/examples): `RaceLabClient` (asyncio),
`SyncClient` and `RaceLabEnv` (Gymnasium interface: `reset()` / `step()`).
