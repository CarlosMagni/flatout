"""Reference agent without learning: holds a target lap time.

Proportional controller on the `lap` events: too slow -> more throttle, too fast ->
less (correction bounded to ±1 level per lap, throttle capped by `--max-throttle`: beyond
the critical speed the car derails, laps get longer and a naive controller would
accelerate even more). Illustrates the asyncio client: `claim`, an `act` loop that is safe
with respect to the dead-man switch, throttle 0 on exit (including Ctrl+C). With no session
at all, it starts a `practice` (no lights); a session prepared by the user is never
replaced: the agent waits for its start (throttle held at 0 by the server until it runs).

    python examples/pace_agent.py --target 7.5
"""

from __future__ import annotations

import argparse
import asyncio

from racelab.client import DEFAULT_URL, Event, RaceLabClient

PERIOD_S = 0.2  # < dead-man (0.6 s): each loop iteration refreshes it
KP = 6.0  # throttle levels per 100 % lap-time deviation (10 % too slow -> +0.6)
MAX_STEP = 1.0  # maximum correction per lap (an outlier lap must not run everything away)
NO_SESSION = ("idle", "finished")


async def main(args: argparse.Namespace) -> None:
    target_ms = args.target * 1000
    throttle = args.start_throttle

    def on_lap(event: Event) -> None:
        nonlocal throttle
        if event.data["slot"] != args.slot or event.data["neutralized"]:
            return
        error = (event.data["ms"] - target_ms) / target_ms  # > 0: too slow
        throttle += max(-MAX_STEP, min(MAX_STEP, KP * error))
        throttle = max(1.0, min(args.max_throttle, throttle))
        print(f"lap {event.data['lap']}: {event.data['ms'] / 1000:.2f} s "
              f"(target {args.target:.2f}) -> throttle {throttle:.1f}")

    async with RaceLabClient(args.url, agent="pace-agent") as client:
        client.on_event("lap", on_lap)
        if client.state["session"]["state"] in NO_SESSION:
            await client.setup(type="practice")
            await client.start()
        await client.claim(args.slot)
        print(f"car {args.slot} claimed, target {args.target:.2f} s (Ctrl+C to stop)")
        try:
            while True:
                await client.act(args.slot, round(throttle))
                await asyncio.sleep(PERIOD_S)
        finally:
            await client.act(args.slot, 0)  # Ctrl+C cancels the task: the finally block runs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--slot", type=int, default=0, help="driven car (0..5)")
    parser.add_argument("--target", type=float, default=7.5, help="target lap time (s)")
    parser.add_argument("--start-throttle", type=float, default=8.0)
    parser.add_argument("--max-throttle", type=float, default=10.0, help="throttle ceiling (0..15)")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        print("\nstopped: throttle reset to 0")
