import logging
from collections import namedtuple

from . import connection, protocol

logger = logging.getLogger(__name__)


class ControlUnit:
    """Interface to a Carrera Digital 124/132 Control Unit."""

    class Status(namedtuple("Status", "fuel start mode pit display")):
        """Response type returned if no timer events are pending.

        This is a :class:`collections.namedtuple` subclass with the
        following read-only attributes:

        +-----------------+-------+-------------------------------------------+
        | Attribute       | Index | Value                                     |
        +=================+=======+===========================================+
        | :attr:`fuel`    | 0     | Eight-item list of fuel levels (0..15)    |
        +-----------------+-------+-------------------------------------------+
        | :attr:`start`   | 1     | Start light indicator (0..9)              |
        +-----------------+-------+-------------------------------------------+
        | :attr:`mode`    | 2     | 4-bit mode bit mask                       |
        +-----------------+-------+-------------------------------------------+
        | :attr:`pit`     | 3     | 8-bit pit lane bit mask                   |
        +-----------------+-------+-------------------------------------------+
        | :attr:`display` | 4     | Number of drivers to display (6 or 8)     |
        +-----------------+-------+-------------------------------------------+

        """

        __slots__ = ()

        FUEL_MODE = 0x1
        """Mode bit mask indicating fule mode is enabled."""

        REAL_MODE = 0x2
        """Mode bit mask indicating real fuel mode is enabled."""

        PIT_LANE_MODE = 0x4
        """Mode bit mask indicating a pit lane adapter is connected."""

        LAP_COUNTER_MODE = 0x8
        """Mode bit mask indicating a lap counter is connected."""

    class Timer(namedtuple("Timer", "address timestamp sector")):
        """Response type for timer events.

        This is a :class:`collections.namedtuple` subclass with the
        following read-only attributes:

        +-------------------+-------+-----------------------------------------+
        | Attribute         | Index | Value                                   |
        +===================+=======+=========================================+
        | :attr:`address`   | 0     | Controller address (0..7)               |
        +-------------------+-------+-----------------------------------------+
        | :attr:`timestamp` | 1     | 32-bit time stamp in milleseconds       |
        +-------------------+-------+-----------------------------------------+
        | :attr:`sector`    | 2     | Sector (1 for start/finish, 2 or 3 for  |
        |                   |       | times reported by Check Lanes)          |
        +-------------------+-------+-----------------------------------------+

        """

        __slots__ = ()

    PACE_CAR_ESC_BUTTON_ID = 1
    """The Control Unit's PACE CAR/ESC button ID."""

    START_ENTER_BUTTON_ID = 2
    """The Control Unit's START/ENTER button ID."""

    SPEED_BUTTON_ID = 5
    """The Control Unit's SPEED button ID."""

    BRAKE_BUTTON_ID = 6
    """The Control Unit's BRAKE button ID."""

    FUEL_BUTTON_ID = 7
    """The Control Unit's FUEL button ID."""

    CODE_BUTTON_ID = 8
    """The Control Unit's CODE button ID."""

    BOT_ADDRESSES = range(6)
    """Controller addresses that can be driven by a software driver (bot).

    Software drivers require the custom CU firmware with the ``B``
    command; addresses 6 and 7 (autonomous and pace car) are not
    covered.

    """

    def __init__(self, device, **kwargs):
        if isinstance(device, connection.Connection):
            self.__connection = device
        else:
            logger.debug("Connecting to %s", device)
            self.__connection = connection.open(device, **kwargs)
            logger.debug("Connection established")

    def botlane(self, address):
        """Request a one-shot lane change for software driver `address`.

        The custom firmware ignores the request if `address` is not
        currently in bot mode.

        """
        self._checkbot(address)
        return self.request(protocol.pack("cYc", b"B", address, b"L"))

    def botrelease(self, address):
        """Hand controller `address` back to the physical controller."""
        self._checkbot(address)
        return self.request(protocol.pack("cYc", b"B", address, b"A"))

    def close(self):
        """Close the connection to the CU."""
        logger.debug("Closing connection")
        self.__connection.close()

    def clrpos(self):
        """Clear/reset the Position Tower display."""
        self.setword(6, 0, 9)

    def ignore(self, mask):
        """Ignore the controllers represented by bitmask `mask`."""
        self.request(protocol.pack("cBC", b":", mask))

    def poll(self):
        """Poll the CU for pending messages.

        The returned value will be an instance of either
        :class:`ControlUnit.Timer` or :class:`ControlUnit.Status`,
        depending on whether any timer events are pending.

        """
        res = self.request(b"?")
        if res.startswith(b"?:"):
            # recent CU versions report two extra unknown bytes with '?:'
            try:
                parts = protocol.unpack("2x8YYYBYC", res)
            except protocol.ChecksumError:
                parts = protocol.unpack("2x8YYYBYxxC", res)
            fuel, (start, mode, pitmask, display) = parts[:8], parts[8:]
            pit = tuple(pitmask & (1 << n) != 0 for n in range(8))
            return ControlUnit.Status(fuel, start, mode, pit, display)
        elif res.startswith(b"?"):
            address, timestamp, sector = protocol.unpack("xYIYC", res)
            return ControlUnit.Timer(address - 1, timestamp, sector)
        else:
            return None  # TODO: raise?

    def press(self, button_id):
        """Simulate pressing the CU button with the given ID."""
        return self.request(protocol.pack("cYC", b"T", button_id))

    def request(self, buf, maxlength=None):
        """Send a message to the CU and wait for a response."""
        logger.debug("Sending message %r", buf)
        self.__connection.send(buf)
        while True:
            res = self.__connection.recv(maxlength)
            if not res:
                logger.warning("Received unknown command response")
                break
            elif res.startswith(buf[0:1]):
                break
            else:
                logger.warning("Received unexpected message %r", res)
        logger.debug("Received message %r", res)
        return res

    def reset(self):
        """Reset the CU timer."""
        self.request(protocol.pack("cYYC", b"=", 1, 0))

    @staticmethod
    def ctl_frame(throttles, lanes=()):
        """Build a `C` frame: controller values for addresses 0..5, all at once.

        Stock CU firmware V339 reads this table *instead of* each physical
        controller, so everything downstream (fuel, start-light hold, false
        start detection) treats the values like a human's. `throttles` has 6
        items: ``None`` leaves the address on its physical controller, 0..15
        drives it from the host (0 holds the car stopped). `lanes` lists the
        addresses whose lane-change button is pressed.

        Per address two characters: the throttle, then ``1`` (button
        pressed) or ``0``. A table value of 0 means "no host value", so a
        host-held throttle of 0 is sent as ``P`` (0x20 after the ``0``
        offset: a bit the firmware ignores, keeping the value non-zero).
        """
        if len(throttles) != 6:
            raise ValueError("C needs 6 values (addresses 0..5)")
        data = bytearray()
        for address, value in enumerate(throttles):
            if value is None:
                data += b"00"
                continue
            if value < 0 or value > 15:
                raise ValueError("Throttle value out of range")
            data.append(ord("P") if value == 0 else ord("0") + value)
            data.append(ord("1") if address in lanes else ord("0"))
        checksum = (sum(c & 0xF for c in data) & 0xF) | 0x30
        return b"C" + bytes(data) + bytes([checksum])

    def setctl(self, throttles, lanes=()):
        """Send a `C` frame (see :meth:`ctl_frame`).

        The CU drops the whole table about 750 ms after the last frame (all
        addresses back to their controllers): resend at least every 500 ms
        while the host drives a car.
        """
        return self.request(self.ctl_frame(throttles, lanes))

    def setbot(self, address, value):
        """Set the throttle of software driver `address` (0..15).

        This puts controller `address` into bot mode: the CU sends
        `value` on the rails and ignores any physical controller on
        that address until :meth:`botrelease` is called.  Note that a
        throttle of zero keeps the bot in control of the car.

        """
        self._checkbot(address)
        if value < 0 or value > 15:
            raise ValueError("Throttle value out of range")
        return self.request(protocol.pack("cYY", b"B", address, value))

    def setbrake(self, address, value):
        """Set the brake value for controller `address`."""
        self.setword(1, address, value, repeat=2)

    def setfuel(self, address, value):
        """Set the fuel value for controller `address`."""
        self.setword(2, address, value, repeat=2)

    def setlap(self, value):
        """Set the current lap displayed by the Position Tower."""
        if value < 0 or value > 255:
            raise ValueError("Lap value out of range")
        self.setlap_hi(value >> 4)
        self.setlap_lo(value & 0xF)

    def setlap_hi(self, value):
        """Set the high nibble of the current lap."""
        self.setword(17, 7, value)

    def setlap_lo(self, value):
        """Set the low nibble of the current lap."""
        self.setword(18, 7, value)

    def setpos(self, address, position):
        """Set the controller's position displayed by the Position Tower."""
        if position < 1 or position > 8:
            raise ValueError("Position out of range")
        self.setword(6, address, position)

    def setspeed(self, address, value):
        """Set the speed value for controller address."""
        self.setword(0, address, value, repeat=2)

    def setword(self, word, address, value, repeat=1):
        if word < 0 or word > 31:
            raise ValueError("Command word out of range")
        if address < 0 or address > 7:
            raise ValueError("Address out of range")
        if value < 0 or value > 15:
            raise ValueError("Value out of range")
        if repeat < 1 or repeat > 15:
            raise ValueError("Repeat count out of range")
        buf = protocol.pack("cBYYC", b"J", word | address << 5, value, repeat)
        return self.request(buf)

    def start(self):
        """Initiate the CU start sequence."""
        self.press(self.START_ENTER_BUTTON_ID)

    def version(self):
        """Retrieve the CU version as a string."""
        res = self.request(b"0")
        if res:
            return protocol.unpack("x4sC", res)[0].decode()
        else:
            return None  # TODO: raise here?

    def _checkbot(self, address):
        if address not in self.BOT_ADDRESSES:
            raise ValueError("Bot address out of range")

    def fwu_start(self):
        """Initiate a CU firmware update."""
        # G: start update, B: control unit
        self.request(protocol.pack("ccC", b"G", b"B"))

    def fwu_write(self, data):
        """Write CU firmware update data."""
        if self.__connection.max_fwu_block_size is None:
            self.request(protocol.pack(f"c{len(data)}sC", b"E", data))
        else:
            n = self.__connection.max_fwu_block_size
            for block in (data[i : i + n] for i in range(0, len(data), n)):
                self.request(protocol.pack(f"cr{len(block)}s", b"F", len(block), block))
            self.request(protocol.pack("cC", b"E"))
