"""Controller interfaces: how the bots reach the cars outside the CU (docs/controller-interface.md).

The CU link (racelab/cu/) drives the bots through the CU itself: its `C` table, or the bot firmware
module's `B`. A controller interface is hardware on a controller socket that plays the hand
controllers instead, so the bots drive on any CU. `ControllerInterface` holds the bot model and the
safety rules and runs its own thread, apart from the CU link; `SerialInterface` speaks the
documented wire protocol over any serial port, so most hardware needs no Python at all.
"""

from .interface import ControllerInterface
from .serial import SerialInterface, list_ports

__all__ = ["ControllerInterface", "SerialInterface", "list_ports"]
