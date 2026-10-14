"""trackloc: localise slot cars with a camera and publish them to RaceLab.

    python -m trackloc <probe|marker|record|map|run|replay|synth|bench>   (see trackloc/README.md)

Layers (one-way dependencies: trackloc imports RaceLab, RaceLab never knows about trackloc):

- core, no I/O and deterministic: `geometry`, `detect`, `mapper`, `tracker`, `synth`, `pipeline`;
- three seams: a frame source (`sources`), a stream of input dicts (crossings, throttles) and
  a sink (`set_track` / `publish`): the RaceLab link (`link_racelab`) or a JSONL file
  (`recorder`), so that a recording replays exactly like the live session;
- front ends: `__main__` (CLI), `drive` (the discovery lap controller).

Importing this package is free: OpenCV and numpy are imported by the modules that use them,
so `tracker` and `drive` (identity and safety logic) stay pure standard library.
"""

__version__ = "0.1.0"
