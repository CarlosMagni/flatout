"""Clock handling: CU clock -> host clock (pure standard library)."""

from __future__ import annotations

from collections import deque

CU_WRAP = 1 << 32  # the CU timestamp is 32 bits of milliseconds


class ClockSync:
    """Offset between the CU clock and the host monotonic clock.

    Every `crossing` carries `cu_ts` (CU clock, ms, 32 bits, set by the CU at the instant of the
    pass) and reaches the host some time later (Bluetooth polling and queuing, 10 to 150 ms).
    The delay is never negative, so `host_receive - cu_time` is the true offset PLUS a delay:
    the best estimate is the minimum over many events. A rolling minimum (window `window_s`
    of host time) follows the slow drift between the two crystals (tens of ppm) while it
    absorbs the queuing. The estimate is biased high by the smallest delay seen: on a real
    link a few ms to a few tens of ms; the camera latency estimate (`estimate_latency`)
    folds any constant part of that bias into one number.
    """

    def __init__(self, window_s: float = 120.0) -> None:
        self.window_s = window_s
        self._samples: deque[tuple[float, float]] = deque()  # (host time, host - cu seconds)
        self._cu_ms: int | None = None  # unwrapped CU time of the latest event (ms)
        self._last_mod: int | None = None
        self.n = 0

    def _unwrap(self, cu_ts: int) -> int:
        if self._last_mod is None or self._cu_ms is None:
            return cu_ts
        delta = (cu_ts - self._last_mod + CU_WRAP // 2) % CU_WRAP - CU_WRAP // 2
        return self._cu_ms + delta

    def add(self, cu_ts: int, t_host: float) -> None:
        """Register an event: CU timestamp `cu_ts` (ms), received at host time `t_host`."""
        cu = self._unwrap(int(cu_ts))
        self._cu_ms, self._last_mod = cu, int(cu_ts) % CU_WRAP
        self._samples.append((t_host, t_host - cu / 1000.0))
        while self._samples and t_host - self._samples[0][0] > self.window_s:
            self._samples.popleft()
        self.n += 1

    @property
    def offset(self) -> float | None:
        """host time = CU time (s, unwrapped) + offset. None before the first event."""
        return min(v for _, v in self._samples) if self._samples else None

    def cu_to_host(self, cu_ts: int) -> float | None:
        """Host time of a CU timestamp (near the latest event), or None before any event."""
        off = self.offset
        if off is None:
            return None
        return self._unwrap(int(cu_ts)) / 1000.0 + off
