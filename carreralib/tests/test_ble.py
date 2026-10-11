import asyncio
import sys
import types
import unittest

from carreralib.connection import ConnectionError, TimeoutError


class FakeBleakClient:
    """Stand-in for bleak.BleakClient: echoes '0' as a version notification."""

    instances = []
    fail_connect = False

    def __init__(self, address, disconnected_callback=None, timeout=None):
        self.address = address
        self.disconnected_callback = disconnected_callback
        self.notify = None
        self.loop = None
        FakeBleakClient.instances.append(self)

    async def __aenter__(self):
        if FakeBleakClient.fail_connect:
            raise OSError("device not found")
        self.loop = asyncio.get_running_loop()
        return self

    async def __aexit__(self, *exc):
        return False

    async def start_notify(self, uuid, callback):
        self.notify = callback

    async def write_gatt_char(self, uuid, data):
        if data == b"0":
            self.notify(None, bytearray(b"53390$"))

    def drop(self):
        self.loop.call_soon_threadsafe(self.disconnected_callback, self)


class BLEConnectionTest(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get("bleak")
        fake = types.ModuleType("bleak")
        fake.BleakClient = FakeBleakClient
        sys.modules["bleak"] = fake
        FakeBleakClient.instances = []
        FakeBleakClient.fail_connect = False

    def tearDown(self):
        if self.saved is None:
            del sys.modules["bleak"]
        else:
            sys.modules["bleak"] = self.saved

    def test_roundtrip_and_link_loss(self):
        from carreralib.ble import BLEConnection

        conn = BLEConnection("00:11:22:33:44:55", timeout=0.5, connect_timeout=1.0)
        conn.send(b"0")
        self.assertEqual(conn.recv(), b"053390")
        FakeBleakClient.instances[0].drop()
        with self.assertRaises(ConnectionError):
            conn.recv()
        # a lost link must refuse writes instead of dropping them
        with self.assertRaises(ConnectionError):
            conn.send(b"B00")
        conn.close()

    def test_connect_failure_does_not_hang(self):
        from carreralib.ble import BLEConnection

        FakeBleakClient.fail_connect = True
        with self.assertRaises(ConnectionError):
            BLEConnection("00:11:22:33:44:55", timeout=0.5, connect_timeout=1.0)

    def test_recv_timeout(self):
        from carreralib.ble import BLEConnection

        conn = BLEConnection("00:11:22:33:44:55", timeout=0.1, connect_timeout=1.0)
        with self.assertRaises(TimeoutError):
            conn.recv()
        conn.close()
