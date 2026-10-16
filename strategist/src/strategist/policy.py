"""Actor-critic policy: a 2x64 tanh MLP over the 20-float observation, 12 actions.

Training uses torch (train.py); the rollouts, the evaluation and the live client only
use this numpy implementation — a checkpoint runs without torch installed. Weights live
in a plain dict of arrays (npz), exported from the torch model after every update."""

from __future__ import annotations

import numpy as np

from .env import N_ACTIONS
from .observation import OBS_DIM

HIDDEN = 64
KEYS = ("W1", "b1", "W2", "b2", "Wp", "bp", "Wv", "bv")


def init_params(seed: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)

    def lin(fan_in: int, fan_out: int) -> tuple[np.ndarray, np.ndarray]:
        w = rng.standard_normal((fan_in, fan_out)) * (1.0 / np.sqrt(fan_in))
        return w.astype(np.float32), np.zeros(fan_out, dtype=np.float32)

    p: dict[str, np.ndarray] = {}
    p["W1"], p["b1"] = lin(OBS_DIM, HIDDEN)
    p["W2"], p["b2"] = lin(HIDDEN, HIDDEN)
    p["Wp"], p["bp"] = lin(HIDDEN, N_ACTIONS)
    p["Wv"], p["bv"] = lin(HIDDEN, 1)
    return p


class NumpyPolicy:
    """Inference: logits and value for a batch, sampling for the rollouts."""

    def __init__(self, params: dict) -> None:
        missing = set(KEYS) - set(params)
        if missing:
            raise ValueError(f"policy checkpoint misses {', '.join(sorted(missing))}")
        self.p = {k: np.asarray(params[k], dtype=np.float32) for k in KEYS}

    @classmethod
    def load(cls, path) -> "NumpyPolicy":
        with np.load(path) as data:
            return cls(dict(data))

    def save(self, path) -> None:
        np.savez(path, **self.p)

    def heads(self, obs) -> tuple[np.ndarray, np.ndarray]:
        x = np.atleast_2d(np.asarray(obs, dtype=np.float32))
        h = np.tanh(x @ self.p["W1"] + self.p["b1"])
        h = np.tanh(h @ self.p["W2"] + self.p["b2"])
        return h @ self.p["Wp"] + self.p["bp"], (h @ self.p["Wv"] + self.p["bv"])[:, 0]

    def act(self, obs, rng: np.random.Generator) -> tuple[int, float, float]:
        """Sample one action: (action, log-prob, value)."""
        logits, value = self.heads(obs)
        logits = logits[0].astype(np.float64)
        z = logits - logits.max()
        pr = np.exp(z)
        pr /= pr.sum()
        action = int(rng.choice(N_ACTIONS, p=pr))
        return action, float(np.log(pr[action] + 1e-12)), float(value[0])

    def greedy(self, obs) -> int:
        return int(np.argmax(self.heads(obs)[0][0]))
