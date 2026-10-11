"""Position tower: coalescing queue towards the CU.

Programming the tower is slow (~150 ms per operation, docs/protocol.md §6).
The engine therefore publishes a *desired* state (lap counter + position of
each car) and `tick()` sends at most one operation per interval: the lap
counter first, then P1, P2… and only what differs from what was already sent.
"""

from __future__ import annotations

from .actions import CUActions

LAP_COUNTER_MODULO = 250  # the tower can only display 0..249 (carreralib convention)
MAX_POSITIONS = 8


class TowerManager:
    def __init__(self, actions: CUActions, *, enabled: bool = True, min_interval_s: float = 0.15) -> None:
        self._actions = actions
        self.enabled = enabled
        self._min_interval_s = min_interval_s
        self._want_lap: int | None = None
        self._want_pos: dict[int, int] = {}  # address -> desired position
        self._sent_lap: int | None = None
        self._sent_pos: dict[int, int] = {}
        self._last_op: float | None = None

    def clear(self) -> None:
        """Clear the tower (immediate) and forget the desired and sent states."""
        if not self.enabled:
            return
        self._actions.clrpos()
        self._want_lap = self._sent_lap = None
        self._want_pos.clear()
        self._sent_pos.clear()

    def update(self, order: list[int], leader_laps: int) -> None:
        """New desired state: `order` = addresses from 1st to last place."""
        if not self.enabled:
            return
        self._want_lap = leader_laps % LAP_COUNTER_MODULO
        self._want_pos = {
            address: position
            for position, address in enumerate(order[:MAX_POSITIONS], start=1)
            if 0 <= address < MAX_POSITIONS
        }

    def tick(self, now: float) -> None:
        """Send at most one operation (priority: lap counter, then P1, P2…)."""
        if not self.enabled:
            return
        if self._last_op is not None and now - self._last_op < self._min_interval_s:
            return
        if self._want_lap is not None and self._want_lap != self._sent_lap:
            self._actions.setlap(self._want_lap)
            self._sent_lap = self._want_lap
        else:
            pending = [
                (position, address)
                for address, position in self._want_pos.items()
                if self._sent_pos.get(address) != position
            ]
            if not pending:
                return
            position, address = min(pending)
            self._actions.setpos(address, position)
            self._sent_pos[address] = position
        self._last_op = now
