"""Live strategist: a RaceLab client that makes the brain's decisions for one car.

On the track, configure the car as Auto with the Race brain (pilot mpc, lane keep, pit
off) — by hand in the UI, or send `bot.profile` first. Then:

    python -m strategist.live --policy out/run1/policy.npz                  # localhost:8130
    python -m strategist.live --policy out/run1/policy.npz --dry-run        # print, send nothing

Every state message feeds the observation memory; at each of our laps the greedy action
goes out: `bot.params` for the risk, `bot.brain` for a lane change or a pit stop. Safety
is the app's: STOP, dead-man, lights and session gates stay in front of everything.
Not exercised on hardware yet — expect to iterate on timing (see README.md).
"""

from __future__ import annotations

import argparse
import time

from racelab.client import DEFAULT_URL, SyncClient

from .env import action_parts
from .observation import ObsState
from .policy import NumpyPolicy


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--policy", required=True)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--slot", type=int, default=0)
    ap.add_argument("--rate", type=float, default=4.0, help="state reads per second")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    policy = NumpyPolicy.load(args.policy)
    obs_state = ObsState()
    obs_state.set_slot(args.slot)
    decided_laps = -1
    with SyncClient(args.url, agent="strategist") as client:
        print(f"strategist sur le slot {args.slot} — Ctrl+C pour sortir"
              + (" (dry-run : rien n'est envoyé)" if args.dry_run else ""))
        try:
            while True:
                state = client.state
                if not state:
                    time.sleep(1 / args.rate)
                    continue
                car = next((c for c in state.get("cars") or [] if c.get("slot") == args.slot), {})
                laps = int(car.get("laps", 0) or 0)
                obs_state.note(state, time.monotonic())
                if decided_laps == -1 or laps > decided_laps:
                    obs = obs_state.build(state)
                    action = policy.greedy(obs)
                    lane, pit, risk = action_parts(action)
                    running = (state.get("session") or {}).get("state") in ("running", "finishing")
                    print(f"tour {laps}: lane={'CHANGE' if lane else 'stay'} "
                          f"pit={'OUI' if pit else 'non'} risk={risk:.1f}"
                          + ("" if running else " (hors session, rien envoyé)"), flush=True)
                    if running and not args.dry_run:
                        client.request("bot.params", slot=args.slot, params={"risk": risk})
                        if lane:
                            client.request("bot.brain", slot=args.slot, request="lane")
                        if pit:
                            client.request("bot.brain", slot=args.slot, request="pit")
                    decided_laps = laps
                time.sleep(1 / args.rate)
        except KeyboardInterrupt:
            print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
