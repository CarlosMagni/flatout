import unittest

from carreralib import ControlUnit
from carreralib.connection import Connection


class EchoConnection(Connection):
    """Records frames and answers like the bot firmware (letter echo)."""

    def __init__(self):
        self.sent = []

    def send(self, buf, offset=0, size=None):
        self.sent.append(bytes(buf))

    def recv(self, maxlength=None):
        return self.sent[-1][:1]


class BotCommandTest(unittest.TestCase):
    def setUp(self):
        self.conn = EchoConnection()
        self.cu = ControlUnit(self.conn)

    def test_setbot(self):
        self.cu.setbot(0, 7)
        self.cu.setbot(1, 10)
        self.cu.setbot(5, 15)
        self.cu.setbot(2, 0)
        self.assertEqual(self.conn.sent, [b"B07", b"B1:", b"B5?", b"B20"])

    def test_botlane_botrelease(self):
        self.cu.botlane(0)
        self.cu.botrelease(3)
        self.assertEqual(self.conn.sent, [b"B0L", b"B3A"])

    def test_range(self):
        for args in ((-1, 0), (6, 0), (7, 0), (0, -1), (0, 16)):
            with self.assertRaises(ValueError):
                self.cu.setbot(*args)
        for address in (-1, 6, 7):
            with self.assertRaises(ValueError):
                self.cu.botlane(address)
            with self.assertRaises(ValueError):
                self.cu.botrelease(address)
        self.assertEqual(self.conn.sent, [])
