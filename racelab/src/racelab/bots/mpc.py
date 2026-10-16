"""Model-predictive pilot ("MPC"): the AI driver v1.

Every decision it imagines `samples` throttle sequences over the next `horizon_s` seconds,
simulates each with the first-order motor model, scores them, keeps the best `elite`, refits
and repeats (`iters`, the cross-entropy method), then sends only the first throttle of the best
plan. The next decision starts from that plan shifted by the time that passed (receding
horizon).

Score of a sequence (lower is better), in metres of progress:
    - progress
    + penalty(risk) x deslot probability in each corner it crosses (v^2 x curvature against the
      corner's grip, blurred by `sigma`)
    + the same penalty if even full braking from the end of the horizon cannot make the next corner
    + 0.01 x throttle changes

It estimates the car while driving, from the published positions only:
- the actuation delay (`latency_s` is only the starting guess): among a few candidates, the one
  that best predicts the measured speed;
- a gain on top speed (heating, a weaker car): least squares with forgetting.
The grip of each corner is learned exactly like the corner speed profile (it subclasses it):
a_lat x factor^2, +3 % after a clean pass at the target, -8 % and frozen after an off track.
Without usable positions it falls back to target pace like the corner profile.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .profiles import POS_MAX_AGE_MS, Context, CornersProfile, curvature, lane_curvature

PLAN_DT = 0.075  # s, one planning step = one CU rail cycle
LATENCIES = (0.05, 0.1, 0.15, 0.2, 0.25, 0.3)  # s, actuation delays the estimator chooses from
FORGET = 0.97  # forgetting factor of the estimators
PEN_SAFE, PEN_ATTACK = 40.0, 3.0  # deslot penalty (m of progress) at risk 0 / risk 1
BRAKE_REACH = 0.85  # terminal check: braking assumed at 85 % of what lifting off gives
NO_GRIP_LIMIT = 1e9  # grip on a straight: never the limit
LEARN_SHARE = 0.75  # grip learning: a pass at >= 90 % of this share of the corner's limit speed teaches


def _logistic(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-1.702 * z))  # ~ normal CDF


@dataclass
class MPCProfile(CornersProfile):
    """Model-predictive pilot. Same fields as the corner profile (grip, cap, motor model,
    fallback), plus the planner's: `risk` (0 = cautious, 1 = attack), the horizon and the
    sampling budget."""

    kind = "mpc"
    risk: float = 0.5
    horizon_s: float = 1.2
    samples: int = 48
    iters: int = 2
    elite: int = 8
    sigma: float = 0.05  # uncertainty on the grip ratio in the deslot probability
    seed: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        for name, lo, hi in (("risk", 0, 1), ("horizon_s", 0.3, 3), ("sigma", 0.01, 0.5)):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{name}: number between {lo} and {hi} expected")
        for name, lo, hi in (("samples", 8, 256), ("iters", 1, 5), ("elite", 2, 64), ("seed", 0, 2 ** 31)):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{name}: integer between {lo} and {hi} expected")
        if self.elite >= self.samples:
            raise ValueError("elite: fewer than samples expected")

    # ------------------------------------------------------------ state

    def reset(self) -> None:
        super().reset()
        self._rng = np.random.default_rng(self.seed)
        self._sent: list[tuple[float, float]] = []  # (time, throttle sent), last ~2 s
        self._lat = min(LATENCIES, key=lambda d: abs(d - self.latency_s))
        self._lat_score = {d: 0.0 for d in LATENCIES}
        self.gain = 1.0  # estimated top-speed gain (not a field)
        self.latency_est = self._lat  # estimated actuation delay, s (not a field)
        self._sxy = self._sxx = 0.0
        self._grip_key: tuple | None = None

    def _drop(self) -> None:
        super()._drop()
        self._mu: np.ndarray | None = None
        self._t_plan: float | None = None
        self._meas: tuple[float, float] | None = None  # (time, speed) of the last measurement

    def output(self, ctx: Context) -> int:
        value = super().output(ctx)
        self._sent.append((ctx.now, float(value)))  # what really goes out (ramp, rounding, cap)
        while len(self._sent) > 2 and self._sent[1][0] < ctx.now - 2.0:
            self._sent.pop(0)
        return value

    def _sent_at(self, t: float) -> float:
        """Throttle that was being sent at time `t` (0 before the history)."""
        value = 0.0
        for ts, u in self._sent:
            if ts <= t:
                value = u
            else:
                break
        return value

    # ------------------------------------------------------------ tables

    def _prepare(self, geo: dict[str, Any]) -> None:
        key = self._key
        super()._prepare(geo)
        if self._key == key and hasattr(self, "_kd"):
            return
        n, ds = geo["n"], geo["ds"]
        pad = round(0.12 / ds)
        self._near_np = np.array(self._near, dtype=np.int64)
        self._kd, self._dn, self._nid, self._kn = [], [], [], []
        ncorner = len(self._spans)
        for lane in (0, 1):
            ak = lane_curvature(geo["kappa"], geo["lane_off"], lane)
            kd = np.array([max(ak[(i + d) % n] for d in range(-pad, pad + 1)) for i in range(n)])
            self._kd.append(kd)
            kmax = np.zeros(max(ncorner, 1))
            for c in range(ncorner):
                sel = self._near_np == c
                kmax[c] = kd[sel].max() if sel.any() else 0.0
            self._kn.append(kmax)
            dn, nid = np.zeros(n), np.full(n, -1, dtype=np.int64)
            dist, nxt = 0.0, -1
            for t in range(2 * n - 1, -1, -1):  # backwards, twice: the lap is periodic
                j = t % n
                c = self._near[j]
                if c >= 0 and self._near[j - 1] != c:  # a corner starts here
                    nxt, dist = c, 0.0
                else:
                    dist += ds
                dn[j], nid[j] = dist, nxt
            self._dn.append(dn)
            self._nid.append(nid)
        self._grip_key = None

    def _speeds(self, geo: dict[str, Any], lane: int) -> list[float]:
        """Reference speed of the grip learning (not a target the car is driven to): a corner
        teaches (+3 %) when the car kept at least 90 % of it. The planner stays under its own
        limit, so the reference is a share of that limit, otherwise a cautious plan never learns."""
        n = geo["n"]
        scale: list[float | None] = [None] * n
        for c, r in enumerate(self._spans):
            f = self._f.get((lane, c), 1.0)
            for i in r:
                scale[i % n] = f if scale[i % n] is None else min(scale[i % n], f)
        top = self.v_max * max(0.0, min(15.0, self.cap)) / 15
        return [min(top, LEARN_SHARE * (1.0 if scale[i] is None else scale[i]) * self._raw[lane][i])
                for i in range(n)]

    def _grip(self, lane: int) -> np.ndarray:
        """Grip (m/s^2) per corner for `lane`: the start value times the learned factor squared."""
        key = (lane, tuple(sorted(self._f.items())), self.a_lat, id(self._near_np))
        if key != self._grip_key:
            self._grip_key = key
            ncorner = len(self._spans)
            self._grip_c = np.array([self.a_lat * self._f.get((lane, c), 1.0) ** 2 for c in range(ncorner)]
                                    + [NO_GRIP_LIMIT])  # index -1 = straight
        return self._grip_c

    # ------------------------------------------------------------ estimation

    def _estimate(self, v: float, t: float, off: bool) -> None:
        last, self._meas = self._meas, (t, v)
        if last is None or off:
            return
        dt = t - last[0]
        if not 0.01 <= dt <= 0.5:
            return
        v0 = last[1]
        mid = t - dt / 2
        for d in LATENCIES:  # which delay explains the measured speed best
            pred = self._model_g(v0, self._sent_at(mid - d), dt, self.gain)
            self._lat_score[d] = FORGET * self._lat_score[d] + (v - pred) ** 2
        best = min(LATENCIES, key=self._lat_score.__getitem__)
        if self._lat_score[best] < 0.9 * self._lat_score[self._lat]:
            self._lat = best
        self.latency_est = self._lat
        u = self._sent_at(mid - self._lat)
        if u >= 3:  # enough excitation: top-speed gain, least squares with forgetting
            vss = self.gain * self.v_max * u / 15
            a = math.exp(-dt / (self.tau_acc if vss >= v0 else self.tau_brake))
            x, y = (1 - a) * self.v_max * u / 15, v - a * v0
            self._sxy = FORGET * self._sxy + x * y
            self._sxx = FORGET * self._sxx + x * x
            self.gain = max(0.5, min(1.6, (self._sxy + 0.3) / (self._sxx + 0.3)))

    def _model_g(self, v: float, u: float, dt: float, g: float) -> float:
        vss = g * self.v_max * u / 15
        return vss + (v - vss) * math.exp(-dt / (self.tau_acc if vss >= v else self.tau_brake))

    # ------------------------------------------------------------ driving

    def _drive(self, ctx: Context) -> float | None:
        pos, geometry = ctx.pos, ctx.geometry
        if not pos or not geometry or pos.get("age_ms", 0) > POS_MAX_AGE_MS:
            self._drop()
            return None
        src = (geometry.get("points"), geometry.get("length_m"))
        if self._src is None or src[0] is not self._src[0] or src[1] != self._src[1]:
            self._src, self._geo, self._key = src, curvature(geometry), None
        geo = self._geo
        s = pos.get("s")
        if (geo is None or isinstance(s, bool) or not isinstance(s, (int, float))
                or not math.isfinite(s) or not 0 <= s <= 1):
            self._drop()
            return None
        self._prepare(geo)
        n, ds = geo["n"], geo["ds"]
        lane = 1 if pos.get("lane") == 1 else 0
        fi = ((s + geo["finish"]) % 1.0) * n
        t_meas = ctx.now - pos.get("age_ms", 0) / 1000
        v = self._speed(pos, fi * ds, t_meas, geo["length"])
        off = bool(pos.get("off"))
        self._observe(geo, lane, int(fi) % n, v, off, ctx.now)  # grip learning (corner profile)
        self._estimate(v, t_meas, off)
        if off:
            self._mu = None
            return 0.0
        # the measurement is `age_ms` old: plan from where the car is now
        s_now = fi * ds + v * (ctx.now - t_meas)
        return self._plan(geo, lane, s_now, v, ctx.now)

    def _plan(self, geo: dict[str, Any], lane: int, s0: float, v0: float, now: float) -> float:
        n, ds, length = geo["n"], geo["ds"], geo["length"]
        cap = max(0, min(15, math.floor(self.cap)))
        H, S, E = max(2, round(self.horizon_s / PLAN_DT)), self.samples, self.elite
        if self._mu is None or len(self._mu) != H:
            self._mu = np.full(H, min(cap, 6.0))
        elif self._t_plan is not None:  # receding horizon: shift by the time that passed
            k = max(0, min(H - 1, round((now - self._t_plan) / PLAN_DT)))
            if k:
                self._mu = np.concatenate([self._mu[k:], np.repeat(self._mu[-1:], k)])
        self._t_plan = now
        # throttles already sent but not applied yet (the delay), then the plan
        lat_steps = self._lat / PLAN_DT
        h_idx = np.arange(H)
        t_apply = now + h_idx * PLAN_DT - self._lat
        sent = np.array([self._sent_at(t) for t in t_apply])
        plan_idx = np.floor(h_idx - lat_steps + 1e-9).astype(int)
        from_plan = plan_idx >= 0
        kd, near, grip_c = self._kd[lane], self._near_np, self._grip(lane)
        dn, nid, kn = self._dn[lane], self._nid[lane], self._kn[lane]
        pen = PEN_SAFE + (PEN_ATTACK - PEN_SAFE) * self.risk
        ea, eb = math.exp(-PLAN_DT / self.tau_acc), math.exp(-PLAN_DT / self.tau_brake)
        vgain = self.gain * self.v_max / 15
        sd = np.full(H, 3.5)
        mu = self._mu
        for _ in range(self.iters):
            U = np.clip(np.rint(mu + sd * self._rng.standard_normal((S, H))), 0, cap)
            U[0] = np.clip(np.rint(mu), 0, cap)
            applied = np.where(from_plan, U[:, np.clip(plan_idx, 0, H - 1)], sent)
            s = np.full(S, s0)
            v = np.full(S, max(0.0, v0))
            seg, prev, cost = np.zeros(S), np.full(S, -2), np.zeros(S)
            for h in range(H):
                vt = vgain * applied[:, h]
                vn = vt + (v - vt) * np.where(vt >= v, ea, eb)
                s = s + (v + vn) / 2 * PLAN_DT
                v = vn
                j = np.floor((s % length) / ds).astype(int) % n
                c = near[j]
                r = np.where(c >= 0, v * v * kd[j] / grip_c[c], 0.0)
                new = c != prev
                cost += np.where(new & (prev >= 0), pen * _logistic((seg - 1) / self.sigma), 0.0)
                seg = np.where(new, r, np.maximum(seg, r))
                prev = c
            cost += np.where(prev >= 0, pen * _logistic((seg - 1) / self.sigma), 0.0)
            j = np.floor((s % length) / ds).astype(int) % n  # can the next corner still be made?
            nc = nid[j]
            ve = np.maximum(v - BRAKE_REACH * dn[j] / self.tau_brake, 0.0)
            ratio = np.where(nc >= 0, ve * ve * kn[np.maximum(nc, 0)] / grip_c[np.maximum(nc, 0)], 0.0)
            cost += pen * _logistic((ratio - 1) / self.sigma)
            cost += -(s - s0) + 0.01 * np.abs(np.diff(U, axis=1)).sum(axis=1)
            elite = U[np.argpartition(cost, E)[:E]]
            mu, sd = elite.mean(axis=0), elite.std(axis=0) + 0.7
        self._mu = mu
        return float(np.clip(np.rint(mu[0]), 0, cap))


# the race brain's pilot registry loads this module (brain.load_pilot("mpc")): Pilot is defined
# before the registry is built, so this import is safe
from .brain import Pilot  # noqa: E402

PILOT = Pilot(MPCProfile, "Predictive (MPC)", "pilot.mpc.label", (
    {"key": "risk", "label_key": "pilot.mpc.risk", "min": 0, "max": 100, "scale": 100},  # stored 0..1, shown in %
    {"key": "a_lat", "label_key": "profile.a_lat", "min": 1, "max": 40},
    {"key": "cap", "label_key": "profile.cap", "min": 0, "max": 15},
    {"key": "target_ms", "label_key": "profile.fallback_time", "min": 1, "max": 120, "scale": 0.001}))
