"""Link latency bench: round trips of the CU's own poll, plus the `C` acknowledgement.

Nothing here moves a car: the samples are the round trips the link thread measures
anyway (`?` poll, `C` frames). The only frame the bench may add is the frame the link
would send in any case (the current table; all slots on their controllers when no
bot is active), never a different one. See docs/api.md `link.bench`.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable
from typing import Any

MAX_SAMPLES = 300
C_PROBES = 40  # idle C frames sent at most (one per rail cycle at best)
BINS = 10
FINISH_GRACE_S = 12.0  # stop waiting for the C probes after this long


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 2)


def stats(samples: list[float]) -> dict[str, Any] | None:
    """min / median / p95 / max / mean / jitter, in ms, from round trips in seconds.

    p95 = nearest rank. jitter = mean absolute difference of consecutive samples."""
    if not samples:
        return None
    xs = sorted(samples)
    n = len(xs)
    jitter = sum(abs(a - b) for a, b in zip(samples, samples[1:])) / (n - 1) if n > 1 else 0.0
    return {"n": n, "min_ms": _ms(xs[0]), "median_ms": _ms(statistics.median(xs)),
            "p95_ms": _ms(xs[max(0, math.ceil(0.95 * n) - 1)]), "max_ms": _ms(xs[-1]),
            "mean_ms": _ms(sum(xs) / n), "jitter_ms": _ms(jitter)}


def histogram(samples: list[float], bins: int = BINS) -> list[dict[str, Any]]:
    if not samples:
        return []
    lo, hi = min(samples), max(samples)
    if hi - lo < 1e-6:
        return [{"from_ms": _ms(lo), "to_ms": _ms(hi), "count": len(samples)}]
    width = (hi - lo) / bins
    counts = [0] * bins
    for x in samples:
        counts[min(bins - 1, int((x - lo) / width))] += 1
    return [{"from_ms": _ms(lo + i * width), "to_ms": _ms(lo + (i + 1) * width), "count": c}
            for i, c in enumerate(counts)]


class Bench:
    """One run: filled by the link thread, finished when `n` polls are in."""

    def __init__(self, n: int, c_target: int, on_done: Callable[[dict[str, Any], str | None], None]):
        self.n = n
        self.c_target = c_target  # idle C probes wanted (0: none, e.g. a bot is active)
        self.on_done = on_done
        self.poll: list[float] = []
        self.c: list[float] = []
        self.lost = 0  # polls with no answer in time
        self.t0 = time.monotonic()

    def wants_c_probe(self) -> bool:
        return len(self.c) < self.c_target

    def finished(self) -> bool:
        if len(self.poll) + self.lost < self.n:
            return False
        return len(self.c) >= self.c_target or time.monotonic() - self.t0 > FINISH_GRACE_S

    def result(self) -> dict[str, Any]:
        s = stats(self.poll) or {}
        return {"n": self.n, "answered": len(self.poll), "lost": self.lost,
                "min_ms": s.get("min_ms"), "median_ms": s.get("median_ms"),
                "p95_ms": s.get("p95_ms"), "max_ms": s.get("max_ms"),
                "mean_ms": s.get("mean_ms"), "jitter_ms": s.get("jitter_ms"),
                "histogram": histogram(self.poll), "c_ack": stats(self.c),
                "duration_ms": _ms(time.monotonic() - self.t0)}
