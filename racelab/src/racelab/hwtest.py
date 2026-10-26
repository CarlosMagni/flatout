"""Hardware test bench (System -> Hardware test): `link.bench` and `test.pulse / lane / handback / report`.

A bench to validate a CU on a real track. Every limit is enforced here, never in the UI, and a test
that moved a car always ends the same way: throttle 0 on the wire, heartbeat back, slot handed back.

The bench knows the CU link and the bots; what depends on the rest of the app (session, mode, link
state, driver list) is asked through `preconditions`, which refuses with an `OpError` whose `code` is
the reason the page words (`hw.why.<code>` in web/js/i18n.js).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import OpError

logger = logging.getLogger(__name__)

THROTTLE_MAX = 6
PULSE_MS = (200, 2000)
HANDBACK_PAUSE_MS = (1000, 2500)
BENCH_MAX = 300
REPORT_MAX = 200_000  # bytes

# What `preconditions(scope, slot)` is asked, in what order its reasons are checked:
#   "link"     the latency test: link connected
#   "run"      what must stay true while a test runs: link connected, no session
#   "move"     a test that moves a car: link, bots on the CU, no session, bots mode, no E-STOP, slot driver enabled
#   "handback" "move", on a CU driven through the C command
# `slot` is None when no slot is concerned (the page asking what is possible).
Preconditions = Callable[[str, "int | None"], None]


class HwTestBench:
    def __init__(self, link: Any, bots: Any, preconditions: Preconditions, *, safety: Any, folder: Path,
                 log: Callable[..., None], event: Callable[[str, dict[str, Any]], None]):
        """`link`: the CU link, always the current one (`bot_applied`, `set_bot`, `resume_heartbeat`,
        `pause_heartbeat`, `bench`, `bench_cancel`); `bots`: the BotManager; `safety`: [safety] config;
        `folder`: where reports are saved; `log(level, text, key, **params)` and `event(name, data)`."""
        self.link, self.bots, self._preconditions, self._safety, self._folder = link, bots, preconditions, safety, folder
        self._log, self._event = log, event
        self.busy: str | None = None  # test in progress (one at a time)
        self.state: dict[str, Any] | None = None  # what the UI shows of it: {kind, slot, phase}

    # ------------------------------------------------------------ what the page and the app ask

    def snapshot(self) -> dict[str, Any] | None:
        return dict(self.state) if self.state else None

    def limits(self) -> dict[str, Any]:
        return {"throttle_max": THROTTLE_MAX, "pulse_ms": list(PULSE_MS),
                "handback_pause_ms": list(HANDBACK_PAUSE_MS), "bench_max": BENCH_MAX}

    def why(self) -> dict[str, str | None]:
        """For each kind of test, the code of what forbids it right now (None: nothing does)."""
        return {"latency": self._why("link"), "move": self._why("move"), "handback": self._why("handback")}

    def protect(self, args: dict[str, Any]) -> None:
        """Refuse a bot command (`bot.*` args) aimed at the slot under test: the cap of a test must not
        be bypassed from another page."""
        if self.state is None or self.state["slot"] is None:
            return
        for a in args.get("actions") or [args]:
            if isinstance(a, dict) and _same_slot(a.get("slot"), self.state["slot"]):
                raise OpError(f"slot {self.state['slot'] + 1} is under a hardware test")

    # ------------------------------------------------------------ refusals

    def _why(self, scope: str) -> str | None:
        try:
            self._check(scope, None)
        except OpError as e:
            return e.code
        return None

    def _check(self, scope: str, slot: int | None) -> None:
        """Every reason a test must not take a slot (repeated at run time by the hold)."""
        self._preconditions(scope, slot)
        if slot is not None:
            s = self.bots.slots[slot]
            if s.driver == "api":
                raise OpError(f"slot {slot + 1} is driven by an API agent ({s.agent or 'unclaimed'})",
                              "slot_api", car=slot + 1, agent=s.agent or "?")
            if s.driver is not None:
                raise OpError(f"slot {slot + 1} is already a bot: release it first", "slot_bot", car=slot + 1)
        if scope == "handback" and self.bots.active_slots():
            # the C table is one frame for all slots: a pause would drop every bot, not only the test's
            raise OpError("the hand-back test needs every slot free of bots (release them first)", "bots_active")

    def _guard(self, scope: str, slot: int | None) -> None:
        if self.busy:
            raise OpError(f"another hardware test is running ({self.busy})", "busy", kind=self.busy)
        self._check(scope, slot)

    def _abort_reason(self) -> str | None:
        if self.bots.estopped:
            return "estop"
        try:
            self._preconditions("run", None)
        except OpError as e:
            return e.code
        return None

    # ------------------------------------------------------------ one test

    def _phase(self, phase: str, **extra: Any) -> None:
        if self.state is not None:
            self.state = {**self.state, "phase": phase, **extra}
            self._event("hwtest", dict(self.state))

    async def _wait_applied(self, slot: int, value: int, timeout: float) -> bool:
        """Until the CU link has put `value` on the wire for the slot."""
        end = time.monotonic() + timeout
        while self.link.bot_applied()[slot] != value:
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.02)
        return True

    async def _hold(self, slot: int, seconds: float) -> dict[str, Any]:
        """Wait `seconds` (server clock), watching E-STOP, link and session; reports what the CU got."""
        t0 = time.monotonic()
        peak, gate, aborted = 0, None, None
        while (left := seconds - (time.monotonic() - t0)) > 0:
            aborted = self._abort_reason()
            if aborted:
                break
            applied = self.link.bot_applied()[slot]
            peak = max(peak, applied or 0)
            gate = self.bots.slots[slot].gate or gate
            await asyncio.sleep(min(0.05, left))
        return {"held_ms": round((time.monotonic() - t0) * 1000), "applied_peak": peak,
                "gate": gate, "aborted": aborted}

    async def _end(self, slot: int) -> dict[str, Any]:
        """Always: throttle 0 on the wire, heartbeat back, slot handed back. Never raises."""
        end = {"zeroed": False, "released": False}
        s = self.bots.slots[slot]
        if s.driver != "manual":
            return end  # somebody else took the slot meanwhile: not ours to touch
        try:
            self.bots.set_manual(slot, 0)
        except Exception:
            logger.exception("hardware test: set_manual(0)")
            try:
                self.link.set_bot(slot, 0)
            except Exception:
                logger.exception("hardware test: set_bot(0)")
        try:
            self.link.resume_heartbeat()  # after the 0 is queued: never a heartbeat at the old throttle
        except Exception:
            logger.exception("hardware test: resume_heartbeat")
        try:
            end["zeroed"] = await self._wait_applied(slot, 0, 0.3)
        except Exception:
            logger.exception("hardware test: wait for 0")
        finally:  # also on a second cancellation (CancelledError) during the wait
            try:
                self.bots.release(slot)
                end["released"] = True
            except Exception:
                logger.exception("hardware test: release")
                try:
                    self.link.set_bot(slot, None)
                except Exception:
                    logger.exception("hardware test: set_bot(None)")
        return end

    async def _run(self, kind: str, slot: int, body, *, scope: str = "move") -> dict[str, Any]:
        """Take the slot as a manual bot, run `body`, and in all cases zero + release it."""
        self._guard(scope, slot)
        self.busy = kind
        self.state = {"kind": kind, "slot": slot, "phase": "start"}
        self._event("hwtest", dict(self.state))
        out: dict[str, Any] = {"slot": slot}
        try:
            self.bots.take(slot, "manual")
            self._phase("run")
            out.update(await body())
        finally:
            try:
                out.update(await self._end(slot))
            finally:
                self.busy = None
                self._phase("done", aborted=out.get("aborted"))
                self.state = None
        self._log("info", f"Hardware test {kind} on slot {slot + 1}: "
                          f"{'aborted (' + out['aborted'] + ')' if out.get('aborted') else 'done'}",
                  "hw_aborted" if out.get("aborted") else "hw_done", kind=kind, car=slot + 1, reason=out.get("aborted"))
        return out

    def _throttle(self, args: dict[str, Any], default: int | None = None) -> int:
        """Requested throttle, hard-capped at THROTTLE_MAX (refused above) and at [safety] max_throttle."""
        throttle = min(_int(args, "throttle", 1, THROTTLE_MAX, default=default), THROTTLE_MAX)
        effective = min(throttle, self._safety.max_throttle)
        if effective < 1:
            raise OpError("[safety] max_throttle is 0: no bot can move", "no_throttle")
        return effective

    async def _move(self, slot: int, throttle: int, ms: int, *, lane: bool = False) -> dict[str, Any]:
        """Hold `throttle` for `ms` (clock started once the CU link has put it on the wire)."""
        self.bots.set_manual(slot, throttle)
        if not await self._wait_applied(slot, throttle, 0.6):
            return {"throttle": throttle, "ms": ms, "held_ms": 0, "applied_peak": 0, "acked": False,
                    "gate": self.bots.slots[slot].gate, "aborted": self._abort_reason()}
        if lane:
            self.bots.lane(slot)  # the existing lane path (C: button held ~120 ms; B: one-shot)
        out = await self._hold(slot, ms / 1000)
        out = {"throttle": throttle, "ms": ms, "acked": out["applied_peak"] == throttle, **out}
        return {**out, "lane_sent": out["aborted"] is None} if lane else out

    # ------------------------------------------------------------ the tests

    async def pulse(self, args: dict[str, Any]) -> dict[str, Any]:
        slot = _int(args, "slot", 0, 5)
        throttle = self._throttle(args)
        ms = _int(args, "ms", *PULSE_MS)
        return await self._run("pulse", slot, lambda: self._move(slot, throttle, ms))

    async def lane(self, args: dict[str, Any]) -> dict[str, Any]:
        """One-shot lane request while the car runs (it applies it at the next lane changer)."""
        slot = _int(args, "slot", 0, 5)
        throttle = self._throttle(args, default=3)
        ms = _int(args, "ms", *PULSE_MS, default=PULSE_MS[1])
        return await self._run("lane", slot, lambda: self._move(slot, throttle, ms, lane=True))

    async def handback(self, args: dict[str, Any]) -> dict[str, Any]:
        slot = _int(args, "slot", 0, 5)
        throttle = self._throttle(args, default=3)
        pause_ms = _int(args, "pause_ms", *HANDBACK_PAUSE_MS, default=1500)

        async def body():
            self.bots.set_manual(slot, throttle)
            if not await self._wait_applied(slot, throttle, 0.6):
                return {"throttle": throttle, "pause_ms": pause_ms, "paused_ms": 0, "acked": False,
                        "aborted": self._abort_reason()}
            run_up = await self._hold(slot, 0.5)  # the car picks up speed
            if run_up["aborted"]:
                return {"throttle": throttle, "pause_ms": pause_ms, "paused_ms": 0, "acked": True,
                        "aborted": run_up["aborted"]}
            self._phase("paused", pause_ms=pause_ms)
            self.link.pause_heartbeat(pause_ms / 1000 + 0.25)  # auto-clears, whatever happens here
            out = await self._hold(slot, pause_ms / 1000)
            return {"throttle": throttle, "pause_ms": pause_ms, "paused_ms": out["held_ms"], "acked": True,
                    "gate": out["gate"], "aborted": out["aborted"]}

        return await self._run("handback", slot, body, scope="handback")

    async def latency(self, args: dict[str, Any]) -> dict[str, Any]:
        """`link.bench`: n polls of the CU, timed; the statistics (cu/bench.py)."""
        n = _int(args, "n", 1, BENCH_MAX, default=100)
        self._guard("link", None)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def done(result, error):
            loop.call_soon_threadsafe(lambda: future.done() or future.set_result((result, error)))

        self.busy = "bench"
        self.state = {"kind": "bench", "slot": None, "phase": "run"}
        timed_out = False
        try:
            try:
                self.link.bench(n, done)
            except (ValueError, RuntimeError) as e:
                raise OpError(str(e)) from None
            try:
                result, error = await asyncio.wait_for(future, _bench_timeout(n))
            except asyncio.TimeoutError:
                # a slow or lossy link is what this test is for: keep what was measured
                partial = self.link.bench_cancel()
                if partial is None or not partial.poll:
                    raise OpError("the CU did not answer in time") from None
                result, error, timed_out = partial.result(), None, True
        finally:
            self.link.bench_cancel()
            self.busy, self.state = None, None
        if error:
            raise OpError(f"latency test interrupted: {error}")
        return {**result, "timed_out": timed_out}

    def save_report(self, report: Any, server: dict[str, Any]) -> dict[str, Any]:
        """The tester's report, saved by the server with what it knows (`server`: version, mode, link...)."""
        if not isinstance(report, dict):
            raise OpError("report: an object is expected")
        if len(json.dumps(report, ensure_ascii=False)) > REPORT_MAX:
            raise OpError("report too large")
        report = {**report, "server": {**server, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}}
        try:
            self._folder.mkdir(parents=True, exist_ok=True)
            stem = "hwtest-" + time.strftime("%Y%m%d-%H%M%S")
            path, n = self._folder / f"{stem}.json", 1
            while path.exists():
                n += 1
                path = self._folder / f"{stem}-{n}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            raise OpError(f"cannot save the report: {e}") from None
        self._log("info", f"Hardware test report saved: {path.name}", "hw_saved", name=path.name)
        return {"name": path.name, "path": str(path), "report": report}


def _bench_timeout(n: int) -> float:
    return 15.0 + n * 0.1


def _same_slot(value: Any, slot: int) -> bool:
    """True if `value` designates `slot` the way the bot ops parse it (`int(args["slot"])`: "0" and 0.0
    are slot 0 too); an unparsable value designates nothing and is refused later by the op itself."""
    try:
        return int(value) == slot
    except (TypeError, ValueError, OverflowError):
        return False


def _int(args: dict[str, Any], key: str, lo: int, hi: int, *, default: int | None = None) -> int:
    """Integer argument within [lo, hi]: refused, never clamped (the limits are safety limits)."""
    value = args.get(key, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or int(value) != value):
        raise OpError(f"{key}: an integer {lo}..{hi} is expected")
    if not lo <= value <= hi:
        raise OpError(f"{key}: {lo}..{hi} expected (got {int(value)})")
    return int(value)
