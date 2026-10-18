// External controllers: keyboard and gamepad driving a car through the app's
// manual path (`bot.throttle` / `bot.lane` / `estop`). One hub instance runs
// for the whole app: the shell wires its sinks (app.js), the Settings view
// edits the bindings and shows a test meter, the race view shows a badge.
//
// Up to two players, each bound to one car slot. A keyboard player ramps the
// throttle with two keys (accelerate / release); a gamepad player maps one
// axis (rest value captured, dead zone + curve). Lane change and E-STOP are
// edge-triggered; the gamepad E-STOP needs a deliberate hold. Nothing is sent
// while a player is off, and a gamepad that goes away zeroes its throttle.

const STORAGE_KEY = "racelab.controllers";
const SEND_MIN_MS = 90;      // at most ~11 sends per second per player
const KEEPALIVE_MS = 500;    // re-send while held
const ESTOP_HOLD_MS = 1000;  // gamepad: hold the button this long
const DETECT_MS = 4000;      // "move the stick" window
const DETECT_MOVE = 0.35;    // axis movement that locks the detection

export const KEY_DEFAULTS = { up: "ArrowUp", down: "ArrowDown", lane: "Space" };
export const PAD_DEFAULTS = { axis: 1, rest: -1, laneButton: 0, estopButton: 9, invert: false };

function defaults(player) {
  return {
    enabled: false,
    slot: player,
    type: "keyboard",   // keyboard | gamepad
    ...KEY_DEFAULTS,
    ...PAD_DEFAULTS,
    pad: "",            // gamepad id (empty: the first one)
    key_mode: "hold",   // hold: press = gas, release = back to 0 | updown: up/down hold
    deadzone: 0.08,
    curve: 1.0,
    ramp_ms: 600,
  };
}

export function readControllers() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (Array.isArray(raw)) {
      return [0, 1].map((i) => ({ ...defaults(i), ...(raw[i] || {}), slot: Number(raw[i]?.slot ?? i), type: raw[i]?.type === "gamepad" ? "gamepad" : "keyboard" }));
    }
  } catch { /* fresh start */ }
  return [defaults(0), defaults(1)];
}

export function writeControllers(list) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(list)); } catch { /* storage unavailable */ }
}

export function keyLabel(code) {
  const pretty = { ArrowUp: "\u2191", ArrowDown: "\u2193", ArrowLeft: "\u2190", ArrowRight: "\u2192", Space: "\u2423", Enter: "\u23ce", ShiftLeft: "\u21e7", ControlLeft: "Ctrl", AltLeft: "Alt", Backspace: "\u232b", Tab: "\u21b9" };
  return pretty[code] || String(code || "?").replace(/^Key/, "");
}

// value (-1..1, pedals rest at -1) -> 0..15 with the captured rest, dead zone and curve
export function axisToThrottle(value, p) {
  const rest = Number.isFinite(p.rest) ? p.rest : -1;
  let x = (value - rest) / (1 - rest || 1);
  if (p.invert) x = 1 - x;
  x = Math.max(0, Math.min(1, x));
  const dz = Math.max(0, Math.min(0.5, Number(p.deadzone) || 0));
  if (x <= dz) return 0;
  x = (x - dz) / (1 - dz);
  return Math.round(Math.pow(x, Number(p.curve) || 1) * 15);
}

const TYPING = "textarea, select, [contenteditable], input:not([type]), input[type=text], input[type=search], input[type=number], input[type=email], input[type=password], input[type=url], input[type=tel]";

export function firstPressedButton() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  for (const g of pads) {
    if (!g) continue;
    for (let b = 0; b < g.buttons.length; b++) {
      if (g.buttons[b]?.pressed) return { pad: g.id, button: b };
    }
  }
  return null;
}

export function padList() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  return Array.from(pads).filter(Boolean);
}

export class ControllerHub {
  constructor() {
    this.players = readControllers();
    this.sinks = null;
    this.keys = new Set();
    this.throttle = [0, 0];
    this.live = [0, 0];
    this.laneCount = [0, 0];
    this.estopCount = [0, 0];
    this.sentAt = [0, 0];
    this.raw = [0, 0];
    this.estopHeld = [0, 0];
    this.lanePrevKey = [false, false];
    this.lanePrevBtn = [false, false];
    this.hadPad = [false, false];
    this.detect = null;      // { player, until, base, move, best }
    this.paused = false;     // E-STOP engaged: everything stops, ramps reset
    this.started = false;
    this.lastT = 0;
    this._frame = this._frame.bind(this);
    this._key = this._key.bind(this);
  }

  setSinks(sinks) { this.sinks = sinks; }

  // swap the bindings (Settings saves): throttle is dropped only where the
  // mapping really changed (off / different car / different kind of input)
  setPlayers(list) {
    this.players.forEach((p, i) => {
      const n = list[i] || {};
      if (this.throttle[i] && (!n.enabled || n.slot !== p.slot || n.type !== p.type)) this.send(i, 0);
    });
    this.players = list;
  }

  playerFor(slot) {
    return this.players.find((p) => p.enabled && p.slot === slot) || null;
  }

  start() {
    if (this.started) return;
    this.started = true;
    addEventListener("keydown", this._key);
    addEventListener("keyup", this._key);
    document.addEventListener("visibilitychange", () => { if (document.hidden) this.zeroAll(); });
    requestAnimationFrame(this._frame);
  }

  // the app engages its E-STOP (Escape key, pad hold, the STOP button): while it
  // is on, nothing is sent and the ramps restart from zero (no surprise restart)
  setPaused(v) {
    v = !!v;
    if (v === this.paused) return;
    this.paused = v;
    if (v) {
      this.raw = [0, 0];
      this.live = [0, 0];
      this.zeroAll();
    }
  }

