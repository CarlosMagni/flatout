# trackloc — camera localisation for RaceLab

`trackloc` watches the track with one camera (an iPhone through macOS Continuity Camera, or a
webcam) and publishes **the track shape and the position of every car** to RaceLab, through the
documented [track connector](../docs/api.md#track-connector-track-shape--positions) messages
(`track.set`, `track.positions`). Agents then read the same `state["positions"]` on the real
track as on the virtual CU.

It is a separate package: trackloc imports RaceLab (its client), RaceLab never imports trackloc.
Any other localisation method (light gates, IMU, an estimator) can replace it by sending the
same two messages.

## Install

```bash
.venv/bin/pip install -e carreralib -e racelab -e trackloc   # from the repository root
source .venv/bin/activate    # in every new terminal: `python -m trackloc` needs the venv
```

macOS asks for the camera permission the first time a terminal runs a camera command: accept it
(System Settings → Privacy & Security → Camera → your terminal app).

## On the track, step by step

1. **Place the camera** 1.5–2.5 m above the track, looking down (it does not need to be exactly
   overhead: 40–90° works). It must see the whole track. Fix it: a bumped camera is re-registered
   automatically, but a stable one is better.
2. **Print the marker**: `python -m trackloc marker` writes `trackloc_marker.pdf` (ArUco
   DICT_4X4_50, id 0, black square 150 mm). Print at 100 % (no "fit to page"), on matt paper, glue
   it flat on a board. **Measure the black square with a ruler**. Lay it flat on the table, next to
   the track, in view, and leave it there.
3. **Check the camera**: `python -m trackloc probe` lists the cameras with their real resolution
   and frame rate. `--camera` then takes any of:
   - an **index**: `--camera 1`;
   - a **part of the name**: `--camera iphone`, `--camera "desk view"` (case does not matter; it
     survives a change of numbering when a device comes and goes);
   - a **stream URL**: `--camera rtsp://192.168.1.20:8554/live` (an IP-camera app on a phone,
     another computer; `--ffmpeg` also accepts it);
   - **nothing**: the command lists the cameras and asks which one.
4. **Aim the camera.** The OpenCV build is headless (no window), so `preview` serves a small page for it:
   ```bash
   python -m trackloc preview --camera iphone            # then open http://127.0.0.1:8140/
   python -m trackloc preview --camera 1 --map track_map.json   # also draws the lanes of a map you already have
   python -m trackloc preview --camera 1 --snapshot aim.jpg     # no browser: one annotated JPEG per second
   ```
   Live view at about 10 fps with: the **marker** outlined and "marker OK / not found" (it gives the metres: lay it
   flat, in view, bigger than about 50 px), the real **resolution and frame rate**, a **coverage** hint (does the
   track fill the frame, does an edge cut it: from the `--map` lanes, otherwise from where things were seen moving),
   an **outline of whatever moves**, and a warning when the *whole* picture keeps changing (exposure, focus or
   white balance not locked). The page (English / French switch) and the terminal show the same checklist:
   - lock exposure, focus and white balance (iPhone: touch and hold the picture until AE/AF LOCK shows);
   - a fixed 30 or 60 fps (no automatic frame rate);
   - a rigid mount;
   - flicker-free light (no bands rolling across the picture);
   - the whole track in view, with a margin;
   - the marker flat on the table and visible.

   It is read-only and moves nothing: it serves on `127.0.0.1` only (no option for another address), answers `GET`
   only, refuses a `Host` that is not a loopback name (DNS rebinding) and requests the browser marks cross-site,
   sends no CORS header and a strict Content-Security-Policy, and imports neither the RaceLab link nor the driving
   code. `--lang fr` for a French terminal and picture, `--duration S` to stop by itself, `--video FILE` to try
   the page without a camera. With `--map`, the lanes are drawn as recorded: a bumped camera shifts them (`run`
   re-registers the map, `preview` does not).
5. **Record first** (always useful, everything below can be replayed offline):
   ```bash
   python -m trackloc record --camera 1 --duration 120
   ```
   Video + per-frame timestamps + RaceLab events (crossings, bot throttles), on one clock, in
   `~/.racelab/trackloc/<date>-record/`.
6. **Map the track** (RaceLab running and connected to the CU, no session running, no parked car
   on the track):
   ```bash
   python -m trackloc map --camera 1 --slot 0 --marker-size 0.150
   ```
   RaceLab drives the car on slot 0 at a low throttle (4/15; raised by one if it stalls, never
   above `--cap`, emergency stop if it still does not move), first on one lane, then changes lane
   for the other. The car is always handed back at the end, on Ctrl-C and on any error. Result:
   `track_map.json`. Options: `--background empty` (take the background from the empty track
   first), `--lanes 1`, `--track-length-m` (known length of lane 0: rescales the metres).
7. **Run**:
   ```bash
   python -m trackloc run --camera 1 --map track_map.json --slots 0,1 --debug overlay.mp4
   ```
   The map appears in RaceLab and the cars move on it. `--record DIR` records at the same time.
   With several cars parked on the track, add `--identify` (see [Roll call](#roll-call)) to name them all before
   tracking starts, instead of waiting for each to cross the finish line.
8. **Measure the latency** on a recording and pass it back (`--latency`):
   `python -m trackloc replay <recording> --map track_map.json` prints how far the crossings say
   the positions lag behind, and the `--latency` to use. Map again with it: the finish line depends on it.

## Roll call

Identity normally comes from the CU's finish-line crossings, so parked or bunched cars stay unnamed until each has
crossed the line cleanly. The CU's `C` command lets software hold any slot's throttle (`bot.claim` / `bot.act` /
`bot.unclaim`, see [docs/api.md](../docs/api.md)), so the roll call simply asks the cars one by one:

```bash
python -m trackloc identify --camera 1 --map track_map.json --slots 0,1,2     # check it works
python -m trackloc run --camera 1 --map track_map.json --slots 0,1,2 --identify  # or do it before tracking
```

For each slot: the camera looks at the parked cars; the slot is claimed and gets a LOW throttle pulse (`--throttle`,
default 4/15, `--pulse-s` 0.5 s) then 0; the car that moved is that slot (exactly one car-sized blob left its place
and one appeared ahead of it along its lane, every other blob is where it was); the slot is unclaimed. If no car
moved, or several did (or a hand is in view), it retries once (one throttle step up when nothing moved, never above
`--cap`, default 8), then reports the slot **unidentified** (it will be named by its first crossing as usual).

- **Safety.** Same rules as the discovery lap, and it only runs between sessions: refused while a session is running,
  while E-STOP is on, or if the CU link is down (checked before every pulse and on every throttle refresh); the
  throttle is refreshed every 0.25 s (RaceLab's dead-man switch cuts it after 0.6 s); a pulse is at most 2 s; in a
  `finally`, on Ctrl-C and on any error (camera stalled, link lost) it sends throttle 0 then unclaims; a refused
  `act` (E-STOP, agent dropped) aborts everything and the slot is never claimed again. It uses `bot.claim` with
  `free: true` (driving outside a session).
- **Naming.** The identified cars are named in the tracker at once (`Tracker.hint`: the confirmed track at the
  parked car's place, told apart from its neighbour by lane): no finish-line crossing needed.
- **Colours.** The roll call also stores each car's colours (a hue/saturation histogram of the car pixels, taken
  while it stood still). The tracker uses them ONLY as a tie-break: when a CU crossing is ambiguous (two tracks both
  on the line at that instant), the track whose colours are clearly closest to that slot's wins; otherwise nothing
  changes. They are not a tracker of their own, and two cars of the same colour decide nothing.
- **Limits.** A car that does not move at the cap is reported, not pushed (stuck, off the track, wrong slot). Cars
  touching each other can merge into one blob: park them a hand apart for the roll call. Untested on the real track
  (only synthetic cars and the virtual CU), like the rest of this package.

## How it works

- **Map = the discovery lap.** Background = per-pixel median over the lap (the moving car
  vanishes from it) or the empty track. Car = difference to the background → blob centroid. Its
  pixel trajectory becomes the lane centre line. The finish line is where the car is at the
  instant the CU reports the crossing. Stretches with no detection (bridges, occlusions) are
  stored as blind zones. A ground homography from the marker converts pixels to metres
  (perspective makes pixel arc length uneven), cross-checked against the lap time at constant
  throttle.
- **Re-registration.** The map keeps its background image. At start, the current view is aligned
  to it (ECC, homography), so moving the phone a little does not require a new map.
- **Tracking.** One 1D Kalman filter per car on (s, v):
  - bots are predicted with the RaceLab motor model, driven by the throttle the CU actually
    applies (known, thanks to the `C` command), with an online gain for motor heating;
  - humans with a speed profile learned from their own laps.
  
  Detections are assigned per lane by gated optimal assignment. **Identity is checked at every
  line crossing** (the CU says which car crossed, to the millisecond). In a blind zone the car
  is predicted, and the gate widens on exit. `off` = a detection far from both lanes, or a car
  missing where it should be visible.
- **Latency.** Positions are predicted to the time they are sent, not the time of the frame.
  Frames are time-stamped at mid-exposure (motion blur lies along the lane: its centroid is the
  position at mid-exposure).

## Synthetic benchmark

`python -m trackloc bench` renders a track through an oblique camera (motion blur, noise,
lighting drift, a bridge, hands over the track) with known ground truth, and measures the whole
pipeline. Current results:

| | result |
|---|---|
| map, 55° camera | lap length +0.5 %, lanes +1.0 / +0.1 %, position RMS 2.2 cm |
| map, 40° / 70° camera | length +0.5 % / +0.3 %, position RMS 3.1 / 2.3 cm |
| map, uncorrected barrel distortion | length −0.5 %, position RMS 4.3 cm |
| race: 2 bots + 1 human, bridge, 2 hands | s error RMS 2.3 cm visible, 4.8 cm around the bridge, worst 19 cm; 0 wrong-identity samples; 0 false off-track |
| pipeline cost, 1280×720 | 2.2 ms per frame (capture and encoding excluded) |

## Not validated yet

Everything above is synthetic. Not tested on the real track: the iPhone's real frame rate and
latency through Continuity Camera, reflections on the rails, real lighting, the real motor
response, bridges and elevated sections (the homography assumes a flat table; the lap-time
check covers lengths, not shapes). Record before tuning anything.

## Tests

```bash
cd trackloc && ../.venv/bin/python -m pytest -q
```

Tests that need OpenCV skip without it. The RaceLab link tests run a real RaceLab app and API
server (virtual CU or no CU), including the safety paths of `map`: normal end, emergency stop
mid-lap, lost connection. The roll call runs closed-loop against a fake CU and the synthetic
camera on one fake clock (cars that obey the throttle they are sent, dead-man switch included);
the preview's server is started for real on 127.0.0.1 and queried with a plain HTTP client.
