"""Software driver manager: 6 slots, each human or bot.

A bot slot has a driver:
- `manual`  : the UI slider (like a controller);
- `profile` : a local autopilot (bots/profiles.py);
- `api`     : an external (RL) agent via the API, with a dead-man switch.

The requested throttle goes through the safety gates before reaching the link
(`gate`); the link then applies its own locks (E-STOP, reconnection).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import SafetyConfig
from ..events import BOT_SLOTS, LinkState
from .profiles import ConstantProfile, Context, Profile, profile_from_dict

logger = logging.getLogger(__name__)

DRIVERS = ("manual", "profile", "api")
RUNNING_STATES = ("running", "finishing")
LIGHT_PHASES = (1, 2, 3, 4, 5, 6, 8, 9)  # lights on / false start: throttle 0


class BotLink(Protocol):
    def set_bot(self, slot: int, value: int | None) -> None: ...
    def lane(self, slot: int) -> None: ...
    def estop(self) -> None: ...
    def clear_estop(self) -> None: ...
    def resume_bots(self) -> None: ...


@dataclass
class BotSlot:
    slot: int
    driver: str | None = None  # None = human (physical controller)
    manual: int = 0
    profile: Profile = field(default_factory=ConstantProfile)
    agent: str | None = None
    api_value: int = 0
    api_last: float = 0.0
    api_free: bool = False  # agent allowed outside a session (RL test bench)
    requested: int = 0
    output: int | None = None  # last value pushed to the link
    gate: str | None = None
    lane_pending_until: float = 0.0


class BotManager:
    def __init__(self, link: BotLink, safety: SafetyConfig, *,
                 on_event: Callable[[str, dict], None] = lambda n, d: None,
                 clock: Callable[[], float] = time.monotonic):
        self._link = link
        self._safety = safety
        self._on_event = on_event
        self._clock = clock
        self.slots = [BotSlot(i) for i in BOT_SLOTS]
        self.enabled = True  # False in classic mode: no B is emitted
        self.estopped = False
        self.link_state = "connecting"
        self.bots_supported: bool | None = None
        self._reconnected = False
        self._session_state = "idle"
        self._phase = 0
        self._fuel_mode = False
        self._cars: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------ commands

    def take(self, slot: int, driver: str = "manual") -> None:
        """The slot becomes a bot (the CU ignores the physical controller)."""
        self._check_usable()
        if driver not in DRIVERS:
            raise ValueError(f"unknown driver: {driver}")
        s = self._slot(slot)
        if driver == "api" and s.driver != "api":
            s.agent, s.api_value = None, 0
        s.driver = driver
        s.manual = 0
        s.profile.reset()
        self._push(s, force=True)
        self._on_event("bot", {"slot": slot, "driver": driver})

    def release(self, slot: int) -> None:
        """Hand the slot back to the physical controller (B<addr>A command)."""
        s = self._slot(slot)
        s.driver, s.agent, s.output = None, None, None
        s.manual = s.api_value = 0
        self._link.set_bot(slot, None)
        self._on_event("bot", {"slot": slot, "driver": None})

    def release_all(self) -> None:
        for s in self.slots:
            if s.driver is not None:
                self.release(s.slot)

    def set_manual(self, slot: int, value: int) -> None:
        s = self._slot(slot)
        if s.driver != "manual":
            raise ValueError("the slot is not under manual control")
        s.manual = _clamp(value)
        self._push(s)

    def set_profile(self, slot: int, data: dict[str, Any]) -> dict[str, Any]:
        s = self._slot(slot)
        s.profile = profile_from_dict(data)
        return s.profile.to_dict()

    def lane(self, slot: int) -> None:
        s = self._slot(slot)
        if s.driver is None:
            raise ValueError("lane change: the slot is not a bot")
        self._link.lane(slot)
        s.lane_pending_until = self._clock() + 0.3

    def estop(self) -> None:
        """Throttle 0 everywhere. API agents lose their claim: after the E-STOP is
        lifted, an agent must `bot.claim` again (nothing restarts by itself at its old throttle)."""
        self.estopped = True
        self._link.estop()
        for s in self.slots:
            s.manual = s.api_value = 0
            s.profile.reset()
            if s.driver == "api" and s.agent is not None:
                self._on_event("agent_lost", {"slot": s.slot, "agent": s.agent, "reason": "estop"})
                s.agent = None
        self._on_event("estop", {"active": True})

    def clear_estop(self) -> None:
        self.estopped = False
        self._link.clear_estop()
        self._on_event("estop", {"active": False})

    # ------------------------------------------------------------ API agents

    def api_claim(self, slot: int, agent: str, *, free: bool = False) -> None:
        s = self._slot(slot)
        if s.driver == "api" and s.agent not in (None, agent):
            raise ValueError(f"slot {slot} already driven by agent {s.agent}")
        if s.driver != "api":
            self.take(slot, "api")
        s.agent, s.api_value, s.api_free = agent, 0, free
        s.api_last = self._clock()

    def api_action(self, slot: int, agent: str, throttle: int | None = None,
                   lane: bool = False) -> None:
        s = self._slot(slot)
        if s.driver != "api" or s.agent != agent:
            raise ValueError(f"slot {slot} not claimed by this agent (bot.claim first)")
        s.api_last = self._clock()
        if throttle is not None:
            s.api_value = _clamp(throttle)
        if lane:
            self.lane(slot)
        self._push(s)

    def api_release(self, slot: int, agent: str) -> None:
        s = self._slot(slot)
        if s.driver == "api" and s.agent == agent:
            self.release(slot)

    def agent_gone(self, agent: str) -> None:
        """Agent disconnected: its slots drop to 0 (the bot stays in charge of the slot)."""
        for s in self.slots:
            if s.driver == "api" and s.agent == agent:
                s.api_value = 0
                s.agent = None
                self._push(s)
                self._on_event("agent_lost", {"slot": s.slot, "agent": agent})

    # ------------------------------------------------------------ inputs

    def on_link(self, st: LinkState) -> None:
        if st.state == "connected" and self.link_state == "lost":
            self._reconnected = True
        self.link_state = st.state
        self.bots_supported = st.bots_supported

    def on_engine_event(self, name: str, data: dict[str, Any]) -> None:
        if name == "lap":
            s = self._slot_or_none(data["slot"])
            if s is not None and s.driver == "profile":
                # empty tank: the car crawls, this lap teaches the profile nothing
                skip = data.get("neutralized", False) or self._fuel_empty(s.slot)
                s.profile.on_lap(data["lap"], data["ms"], skip)
        elif name == "session" and data.get("state") == "grid":
            for s in self.slots:
                s.profile.reset()

    def tick(self, engine_snapshot: dict[str, Any]) -> None:
        """20 Hz: compute each bot's throttle and push it to the link."""
        now = self._clock()
        self._session_state = engine_snapshot["session"]["state"]
        self._phase = engine_snapshot["cu"]["phase"]
        self._fuel_mode = bool(engine_snapshot["cu"].get("fuel_mode"))
        self._cars = {c["slot"]: c for c in engine_snapshot["cars"]}
        if self._reconnected:
            # docs/bot-protocol.md §1: after an outage, throttle 0 before resuming
            self._reconnected = False
            for s in self.slots:
                s.manual = s.api_value = 0
                s.profile.reset()
            self._link.resume_bots()
            self._on_event("bots_reset", {"reason": "reconnection"})
        for s in self.slots:
            if s.driver is None:
                continue
            self._push(s, now=now)
            if s.driver == "profile" and s.output and s.profile.take_lane_request():
                self.lane(s.slot)

    # ------------------------------------------------------------ computation

    def _requested(self, s: BotSlot, now: float) -> int:
        if s.driver == "manual":
            return s.manual
        if s.driver == "api":
            return s.api_value
        car = self._cars.get(s.slot, {})
        since = car.get("since_cross_ms")
        rival_ms = None
        if s.profile.kind == "rival":
            rival = self._cars.get(getattr(s.profile, "rival_slot", -1), {})
            best = sorted(ms for ms in rival.get("recent") or [] if ms)[:3]
            if best:
                rival_ms = sum(best) / len(best)
        ctx = Context(now=now, since_cross_s=None if since is None else since / 1000,
                      laps=car.get("laps", 0), rival_lap_ms=rival_ms)
        return s.profile.output(ctx)

    def _gate(self, s: BotSlot, now: float) -> str | None:
        if self.estopped:
            return "estop"
        if self.link_state != "connected":
            return "link"
        if self._phase in LIGHT_PHASES:
            return "lights"
        car = self._cars.get(s.slot, {})
        if s.driver in ("profile", "api"):
            if car.get("finished") or car.get("eliminated"):
                return "finished"
            if s.driver == "api":
                if s.agent is None:
                    return "agent"
                if (now - s.api_last) * 1000 > self._safety.deadman_ms:
                    return "deadman"
                if not s.api_free and self._session_state not in RUNNING_STATES:
                    return "session"
            elif self._session_state not in RUNNING_STATES:
                return "session"
        return None

    def _push(self, s: BotSlot, *, now: float | None = None, force: bool = False) -> None:
        if s.driver is None or not self.enabled:
            return
        now = self._clock() if now is None else now
        s.requested = self._requested(s, now)
        s.gate = self._gate(s, now)
        value = 0 if s.gate else min(s.requested, self._safety.max_throttle)
        if s.driver == "profile" and s.gate:
            s.profile.relaunch()  # the next start ramps up again
        if force or value != s.output:
            s.output = value
            self._link.set_bot(s.slot, value)

    def _fuel_empty(self, slot: int) -> bool:
        return self._fuel_mode and self._cars.get(slot, {}).get("fuel") == 0

    # ------------------------------------------------------------ reading

    def snapshot(self, applied: list[int | None] | None = None) -> list[dict[str, Any]]:
        now = self._clock()
        out = []
        for s in self.slots:
            out.append({
                "slot": s.slot,
                "active": s.driver is not None,
                "driver": s.driver,
                "throttle": s.output if s.driver else None,
                "requested": s.requested if s.driver else None,
                "applied": applied[s.slot] if applied else None,
                "manual": s.manual,
                "gate": s.gate if s.driver else None,
                "profile": s.profile.to_dict(),
                "agent": s.agent,
                "api_age_ms": round((now - s.api_last) * 1000) if s.driver == "api" else None,
                "lane_pending": now < s.lane_pending_until,
                "fuel_empty": bool(s.driver) and self._fuel_empty(s.slot),
            })
        return out

    def load_profiles(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            try:
                self._slot(int(key)).profile = profile_from_dict(value)
            except (ValueError, TypeError) as e:
                logger.warning("profile of slot %s ignored: %s", key, e)

    def dump_profiles(self) -> dict[str, Any]:
        return {str(s.slot): s.profile.to_dict() for s in self.slots}

    def active_slots(self) -> list[int]:
        return [s.slot for s in self.slots if s.driver is not None]

    # ------------------------------------------------------------ helpers

    def _check_usable(self) -> None:
        if not self.enabled:
            raise ValueError("software drivers unavailable in classic mode")
        if self.bots_supported is False:
            raise ValueError("the CU firmware does not support the B command")

    def _slot(self, slot: int) -> BotSlot:
        if not isinstance(slot, int) or slot not in BOT_SLOTS:
            raise ValueError("invalid bot slot (0..5)")
        return self.slots[slot]

    def _slot_or_none(self, slot: int) -> BotSlot | None:
        return self.slots[slot] if slot in BOT_SLOTS else None


def _clamp(value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError("throttle: integer 0..15 expected") from None
    if not 0 <= v <= 15:
        raise ValueError("throttle: integer 0..15 expected")
    return v
