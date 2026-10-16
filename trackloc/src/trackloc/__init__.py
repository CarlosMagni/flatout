"""trackloc: localise slot cars with a camera and publish them to RaceLab.

    python -m trackloc <probe|marker|preview|record|map|identify|run|replay|synth|bench>   (see trackloc/README.md)

Layers (one-way dependencies: trackloc imports RaceLab, RaceLab never knows about trackloc):

- core, no I/O and deterministic: `geometry`, `detect`, `mapper`, `tracker`, `synth`, `pipeline`;
- three seams: a frame source (`sources`), a stream of input dicts (crossings, throttles) and
  a sink (`set_track` / `publish`): the RaceLab link (`link_racelab`) or a JSONL file
  (`recorder`), so that a recording replays exactly like the live session;
- front ends: `__main__` (CLI), `drive` (the discovery lap controller), `identify` (the roll call: which car is
  which slot), `preview` (aim the camera: a read-only page on 127.0.0.1, imports nothing that drives).

Importing this package is free: OpenCV and numpy are imported by the modules that use them,
so `tracker` and `drive` (identity and safety logic) stay pure standard library.
"""

__version__ = "0.1.0"
