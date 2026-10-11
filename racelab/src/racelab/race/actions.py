"""CU actions requested by the race logic.

The engine and the tower never talk to the link directly: they receive an
object that implements `CUActions` (the app wires it to `CULink`, the tests
to `RecordingActions`). All methods are non-blocking (queued).
"""

from __future__ import annotations

from typing import Any, Protocol


class CUActions(Protocol):
    def press(self, button: int) -> None: ...  # T<n> (events.BUTTON_*)

    def reset_timer(self) -> None: ...  # =10

    def ignore(self, mask: int) -> None: ...  # :<mask> (bit = address excluded from timing)

    def clrpos(self) -> None: ...  # position tower: clear

    def setpos(self, address: int, position: int) -> None: ...  # J mot 6

    def setlap(self, lap: int) -> None: ...  # J mots 17/18 (0..255)


class RecordingActions:
    """Test double: records every call, in order.

    `calls` holds tuples `(name, *arguments)`, e.g. `("press", 2)`.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def press(self, button: int) -> None:
        self.calls.append(("press", button))

    def reset_timer(self) -> None:
        self.calls.append(("reset_timer",))

    def ignore(self, mask: int) -> None:
        self.calls.append(("ignore", mask))

    def clrpos(self) -> None:
        self.calls.append(("clrpos",))

    def setpos(self, address: int, position: int) -> None:
        self.calls.append(("setpos", address, position))

    def setlap(self, lap: int) -> None:
        self.calls.append(("setlap", lap))

    def of(self, name: str) -> list[tuple[Any, ...]]:
        """Arguments of all calls to `name`, e.g. `of("press") == [(2,), (2,)]`."""
        return [call[1:] for call in self.calls if call[0] == name]

    def clear(self) -> None:
        self.calls.clear()
