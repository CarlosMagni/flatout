"""Per-car tracking on the lap: one small Kalman filter per car, identity anchored by the CU.

Pure standard library (no numpy, no OpenCV): the safety- and identity-critical logic
runs in CI and can be unit-tested with hand-made detections.

State of a car: `x` = position along the centre line (m, from the finish line, wraps at
`length`), `v` = speed (m/s along `x`) and, for a bot, `g` = gain of the motor model.

- Bot (known applied throttle, `bots[].applied` in the RaceLab state): the prediction is
  the motor model of `racelab.sim.physics` (first-order speed towards
  `g * VMAX * throttle / 15`); `g` is part of the state, so a hot motor, a lower speed
  setting or a heavier car is learned online instead of assumed.
- Human (unknown throttle): constant velocity, relaxing towards a per-car speed
  prior learned from its own laps (`SpeedPrior`) once it has some.
- Measurement = a detection already projected on the map (`Detection`). Association is a
  gated, globally optimal assignment (`assign`, exhaustive DP: at most ~8 cars).
- Identity comes from the CU: a `crossing` of slot k at CU time t means "car k was on the
  anchor (finish line, or a Check Lane) at t". The confirmed track nearest to the anchor
  at t gets label k (`add_anchor`), and a swap of labels is repaired the same way.
  Anchors are evaluated at the crossing time from the track history, not at arrival
  time: the camera latency is larger than the BLE delay, so they wait for the frame
  that covers them.
- A car is published only once an anchor (or a single expected car) labelled it.
- Roll call (`identify`): `hint(slot, x, lane)` says "car `slot` is parked here": the confirmed unlabelled
  track there gets the label at once, without waiting for a crossing. `set_signature(slot, sig)` stores
  the car's colours; they are used ONLY to break a tie when a crossing is ambiguous (`_by_signature`).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from racelab.sim.physics import SPEED_MIN_SCALE, TAU_ACC, TAU_BRAKE, VMAX

from .appearance import Signature, blend, distance

# a track that is not seen for this long (s), outside the blind zones, is "off"
LOST_OFF_S = 1.0


def wrap(d: float, length: float) -> float:
    """Signed difference on a loop, in (-length/2, length/2]."""
    return (d + length / 2) % length - length / 2


@dataclass(frozen=True)
class TrackerMap:
    """What the tracker needs to know about the circuit (metres on the centre line)."""

    length: float
    kappa: tuple[float, ...] = (1.0, 1.0)  # lane length / centre line length, per lane
    blind: tuple[tuple[float, float], ...] = ()  # [x0, x1] stretches where no car is seen
    changers: tuple[float, ...] = ()  # x of the lane changers
    anchors: dict[int, float] = field(default_factory=lambda: {1: 0.0})  # sector -> x
    gain_hint: float | None = None  # motor gain measured during the discovery lap

    def in_blind(self, x: float, margin: float = 0.0) -> bool:
        length = self.length
        return any((x - a + margin) % length <= (b - a) % length + 2 * margin for a, b in self.blind)


@dataclass
class Detection:
    """A blob of the image, projected on the map (what the tracker consumes)."""

    t: float
    lane: int  # nearest lane (0 = left in the driving direction)
    x: float  # m along the centre line
    area: float = 0.0
    off: bool = False  # outside every lane corridor (derailed car, or something else)
    occluder: bool = False  # too big to be a car (hand): x is its centre, `extent` half-length (m)
    extent: float = 0.0
    sigma: float | None = None  # position noise (m), default: TrackerConfig.sigma_meas
    sig: Signature | None = None  # colours of the blob (only computed when the roll call stored signatures)


@dataclass
class TrackerConfig:
    sigma_meas: float = 0.015  # m, blob centroid noise
    sigma_time: float = 0.006  # s, frame timestamp jitter (becomes sigma_time * v of position noise)
    q_bot: float = 0.8  # m/s², acceleration noise when the motor model drives the prediction
    q_human: float = 2.0  # m/s², unknown throttle
    q_gain: float = 0.004  # 1/sqrt(s), random walk of the motor gain
    gain0: float = 1.0
    gain_sigma0: float = 0.35
    gate_sigma: float = 4.0
    gate_floor_m: float = 0.04  # the innovation std never goes below this (m)
    max_speed: float = 6.0  # m/s, a detection further ahead than this since the last one is not this car
    back_tol_m: float = 0.10  # a slot car does not drive backwards: a detection behind its last position is not it
    lane_penalty: float = 9.0  # added to the assignment cost of a lane change away from a changer
    lane_penalty_changer: float = 1.5
    changer_zone_m: float = 0.5
    confirm_hits: int = 6
    prune_unlabelled_s: float = 0.6
    lost_off_s: float = LOST_OFF_S
    off_gate_m: float = 0.7  # an off-corridor detection this close to a lost track explains it
    off_frames: int = 4  # frames of that evidence before flagging the car off
    reacquire_gate_m: float = 0.5
    anchor_window_m: float = 0.30
    anchor_speed_s: float = 0.06  # CU/clock timing uncertainty, grows the window with speed
    anchor_sigma_m: float = 0.05
    anchor_group_s: float = 0.12
    anchor_max_wait_s: float = 1.5
    history_s: float = 3.0
    speed_bins: int = 60
    speed_prior_tau: float = 0.20
    use_speed_prior: bool = True
    hint_gate_m: float = 0.25  # a roll-call hint names the track this close to the parked car
    hint_ttl_s: float = 30.0  # a hint that finds no track for this long (tracker time) is dropped
    sig_margin: float = 0.12  # appearance tie-break: the best candidate must beat the next one by this
    sig_max: float = 0.55  # ... and be at most this far from the car's signature


class SpeedPrior:
    """Speed of a (human) driver along the lap, learned from its own laps (m/s per bin)."""

    def __init__(self, length: float, bins: int = 60) -> None:
        self.length, self.bins = length, bins
        self.v = [0.0] * bins
        self.n = [0] * bins

    def _bin(self, x: float) -> int:
        return int((x % self.length) / self.length * self.bins) % self.bins

    def learn(self, x: float, v: float) -> None:
        i = self._bin(x)
        w = 0.35 if self.n[i] else 1.0
        self.v[i] += w * (v - self.v[i])
        self.n[i] += 1

    def at(self, x: float) -> float | None:
        i = self._bin(x)
        return self.v[i] if self.n[i] >= 2 else None


def _ident() -> list[list[float]]:
    return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _mm(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _tr(a: list[list[float]]) -> list[list[float]]:
    return [[a[j][i] for j in range(3)] for i in range(3)]


class Track:
    """One tracked car (a track exists as soon as something moves on the lane)."""

    def __init__(self, tid: int, t: float, x: float, lane: int, cfg: TrackerConfig) -> None:
        self.id = tid
        self.slot: int | None = None
        self.t = t
        self.t_meas = t  # last detection
        self.t_ok = t  # last time the absence of a detection was not suspicious
        self.x_meas = x  # position of the last detection
        self.off_votes = 0
        self.sig: Signature | None = None  # running average of the colours of its detections
        self.x = x
        self.v = 0.0
        self.g = cfg.gain0
        self.lane = lane
        self.hits = 1
        self.off = False
        sx = cfg.sigma_meas
        self.P = [[sx * sx, 0.0, 0.0], [0.0, 4.0, 0.0], [0.0, 0.0, 0.0]]
        self.history: deque[tuple[float, float, float]] = deque()  # (t, x, v)

    def x_at(self, t: float, length: float) -> float:
        h = self.history
        if not h:
            return self.x
        if t >= h[-1][0]:
            return (h[-1][1] + h[-1][2] * (t - h[-1][0])) % length
        prev = h[0]
        if t <= prev[0]:
            return prev[1]
        for cur in list(h)[1:]:
            if cur[0] >= t:
                f = (t - prev[0]) / (cur[0] - prev[0]) if cur[0] > prev[0] else 0.0
                return (prev[1] + f * wrap(cur[1] - prev[1], length)) % length
            prev = cur
        return h[-1][1]


@lru_cache(maxsize=None)
def _best(costs: tuple[tuple[float, ...], ...], unassigned: float, i: int, used: int) -> tuple[float, tuple[int, ...]]:
    if i == len(costs):
        return 0.0, ()
    skip_cost, skip = _best(costs, unassigned, i + 1, used)
    best = (unassigned + skip_cost, (-1, *skip))
    for j, c in enumerate(costs[i]):
        if c < math.inf and not used >> j & 1:
            sub_cost, sub = _best(costs, unassigned, i + 1, used | 1 << j)
            if c + sub_cost < best[0]:
                best = (c + sub_cost, (j, *sub))
    return best


def assign(costs: list[list[float]], unassigned: float) -> list[int | None]:
    """Min-cost one-to-one assignment of rows to columns; `inf` = forbidden pair, an
    unassigned row costs `unassigned`. Exhaustive DP over the used columns (small sizes)."""
    if not costs:
        return []
    cols = len(costs[0])
    if cols > 12:  # keep the 12 columns with the cheapest pair
        keep = sorted(range(cols), key=lambda j: min(r[j] for r in costs))[:12]
        picks = assign([[r[j] for j in keep] for r in costs], unassigned)
        return [None if p is None else keep[p] for p in picks]
    _best.cache_clear()
    _, picks = _best(tuple(tuple(r) for r in costs), unassigned, 0, 0)
    return [None if p < 0 else p for p in picks]


@dataclass
class Anchor:
    slot: int
    sector: int
    t: float  # crossing time, host clock


class Tracker:
    """All the cars of the lap. Feed it `update(t, detections)` once per frame, `add_anchor`
    for every CU crossing, `set_applied` for every state; read `positions(now)`."""

    def __init__(self, track: TrackerMap, cfg: TrackerConfig | None = None, *,
                 expected_slots: tuple[int, ...] = ()) -> None:
        self.map = track
        self.cfg = cfg or TrackerConfig()
        self.expected_slots = tuple(expected_slots)
        self.tracks: list[Track] = []
        self.t = 0.0
        self.started = False
        self._next_id = 1
        self._applied: dict[int, list[tuple[float, int | None]]] = {}  # slot -> [(t, applied throttle)]
        self._settings: dict[int, tuple[int, int]] = {}  # slot -> (speed, brake) programmed on the car
        self._gain: dict[int, float] = {}
        self._prior: dict[int, SpeedPrior] = {}
        self._anchors: list[Anchor] = []
        self.signatures: dict[int, Signature] = {}  # slot -> colours, from the roll call
        self._hints: dict[int, list[Any]] = {}  # slot -> [x, lane, tracker time of the first update]
        self.crossings: list[dict[str, Any]] = []  # one entry per resolved CU crossing
        self.stats = {"relabels": 0, "anchored": 0, "ambiguous": 0, "unmatched": 0,
                      "spawned": 0, "off_events": 0, "named": 0, "sig_resolved": 0}

    # ------------------------------------------------------------------ inputs

    def set_applied(self, slot: int, throttle: int | None, t: float | None = None) -> None:
        """Throttle applied by the CU to a bot from time `t` on (None: a human holds the controller)."""
        hist = self._applied.setdefault(slot, [])
        hist.append((self.t if t is None else t, throttle))
        if len(hist) > 64:
            del hist[:32]

    def set_settings(self, slot: int, speed: int = 15, brake: int = 15) -> None:
        """Speed and brake settings (0..15) programmed on the car: scale the motor model."""
        self._settings[slot] = (speed, brake)

    def _thr_at(self, slot: int | None, t: float) -> int | None:
        hist = self._applied.get(slot) if slot is not None else None
        if not hist:
            return None
        for ts, thr in reversed(hist):
            if ts <= t:
                return thr
        return hist[0][1]

    def set_signature(self, slot: int, sig: Signature) -> None:
        """Colours of the car of `slot` (roll call): a tie-breaker for ambiguous crossings, nothing more."""
        self.signatures[slot] = sig

    def hint(self, slot: int, x: float, lane: int) -> None:
        """The car of `slot` is parked at `x` (m on the centre line) in `lane` (roll call): the confirmed
        unlabelled track there is named at once. The hint is dropped once the slot has a track, or after
        `hint_ttl_s`."""
        self._hints[slot] = [x % self.map.length, lane, None]

    def add_anchor(self, slot: int, sector: int, t_cross: float) -> None:
        if sector in self.map.anchors:
            self._anchors.append(Anchor(slot, sector, t_cross))

    def update(self, t: float, detections: list[Detection]) -> None:
        cfg = self.cfg
        if self.started and t <= self.t:
            return
        self.started, self.t = True, t
        for tr in self.tracks:
            self._propagate(tr, t)
        normal = [d for d in detections if not d.off and not d.occluder]
        off = [d for d in detections if d.off and not d.occluder]
        occluders = [d for d in detections if d.occluder]
        pairs = self._associate(t, normal)
        used = set()
        for tr, d in pairs:
            used.add(id(d))
            self._measure(tr, d, t)
        for tr in self.tracks:
            if all(tr is not p[0] for p in pairs):
                self._missed(tr, t, off, occluders)
        self._spawn(t, [d for d in normal if id(d) not in used])
        self._prune(t)
        for tr in self.tracks:
            tr.history.append((t, tr.x, tr.v))
            while tr.history and t - tr.history[0][0] > cfg.history_s:
                tr.history.popleft()
        self._apply_hints(t)
        self._resolve_anchors(t)
        self._solo_label()

    # ------------------------------------------------------------------ model

    def _kind(self, tr: Track, t: float) -> tuple[bool, float, float]:
        """(bot, speed target at full gain in x-units, braking time constant) of a track at time t."""
        thr = self._thr_at(tr.slot, t)
        if thr is None:
            return False, 0.0, TAU_BRAKE
        speed, brake = self._settings.get(tr.slot, (15, 15))
        kappa = self.map.kappa[min(tr.lane, len(self.map.kappa) - 1)]
        vmax = VMAX * (SPEED_MIN_SCALE + (1 - SPEED_MIN_SCALE) * speed / 15)
        return True, vmax * thr / 15 / kappa, TAU_BRAKE / (0.5 + 0.5 * brake / 15)

    def _propagate(self, tr: Track, t: float, *, commit: bool = True) -> tuple[float, float, float, list[list[float]]]:
        """Advance the filter of `tr` to `t`; returns the new (x, v, g, P). With
        `commit=False` the track is left untouched (extrapolation for publication)."""
        cfg = self.cfg
        dt = t - tr.t
        x, v, g, P = tr.x, tr.v, tr.g, tr.P
        if dt <= 0:
            return x, v, g, P
        if tr.off:
            return x, 0.0, g, P
        bot, c, tau_brake = self._kind(tr, t if not commit else (tr.t + t) / 2)
        F = _ident()
        if bot:
            target = g * c
            tau = TAU_ACC if target >= v else tau_brake
            e = math.exp(-dt / tau)
            nx = x + target * dt + (v - target) * tau * (1 - e)
            nv = target + (v - target) * e
            F = [[1.0, tau * (1 - e), c * (dt - tau * (1 - e))], [0.0, e, c * (1 - e)], [0.0, 0.0, 1.0]]
            q = cfg.q_bot
            qg = cfg.q_gain ** 2 * dt
        else:
            prior = self._prior.get(tr.slot) if tr.slot is not None and cfg.use_speed_prior else None
            target = prior.at(x + v * 0.1) if prior else None
            if target is not None:
                e = math.exp(-dt / cfg.speed_prior_tau)
                nv = target + (v - target) * e
                nx = x + target * dt + (v - target) * cfg.speed_prior_tau * (1 - e)
                F = [[1.0, cfg.speed_prior_tau * (1 - e), 0.0], [0.0, e, 0.0], [0.0, 0.0, 1.0]]
            else:
                nx, nv = x + v * dt, v
                F[0][1] = dt
            q, qg = cfg.q_human, 0.0
        nP = _mm(_mm(F, P), _tr(F))
        nP[0][0] += q * q * dt ** 3 / 3
        nP[0][1] += q * q * dt ** 2 / 2
        nP[1][0] += q * q * dt ** 2 / 2
        nP[1][1] += q * q * dt
        nP[2][2] += qg
        nv = max(0.0, nv)
        if commit:
            tr.x, tr.v, tr.g, tr.P, tr.t = nx % self.map.length, nv, g, nP, t
        return nx % self.map.length, nv, g, nP

    def _update(self, tr: Track, z: float, r: float) -> float:
        """Scalar Kalman update with a position measurement; returns the innovation."""
        L = self.map.length
        y = wrap(z - tr.x, L)
        P = tr.P
        s = P[0][0] + r
        k = [P[i][0] / s for i in range(3)]
        tr.x = (tr.x + k[0] * y) % L
        tr.v = max(0.0, tr.v + k[1] * y)
        tr.g = min(2.0, max(0.1, tr.g + k[2] * y))
        tr.P = [[P[i][j] - k[i] * P[0][j] for j in range(3)] for i in range(3)]
        return y

    def _innovation_var(self, tr: Track, sigma: float) -> float:
        cfg = self.cfg
        r = sigma * sigma + (cfg.sigma_time * tr.v) ** 2
        return max(tr.P[0][0] + r, cfg.gate_floor_m ** 2)

    # ------------------------------------------------------------------ association

    def _lane_cost(self, tr: Track, d: Detection) -> float:
        if d.lane == tr.lane:
            return 0.0
        near = any(abs(wrap(d.x - c, self.map.length)) <= self.cfg.changer_zone_m for c in self.map.changers)
        return self.cfg.lane_penalty_changer if near or not self.map.changers else self.cfg.lane_penalty

    def _associate(self, t: float, normal: list[Detection]) -> list[tuple[Track, Detection]]:
        cfg = self.cfg
        L = self.map.length
        rows = list(self.tracks)
        costs: list[list[float]] = []
        for tr in rows:
            row = []
            for d in normal:
                gate = cfg.reacquire_gate_m if tr.off else None
                dx = wrap(d.x - tr.x, L)
                if gate is not None:
                    row.append(dx * dx / gate ** 2 * cfg.gate_sigma ** 2 * 0.5 if abs(dx) <= gate else math.inf)
                    continue
                fwd = (d.x - tr.x_meas) % L  # kinematics: not behind the last sighting, not further than max speed
                if cfg.max_speed * (t - tr.t_meas) + 0.10 < fwd < L - cfg.back_tol_m:
                    row.append(math.inf)
                    continue
                s = self._innovation_var(tr, d.sigma or cfg.sigma_meas)
                m = dx * dx / s
                row.append(m + self._lane_cost(tr, d) + (0.3 if tr.slot is None else 0.0)
                           if m <= cfg.gate_sigma ** 2 else math.inf)
            costs.append(row)
        picks = assign(costs, cfg.gate_sigma ** 2 + 0.5 * cfg.lane_penalty)
        return [(tr, normal[j]) for tr, j in zip(rows, picks) if j is not None]

    def _measure(self, tr: Track, d: Detection, t: float) -> None:
        was_off = tr.off
        if was_off:  # the car is back on the track (or was never off): restart from the measurement
            tr.off, tr.v, tr.x = False, 0.0, d.x
            tr.P = [[self.cfg.sigma_meas ** 2, 0.0, 0.0], [0.0, 4.0, 0.0], [0.0, 0.0, tr.P[2][2]]]
        else:
            self._update(tr, d.x, (d.sigma or self.cfg.sigma_meas) ** 2 + (self.cfg.sigma_time * tr.v) ** 2)
        tr.lane, tr.t_meas, tr.t_ok, tr.off_votes, tr.hits = d.lane, t, t, 0, tr.hits + 1
        tr.x_meas = d.x
        if d.sig is not None:
            tr.sig = blend(tr.sig, d.sig)
        if tr.slot is not None:
            self._learn(tr)

    def _learn(self, tr: Track) -> None:
        if tr.slot is None:
            return
        if self._thr_at(tr.slot, tr.t) is not None:
            self._gain[tr.slot] = tr.g
        elif tr.hits > 8 and tr.P[1][1] < 0.5:
            self._prior.setdefault(tr.slot, SpeedPrior(self.map.length, self.cfg.speed_bins)).learn(tr.x, tr.v)

    def _missed(self, tr: Track, t: float, off: list[Detection], occluders: list[Detection]) -> None:
        """A confirmed, labelled track got no detection this frame."""
        cfg, L = self.cfg, self.map.length
        if tr.slot is None or tr.hits < cfg.confirm_hits or tr.off:
            return
        if any(abs(wrap(tr.x - d.x, L)) <= d.extent + 0.1 for d in occluders) or self.map.in_blind(tr.x, 0.10):
            tr.t_ok = t  # something big covers it, or it is in a blind zone: not suspicious
            tr.off_votes = 0
            return
        near = [d for d in off if abs(wrap(d.x - tr.x, L)) <= cfg.off_gate_m]
        tr.off_votes = tr.off_votes + 1 if near else 0
        if tr.off_votes >= cfg.off_frames:  # a blob outside the corridor, where the car vanished
            d = min(near, key=lambda d: abs(wrap(d.x - tr.x, L)))
            tr.off, tr.x, tr.v = True, d.x, 0.0
            self.stats["off_events"] += 1
        elif t - tr.t_ok > cfg.lost_off_s:  # nothing at all for too long, in plain view
            tr.off, tr.v = True, 0.0
            self.stats["off_events"] += 1

    def _spawn(self, t: float, left: list[Detection]) -> None:
        L = self.map.length
        for d in left:
            if any(abs(wrap(d.x - tr.x, L)) < 0.25 and tr.hits >= self.cfg.confirm_hits for tr in self.tracks):
                continue  # the other half of a car changing lane, or the same car split in two
            if any(abs(wrap(d.x - tr.x, L)) < 0.05 and tr.lane == d.lane for tr in self.tracks):
                continue
            tr = Track(self._next_id, t, d.x, d.lane, self.cfg)
            self._next_id += 1
            self.tracks.append(tr)
            self.stats["spawned"] += 1

    def _prune(self, t: float) -> None:
        keep = []
        for tr in self.tracks:
            if tr.slot is None and t - tr.t_meas > self.cfg.prune_unlabelled_s:
                continue
            keep.append(tr)
        self.tracks = keep

    # ------------------------------------------------------------------ identity

    def _confirmed(self) -> list[Track]:
        return [tr for tr in self.tracks if tr.hits >= self.cfg.confirm_hits and not tr.off]

    def _holder(self, slot: int) -> Track | None:
        return next((tr for tr in self.tracks if tr.slot == slot), None)

    def _solo_label(self) -> None:
        if len(self.expected_slots) != 1 or self._holder(self.expected_slots[0]) is not None:
            return
        cand = self._confirmed()
        if len(cand) == 1 and cand[0].slot is None:
            self._set_label(cand[0], self.expected_slots[0])

    def _set_label(self, tr: Track, slot: int | None) -> None:
        tr.slot = slot
        if slot is not None:
            if self._thr_at(slot, self.t) is not None:
                tr.g = self._gain.get(slot, self.map.gain_hint or self.cfg.gain0)
                tr.P[2][2] = (0.08 if slot in self._gain else self.cfg.gain_sigma0) ** 2

    def _apply_hints(self, t: float) -> None:
        cfg, L = self.cfg, self.map.length
        for slot, h in list(self._hints.items()):
            h[2] = t if h[2] is None else h[2]
            if self._holder(slot) is not None or t - h[2] > cfg.hint_ttl_s:
                del self._hints[slot]
        cands = [tr for tr in self._confirmed() if tr.slot is None]
        if not self._hints or not cands:
            return
        slots = list(self._hints)
        costs = []
        for slot in slots:
            x, lane, _ = self._hints[slot]
            row = []
            for tr in cands:
                dx = wrap(tr.x - x, L)
                row.append((dx / cfg.hint_gate_m) ** 2 + (0.0 if tr.lane == lane else 4.0) if abs(dx) <= cfg.hint_gate_m else math.inf)
            costs.append(row)
        for slot, j in zip(slots, assign(costs, 1e9)):
            if j is not None:
                self._set_label(cands[j], slot)
                del self._hints[slot]
                self.stats["named"] += 1

    def _by_signature(self, slot: int, tracks: list[Track]) -> Track | None:
        """Tie-break: the track whose colours are clearly the closest to the car of `slot`, else None
        (every candidate needs colours of its own: without them nothing is compared)."""
        sig = self.signatures.get(slot)
        if sig is None or len(tracks) < 2 or any(tr.sig is None for tr in tracks):
            return None
        ranked = sorted((distance(tr.sig, sig), tr.id, tr) for tr in tracks)
        (d0, _, best), (d1, _, _) = ranked[0], ranked[1]
        return best if d0 <= self.cfg.sig_max and d1 - d0 >= self.cfg.sig_margin else None

    def _resolve_anchors(self, t: float) -> None:
        cfg, L = self.cfg, self.map.length
        self._anchors = [a for a in self._anchors if t - a.t <= cfg.anchor_max_wait_s]
        ready = sorted((a for a in self._anchors if a.t <= t), key=lambda a: a.t)
        while ready:
            first = ready[0]
            batch = [a for a in ready if a.sector == first.sector and a.t - first.t <= cfg.anchor_group_s]
            for a in batch:
                ready.remove(a)
                self._anchors.remove(a)
            self._resolve_batch(batch)

    def _resolve_batch(self, batch: list[Anchor]) -> None:
        cfg, L = self.cfg, self.map.length
        cands = self._confirmed()
        sector = batch[0].sector
        ax = self.map.anchors[sector]
        dist = [[wrap(tr.x_at(a.t, L) - ax, L) for tr in cands] for a in batch]
        costs = []
        for i, a in enumerate(batch):
            w = []
            for j, tr in enumerate(cands):
                win = cfg.anchor_window_m + cfg.anchor_speed_s * tr.v
                w.append((dist[i][j] / win) ** 2 if abs(dist[i][j]) <= win else math.inf)
            costs.append(w)
        picks = assign(costs, 1.0 + 1e-9)
        taken: set[int] = set()  # tracks chosen by the colour tie-break in this batch
        matched_slots = {a.slot for a, p in zip(batch, picks) if p is not None}
        for i, (a, p) in enumerate(zip(batch, picks)):
            if p is None:
                self.stats["unmatched"] += 1
                self.crossings.append({"slot": a.slot, "sector": a.sector, "t": a.t, "matched": False})
                continue
            tr, d = cands[p], dist[i][p]
            others = sorted(abs(dist[i][j]) for j in range(len(cands)) if j != p)
            ambiguous = bool(others) and others[0] < max(2 * abs(d), abs(d) + 0.12) \
                and others[0] <= cfg.anchor_window_m + cfg.anchor_speed_s * tr.v
            if ambiguous and tr.slot != a.slot:  # a tie: the colours may tell (ponytail: one anchor at a time)
                near = [j for j, c in enumerate(cands) if abs(dist[i][j]) <= cfg.anchor_window_m + cfg.anchor_speed_s * c.v
                        and c.id not in taken]
                alt = self._by_signature(a.slot, [cands[j] for j in near])
                if alt is not None:
                    tr, d, ambiguous = alt, dist[i][cands.index(alt)], False
                    self.stats["sig_resolved"] += 1
            taken.add(tr.id)
            entry = {"slot": a.slot, "sector": a.sector, "t": a.t, "matched": True, "resid_m": d,
                     "v": tr.v, "track": tr.id, "labelled": tr.slot == a.slot}
            if tr.slot != a.slot:
                if ambiguous:
                    self.stats["ambiguous"] += 1
                    entry["matched"] = False
                    self.crossings.append(entry)
                    continue
                holder = self._holder(a.slot)
                old = tr.slot
                self._set_label(tr, a.slot)
                if holder is not None and holder is not tr:
                    swap = old if old is not None and old not in matched_slots else None
                    self._set_label(holder, swap)
                self.stats["relabels"] += 1
                entry["relabelled"] = True
            self.crossings.append(entry)
            self.stats["anchored"] += 1
            # soft correction: the car WAS on the anchor at a.t
            self._update(tr, (tr.x - d) % L, (cfg.anchor_sigma_m + 0.03 * tr.v) ** 2)

    # ------------------------------------------------------------------ outputs

    def positions(self, now: float) -> list[dict[str, Any]]:
        """Positions of the labelled cars at `now`, predicted forward from their last
        measurement (latency compensation), as `track.positions` entries (s = lap fraction)."""
        L = self.map.length
        out = []
        for tr in self.tracks:
            if tr.slot is None or tr.hits < self.cfg.confirm_hits:
                continue
            x, v, _, _ = self._propagate(tr, now, commit=False) if now > tr.t else (tr.x, tr.v, tr.g, tr.P)
            kappa = self.map.kappa[min(tr.lane, len(self.map.kappa) - 1)]
            out.append({"slot": tr.slot, "s": (x % L) / L, "lane": tr.lane,
                        "v": round(v * kappa, 3), "off": tr.off})
        return sorted(out, key=lambda c: c["slot"])

    def snapshot(self) -> list[dict[str, Any]]:
        """Debug view of every track (including unlabelled ones)."""
        return [{"id": tr.id, "slot": tr.slot, "x": tr.x, "v": tr.v, "g": tr.g, "lane": tr.lane,
                 "hits": tr.hits, "off": tr.off, "sx": math.sqrt(max(tr.P[0][0], 0.0))} for tr in self.tracks]


def estimate_latency(crossings: list[dict[str, Any]], min_speed: float = 0.6) -> dict[str, float | None]:
    """Camera latency still missing from the frame timestamps, from the CU crossings.

    At a crossing the car is on the line, so the track's residual `x - anchor` equals
    `C - v * dL` (dL = latency not yet compensated; C a constant error of the finish
    point, e.g. a map made with the same latency error at another speed). Returns the
    slope-only estimate (C = 0, median) and the regression with intercept (needs a
    spread of speeds), in seconds, positive = frames older than assumed."""
    pts = [(e["resid_m"], e["v"]) for e in crossings
           if e.get("matched") and e.get("labelled") and e["v"] >= min_speed]
    out: dict[str, float | None] = {"n": float(len(pts)), "median": None, "regression": None, "residual_m": None}
    if not pts:
        return out
    ratios = sorted(-r / v for r, v in pts)
    out["median"] = ratios[len(ratios) // 2]
    out["residual_m"] = sum(r for r, _ in pts) / len(pts)
    vs = [v for _, v in pts]
    mv = sum(vs) / len(vs)
    var = sum((v - mv) ** 2 for v in vs)
    if len(pts) >= 4 and math.sqrt(var / len(pts)) >= 0.4:
        mr = sum(r for r, _ in pts) / len(pts)
        slope = sum((v - mv) * (r - mr) for r, v in pts) / var
        out["regression"] = -slope
    return out
