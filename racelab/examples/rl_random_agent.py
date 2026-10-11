"""Random agent on `RaceLabEnv`: a random throttle at every step.

Run it with `racelab --sim --no-browser` (see README.md). Serves as a skeleton: replace
`env.action_space.sample()` with a policy's decision.

    python examples/rl_random_agent.py --episodes 3 --laps 2
"""

from __future__ import annotations

import argparse

from racelab.client import DEFAULT_URL, RaceLabEnv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--slot", type=int, default=0, help="driven car (0..5)")
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--laps", type=int, default=2, help="timed laps per episode")
    args = parser.parse_args()

    # max_steps bounds the length of an episode (~45 s at 13.3 Hz): a random throttle often derails
    with RaceLabEnv(args.url, args.slot, agent="rl-random", episode_laps=args.laps, max_steps=600) as env:
        for episode in range(args.episodes):
            obs, info = env.reset()
            total, steps, done = 0.0, 0, False
            while not done:
                obs, reward, terminated, truncated, info = env.step(env.action_space.sample())
                total += reward
                steps += 1
                done = terminated or truncated
            ending = "terminated" if terminated else "truncated"
            print(f"episode {episode + 1}: {ending} after {steps} steps, return {total:.2f}, "
                  f"laps {[round(ms / 1000, 2) for ms in info['lap_ms']]} s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped (throttle reset to 0 by env.close())")
