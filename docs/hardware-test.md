# First field test: `C` command, camera, map autopilot

A numbered checklist for the first real-hardware session on a Carrera Digital 124 set with 4 cars. It
validates three things that so far only ran against the virtual CU and synthetic images:

1. the stock CU's `C` command over AppConnect Bluetooth (no firmware flash);
2. the camera localisation (`trackloc`, an iPhone through Continuity Camera);
3. the autopilot that drives from the map (the **Corner speed (map)** profile).

Do it in this order: each part assumes the previous one passed. Everything is done from the two web pages
(RaceLab on `http://127.0.0.1:8130/`, trackloc on the address `serve` prints); the CLI is only the fallback,
shown in *Fallback* lines. Tick the boxes and write the numbers down for the report (part 10).

**Rules for the whole day**

- Keep a hand on **STOP** in RaceLab (or `Esc`) and the other within reach of the track's power supply.
  STOP in trackloc only ends *its* action and hands the cars back: it is not an E-STOP.
- Never hold a controller on a slot that the software drives, except in the bench test of step 1.
- One thing moves at a time. If anything surprises you: STOP, note the time, keep the log (System → Log), and go on
  only when you understand it.
- Record video during every moving test from step 4 on (trackloc *Record*, or a *Run* with `--record`). A bad
  result with a recording can be analysed at home; without it, it is only a story.

## 0. Pre-flight at home

- [ ] Repository up to date, virtual environment activated in **every** terminal:
  `source .venv/bin/activate` (`python -m trackloc` and `racelab` need it).
- [ ] `racelab --sim --stock-firmware --no-browser` starts, the page opens, System → Hardware test shows its
  five steps. Quit it.
- [ ] `cd racelab && ../.venv/bin/python -m pytest -q` and `cd trackloc && ../.venv/bin/python -m pytest -q` pass.
- [ ] Camera permission granted to the terminal app you will use (System Settings → Privacy & Security → Camera).
  Check: `python -m trackloc probe` lists the iPhone (Continuity Camera needs the iPhone unlocked, nearby, same
  Apple ID, Wi-Fi and Bluetooth on) with a real resolution and frame rate.