  zeroAll() {
    this.players.forEach((_, i) => { if (this.throttle[i]) this.send(i, 0); });
  }

  // start watching the pads: the axis that moves the most wins, its rest value
  // is captured as the "released" position (works for sticks and pedals)
  detectAxis(i, ms = DETECT_MS) {
    const p = this.players[i];
    const pad = this._pick(p);
    if (!pad) return false;
    this.detect = { player: i, until: performance.now() + ms, base: pad.axes.map((v) => v), move: 0, best: -1 };
    return true;
  }

  _pick(p) {
    const pads = padList();
    if (p.pad) return pads.find((g) => g.id === p.pad) || null;
    return pads[0] || null;
  }

  send(i, v) {
    const p = this.players[i];
    if (!p || !this.sinks) return;
    this.throttle[i] = v;
    this.live[i] = v;
    this.sentAt[i] = this.lastT;
    this.sinks.throttle(p.slot, v);
  }

  _key(ev) {
    if (ev.target?.closest?.(TYPING)) return;  // typing somewhere: hands off
    const wanted = this.players.some((p) => p.enabled && p.type === "keyboard" &&
      (ev.code === p.up || ev.code === p.down || ev.code === p.lane));
    if (wanted) ev.preventDefault();
    if (ev.type === "keydown") this.keys.add(ev.code);
    else this.keys.delete(ev.code);
  }

  _frame(t) {
    if (!this.started) return;
    const dt = Math.min(100, this.lastT ? t - this.lastT : 16);
    this.lastT = t;
    this._detectFrame(t);
    if (this.paused) {
      this.players.forEach((_, i) => { if (this.throttle[i]) this.send(i, 0); this.live[i] = 0; });
      requestAnimationFrame(this._frame);
      return;
    }
    this.players.forEach((p, i) => {
      if (!p.enabled || !this.sinks) {
        if (this.throttle[i] !== 0) this.send(i, 0);
        this.live[i] = 0;
        return;
      }
      if (p.type === "gamepad") this._framePad(p, i, t, dt);
      else this._frameKeys(p, i, t, dt);
    });
    requestAnimationFrame(this._frame);
  }

  _frameKeys(p, i, t, dt) {
    const ramp = Math.max(50, Number(p.ramp_ms) || 600);
    const up = this.keys.has(p.up);
    if (p.key_mode === "updown") {
      const down = this.keys.has(p.down);
      if (up) this.raw[i] = Math.min(1, this.raw[i] + dt / ramp);
      else if (down) this.raw[i] = Math.max(0, this.raw[i] - dt / ramp);
    } else if (up) {
      this.raw[i] = Math.min(1, this.raw[i] + dt / ramp);
    } else {
      this.raw[i] = Math.max(0, this.raw[i] - dt / ramp);  // released: back to 0
    }
    const lane = this.keys.has(p.lane);
    if (lane && !this.lanePrevKey[i]) this._pulse(i, "lane");
    this.lanePrevKey[i] = lane;
    const v = Math.round(this.raw[i] * 15);
    this.live[i] = v;
    this._maybeSend(i, v, t);
  }

  _framePad(p, i, t, dt) {
    const pad = this._pick(p);
    if (!pad) {
      if (this.hadPad[i]) {
        this.hadPad[i] = false;
        this.send(i, 0);
        this.sinks.status?.("ctl.pad_gone");
      }
      this.live[i] = 0;
      return;
    }
    this.hadPad[i] = true;
    const pressed = (n) => !!pad.buttons[n]?.pressed;
    const lane = pressed(p.laneButton);
    if (lane && !this.lanePrevBtn[i]) this._pulse(i, "lane");
    this.lanePrevBtn[i] = lane;
    const estopDown = pressed(p.estopButton);
    this.estopHeld[i] = estopDown ? this.estopHeld[i] + dt : 0;
    if (this.estopHeld[i] >= ESTOP_HOLD_MS) {
      this.estopHeld[i] = -1e9;  // require a release before firing again
      this.estopCount[i]++;
      this.sinks.estop();
    }
    const v = axisToThrottle(pad.axes[p.axis] ?? p.rest ?? -1, p);
    this.live[i] = v;
    this._maybeSend(i, v, t);
  }

  _maybeSend(i, v, t) {
    if (!this.sinks) return;
    if (v !== this.throttle[i]) {
      if (t - this.sentAt[i] >= SEND_MIN_MS) this.send(i, v);
    } else if (v > 0 && t - this.sentAt[i] >= KEEPALIVE_MS) {
      this.send(i, v);
    }
  }

  _pulse(i, what) {
    if (!this.sinks) return;
    if (what === "lane") { this.laneCount[i]++; this.sinks.lane(this.players[i].slot); }
  }

  _detectFrame(t) {
    const d = this.detect;
    if (!d) return;
    const p = this.players[d.player];
    const pad = this._pick(p);
    if (!pad || t > d.until) { this.detect = null; return; }
    pad.axes.forEach((v, ax) => {
      const move = Math.abs(v - d.base[ax]);
      if (move > d.move) { d.move = move; d.best = ax; }
    });
    if (d.best >= 0 && d.move >= DETECT_MOVE) {
      p.axis = d.best;
      p.pad = pad.id;
      p.rest = d.base[d.best];
      writeControllers(this.players);
      this.detect = null;
    }
  }
}

// the app-wide hub: app.js wires the sinks, views read it directly
export const hub = new ControllerHub();
