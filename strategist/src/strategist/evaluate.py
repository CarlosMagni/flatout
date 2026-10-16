"""Evaluate the strategist against the baselines on fixed seeds (the spec's protocol).

    python -m strategist.evaluate --policy out/run2/policy.npz --baseline both --races 12 \\
        --laps 26 --opponents 3 --pilot-params '{"a_lat": 18.0}'
    python -m strategist.evaluate --policy out/run2/policy.npz --ablate lane   # pit / risk too

Arms on the SAME seeds: `strategist` (greedy policy decisions, optionally ablated),
`rule` (the race brain alone and autonomous: lane fastest, pit auto — no strategist
requests) and `random`. Every arm drives the same pilot and the same pilot parameters
(`--pilot`, `--pilot-params`). Reports mean position, win rate, mean race time, dry and
offs rates, pit stops.
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from .env import N_ACTIONS, RaceEnv, action_parts
from .policy import NumpyPolicy


def _profiles(pilot: str, params: dict | None = None) -> tuple[dict, dict]:
    p = params or {}
    strategist = {"kind": "brain", "pilot": pilot, "pilot_params": p,
                  "lane": {"policy": "keep"}, "pit": {"policy": "off", "min_laps": 5}}
    rule = {"kind": "brain", "pilot": pilot, "pilot_params": p,
            "lane": {"policy": "fastest"}, "pit": {"policy": "auto"}}
    return strategist, rule


def _ablated(action: int, ablate: str | None) -> int:
    if not ablate:
        return action
    lane, pit, _risk = action_parts(action)
    ridx = action % 3
    if ablate == "lane":
        lane = 0
    elif ablate == "pit":
        pit = 0
    elif ablate == "risk":
        ridx = 1
    return lane * 6 + pit * 3 + ridx


def run_arm(arm: str, *, pilot: str, seeds: list[int], laps: int, opponents: int,
            policy: NumpyPolicy | None = None, ablate: str | None = None,
            pilot_params: dict | None = None) -> list[dict]:
    strategist_p, rule_p = _profiles(pilot, pilot_params)
    rows: list[dict] = []
    rng = np.random.default_rng(1234)
    for seed in seeds:
        if arm == "rule":
            env = RaceEnv(rule_p, seed=seed, laps=laps, opponents=opponents)
            info = env.run_episode(None)
        else:
            env = RaceEnv(strategist_p, seed=seed, laps=laps, opponents=opponents)
            env.reset(seed)

            def choose(obs, _env, arm=arm):
                action = int(rng.integers(N_ACTIONS)) if arm == "random" else policy.greedy(obs)
                return _ablated(action, ablate)

            info = env.run_episode(choose)
        rows.append(info)
    return rows


def summarize(rows: list[dict]) -> dict[str, float]:
    pos = np.array([r["position"] for r in rows], dtype=float)
    ms = np.array([r["race_ms"] for r in rows], dtype=float)
    return {"races": len(rows), "position": float(pos.mean()), "win": float((pos == 1).mean()),
            "race_s": float(ms.mean()) / 1000.0,
            "dry": float(np.mean([1.0 if r["dry"] else 0.0 for r in rows])),
            "offs": float(np.mean([r["offs"] for r in rows])),
            "stops": float(np.mean([r["stops"] for r in rows]))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--policy", default=None, help="checkpoint for the strategist arm")
    ap.add_argument("--baseline", default="rule", choices=("rule", "random", "both", "none"))
    ap.add_argument("--races", type=int, default=12)
    ap.add_argument("--seed0", type=int, default=500000)
    ap.add_argument("--laps", type=int, default=8)
    ap.add_argument("--opponents", type=int, default=2)
    ap.add_argument("--pilot", default="mpc", choices=("mpc", "corners"))
    ap.add_argument("--pilot-params", default="{}",
                    help="keep the pilot calibrated like training, e.g. '{\"a_lat\": 18.0}'")
    ap.add_argument("--ablate", default=None, choices=("lane", "pit", "risk"))
    args = ap.parse_args()

    params = json.loads(args.pilot_params or "{}")
    seeds = [args.seed0 + i for i in range(args.races)]
    arms: list[tuple[str, object]] = []
    if args.policy:
        policy = NumpyPolicy.load(args.policy)
        name = f"strategist{('-' + args.ablate) if args.ablate else ''}"
        arms.append((name, lambda: run_arm("strategist", pilot=args.pilot, seeds=seeds,
                                           laps=args.laps, opponents=args.opponents,
                                           policy=policy, ablate=args.ablate,
                                           pilot_params=params)))
    if args.baseline in ("rule", "both"):
        arms.append(("rule brain", lambda: run_arm("rule", pilot=args.pilot, seeds=seeds,
                                                   laps=args.laps, opponents=args.opponents,
                                                   pilot_params=params)))
    if args.baseline in ("random", "both"):
        arms.append(("random", lambda: run_arm("random", pilot=args.pilot, seeds=seeds,
                                               laps=args.laps, opponents=args.opponents,
                                               pilot_params=params)))

    print(f"{args.races} courses, {args.laps} tours, {1 + args.opponents} voitures, "
          f"pilote {args.pilot} {params}, seeds {seeds[0]}..{seeds[-1]}")
    print(f"{'arm':<18} {'pos':>5} {'win':>5} {'course s':>8} {'dry':>5} {'offs':>5} {'pits':>5}")
    for name, run in arms:
        s = summarize(run())
        print(f"{name:<18} {s['position']:>5.2f} {100 * s['win']:>4.0f}% {s['race_s']:>8.1f} "
              f"{100 * s['dry']:>4.0f}% {s['offs']:>5.2f} {s['stops']:>5.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