- [ ] Marker printed: `python -m trackloc marker` writes `trackloc_marker.pdf`. Print at **100 %** (no "fit to
  page"), matt paper, glued flat on a board. **Measure the black square with a ruler** and write it down in
  millimetres (nominal 150): the metres of the map depend on it.
- [ ] In the bag: tape measure, masking tape, a stopwatch, a phone tripod or a rigid mount (1.5 to 2.5 m above the
  track), chargers, spare batteries for the controllers. Mac, iPhone and cars charged.
- [ ] The CU is on stock firmware V338 or V339 (System → Connection shows it once connected). Older: update it
  from System → Advanced options at home, not on the track.

## 1. Bench safety, car on blocks

Wheels in the air (a small box under the chassis), track power on. Start `racelab --ble --no-browser`, open the
page, choose the CU in System → Connection, switch to **With bots**.

- [ ] **E-STOP.** Start the car at a low throttle (a Hardware test pulse, or a Manual bot at 3). Press **STOP**, then
  repeat with `Esc`. PASS: the wheels stop at once both times, and nothing restarts when you clear STOP.
- [ ] **Dead-man.** Bot at 3, then `kill -9` the RaceLab process from another terminal (a clean `Ctrl-C` hands back
  by itself, only a kill tests the CU's own watchdog). PASS: within about 1 s the car is back on its controller (with
  no controller plugged in, it stops).
- [ ] **Bluetooth drop.** Bot at 3, then switch the Mac's Bluetooth off (or the CU). PASS: the car stops or returns to
  its controller within about 1 s, RaceLab shows the link lost. Switch it back on: RaceLab reconnects and the bots
  stay at 0 until you act (they never resume by themselves).
- [ ] Optional: the lane button over a switch makes one change per press.

**NO-GO**: any of the three does not stop the car. Do not go to the track. Note the CU version and the log.

## 2. The `C` command: System → Hardware test

One car (slot 1) on a **straight**, the others off the track, no session, bots on. The page has a sticky warning
bar with its own STOP. Do the steps in order. What PASS looks like:

- [ ] **1. Link check**: transport AppConnect Bluetooth, the CU version, bot transport `C`, bots supported. PASS:
  the line "the CU is connected and takes the stock C command". If it says B or none: stop, you are not testing `C`.
- [ ] **2. Latency**: 100 round trips, then 300. PASS (the panel decides): nothing lost, **p95 at most 150 ms**, no
  round trip above 400 ms. **Write down the p95, the median, the max and the C acknowledgement p95**: it is the BLE
  number we want from this day. If it fails, move the Mac closer to the CU and measure again; failing twice is a
  NO-GO for bots over Bluetooth (the CU's 750 ms rule leaves no room).
- [ ] **3. Throttle pulse**: slot 1, throttle 3, 1 s. PASS: the panel says the link applied the throttle, then 0, then
  handed back, **and you answer Yes** (the car moved, then stopped). Repeat at 4 and 6 (6 is the ceiling: the
  server refuses 7 and above, and durations outside 0.2 to 2 s, it never clamps). If it does not move at 3, raise
  one by one and note the lowest throttle that moves it.
- [ ] **4. Lane change**: the same pulse plus one lane request, started just before a lane changer. PASS: Yes, the
  car changed lane at the next changer.
- [ ] **5. Hand-back (CU watchdog)**: no controller held on that slot, every other slot free of bots. RaceLab holds a
  throttle, then stops refreshing the `C` frame for 1.5 s. PASS: the car stops (or its controller takes over)
  about 0.75 s after the pause starts, that is **between about 0.5 s and 1.0 s**, and you answer Yes. The panel cannot
  measure it: film the car and the screen with the iPhone in slow motion, or have someone call it with a stopwatch.
  Do it twice. If the car keeps going the whole 1.5 s, the CU's watchdog does not act: NO-GO for bots, report it.
- [ ] **6. Report**: write the track, car, Bluetooth distance and anything odd in Notes, then **Copy report**. It is
  also saved in the data folder (`hardware-tests/hwtest-<date>.json`, under `~/.racelab` by default).
- [ ] Once: press STOP during a test. PASS: the slot is zeroed and handed back, the chip says Interrupted.

## 3. Camera aiming: `trackloc serve`

Mount the iPhone 1.5 to 2.5 m above the track, looking down (40 to 90 degrees is fine), seeing the whole track with a
margin. Lay the marker flat on the table, in view, next to the track. Lock exposure, focus and white balance on the
iPhone (touch and hold the picture until AE/AF LOCK shows), and use a fixed 30 or 60 fps.

Start RaceLab first (connected), then the page, so the connector card appears:

```bash
python -m trackloc serve --camera iphone --port 8140     # open the address it prints (it ends with ?k=...)
```

RaceLab's Track panel header and System → Connectors show a chip "Camera (trackloc)" with an **Open** link to
the page (the printed address works too). In trackloc: **Open the camera**, enter the **marker size** in
millimetres (your measured value).

- [ ] The preview says "marker OK" and shows the real resolution and frame rate (note them; 1280x720 at 30 fps is
  enough).
- [ ] Coverage: the whole track is in the frame, no edge cuts it.
- [ ] No "whole picture keeps changing" warning (exposure, focus and white balance are locked).
- [ ] Rigid: the picture does not move when you tap the table.
- [ ] No light bands rolling across the picture (flicker): change or switch off the light if there are.
- [ ] No strong reflection on the rails, no cable or hand over the track.
- [ ] The marker is bigger than about 50 px on the image, flat, and not on the track.

*Fallback*: `python -m trackloc preview --camera iphone`, then `http://127.0.0.1:8140/` (or `--snapshot aim.jpg`).

## 4. Record first

Before anything is mapped, record 2 minutes with cars **driven by hand** (controllers, nothing driven by software):
it costs nothing and gives the first real footage, with the CU crossings on the same clock.

- [ ] trackloc page: **Record**, 120 s. Drive 1 car, then 2, a few laps each.
- [ ] The folder `~/.racelab/trackloc/serve/<date>-record/` exists with `video.mkv`, `frames.jsonl`, `inputs.jsonl`,
  `racelab.jsonl`, `meta.json`; the video plays and the cars are visible in it.

*Fallback*: `python -m trackloc record --camera iphone --duration 120`.

## 5. Map, then check it against the tape measure

First measure the real length of the lane-1 centre line with the tape (follow the slot all the way round, twice) and
write it in metres. Clear the track: no parked car except the mapping car, no session running.

- [ ] trackloc page: **Map**, car 1, throttle 4 (the page never goes above 8 of 15), two lanes, background median
  (choose "empty track" if hands or parked cars cannot be avoided), marker size set. Confirm the dialog, keep a hand
  on STOP in RaceLab. RaceLab drives the car at a low throttle, one lane then the other; a stall raises the throttle
  by one, never above the cap. The car is always handed back (end, STOP, error).
- [ ] Read the map summary: length, lanes, blind zones, how the metres were obtained.
- [ ] **GO if the map length is within 2 % of the tape measure** (8.00 m measured: 7.84 to 8.16 m). Outside: do not
  run. Check the marker size and the tape, then map again. Note both numbers.
- [ ] The shape in RaceLab (Track panel) looks like your track.
- [ ] Note the blind zones (bridge, shadow): detection is expected to drop there.

If it fails to map, keep the folder of the attempt (`<date>-map/`) and send it; do not tune blindly.

*Fallback*: `python -m trackloc map --camera iphone --slot 0 --marker-size 0.150` (`--track-length-m <tape>`
rescales the metres to the tape: a last resort, and write down that you used it).

## 6. Identify (roll call)

Park the cars you will use, a hand apart, each on its slot. Bots on, no session, STOP in hand.

- [ ] trackloc page: **Name the cars**, tick the slots on the track. Each gets a low pulse (4 of 15, 0.5 s) and the
  car that moved is that slot.
- [ ] **GO if every named car is the right one** (check by eye that the car you see move is the slot it pulsed).
  "Unidentified" is acceptable (the first crossing names it); a wrong name is not.
- [ ] A car reported as not moving at the cap: check it is on its slot and on the rail, with no controller held.

## 7. Run with 1 car, then 2, and watch identity

- [ ] **One car**: trackloc **Run**, slot 1, latency field at 0 the first time. The page runs one action at a time, so it
  cannot record during a Run: for the detection check below, record the same run with the CLI
  (`python -m trackloc run --camera iphone --map track_map.json --slots 0 --record rec1`) instead. Drive it by hand for 5 laps, or let a Constant-throttle bot at 4 drive. In RaceLab the car
  follows its real position on the map. After the run, trackloc shows the **latency still missing**: write it down;
  if it is more than 100 ms, click **Use** and map again (the finish line depends on it).
- [ ] **Detection**: on that recording, `python -m trackloc replay <record folder> --map track_map.json --debug
  overlay.mp4 --slots 0`. **GO if the car is detected in more than 95 % of the frames where it is visible** (outside
  the blind zones). There is no built-in counter: scrub the overlay, take 200 consecutive frames outside the blind
  zones and count those where the detection is on the car. Write the number.
- [ ] **Two cars**, one per lane, then in the same lane, 5 laps each, with the roll call first.
- [ ] **Identity**: the name shown on each car must stay the car you know. **GO: 0 identity swaps over 20 laps** in
  total with 2 or more cars (a swap = two cars exchange names, even if the next crossing fixes it). `replay` prints
  an `identity:` line: `relabels` counts every correction at a finish line, including the first naming of each
  car, so treat any relabel beyond one per car as a swap to look at.
- [ ] Then 3 and 4 cars, only if the 2-car runs had 0 swaps.

## 8. The corners autopilot, starting with a low cap

Prerequisite: the map is in RaceLab with a length in metres (step 5), positions flow (step 7), and the hand-back and
E-STOP passed. Open the Settings of the Auto car (the gear next to its switch) and choose **Corner speed (map)**.

- [ ] **Set the cap before the car moves: 4** (the default is 12, too high for a first run). Grip at start 10 m/s^2
  and margin 5 % are the defaults; for the first real run use a margin of 15 %. The global `max_throttle` still
  applies on top.
- [ ] One car alone on the track. Switch it to Auto, watch 3 laps with a hand on STOP. PASS: it finishes the laps,
  slows before corners, no off-track.
- [ ] Raise the cap by one only after 3 clean laps at the current cap. Stop at the first off-track, or when it is
  clearly slower than your own lap.
- [ ] Fallback check, once, at cap 4: stop the trackloc **Run** while the car is on Auto. After 0.5 s without
  positions the profile holds the target lap time: no sudden stop, no sudden acceleration. Restart Run.
- [ ] **Lap times**: note the first 10 laps. **GO: they trend down** (best of the last 5 faster than best of the first
  5, with no more off-track flags). If not, see Troubleshooting.
- [ ] The learned corner factors reset at the next session, after STOP, and when the profile is saved again: do not
  save the profile between laps if you want to watch the learning.

## 8b. The race brain: lane changes on purpose, then one pit stop

Prerequisite: step 8 passed (3 clean laps at a low cap), one car alone, hand on STOP. The camera map has no pit zone
yet: park the car on the pit entry switch, read its `s` in `curl -s localhost:8130/api/state` (`positions.cars`), do
the same at the pit exit, and enter both as percentages (`s` × 100) in the brain's Settings (**Pit zone**). Turn the CU's fuel mode and pit
lane on.

- [ ] Settings → **Race brain**: pilot *Corner speed (map)* with the cap that passed in step 8, lane changes
  **Alternates lanes** every 1 lap, pit stops **No pit stop** for now. Save, switch the car to Auto.
- [ ] **Lane changes**, 5 laps. PASS: one change per lap, each at a lane changer, the chip says "Changing lane (every
  lap)" just before, no change anywhere else, and the car never takes the pit lane by itself. A change one changer
  late means the request reached the car too late: raise **Ask up to … m before the changer** by 0.5 m (default 2) and
  repeat; note the value that works.
- [ ] **Pit stop**: Settings → **Pit now** (any pit setting). PASS: the chip says "Pit stop this lap", the car takes the
  pit lane at the next pit entry, drives in slowly (pit lane throttle 4), stops, the fuel gauge climbs back to 15
  ("Refuelling"), then it leaves and laps on. If the fuel does not rise, it leaves after 12 s anyway: note it (how the
  real CU refuels is not verified yet).
- [ ] Once, STOP while the car is stopped in the pit lane. PASS: it stays at 0; after clearing STOP it ends its stop and
  leaves.

## 9. Three calibration captures

They give the real motor numbers; the simulator's model is only a guess. Start trackloc **Record** for each (it
records the RaceLab events, so every throttle change is on the video's clock). Track clear, one car.

- [ ] **A. Throttle steps 0 to k on a straight** (folder `A-steps`): car stopped just before a straight, camera on it.
  Hardware test pulses at k = 2, 3, 4, 5, 6 (1.5 s each, a few seconds between). For k = 8 and 10, a
  Constant-throttle bot with the cap at k, started on the straight, ended by STOP at the end of the next straight:
  note k and the start time.
- [ ] **B. Lift-off at two speeds** (folders `B-lift-lo`, `B-lift-hi`): Constant-throttle bot at about 4, then about 7,
  long enough for the speed to settle (3 laps). On a straight set the throttle to 0 from RaceLab (the Manual fader
  to 0, not STOP) and note the time. Three lift-offs per speed.
- [ ] **C. Actuation delay** (folder `C-delay`): from rest, a Hardware test pulse at 4 for 1 s, 10 times, recording at
  the highest frame rate the iPhone gives (60 fps if possible). The delay is the time from the throttle event in
  `racelab.jsonl` to the first frame where the car moves. Write the 10 values.

For each: the car, the battery or power level, and whether the car was cold (first run) or warm.

## 10. What to save and send back

- [ ] `~/.racelab/hardware-tests/hwtest-<date>.json` (the Hardware test report with your notes).
- [ ] `~/.racelab/trackloc/serve/<date>-map/` and every `<date>-record/` folder, A, B and C included. Videos are
  large: copy them to a drive and send the JSON files first.
- [ ] The RaceLab log (System → Log, copied into a text file), the CU version (System → Connection), the transport.
- [ ] The numbers: BLE latency p95 / median / max / C ack p95; map length vs tape; marker size measured; detection
  %; identity swaps over N laps; hand-back timing (two measures); camera resolution and fps; the latency estimate;
  the lap times of step 8 and the cap reached.
- [ ] Photos: the camera mount, the marker, the track as the camera sees it.

## Go / no-go summary

| Criterion | GO | If not |
|---|---|---|
| Bench: E-STOP, dead-man, Bluetooth drop | car stops or returns to its controller within about 1 s, every time | no track test; send the log |
| Hardware test link | transport `C`, bots supported | stop: `C` is not what is being tested |
| BLE latency | nothing lost, p95 <= 150 ms, no round trip > 400 ms; **p95 noted** | move closer; failed twice: no bots over BLE |
| Hand-back after link loss | the car stops about 750 ms after the refresh stops (0.5 to 1.0 s), twice | do not drive bots on this CU |
| Aim | "marker OK", coverage OK, locked exposure | fix the mount or the light before mapping |
| Map length | within 2 % of the tape measure | do not run; check marker size; map again |
| Detection | more than 95 % of the frames where the car is visible | more light, locked exposure, empty-track background; send the recording |
| Identity | 0 swaps over 20 laps | send the recording; no several-car autopilot |
| Autopilot | 3 clean laps at each cap, lap times trending down | back to Target pace; send the recording |
| Race brain | one lane change per lap, at a changer; one pit stop with the fuel back at 15 | lane changes and pit stops off; send the log and the recording |

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| **Camera will not open** | the terminal has no camera permission; the iPhone is locked, far away or on another Apple ID; another app holds the camera | System Settings → Privacy & Security → Camera: allow your terminal and restart it. Unlock the iPhone, bring it close, Wi-Fi and Bluetooth on. Close Zoom, Photo Booth, FaceTime. `python -m trackloc probe`. Try `--ffmpeg` or a stream URL (an IP-camera app). In `serve`, pick it in the list and click *Open the camera* again |
| **Marker not found** | too small or too far; printed scaled ("fit to page"); glare on glossy paper; not flat; partly out of the frame | the square must be at least about 50 px on the image. Reprint at 100 % on matt paper, glue flat, lay it on the table in view, cut the glare, lock exposure |
| **Car does not move on a pulse** | throttle too low for this car; a controller is held on that slot; car off the rail or in a dead spot; bots are off; the step says Not run | read the line under the chip. Raise to 4, 5, 6. Release the controller. Check the car on its slot, the track power and the *With bots* switch. If the panel says the link never confirmed, read the gate in the message and System → Log |
| **RaceLab refuses (403, "host not allowed")** | RaceLab is reached by a name that is not `localhost` or an IP, or from another origin | use `http://127.0.0.1:8130/` or `localhost`. For another name, add it to `[api] allowed_hosts` in `racelab.toml` (`allowed_origins` for a page on another origin). A token set in `[api]` must be given to trackloc with `--token`. For trackloc's own page: 421 = bad Host, 403 = missing `?k=` key or cross-site request: use the address printed by this run |
| **trackloc answers 409** | another action is running | press STOP in the trackloc page and wait for the status to go idle |
| **No connector chip in RaceLab** | RaceLab started after `serve` (it retries), or is an older version without `connector.set` | wait a few seconds; update RaceLab; trackloc prints one warning in its terminal |
| **Lap times not improving** | no fresh positions (the profile fell back to the target lap time); the shape has no length in metres; a corner frozen after an off-track; cap too low; cold car or low battery; camera latency not corrected | check the Track panel shows moving positions and a length; if the car lifts where it need not, raise Grip at start, if it goes off, lower it or widen the margin; raise the cap by one; map again with the latency estimate; warm the car up |
| **Cars exchange names** | cars bunched, a hand over the track, a blind zone, same colours | record and send; roll call with the cars a hand apart; check the camera latency |
| **Map length far from the tape** | wrong marker size; marker not flat or not on the track plane; lens distortion | measure the square again; re-lay the marker; use the iPhone's main lens, not the ultra-wide; send the recording |

## Not verified

Everything here has only run against the virtual CU and synthetic images. The limits in this file are either
enforced by the Hardware test panel (150 ms, 400 ms, throttle 6) or this plan's own judgement (0.5 to 1.0 s, 2 %,
95 %, 20 laps, cap 4, margin 15 %). The real set may need other ones: record what you see and do not bend the data
to the limits.
