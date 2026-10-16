"""PPO training for the strategist, on the headless RaceEnv.

Rollouts run in worker processes with the numpy policy (no torch there): every update the
current weights are broadcast, each worker plays a few full races and returns its
episodes, the main process does the PPO epochs in torch and exports the weights again —
the exact same weights the workers sampled with.

    python -m strategist.train --quick
    python -m strategist.train --updates 40 --workers 8 --episodes-per-worker 4 \\
        --laps 26 --opponents 3 --pilot mpc --pilot-params '{"a_lat": 18.0}' --out out/run2

The pilot must be calibrated for the track (the defaults are deliberately cautious: a_lat
10 vs the simulator's 20): a slower car leaves no room for strategy. Races longer than a
tank (`laps` > ~20) make the pit decision matter; `opponents` >= 2 spreads the positions.

Outputs: OUT/policy.npz (every update), OUT/curve.csv (update,reward,position,win,dry,offs).
"""

from __future__ import annotations

import argparse
import csv
import json
import pickle
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from .env import DEFAULT_PROFILE, N_ACTIONS, RaceEnv
from .observation import OBS_DIM
from .policy import HIDDEN, NumpyPolicy, init_params

GAMMA, LAM, CLIP = 0.99, 0.95, 0.2
EPOCHS, MINIBATCH, LR = 4, 256, 3e-4
VF_COEF, ENT_COEF = 0.5, 0.01


# ------------------------------------------------------------------ rollouts (workers)


def _worker(task: dict) -> tuple[list[list[tuple]], list[dict]]:
    """Play episodes for the given seeds with the broadcast weights. Runs in a process
    that never touches torch: the policy is the numpy one, exactly the exported weights.
    Returns (episodes, final info of each)."""
    policy = NumpyPolicy(pickle.loads(task["weights"]))
    env = RaceEnv(task["profile"], seed=task["seeds"][0], laps=task["laps"],
                  opponents=task["opponents"], ref_lap_ms=task["ref_lap_ms"],
                  track_per_seed=task["track_per_seed"])
    rng = np.random.default_rng(task["rng_seed"])
    episodes: list[list[tuple]] = []
    infos: list[dict] = []
    for seed in task["seeds"]:
        obs = env.reset(seed)
        episode: list[tuple] = []
        while True:
            action, logp, value = policy.act(obs, rng)
            nobs, reward, terminated, truncated, info = env.step(action)
            episode.append((obs, action, logp, value, float(reward)))
            obs = nobs
            if terminated or truncated:
                break
        episodes.append(episode)
        infos.append(info)
    return episodes, infos


