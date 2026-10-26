"""The link to the Control Unit: a single thread that owns the connection.

Responsibilities (docs/bot-protocol.md, safety rules):
- connection + automatic reconnection, version read, bot transport (stock C table or B module);
- continuous `?` poll -> StatusMsg / TimerMsg;
- command queue (buttons, position tower, programming);
- bot throttle: coalescing (≤ 1 send per slot per rail cycle), heartbeat,
  priority E-STOP, throttle 0 forced on every reconnection, throttle 0 +
  hand control back on shutdown. The rules are drive/model.py's, shared with the
  controller interfaces; here: how they reach the CU (C table, B module).

All public methods are thread-safe and non-blocking; events are delivered
via `emit` from the I/O thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from carreralib import ControlUnit
from carreralib.connection import Connection
from carreralib.connection import ConnectionError as CUConnectionError
from carreralib.connection import TimeoutError as CUTimeoutError

from ..config import ConnectionConfig, SafetyConfig
from ..drive.model import BotModel
from ..events import (
    BOT_SLOTS,
    BotSent,
    FirmwareProgress,
    LinkState,
    LinkStats,
    RawFrame,
    StatusMsg,
    TimerMsg,
)
from .bench import C_PROBES, Bench
from .unit import FORBIDDEN_LETTERS, CommandRejected, Parsed, RaceControlUnit

logger = logging.getLogger(__name__)

_UNKNOWN = object()  # throttle actually applied by the CU is unknown
# C table (stock V339): the CU drops it ~750 ms after the last frame, so resend well before. Each frame
# costs a poll round trip on this shared link (~50 ms over the AppConnect), which is why it is not the
# interface's 100 ms; [safety] heartbeat_ms can only shorten it.
CTL_HEARTBEAT_S = 0.3
# firmware versions with the C table: added in V338, identical in V339 (V336/V337 do not
# dispatch C); checked against the official images, V339 rebuilt in firmware/reconstruction
CTL_FIRMWARES = ("338", "339")
# pause between two polls: a real CU link waits on I/O anyway, a virtual CU answers at once and
# the loop would starve the app; bot commands, E-STOP and queued commands wake it immediately
POLL_PAUSE_S = 0.004
BOOTLOADER_WAIT_S = 2.0  # after `GB`, time for the bootloader to start (carreralib.fw)
REBOOT_WAIT_S = 3.0  # after the last line, before looking for the rebooted CU


class LinkLost(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code  # LinkState.error_code


class FirmwareFlashed(Exception):
    """Image fully sent: the CU reboots, we reconnect."""


class FirmwareFailed(Exception):
    pass


class _FirmwareJob:
    def __init__(self, lines: list[bytes], recovery: bool):
        self.lines = lines
        self.recovery = recovery  # CU already in bootloader: no `GB`, no handshake
        self.abort = threading.Event()


@dataclass
class _Command:
    label: str
    run: Callable[[RaceControlUnit], object]
    on_done: Callable[[object, Exception | None], None] | None = None


class CULink:
    def __init__(
        self,
        opener: Callable[[], Connection],
        *,
        transport: str,
        device: str | None,
        conn_cfg: ConnectionConfig,
        safety: SafetyConfig,
        emit: Callable[[object], None],
    ):
        self._opener = opener
        self._transport = transport
        self._device = device
        self._cfg = conn_cfg
        self._safety = safety
        self._emit = emit

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # bots: desired (app, the shared model) / applied (CU)
        self._bots = BotModel(safety, heartbeat_s=min(safety.heartbeat_ms / 1000, CTL_HEARTBEAT_S),
                              min_gap_s=safety.min_bot_interval_ms / 1000, wake=self._wake.set)
        self._sent: list[object] = [_UNKNOWN] * 6  # int | None | _UNKNOWN
        self._sent_at = [0.0] * 6
        self._last_known: list[int | None] = [None] * 6  # last throttle actually acknowledged
        self._bench: Bench | None = None

        self._high: deque[_Command] = deque()
        self._low: deque[_Command] = deque()
        self._job: _FirmwareJob | None = None
        self._rebooting = False

        self.state = "closed"
        self.version: str | None = None
        self.bots_supported: bool | None = None  # software drivers available (C or B)
        self.bot_transport: str | None = None  # "C" | "B" | None
        self.bot_module: bool | None = None  # the bot firmware module answers B
        self.trace = False  # raw frames to the console (excluding polls)
        self.trace_polls = False

        self._ever_connected = False
        self._stats = {"rx": 0, "tx": 0, "bot": 0}
        self._rtt: float | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="cu-link", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 4.0) -> None:
        """Clean stop: throttle 0 (+ hand control back) on the bots, then close."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.error("the link thread did not stop in time")

    # ------------------------------------------------------------ API bots

    def set_bot(self, slot: int, value: int | None) -> None:
        """Desired throttle of the slot (0..15), or None to hand control back to the controller."""
        self._bots.set_bot(slot, value)

    def lane(self, slot: int) -> None:
        self._bots.lane(slot)

    def estop(self) -> None:
        """Emergency stop: throttle 0 on all active bots, before any other frame."""
        self._bots.estop()

    def clear_estop(self) -> None:
        self._bots.clear_estop()

    # ------------------------------------------------------------ hardware test (docs/api.md)

    def pause_heartbeat(self, seconds: float) -> None:
        """Hand-back test: stop refreshing the C table for `seconds` (<= 3 s), so that the CU's
        ~750 ms expiry hands the slots back. Only the idle heartbeat is held: a changed frame
        or an E-STOP still goes out. The pause ends by itself; when it does, the active bots are
        zeroed, never resumed at their old throttle."""
        if self.bot_transport != "C":
            raise ValueError("heartbeat pause: only with the C command")
        self._bots.pause_heartbeat(seconds)

    def resume_heartbeat(self) -> None:
        self._bots.resume_heartbeat()

    @property
    def hb_paused(self) -> bool:
        return self._bots.hb_pause_until > time.monotonic()

    def bench(self, n: int, on_done: Callable[[dict, str | None], None]) -> None:
        """Time `n` round trips of the CU poll (see cu/bench.py); `on_done(result, error)` runs in the link thread."""
        with self._lock:
            if self.state != "connected":
                raise ValueError("CU not connected")
            if self._bench is not None:
                raise RuntimeError("a latency test is already running")
            idle = all(t is None for t in self._bots.targets())
            c_target = min(n, C_PROBES) if self.bot_transport == "C" and idle else 0
            self._bench = Bench(n, c_target, on_done)
        self._wake.set()

    def bench_cancel(self) -> Bench | None:
        """Stop the run (if any) and hand back what it collected."""
        with self._lock:
            bench, self._bench = self._bench, None
        return bench

    def _bench_end(self, error: str | None = None) -> None:
        with self._lock:
            bench, self._bench = self._bench, None
        if bench is not None:
            try:
                bench.on_done(bench.result(), error)
            except Exception:
                logger.exception("bench callback")

    def resume_bots(self) -> None:
        """Lift the hold set on reconnection (the app has acknowledged throttle 0)."""
        self._bots.resume()

    @property
    def holds(self) -> frozenset[str]:
        return self._bots.holds

    def bot_targets(self) -> list[int | None]:
        return self._bots.targets()

    def bot_applied(self) -> list[int | None]:
        with self._lock:
            return [None if v is _UNKNOWN else v for v in self._sent]  # type: ignore[misc]

    # ------------------------------------------------------------ other commands

    def press(self, button: int) -> None:
        self._queue(self._high, f"T{button}", lambda cu: cu.press(button))

    def reset_timer(self) -> None:
        self._queue(self._high, "=", lambda cu: cu.reset())

    def ignore(self, mask: int) -> None:
        self._queue(self._high, f":{mask:02x}", lambda cu: cu.ignore(mask & 0xFF))

    def clrpos(self) -> None:
        self._queue(self._low, "clrpos", lambda cu: cu.clrpos())

    def setpos(self, address: int, position: int) -> None:
        self._queue(self._low, f"pos{address}={position}", lambda cu: cu.setpos(address, position))

    def setlap(self, lap: int) -> None:
        self._queue(self._low, f"lap={lap}", lambda cu: cu.setlap(lap % 256))

    def setword(self, word: int, address: int, value: int, repeat: int = 1,
                on_done: Callable[[object, Exception | None], None] | None = None) -> None:
        """J programming word (car setup: reserved for the Garage panel)."""
        self._queue(self._high, f"J{word}/{address}={value}",
                    lambda cu: cu.setword(word, address, value, repeat), on_done)

    def send_raw(self, frame: bytes,
                 on_done: Callable[[object, Exception | None], None] | None = None) -> None:
        """Diagnostics console: frame without `"`/`$`, forbidden letters refused."""
        if not frame or frame[0] in FORBIDDEN_LETTERS:
            raise ValueError("empty frame or forbidden command (G/E/F/C)")
        if frame[0:1] == b"B":
            raise ValueError("use the bot commands, not the console, for B")
        self._queue(self._high, f"raw {frame!r}", lambda cu: cu.request(frame), on_done)

    # ------------------------------------------------------------ firmware update

    def firmware_update(self, lines: list[bytes], *, recovery: bool = False) -> None:
        """Flash the image (.HMF lines); progress arrives as FirmwareProgress.

        Flashing runs in the link thread, between two cycles: nothing else is
        sent to the CU meanwhile. `recovery`: the CU stayed in its bootloader
        (interrupted flash), so the lines are sent again directly.
        """
        if not lines:
            raise ValueError("empty image")
        with self._lock:
            if self._job is not None:
                raise RuntimeError("an update is already in progress")
            self._job = _FirmwareJob(list(lines), recovery)
        self._wake.set()

    def firmware_abort(self) -> None:
        job = self._job
        if job is not None:
            job.abort.set()

    @property
    def updating(self) -> bool:
        return self._job is not None

    def _queue(self, q: deque[_Command], label: str, run, on_done=None) -> None:
        with self._lock:
            if len(q) > 200:
                raise RuntimeError("CU command queue full")
            q.append(_Command(label, run, on_done))
        self._wake.set()

    def queue_length(self) -> int:
        with self._lock:
            return len(self._high) + len(self._low)

    # ------------------------------------------------------------ thread I/O

    def _set_state(self, state: str, error: str | None = None, error_code: str | None = None) -> None:
        self.state = state
        self._emit(LinkState(state, self._transport, self._device, self.version,
                             self.bots_supported, error, self.bot_transport, self.bot_module, error_code))

    def _run(self) -> None:
        while not self._stop.is_set():
            pause = self._cfg.reconnect_delay
            self._set_state("lost" if self._ever_connected and not self._rebooting else "connecting")
            try:
                conn = self._opener()
            except Exception as e:  # BLE device not found, port missing…
                logger.warning("cannot connect to the CU: %s", e)
                self._set_state("lost" if self._ever_connected else "connecting", str(e))
                self._stop.wait(self._cfg.reconnect_delay)
                continue
            cu = RaceControlUnit(conn, on_message=self._dispatch, on_raw=self._raw,
                                 response_timeout=self._cfg.timeout)
            try:
                job = self._job
                if job is not None and job.recovery:
                    self._flash(cu, job)
                else:
                    self._session(cu)
            except FirmwareFlashed:
                self._rebooting = True
                pause = REBOOT_WAIT_S
                self._set_state("connecting")
            except FirmwareFailed as e:
                self._set_state("lost", str(e))
            except (LinkLost, CUConnectionError, CommandRejected, OSError) as e:
                logger.warning("CU link lost: %s", e)
                self._bots.hold_at_zero()  # throttle 0 and hold until the app takes control again
                with self._lock:
                    self._sent = [_UNKNOWN] * 6
                self._set_state("lost", str(e), getattr(e, "code", None))
            except Exception:
                logger.exception("unexpected error in the CU link")
                self._set_state("lost", "internal error (see the log)")
            finally:
                self._bench_end("link lost")
                try:
                    conn.close()
                except Exception:
                    pass
            if not self._stop.is_set():
                self._stop.wait(pause)
        self._set_state("closed")

    def _session(self, cu: RaceControlUnit) -> None:
        self._handshake(cu)
        timeouts = 0
        last_stats = time.monotonic()
        while True:
            if self._stop.is_set():
                self._shutdown(cu)
                return
            if self._job is not None:
                self._flash(cu, self._job)  # raises FirmwareFlashed / FirmwareFailed
            try:
                self._service(cu)
                timeouts = 0
                if self._wake.wait(POLL_PAUSE_S):
                    self._wake.clear()
            except CUTimeoutError as e:
                timeouts += 1
                if self._bench is not None:
                    self._bench.lost += 1
                logger.debug("CU timeout (%d): %s", timeouts, e)
                if timeouts >= self._cfg.max_timeouts:
                    raise LinkLost(f"{timeouts} missed replies") from e
            now = time.monotonic()
            if now - last_stats >= 1.0:
                self._publish_stats(now - last_stats)
                last_stats = now

    def _handshake(self, cu: RaceControlUnit) -> None:
        """Version, bot transport, and the bots back in a known state (throttle 0), before any poll."""
        try:
            self.version = cu.version()
        except (CommandRejected, CUTimeoutError) as e:
            raise LinkLost("the CU does not give its version (powered off, or stuck in update mode?)",
                           "no_version") from e
        try:
            module = cu.probe_bots()
        except CUTimeoutError:
            # Hardware-8 (STM32, v8xxx) CUs answer the version and the status but silently
            # ignore unknown commands (no '#' refusal): prove the link is alive with a
            # status poll, then treat the missing B answer as "stock firmware" (no module).
            try:
                cu.poll_message()
            except (CUTimeoutError, CommandRejected) as e:
                raise LinkLost("the CU does not answer at all (powered off?)") from e
            module = False
            logger.info("the CU answers but ignores B/A (hardware 8?): no bot module")
        transport = self._choose_transport(self.version, module)
        self._rebooting = False
        with self._lock:
            self.bot_module = module
            self.bot_transport = transport
            self.bots_supported = transport is not None
        self._bots.forget()
        target = self._bots.targets()
        if transport == "C":
            # throttle 0 on the app's bots, every other slot on its controller
            frame = self._bots.frame(time.monotonic())
            try:
                cu.setctl(list(frame.values))
            except CommandRejected:
                # e.g. an adapter that filters C: fall back to the bot module if there is one
                logger.error("the CU (or its adapter) rejects the C command")
                transport = "B" if module else None
                with self._lock:
                    self.bot_transport = transport
                    self.bots_supported = transport is not None
            else:
                self._bots.sent(frame, time.monotonic())
                for slot in BOT_SLOTS:
                    self._mark_sent(slot, frame.values[slot])
                if module and not self._ever_connected and self._safety.release_all_on_connect:
                    for slot in BOT_SLOTS:  # a previous B session may have left the module active
                        cu.botrelease(slot)
        if transport == "B":
            # no other frame before the CU is back in a known state:
            # throttle 0 on the app's bots, and (first connection) control handed
            # back on the other slots: a previous crash may have left a bot active.
            release = not self._ever_connected and self._safety.release_all_on_connect
            for slot in BOT_SLOTS:
                if target[slot] is not None:
                    cu.setbot(slot, 0)
                    self._mark_sent(slot, 0)
                else:
                    if release:
                        cu.botrelease(slot)
                    self._mark_sent(slot, None)
        self._ever_connected = True
        self._set_state("connected")

    def _service(self, cu: RaceControlUnit) -> None:
        if self.bot_transport == "C":
            self._service_ctl(cu)
        elif self.bot_transport == "B":
            self._service_bots(cu)
        cmd = None
        with self._lock:
            if self._high:
                cmd = self._high.popleft()
            elif self._low:
                cmd = self._low.popleft()
        if cmd is not None:
            self._run_command(cu, cmd)
        msg = cu.poll_message()
        if cu.last_rtt is not None:
            self._rtt = cu.last_rtt if self._rtt is None else 0.8 * self._rtt + 0.2 * cu.last_rtt
        bench = self._bench
        if bench is not None:
            if cu.last_rtt is not None and len(bench.poll) < bench.n - bench.lost:
                bench.poll.append(cu.last_rtt)
            if bench.finished():
                self._bench_end()
        if msg is not None:
            self._dispatch(msg)

    def _service_bots(self, cu: RaceControlUnit) -> None:
        now = time.monotonic()
        heartbeat = self._safety.heartbeat_ms / 1000  # the module's watchdog is 1 s, the C table's 0.75 s
        min_gap = self._bots.min_gap_s
        urgent, lanes, target = self._bots.take()
        with self._lock:
            sent = list(self._sent)
            sent_at = list(self._sent_at)
        if urgent:
            for slot in BOT_SLOTS:
                if target[slot] is not None or sent[slot] not in (None, _UNKNOWN):
                    self._send_bot(cu, slot, 0)
                    sent[slot] = 0
                    sent_at[slot] = now
        for slot in BOT_SLOTS:
            want = target[slot]
            have = sent[slot]
            if want is None:
                if have is not None:
                    self._send_bot(cu, slot, None)
            elif want != have:
                if now - sent_at[slot] >= min_gap or have is _UNKNOWN:
                    self._send_bot(cu, slot, want)
            elif now - sent_at[slot] >= heartbeat:
                self._send_bot(cu, slot, want)
        for slot in lanes:
            if target[slot] is not None:
                try:
                    cu.botlane(slot)
                    self._stats["bot"] += 1
                    self._emit(BotSent(slot, target[slot], lane=True, host_t=time.monotonic()))
                except CommandRejected:
                    logger.warning("lane change refused (slot %d)", slot)

    def _choose_transport(self, version: str | None, module: bool) -> str | None:
        """C on a V338/V339 CU (stock or ours: no custom firmware needed), else B if the module answers."""
        ctl = (bool(version) and version[1:4] in CTL_FIRMWARES
               and version[0] == "5")  # hw8 (v8xxx) is mute to C, A and B (probed 2026-10-18)
        pref = self._cfg.bot_transport
        if pref == "C":
            return "C" if ctl else None
        if pref == "B":
            return "B" if module else None
        return "C" if ctl else ("B" if module else None)

    def _service_ctl(self, cu: RaceControlUnit) -> None:
        """Software drivers through the stock C table: one frame for the 6 controllers."""
        now = time.monotonic()
        bench = self._bench
        frame = self._bots.frame(now, probe=bench is not None and bench.wants_c_probe())
        if frame is None:
            return
        try:
            cu.setctl(list(frame.values), frame.lanes)
        except CommandRejected:
            logger.error("the CU (or its adapter) rejects the C command")
            with self._lock:
                self.bot_transport = "B" if self.bot_module else None
                self.bots_supported = self.bot_transport is not None
                self._sent = [_UNKNOWN] * 6
            self._set_state("connected", "C command rejected by the CU")
            return
        self._bots.sent(frame, now)
        if bench is not None and cu.last_rtt is not None:
            bench.c.append(cu.last_rtt)
        self._stats["bot"] += 1
        for slot in BOT_SLOTS:
            if frame.values[slot] is not None or frame.prev is None or frame.prev[0][slot] is not None:
                self._mark_sent(slot, frame.values[slot])
        for slot, value, lane in frame.changes():
            self._emit(BotSent(slot, value, lane=lane, host_t=time.monotonic()))

    def _send_bot(self, cu: RaceControlUnit, slot: int, value: int | None) -> None:
        try:
            if value is None:
                cu.botrelease(slot)
            else:
                cu.setbot(slot, value)
        except CommandRejected:
            logger.error("the CU rejects the B command: firmware without bot module?")
            with self._lock:
                self.bots_supported = False
            self._set_state("connected", "B command rejected by the CU")
            return
        self._stats["bot"] += 1
        self._mark_sent(slot, value)
        self._emit(BotSent(slot, value, host_t=time.monotonic()))

    def _mark_sent(self, slot: int, value: int | None) -> None:
        with self._lock:
            self._sent[slot] = value
            self._last_known[slot] = value
            self._sent_at[slot] = time.monotonic()

    def _run_command(self, cu: RaceControlUnit, cmd: _Command) -> None:
        result, error = None, None
        try:
            result = cmd.run(cu)
        except CommandRejected as e:
            error = e
            logger.warning("command rejected by the CU: %s", cmd.label)
        except ValueError as e:
            error = e
            logger.warning("invalid command %s: %s", cmd.label, e)
        except CUTimeoutError as e:
            error = e
            logger.warning("no response to %s", cmd.label)
        if cmd.on_done is not None:
            try:
                cmd.on_done(result, error)
            except Exception:
                logger.exception("command callback")

    def _shutdown(self, cu: RaceControlUnit) -> None:
        target = self._bots.targets()
        with self._lock:
            active = [s for s in BOT_SLOTS if self._sent[s] is not None or target[s] is not None]
            supported = self.bots_supported
            transport = self.bot_transport
        if not supported:
            return
        if transport == "C":
            try:
                cu.setctl(list(self._bots.final_frame().values))
            except Exception:
                logger.warning("could not release the C table (it expires by itself in ~0.75 s)")
            return
        for slot in active:
            try:
                cu.setbot(slot, 0)
                if self._safety.release_on_exit:
                    cu.botrelease(slot)
                self._mark_sent(slot, None if self._safety.release_on_exit else 0)
            except Exception as e:
                logger.error("shutdown: cannot stop bot %d (%s)", slot, e)

    def _flash(self, cu: RaceControlUnit, job: _FirmwareJob) -> None:
        total, done = len(job.lines), 0

        def progress(stage: str, error: str | None = None) -> None:
            self._emit(FirmwareProgress(stage, done, total, error, time.monotonic()))

        try:
            self._set_state("updating")
            progress("prepare")
            with self._lock:
                # nothing from the old session may be sent after the restart
                self._high.clear()
                self._low.clear()
                self._bots.clear()
            if not job.recovery and self.bot_transport == "C":
                cu.setctl([None] * 6)  # rails: no host value during the flash
            if not job.recovery and self.bot_module:
                for slot in BOT_SLOTS:  # rails: no active bot during the flash
                    cu.setbot(slot, 0)
                    cu.botrelease(slot)
            cu.firmware_mode = True
            if not job.recovery:
                progress("bootloader")
                cu.fwu_start()
                if self._stop.wait(BOOTLOADER_WAIT_S):
                    raise FirmwareFailed("app shut down during the update")
            last = 0.0
            for done, line in enumerate(job.lines, 1):
                if job.abort.is_set() or self._stop.is_set():
                    done -= 1
                    raise FirmwareFailed(f"update interrupted at line {done + 1}/{total}")
                cu.fwu_write(line)
                now = time.monotonic()
                if now - last >= 0.1 or done == total:
                    progress("write")
                    last = now
            progress("reboot")
        except FirmwareFailed as e:
            progress("failed", str(e))
            raise
        except (CommandRejected, CUConnectionError, OSError, ValueError) as e:
            where = f"line {done}/{total}" if done else "entering update mode"
            reason = ("rejected by the CU" if isinstance(e, CommandRejected)
                      else "no response" if isinstance(e, CUTimeoutError) else str(e) or type(e).__name__)
            message = f"failed ({where}): {reason}"
            progress("failed", message)
            raise FirmwareFailed(message) from e
        finally:
            cu.firmware_mode = False
            with self._lock:
                self._job = None
                self._sent = [None] * 6  # the CU restarts without bots (or stays in bootloader)
                self._last_known = [None] * 6
        self.version, self.bots_supported = None, None
        self.bot_transport, self.bot_module = None, None
        raise FirmwareFlashed()

    def unsafe_slots(self) -> list[int]:
        """Slots whose last throttle acknowledged by the CU was not 0 (link lost at shutdown).

        None with the C table: the CU drops it by itself ~0.75 s after the last frame."""
        with self._lock:
            if self.bot_transport == "C":
                return []
            return [s for s in BOT_SLOTS if self._last_known[s] not in (None, 0)]

    def _dispatch(self, msg: Parsed) -> None:
        now = time.monotonic()
        self._stats["rx"] += 1
        if isinstance(msg, ControlUnit.Status):
            self._emit(StatusMsg(tuple(msg.fuel), msg.start, msg.mode, tuple(msg.pit),
                                 msg.display, now))
        elif isinstance(msg, ControlUnit.Timer):
            self._emit(TimerMsg(msg.address, msg.timestamp, msg.sector, now))

    def _raw(self, direction: str, data: bytes) -> None:
        if direction == "tx":
            self._stats["tx"] += 1
        if not self.trace:
            return
        noise = data == b"?" if direction == "tx" else data[:2] == b"?:"
        if noise and not self.trace_polls:
            return
        self._emit(RawFrame(direction, data, time.monotonic()))

    def _publish_stats(self, dt: float) -> None:
        s, self._stats = self._stats, {"rx": 0, "tx": 0, "bot": 0}
        self._emit(LinkStats(
            rtt_ms=None if self._rtt is None else self._rtt * 1000,
            rx_per_s=s["rx"] / dt,
            tx_per_s=s["tx"] / dt,
            bot_tx_per_s=s["bot"] / dt,
            queue=self.queue_length(),
        ))
