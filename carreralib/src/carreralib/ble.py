import asyncio
import logging
import queue
import threading

from .connection import BufferTooShort, Connection, ConnectionError, TimeoutError

SERVICE_UUID = "39df7777-b1b4-b90b-57f1-7144ae4e4a6a"
OUTPUT_UUID = "39df8888-b1b4-b90b-57f1-7144ae4e4a6a"
NOTIFY_UUID = "39df9999-b1b4-b90b-57f1-7144ae4e4a6a"

logger = logging.getLogger(__name__)


class BleakThread(threading.Thread):
    STOP_DATA = b""

    LINK_LOST = object()
    """Input queue sentinel: the BLE link is gone for good."""

    def __init__(self, address, connect_timeout=None):
        super().__init__(daemon=True)
        self.__address = address
        self.__connect_timeout = connect_timeout
        self.__connected = threading.Event()
        self.__cancelled = threading.Event()
        self.__error = None
        self.__lost = False
        self.__loop = None
        self.__input = queue.Queue()
        self.__output = asyncio.Queue()

    def run(self):
        try:
            asyncio.run(self.main())
        except Exception as e:
            logger.error("BLE connection failed: %r", e)
            self.__error = e
        finally:
            # unblock start() and any pending recv() whatever happened
            self.__lost = True
            self.__input.put(self.LINK_LOST)
            self.__connected.set()

    def start(self, timeout=None):
        super().start()
        if not self.__connected.wait(timeout):
            self.stop()
            raise TimeoutError("Timeout waiting for BLE connection")
        if self.__error is not None:
            raise ConnectionError(f"BLE connection failed: {self.__error}")
        if self.__lost:
            raise ConnectionError("BLE connection closed")

    def recv(self, timeout):
        try:
            data = self.__input.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Timeout waiting for BLE input")
        if data is self.LINK_LOST:
            self.__input.put(self.LINK_LOST)  # every later call fails as well
            raise ConnectionError("BLE connection lost")
        return data

    def send(self, data, timeout):
        loop = self.__loop
        # a write must never be dropped silently: the caller has to know
        # that, e.g., a "throttle 0" command did not go out
        if loop is None or not loop.is_running() or self.__lost:
            if data is self.STOP_DATA:
                return
            raise ConnectionError("BLE connection is not active")

        async def coro():
            await self.__output.put(data)

        try:
            f = asyncio.run_coroutine_threadsafe(coro(), loop)
            return f.result(timeout=timeout)
        except RuntimeError:
            if loop.is_running():
                raise
            if data is not self.STOP_DATA:
                raise ConnectionError("BLE connection is not active")

    def stop(self, timeout=None):
        self.__cancelled.set()
        self.send(self.STOP_DATA, timeout)

    async def main(self):
        from bleak import BleakClient

        self.__loop = asyncio.get_running_loop()

        def disconnected(_client):
            logger.warning("BLE device disconnected")
            self.__lost = True
            self.__input.put_nowait(self.LINK_LOST)
            self.__output.put_nowait(self.STOP_DATA)

        kwargs = {"disconnected_callback": disconnected}
        if self.__connect_timeout is not None:
            kwargs["timeout"] = self.__connect_timeout

        async with BleakClient(self.__address, **kwargs) as client:
            logger.info("Connected to BLE device: %r", client)
            if self.__cancelled.is_set():
                return

            def notify(_, data: bytearray):
                logger.debug("Received BLE notification: %r", data)
                try:
                    self.__input.put_nowait(data)
                except queue.Full:
                    logger.error("BLE input queue is full")

            await client.start_notify(NOTIFY_UUID, notify)

            self.__connected.set()

            while True:
                data = await self.__output.get()
                if data is not self.STOP_DATA:
                    logger.debug("Writing BLE data: %r", data)
                    await client.write_gatt_char(OUTPUT_UUID, data)
                else:
                    break

            logger.info("Closing BLE connection: %r", client)


class BLEConnection(Connection):
    def __init__(self, address, timeout=1.0, connect_timeout=20.0):
        self.__thread = t = BleakThread(address, connect_timeout)
        self.__timeout = timeout
        t.start(connect_timeout + 5.0 if connect_timeout is not None else None)

    def close(self):
        self.__thread.stop()
        self.__thread.join()

    def recv(self, maxlength=None):
        buf = self.__thread.recv(self.__timeout)
        # BLE notifications ending with '$' do not start with command letter
        if buf.endswith(b"$"):
            if len(buf) == 6:
                buf = b"0" + buf[:-1]
            else:
                buf = b"?" + buf[:-1]
        if maxlength is not None and maxlength < len(buf):
            raise BufferTooShort("Buffer too short for data received")
        else:
            return buf

    def send(self, buf, offset=0, size=None):
        n = len(buf)
        if offset < 0:
            raise ValueError("offset is negative")
        elif n < offset:
            raise ValueError("buffer length < offset")
        elif size is None:
            size = n - offset
        elif size < 0:
            raise ValueError("size is negative")
        elif offset + size > n:
            raise ValueError("buffer length < offset + size")
        self.__thread.send(buf[offset : offset + size], self.__timeout)

    max_fwu_block_size = 18

    @classmethod
    def scan(cls):
        from bleak import BleakScanner
        from bleak.exc import BleakError

        def is_cu(device, *_):
            return device.name == "Control_Unit"

        async def discover():
            # TODO: this will return only the first CU/AppConnect device found
            try:
                device = await BleakScanner.find_device_by_filter(is_cu)
                return [(device.address, device.name)] if device else []
            except BleakError:
                return []

        yield from asyncio.run(discover())