def _advantages(episode: list[tuple], last_value: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """GAE(lam) over one episode. `last_value` is 0 at the terminal state."""
    rewards = np.array([t[4] for t in episode], dtype=np.float32)
    values = np.array([t[3] for t in episode], dtype=np.float32)
    adv = np.zeros_like(rewards)
    gae, next_value = 0.0, last_value
    for t in reversed(range(len(rewards))):
        delta = rewards[t] + GAMMA * next_value - values[t]
        gae = delta + GAMMA * LAM * gae
        adv[t] = gae
        next_value = values[t]
    return adv, rewards


# ------------------------------------------------------------------ torch side (main)


def _torch_model(params: dict):
    import torch
    import torch.nn as nn

    class MLP(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.l1 = nn.Linear(OBS_DIM, HIDDEN)
            self.l2 = nn.Linear(HIDDEN, HIDDEN)
            self.pi = nn.Linear(HIDDEN, N_ACTIONS)
            self.v = nn.Linear(HIDDEN, 1)

        def forward(self, x):
            h = torch.tanh(self.l1(x))
            h = torch.tanh(self.l2(h))
            return self.pi(h), self.v(h).squeeze(-1)

    model = MLP()
    with torch.no_grad():  # the rollout weights, mapped in (torch stores (out, in))
        model.l1.weight.copy_(torch.from_numpy(params["W1"].T))
        model.l1.bias.copy_(torch.from_numpy(params["b1"]))
        model.l2.weight.copy_(torch.from_numpy(params["W2"].T))
        model.l2.bias.copy_(torch.from_numpy(params["b2"]))
        model.pi.weight.copy_(torch.from_numpy(params["Wp"].T))
        model.pi.bias.copy_(torch.from_numpy(params["bp"]))
        model.v.weight.copy_(torch.from_numpy(params["Wv"].T))
        model.v.bias.copy_(torch.from_numpy(params["bv"]))
    return model


def _export(model) -> dict[str, np.ndarray]:
    import torch
    with torch.no_grad():
        return {
            "W1": model.l1.weight.numpy().T.copy(), "b1": model.l1.bias.numpy().copy(),
            "W2": model.l2.weight.numpy().T.copy(), "b2": model.l2.bias.numpy().copy(),
            "Wp": model.pi.weight.numpy().T.copy(), "bp": model.pi.bias.numpy().copy(),
            "Wv": model.v.weight.numpy().T.copy(), "bv": model.v.bias.numpy().copy(),
        }


def _update(model, episodes: list[list[tuple]]) -> dict[str, float]:
    """The PPO epochs. Returns a few numbers for the log."""
    import torch

    obs = torch.from_numpy(np.stack([t[0] for ep in episodes for t in ep]))
    actions = torch.tensor([t[1] for ep in episodes for t in ep], dtype=torch.int64)
    logp_old = torch.tensor([t[2] for ep in episodes for t in ep], dtype=torch.float32)
    values_old = torch.tensor([t[3] for ep in episodes for t in ep], dtype=torch.float32)
    adv = torch.from_numpy(np.concatenate([_advantages(ep)[0] for ep in episodes]))
    ret = adv + values_old
    adv = (adv - adv.mean()) / (adv.std() + 1e-8)

    opt = torch.optim.Adam(model.parameters(), lr=LR)
    n = len(actions)
    idx = np.arange(n)
    stats = {"loss": 0.0, "entropy": 0.0}
    for _epoch in range(EPOCHS):
        np.random.shuffle(idx)
        for start in range(0, n, MINIBATCH):
            mb = torch.from_numpy(idx[start:start + MINIBATCH])
            logits, value = model(obs[mb])
            dist = torch.distributions.Categorical(logits=logits)
            logp = dist.log_prob(actions[mb])
            ratio = torch.exp(logp - logp_old[mb])
            unclipped = ratio * adv[mb]
            clipped = torch.clamp(ratio, 1 - CLIP, 1 + CLIP) * adv[mb]
            policy_loss = -torch.min(unclipped, clipped).mean()
            value_loss = ((value - ret[mb]) ** 2).mean()
            entropy = dist.entropy().mean()
            loss = policy_loss + VF_COEF * value_loss - ENT_COEF * entropy
            opt.zero_grad()
            loss.backward()
            opt.step()
            stats["loss"] = float(loss.detach())
            stats["entropy"] = float(entropy.detach())
    return stats


# ------------------------------------------------------------------ main loop


def _episode_infos(episodes: list[list[tuple]], infos: list[dict]) -> dict[str, float]:
    rewards = np.array([sum(t[4] for t in ep) for ep in episodes], dtype=np.float32)
    pos = np.array([i.get("position", 0) for i in infos], dtype=np.float32)
    dry = np.array([1.0 if i.get("dry") else 0.0 for i in infos], dtype=np.float32)
    offs = np.array([i.get("offs", 0) for i in infos], dtype=np.float32)
    return {"reward": float(rewards.mean()), "position": float(pos.mean()),
            "win": float((pos == 1).mean()), "dry": float(dry.mean()), "offs": float(offs.mean())}


def train(args) -> Path:
    import torch
    torch.set_num_threads(2)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pilot_params = json.loads(args.pilot_params or "{}")
    profile = {"kind": "brain", "pilot": args.pilot, "pilot_params": pilot_params,
               "lane": {"policy": "keep"}, "pit": {"policy": "off", "min_laps": 5}} if args.pilot \
        else DEFAULT_PROFILE
    if args.init:
        params = dict(np.load(args.init))  # warm start from a checkpoint
    else:
        params = init_params(seed=args.seed)
    model = _torch_model(params)
    rng = np.random.default_rng(args.seed)
    curve = (out / "curve.csv").open("w", newline="")
    writer = csv.writer(curve)
    writer.writerow(["update", "reward", "position", "win", "dry", "offs", "seconds"])

    pool = Pool(processes=args.workers) if args.workers > 1 else None
    try:
        for update in range(1, args.updates + 1):
            t0 = time.time()
            weights = pickle.dumps(params if update == 1 else _export(model))
            per = args.episodes_per_worker
            seeds_all = [args.seed * 100000 + update * 1000 + k
                         for k in range(args.workers * per)]
            tasks = []
            # fine chunks (2 episodes): episodes vary a lot in length (a dry crawl doubles one),
            # so one big chunk per worker leaves the others idle behind the slowest (~50 % CPU);
            # many small tasks let the pool steal work and keep every core busy.
            for i in range(0, len(seeds_all), 2):
                tasks.append({"weights": weights, "seeds": seeds_all[i:i + 2], "profile": profile,
                              "laps": args.laps, "opponents": args.opponents,
                              "ref_lap_ms": args.ref_lap_ms, "rng_seed": int(rng.integers(2 ** 31)),
                              "track_per_seed": not args.single_track})
            if pool is None:
                results = [_worker(task) for task in tasks]
            else:
                results = pool.map(_worker, tasks)
            episodes = [ep for eps, _ins in results for ep in eps]
            infos = _episode_infos(episodes, [i for _eps, ins in results for i in ins])
            stats = _update(model, episodes)
            np.savez(out / "policy.npz", **_export(model))
            writer.writerow([update, round(infos["reward"], 3), round(infos["position"], 2),
                             round(infos["win"], 2), round(infos["dry"], 2), round(infos["offs"], 2),
                             round(time.time() - t0, 1)])
            curve.flush()
            print(f"update {update:3d} | {len(episodes)} courses, "
                  f"{sum(len(e) for e in episodes)} décisions | reward {infos['reward']:6.2f} "
                  f"| pos {infos['position']:.2f} | win {100 * infos['win']:3.0f}% "
                  f"| dry {100 * infos['dry']:3.0f}% | offs {infos['offs']:.2f} "
                  f"| ent {stats['entropy']:.2f} | {time.time() - t0:.0f}s", flush=True)
    finally:
        if pool is not None:
            pool.close()
            pool.join()
        curve.close()
    return out / "policy.npz"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--updates", type=int, default=40)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--episodes-per-worker", type=int, default=4)
    ap.add_argument("--laps", type=int, default=8)
    ap.add_argument("--opponents", type=int, default=2)
    ap.add_argument("--pilot", default="mpc", choices=("mpc", "corners"))
    ap.add_argument("--pilot-params", default="{}",
                    help="pilot JSON, e.g. '{\"a_lat\": 18.0}' (calibrate for the track!)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--init", default=None, help="warm-start checkpoint (.npz)")
    ap.add_argument("--single-track", action="store_true",
                    help="the one built-in track (default: one generated track per episode seed)")
    ap.add_argument("--ref-lap-ms", type=float, default=6000.0)
    ap.add_argument("--out", default="out/train")
    ap.add_argument("--quick", action="store_true", help="2 updates, 2 workers: a smoke run")
    args = ap.parse_args()
    if args.quick:
        args.updates, args.workers, args.episodes_per_worker, args.laps = 2, 2, 2, 4
        args.out = "out/smoke"
    path = train(args)
    print(f"poids finaux : {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
