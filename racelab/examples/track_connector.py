"""Example track connector: circuit shape + estimated positions.

A model to copy for a camera connector: it declares the circuit shape
(`set_track`) then sends the positions at a fixed rate (`send_positions`).
Here the position is ESTIMATED from the CU data (time since the last
line crossing / the car's typical lap): useful on the real track without a
camera, but blind to crashes and stops. A camera would replace `estimate()`
with car detection in the image.

    python examples/track_connector.py                     # example shape (oval)
    python examples/track_connector.py --track track.json  # your shape

`track.json`: {"points": [[x, y], ...], "y_axis": "up" | "down", "finish": 0.0,
"pit": [start, end], "changers": [...], "lane_offset": ..., "name": "..."}
(free units: pixels of a top-down photo, centimeters…). The UI map
disappears when the connector stops. Does not work with the virtual CU,
which already provides its own track.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics

from racelab.client import DEFAULT_URL, RaceLabClient

PERIOD_S = 0.05  # 20 Hz


def oval(n: int = 120) -> dict:
    """An example track: elongated oval, finish line in the middle of a straight."""
    points = []
    for i in range(n):
        a = 2 * math.pi * i / n
        points.append([math.cos(a) * 3.0 + math.copysign(1.0, math.cos(a)) * 0.6, math.sin(a) * 1.2])
    return {"points": points, "finish": 0.25, "name": "Oval (example)", "lane_offset": 0.09}


def estimate(car: dict) -> dict | None:
    """Estimated lap fraction = time since the line / typical lap (median of the recent ones)."""
    since = car.get("since_cross_ms")
    recent = [ms for ms in car.get("recent") or [] if ms]
    if since is None or not recent:
        return None
    typical = statistics.median(recent)
    frac = since / typical
    # beyond a lap and a half without a pass: stopped or off track, leave it at the end of the lap
    return {"slot": car["slot"], "s": min(frac, 0.999), "lane": 0, "off": frac > 1.5}


async def main(args: argparse.Namespace) -> None:
    geometry = oval()
    if args.track:
        with open(args.track, encoding="utf-8") as f:
            geometry = json.load(f)
    async with RaceLabClient(args.url, agent=args.name, state_hz=20) as client:
        shape = await client.set_track(geometry)
        print(f"track \"{shape.get('name') or args.name}\" declared ({len(shape['points'])} points)")
        print("estimated positions at 20 Hz (Ctrl+C to stop: the map disappears)")
        while True:
            cars = [p for car in client.state.get("cars", []) if (p := estimate(car))]
            await client.send_positions(cars)
            await asyncio.sleep(PERIOD_S)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--name", default="estimation", help="name shown as the track source")
    parser.add_argument("--track", help="JSON file of the circuit shape")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        print("\nconnector stopped")
