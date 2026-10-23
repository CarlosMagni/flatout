// Race screen: session, live timing, track map (simulator), software drivers.
import {
  GATE_LABEL, STATE_CHIP, STATE_LABEL, clock, gap, h, inkOn, lapTime, s, setText, toggle, wallTime,
} from "../dom.js";
import { logText, t, tOr } from "../i18n.js";
import { botsUnavailable, onEvent, profileLabel as profileKindLabel, request, sessionLabel, store } from "../store.js";
import { FuelGauge, Fader, fillConnectors, Lights, Segmented, Spark, Stepper, Toggle, confirmModal, openDrawer, run, toast } from "../ui/components.js";
import { hub } from "../input.js";
import { ConnectChooser } from "./connect.js";

const LAP_MODES = new Set(["race", "rival", "elimination"]);
const TIMED = new Set(["endurance", "qualifying"]);
const QUICK_RACE_LAPS = 10;
const QUICK_QUALIFYING_MIN = 5;
const SPACE_QUIET_MS = 2000;

export class RaceView {
  constructor(root) {
    this.root = root;
    this.rows = new Map();
    this.lastLaps = new Map();
    this.off = [];
    this.build();
    this.off.push(onEvent("*", (data, name) => this.onEvent(name, data)));
    // new track shape (connector or virtual CU): the map is redrawn
    this.off.push(onEvent("track", (data) => {
      if (store.meta) store.meta.track = data.geometry;
      this.track = null;
      this.mapBox.querySelector(".trackmap").replaceChildren();
    }));
    this.transitionAt = -Infinity; // performance.now() of the last session state change
    this.keyHandler = (ev) => this.onKey(ev);
    document.addEventListener("keydown", this.keyHandler);
  }

  destroy() {
    this.off.forEach((f) => f());
    document.removeEventListener("keydown", this.keyHandler);
  }

  build() {
    // session bar
    this.title = h("div.session-title");
    this.typeLabel = h("div.eyebrow");
    this.stateChip = h("span.chip");
    this.subInfo = h("span.dim");
    this.counterValue = h("div.value");
    this.counterLabel = h("div.eyebrow");
    this.lights = Lights();
    this.clockValue = h("div.value");
    this.clockBox = h("div.clock", {}, h("div.eyebrow", { text: t("race.elapsed") }), this.clockValue);
    this.actions = h("div.session-actions");
    this.bar = h("section.panel.session-bar", {},
      h("div", {}, this.typeLabel, this.title, h("div.session-sub", {}, this.stateChip, this.subInfo)),
      h("div.counter", {}, this.counterValue, this.counterLabel),
      this.lights.el,
      this.clockBox,
      this.actions);

    // timing
    this.head = h("div.trow.head", {},
      h("div.pos", { text: t("race.col.pos") }), h("div", { text: t("race.col.driver") }), h("div.r", { text: t("race.col.laps") }),
      h("div.r", { text: t("race.col.last") }), h("div.r", { text: t("race.col.best") }), h("div.r.gapcol", { text: t("race.col.gap") }),
      h("div.r", { text: t("race.col.int") }), h("div.r", { text: t("race.col.fuel") }));
    this.tbody = h("div");
    this.emptyBox = h("div.empty.hidden");
    this.chooser = new ConnectChooser();
    this.welcome = h("div.welcome.hidden", {},
      h("h3", { text: t("race.welcome.title") }),
      h("p", { text: t("race.welcome.text") }),
      this.chooser.el);
    this.timing = h("section.panel.timing", {}, this.head, this.tbody, this.emptyBox, this.welcome);

    // right column
    this.bestValue = h("div.value", { text: "—" });
    this.bestWho = h("div.who-line");
    this.best = h("section.panel.best", {}, h("div.eyebrow", { text: t("race.best_lap") }), this.bestValue, this.bestWho);
    this.mapSource = h("span.chip");
    this.mapConnectors = h("span.connector-chips");
    this.mapBox = h("section.panel.hidden", {},
      h("div.panel-head", {}, h("h2", { text: t("race.track") }), h("span.spacer"), this.mapConnectors, this.mapSource),
      h("div.trackmap"));
    this.feed = h("ul.feed");
    this.feedBox = h("section.panel", {}, h("div.panel-head", {}, h("h2", { text: t("race.feed") })), this.feed);
    this.side = h("aside.side", {}, this.best, this.mapBox, this.feedBox);

    // software drivers ("bots"); in normal mode, an invitation to turn them on
    this.botCards = [];
    this.botsGrid = h("div.bots-grid");
    this.botsNote = h("span.dim", { title: t("bots.note_title") });
    this.botsOff = h("button.btn.sm", {
      title: t("bots.off_title"), onclick: () => setMode("classic"),
    }, t("bots.off"));
    this.botsHead = h("div.bots-head", {}, h("h2", { text: t("race.bots") }), this.botsNote, h("span.spacer"), this.botsOff);
    this.inviteText = h("p");
    this.inviteBtn = h("button.btn.primary", { onclick: () => setMode("advanced") }, t("bots.invite.go"));
    this.invite = h("div.panel.bots-invite", {},
      h("div.bots-invite-text", {}, h("h3", { text: t("bots.invite.title") }), this.inviteText),
      this.inviteBtn);
    this.bots = h("section.bots.hidden", {}, this.invite, this.botsHead, this.botsGrid);

    this.shortBanner = h("div.short-banner", { hidden: true }, t("race.short_banner"));
    this.root.append(
      h("div.race", {}, this.shortBanner, this.bar, h("div.race-grid", {}, this.timing, this.bots, this.side)));

    this.fillFeed();
  }

  hello() {
    this.track = null;
    this.mapBox.querySelector(".trackmap").replaceChildren();
    this.fillFeed();
  }

  fillFeed() {
    this.feed.replaceChildren();
    const entries = (store.log || []).slice(-15);
    if (!entries.length) this.feed.append(h("li.none", { text: t("race.feed_empty") }));
    for (const entry of entries) this.addFeed(logText(entry), entry.level, entry.t);
  }

  // ------------------------------------------------------------ update

  update(state) {
    const sess = state.session;
    if (this.state && this.state.session.state !== sess.state) this.transitionAt = performance.now();
    this.state = state;
    this.updateBar(state, sess);
    this.updateTiming(state, sess);
    this.updateBest(state, sess);
    this.updateMap(state);
    this.updateBots(state);
  }

  updateBar(state, sess) {
    const type = store.meta?.session_types?.[sess.type];
    setText(this.typeLabel, sess.state === "idle" ? "RaceLab" : sessionLabel(sess.type));
    setText(this.title, sess.state === "idle" ? t("race.idle_title") : sess.title);
    const shorted = (state.cu?.phase ?? 0) >= 10;
    this.stateChip.className = `chip ${STATE_CHIP[sess.state] || ""}`;
    setText(this.stateChip, shorted ? t("state.short_circuit")
      : sess.false_start ? t("state.false_start") : STATE_LABEL[sess.state] || sess.state);
    if (sess.false_start || shorted) this.stateChip.className = "chip stop";
    this.shortBanner.hidden = !shorted;

    const running = state.cars.filter((c) => c.enabled && !c.eliminated).length;
    setText(this.subInfo, sess.type === "elimination" && sess.state !== "idle" ? t("race.on_track", { n: running }) : "");

    let value = "—", label = "";
    let showClock = true;
    if (sess.state === "idle") {
      value = "—"; label = t("race.lap");
    } else if (LAP_MODES.has(sess.type) && sess.laps && sess.type !== "elimination") {
      const lap = Math.min((sess.leader_lap || 0) + (sess.state === "finished" ? 0 : 1), sess.laps);
      this.counterValue.replaceChildren(String(Math.max(lap, sess.state === "grid" ? 0 : 1)), h("small", { text: `/${sess.laps}` }));
      label = sess.state === "finishing" || sess.state === "finished" ? t("race.finish") : t("race.lap");
      value = null;
    } else if (sess.type === "elimination") {
      value = String((sess.leader_lap || 0) + (sess.state === "finished" ? 0 : 1)); label = t("race.lap");
    } else if (TIMED.has(sess.type) || sess.duration_s) {
      value = clock(sess.remaining_s ?? sess.duration_s); label = t("race.remaining");
    } else {
      value = clock(sess.elapsed_s); label = t("race.elapsed"); showClock = false;
    }
    if (value !== null) setText(this.counterValue, value);
    setText(this.counterLabel, label);
    toggle(this.clockBox, "hidden", !showClock);
    setText(this.clockValue, clock(sess.elapsed_s));
    this.lights.update(state.cu, sess.state);
    // lights are only useful for a lights-start session, or if the CU lights them itself
    const lightsUseful = type?.uses_lights || state.cu.phase !== 0 || sess.state === "idle";
    this.lights.el.style.visibility = lightsUseful ? "" : "hidden";
    this.renderActions(sess.state);
  }

  // a real button (not a ghost link): the big screen is THE view for people standing by the track
  bigScreenButton() {
    const tv = s("svg", { viewBox: "0 0 24 24", width: 16, height: 16, fill: "none", stroke: "currentColor",
      "stroke-width": 2, "stroke-linecap": "round", "aria-hidden": "true" },
    s("rect", { x: 2, y: 4, width: 20, height: 13, rx: 2 }), s("path", { d: "M8 21h8M12 17v4" }));
    return h("a.btn", { href: "#/screen", title: t("race.big_screen_title") }, tv, t("race.big_screen"));
  }

  renderActions(st) {
    if (this.actionsFor === st) return;
    this.actionsFor = st;
    const b = (label, cls, fn, key) => {
      const el = h(`button.btn${cls ? "." + cls : ""}`, { onclick: fn }, label, key ? h("kbd", { text: key }) : null);
      return el;
    };
    const start = () => run("race.start").catch(() => {});
    const stop = async () => {
      const ok = await confirmModal({
        title: t("race.finish_confirm.title"),
        text: t("race.finish_confirm.text"),
        confirmLabel: t("race.act.finish"), danger: true,
      });
      if (ok) run("race.stop").catch(() => {});
    };
    const list = {
      idle: [b(t("race.act.setup"), "primary", () => (location.hash = "#/setup"))],
      grid: [b(t("race.act.edit"), "ghost", () => (location.hash = "#/setup")), b(t("race.act.start"), "go", start, t("key.space"))],
      starting: [b(t("common.cancel"), "danger", () => run("race.stop").catch(() => {}))],
      running: [b(t("race.act.finish"), "danger", stop), b(t("race.act.pause"), "", () => run("race.pause").catch(() => {}), t("key.space"))],
      paused: [b(t("race.act.finish"), "danger", stop), b(t("race.act.resume"), "primary", () => run("race.resume").catch(() => {}), t("key.space"))],
      finishing: [b(t("race.act.close_now"), "", () => run("race.stop").catch(() => {}))],
      finished: [b(t("race.act.results"), "ghost", () => (location.hash = "#/history")), b(t("race.act.new"), "primary", () => this.again())],
    }[st] || [];
    this.actions.replaceChildren(this.bigScreenButton(), ...list);
  }

  async again() {
    const settings = store.meta?.session;
    try {
      await run("race.setup", { settings });
    } catch { /* toast already shown */ }
  }

  onKey(ev) {
    if (ev.key !== " " || ev.repeat) return;
    if (ev.target.closest("input, textarea, select, button, a, summary, [tabindex], .fader") || document.querySelector(".drawer, .modal")) return;
    const st = this.state?.session.state;
    const op = { grid: "race.start", running: "race.pause", paused: "race.resume" }[st];
    if (op) {
      ev.preventDefault();
      // a second press right after a change (start, pause...) is a double tap, not a new order
      if (performance.now() - this.transitionAt < SPACE_QUIET_MS) return;
      run(op).catch(() => {});
    }
  }

  // ------------------------------------------------------------ timing

  updateTiming(state, sess) {
    const noCu = state.link.transport === "none";
    const idle = sess.state === "idle" || noCu;
    toggle(this.welcome, "hidden", !noCu);
    toggle(this.emptyBox, "hidden", !idle || noCu);
    toggle(this.head, "hidden", idle);
    toggle(this.tbody, "hidden", idle);
    if (noCu) {
      this.chooser.update(state);
      return;
    }
    if (idle) {
      this.renderEmpty();
      return;
    }
    const fuelShown = state.cu.pit_lane || state.cu.fuel_mode;
    const cars = state.cars.filter((c) => c.enabled || c.laps > 0);
    const seen = new Set();
    const order = [];
    for (const car of cars) {
      let row = this.rows.get(car.slot);
      if (!row) {
        row = this.makeRow(car);
        this.rows.set(car.slot, row);
      }
      this.fillRow(row, car, sess, state, fuelShown);
      seen.add(car.slot);
      order.push(row.el);
    }
    for (const [slot, row] of this.rows) {
      if (!seen.has(slot)) { row.el.remove(); this.rows.delete(slot); }
    }
    this.reorder(order);
  }

  renderEmpty() {
    if (this.emptyBox.childElementCount) return;
    const quick = (label, settings, startNow) => h("button.btn", {
      onclick: async () => {
        try {
          // the button's label is the title, in the UI language (the server's default title is English)
          await run("race.setup", { settings: { ...settings, title: label } });
          if (startNow) await run("race.start");
        } catch { /* toast already shown */ }
      },
    }, label);
    this.emptyBox.append(
      h("h3", { text: t("race.empty.title") }),
      h("p", { text: t("race.empty.text") }),
      h("div.quick", {},
        quick(sessionLabel("practice"), { type: "practice" }, true),
        quick(t("race.quick.race", { laps: QUICK_RACE_LAPS }), { type: "race", laps: QUICK_RACE_LAPS }),
        quick(t("race.quick.qualifying", { min: QUICK_QUALIFYING_MIN }), { type: "qualifying", duration_s: QUICK_QUALIFYING_MIN * 60 }),
        h("a.btn.primary", { href: "#/setup" }, t("race.quick.setup"))));
  }

  makeRow(car) {
    const r = {};
    r.pos = h("div.pos");
    r.carno = h("span.carno");
    r.name = h("span.name");
    r.tags = h("span.tags");
    r.laps = h("div.r.laps");
    r.last = h("div.r.time");
    r.best = h("div.r.time");
    r.gap = h("div.r.time.dim");
    r.interval = h("div.r.time.dim");
    r.fuel = FuelGauge();
    r.el = h("div.trow", {},
      r.pos,
      h("div.driver", {}, r.carno, h("div", { style: { minWidth: 0 } }, r.name), r.tags),
      r.laps, r.last, r.best, r.gap, r.interval, h("div", {}, r.fuel.el));
    return r;
  }

  fillRow(r, car, sess, state, fuelShown) {
    r.el.style.setProperty("--car", car.color);
    r.carno.style.color = inkOn(car.color);
    setText(r.pos, car.position ?? "—");
    setText(r.carno, car.slot + 1);
    setText(r.name, car.name);
    const tags = [];
    if (state.mode === "advanced" && car.controller !== "human") {
      tags.push(h(`span.who.is-${car.controller}`, { text: car.controller === "api" ? t("bot.driver.api") : t("bot.tag") }));
    }
    if (car.finished) tags.push(h("span.flag-done", { title: t("car.finished") }));
    if (car.eliminated) tags.push(h("span.chip.stop", { text: t("car.eliminated") }));
    const sig = tags.map((tag) => tag.outerHTML).join("");
    if (r.tagSig !== sig) { r.tagSig = sig; r.tags.replaceChildren(...tags); }
    toggle(r.el, "out", car.eliminated);

    setText(r.laps, car.laps);
    setText(r.last, lapTime(car.last_ms));
    toggle(r.last, "sb", car.last_sb);
    toggle(r.last, "pb", car.last_pb && !car.last_sb);
    setText(r.best, lapTime(car.best_ms));
    toggle(r.best, "sb", sess.best && sess.best.slot === car.slot);
    const leader = car.position === 1;
    setText(r.gap, leader ? "" : gap(car.gap_ms, car.gap_laps));
    setText(r.interval, leader ? "" : gap(car.interval_ms, car.interval_laps));
    if (fuelShown) r.fuel.update(car.fuel, car.pit);
    else r.fuel.update(null, car.pit);

    const prev = this.lastLaps.get(car.slot);
    if (prev != null && car.laps > prev) {
      r.el.classList.remove("flash");
      void r.el.offsetWidth;
      r.el.classList.add("flash");
    }
    this.lastLaps.set(car.slot, car.laps);
  }

  // reorders with a FLIP animation (rows slide to their new place)
  reorder(order) {
    const current = [...this.tbody.children];
    if (current.length === order.length && current.every((el, i) => el === order[i])) return;
    const before = new Map(current.map((el) => [el, el.getBoundingClientRect().top]));
    this.tbody.replaceChildren(...order);
    for (const el of order) {
      const top = before.get(el);
      if (top == null) continue;
      // rects are in screen px, translateY in the page's own (zoomed on wide screens, style.css)
      const dy = (top - el.getBoundingClientRect().top) / (el.currentCSSZoom || 1);
      if (!dy) continue;
      el.animate([{ transform: `translateY(${dy}px)` }, { transform: "none" }],
        { duration: 420, easing: "cubic-bezier(.2,.7,.2,1)" });
    }
  }

  updateBest(state, sess) {
    const best = sess.best;
    if (!best) {
      setText(this.bestValue, "—");
      this.bestWho.replaceChildren(h("span.faint", { text: t("race.best_lap_none") }));
      this.bestSig = null;
      return;
    }
    const sig = `${best.slot}:${best.ms}`;
    if (this.bestSig === sig) return;
    this.bestSig = sig;
    const car = state.cars.find((c) => c.slot === best.slot);
    setText(this.bestValue, lapTime(best.ms));
    this.bestWho.replaceChildren(
      h("span.swatch", { style: { "--car": car?.color } }),
      h("span", { text: car?.name || `#${best.slot + 1}` }),
      h("span.faint", { text: t("race.best_lap_on", { n: best.lap }) }));
  }

  // ------------------------------------------------------------ map (simulator)

  updateMap(state) {
    const geo = store.meta?.track;
    const show = !!geo?.points?.length;
    // a connector without a track (light gates...) still gets its chip
    fillConnectors(this.mapConnectors, state.connectors);
    toggle(this.mapBox, "hidden", !show && !state.connectors?.length);
    toggle(this.mapSource, "hidden", !show);
    toggle(this.mapBox.querySelector(".trackmap"), "hidden", !show);
    if (!show) return;
    const pos = state.positions;
    const label = geo.source === "sim" ? t("track.virtual_cu") : geo.name || geo.source || t("track.connector");
    setText(this.mapSource, pos?.stale ? t("track.stale") : label);
    this.mapSource.title = t("track.source", { label });
    this.mapSource.className = `chip map-source${pos?.stale ? " caution" : ""}`;
    if (!this.track) this.track = new TrackMap(this.mapBox.querySelector(".trackmap"), geo);
    this.track.update(pos?.cars || [], state.cars, !!pos?.stale);
  }

  // ------------------------------------------------------------ race feed

  onEvent(name, data) {
    const SESSION_FEED = {
      grid: () => t("feed.session_ready", { title: data.title || this.state?.session.title || "" }),
      running: (prev) => (prev === "starting" ? t("feed.start") : prev === "paused" ? t("feed.resume") : null),
      paused: () => t("feed.paused"),
      finishing: () => t("feed.finishing"),
      finished: () => t("feed.finished"),
    };
    if (name === "session") {
      const text = SESSION_FEED[data.state]?.(data.prev);
      if (text) this.addFeed(text, data.state === "paused" ? "warn" : "info");
    } else if (name === "log") {
      this.addFeed(logText(data), data.level, data.t);
    } else if (name === "lap" && data.session_best && !data.neutralized) {
      const car = this.state?.cars.find((c) => c.slot === data.slot);
      this.addFeed(t("feed.best_lap", { name: car?.name || "#" + (data.slot + 1), time: lapTime(data.ms) }), "sb");
    } else if (name === "pit_in") {
      const car = this.state?.cars.find((c) => c.slot === data.slot);
      this.addFeed(t("feed.pit_in", { name: car?.name || "#" + (data.slot + 1) }), "info");
    } else if (name === "fuel_low") {
      const car = this.state?.cars.find((c) => c.slot === data.slot);
      this.addFeed(t("feed.fuel_low", { name: car?.name || "#" + (data.slot + 1) }), "warn");
    } else if (name === "short_circuit") {
      this.addFeed(t("feed.short_circuit"), "error");
    }
  }

  addFeed(text, level = "info", epoch) {
    this.feed.querySelector(".none")?.remove();
    const when = wallTime(epoch ?? Date.now() / 1000);
    const cls = { warn: "warn", error: "bad", sb: "sb" }[level] || "";
    const li = h("li", {}, h("span.t", { text: when }), h(`span${cls ? "." + cls : ""}`, { text }));
    this.feed.prepend(li);
    while (this.feed.childElementCount > 40) this.feed.lastChild.remove();
  }

  // ------------------------------------------------------------ bots

  updateBots(state) {
    const link = state.link;
    const advanced = state.mode === "advanced" && link.transport !== "none";
    const idle = ["idle", "grid", "finished"].includes(state.session?.state);
    // invitation: a CU is connected, bots are off, and no session is running
    const invite = !advanced && idle && link.state === "connected" && link.bots_supported != null;
    toggle(this.bots, "hidden", !advanced && !invite);
    toggle(this.invite, "hidden", advanced);
    toggle(this.botsHead, "hidden", !advanced);
    toggle(this.botsGrid, "hidden", !advanced);
    if (!advanced) {
      if (invite) this.updateInvite(state);
      return;
    }
    this.botsOff.disabled = !idle;
    this.botsOff.title = idle ? t("bots.off_title") : t("mode.busy");
    if (!this.botCards.length) {
      for (const bot of state.bots) {
        const card = new BotCard(bot.slot);
        this.botCards.push(card);
        this.botsGrid.append(card.el);
      }
    }
    let note = "";
    const usable = link.bots_supported !== false;
    if (!usable) note = t("bots.unavailable", { why: botsUnavailable(state) || "" });
    else if (link.state !== "connected") note = t("bots.waiting_cu");
    else if (state.estop) note = t("bots.estop_note");
    else note = t("bots.note");
    setText(this.botsNote, note);
    for (const card of this.botCards) {
      const bot = state.bots[card.slot];
      const car = state.cars.find((c) => c.slot === card.slot) || store.meta?.drivers?.[card.slot];
      card.update(bot, car, state, usable);
    }
  }

  updateInvite(state) {
    const old = state.link.bots_supported === false;
    setText(this.inviteText, old ? t("bots.invite.old", { version: state.link.version ?? "?" }) : t("bots.invite.text"));
    toggle(this.invite, "muted", old);
    toggle(this.inviteBtn, "hidden", old);
  }
}

function setMode(mode) {
  run("mode.set", { mode }, mode === "advanced" ? t("mode.set_advanced") : t("mode.set_classic")).catch(() => {});
}

// ------------------------------------------------------------ bot card

// Switching a car on takes it in "manual" (throttle 0: nothing moves until the slider is pushed);
// Auto is one tap away
const DEFAULT_DRIVER = "manual";
const DRIVER_OPTS = [
  { value: "profile", label: t("bot.driver.profile"), title: t("bot.driver.profile_title") },
  { value: "manual", label: t("bot.driver.manual"), title: t("bot.driver.manual_title") },
  { value: "api", label: t("bot.driver.api"), title: t("bot.driver.api_title") },
];

const HOVER = matchMedia("(hover: hover)"); // no keyboard hint on a touch screen

class BotCard {
  constructor(slot) {
    this.slot = slot;
    this.inflight = false;
    this.queued = null;
    this.choice = DEFAULT_DRIVER;
    this.carno = h("span.carno");
    this.name = h("span.name");
    this.toggle = Toggle(false, (on) => (on ? this.setDriver(this.choice) : this.setDriver(null)));
    this.toggle.el.title = t("bot.toggle_title");
    this.seg = Segmented(DRIVER_OPTS, this.choice, (v) => this.setDriver(v));
    this.fader = Fader({ onChange: (v) => this.sendThrottle(v), limit: store.meta?.safety?.confirm_throttle_above ?? 12 });
    this.gasValue = document.createTextNode("–");
    this.gasLabel = h("small");
    this.gas = h("div.gas", {}, this.gasValue, this.gasLabel);
    this.note = h("div.bot-note");
    this.padChip = h("span.pad-chip", { hidden: true });
    this.lane = h("button.btn.sm", { title: t("bot.lane_title"), onclick: () => run("bot.lane", { slot }).catch(() => {}) }, t("bot.lane"));
    this.unlock = h("button.btn.sm.unlock", { title: t("bot.unlock_title"), onclick: () => this.toggleUnlock() }, t("bot.unlock_off"));
    this.gear = h("button.btn.sm", { title: t("bot.settings_title"), onclick: () => this.openProfile() }, t("bot.settings"));
    this.spark = Spark();
    this.el = h("div.bot", { role: "group" },
      h("div.bot-head", {}, this.carno, this.name, this.padChip, this.toggle.el),
      this.seg.el,
      h("div.bot-body", {}, h("div.fader-col", {}, this.fader.el, this.fader.zero), h("div.bot-readout", {}, this.gas, this.note)),
      h("div.bot-actions", {}, this.lane, this.unlock, this.gear),
      this.spark.el);
  }

  update(bot, car, state, usable) {
    this.bot = bot;
    const color = car?.color || "#888";
    this.el.style.setProperty("--car", color);
    this.carno.style.color = inkOn(color);
    setText(this.carno, this.slot + 1);
    setText(this.name, car?.name || t("bot.slot", { n: this.slot + 1 }));
    const mapped = hub.playerFor(this.slot);
    this.padChip.hidden = !mapped;
    if (mapped) setText(this.padChip, mapped.type === "gamepad" ? "\ud83c\udfae" : "\u2328\ufe0f");
    const who = this.name.textContent;
    this.el.setAttribute("aria-label", who);
    this.fader.el.setAttribute("aria-label", t("bot.fader_aria", { name: who }));
    this.fader.zero.setAttribute("aria-label", t("bot.zero_aria", { name: who }));
    this.toggle.input.setAttribute("aria-label", t("bot.toggle_aria", { name: who }));
    this.seg.el.setAttribute("aria-label", t("bot.driver_aria", { name: who }));
    const connected = state.link.state === "connected";
    const driver = bot.driver;
    this.choice = driver || DEFAULT_DRIVER; // a released car comes back in manual, whatever it ran before
    this.seg.set(this.choice);
    toggle(this.seg.el, "dim-off", !driver);
    this.seg.buttons.forEach((b) => (b.disabled = !usable || !connected || (!driver && car?.enabled === false)));
    const enabled = car?.enabled !== false;
    this.toggle.input.checked = !!driver;
    this.toggle.input.disabled = !usable || (!driver && (!connected || !enabled));
    toggle(this.el, "idle", !driver);

    const shown = driver ? bot.throttle ?? 0 : 0;
    this.gasValue.nodeValue = driver ? String(shown) : "–";
    setText(this.gasLabel, driver ? t("bot.gas") : "");
    if (driver === "manual") {
      this.fader.setMode(state.estop ? "disabled" : "edit");
      this.fader.set(bot.manual);
    } else {
      this.fader.setMode(driver ? "readonly" : "disabled");
      this.fader.set(shown);
    }
    toggle(this.unlock, "hidden", driver !== "manual");
    toggle(this.unlock, "on", this.fader.unlocked);
    this.unlock.setAttribute("aria-pressed", this.fader.unlocked ? "true" : "false");
    setText(this.unlock, this.fader.unlocked ? t("bot.unlock_on") : t("bot.unlock_off"));
    toggle(this.gear, "hidden", driver !== "profile");
    this.lane.disabled = !driver || !connected;
    // the accessible name keeps the visible text and adds the car
    for (const b of [this.lane, this.unlock, this.gear]) b.setAttribute("aria-label", `${b.textContent}, ${who}`);

    let note;
    if (!driver && !enabled) note = [h("span.faint", { text: t("bot.disabled") })];
    else if (!driver) note = [h("span.faint", { text: t("bot.controller_in_charge") })];
    else if (bot.gate) note = [h("span.chip.caution", { text: GATE_LABEL[bot.gate] || bot.gate })];
    else if (bot.fuel_empty) note = [h("span.chip.stop", { text: t("bot.tank_empty") }), h("div.faint", { text: t("bot.tank_hint"), style: { marginTop: "4px" } })];
    else if (driver === "profile") {
      note = [h("span", { text: profileText(bot.profile) })];
      // corners profile: is it driving from the map, or holding the lap pace because positions are missing?
      if (bot.profile?.kind === "corners" && bot.drive) {
        note.push(h(`div.chip.${bot.drive === "map" ? "info" : "caution"}`, { text: t(`bot.drive.${bot.drive === "map" ? "map" : "pace"}`), style: { marginTop: "4px" } }));
      }
      // race brain: what it is doing, and why it cannot do more (i18n key + args from the server)
      if (bot.profile?.kind === "brain" && bot.brain) {
        note.push(h(`div.chip.${bot.brain.intent_args?.why ? "caution" : "info"}`, { text: brainIntent(bot.brain, state), style: { marginTop: "4px" } }));
      }
    }
    else if (driver === "api") note = [h("span", { text: bot.agent ? t("bot.agent", { name: bot.agent }) : t("bot.agent_waiting") })];
    else note = [h("span.faint", { text: bot.requested !== bot.throttle ? t("bot.requested", { n: bot.requested }) : t(HOVER.matches ? "bot.keyboard_hint" : "bot.slide_hint") })];
    if (bot.lane_pending) note.push(h("div.chip.info", { text: t("bot.lane_requested"), style: { marginTop: "4px" } }));
    const sig = note.map((n) => n.outerHTML).join("");
    if (sig !== this.noteSig) { this.noteSig = sig; this.note.replaceChildren(...note); }
    this.spark.push(driver ? bot.throttle : 0);
  }

  async setDriver(value) {
    try {
      if (!value) await run("bot.release", { slot: this.slot });
      else {
        this.choice = value;
        await run("bot.take", { slot: this.slot, driver: value });
      }
      if (value !== "manual") this.fader.setUnlocked(false);
    } catch { /* toast already shown */ }
  }

  sendThrottle(v) {
    if (this.inflight) { this.queued = v; return; }
    this.inflight = true;
    request("bot.throttle", { slot: this.slot, value: v })
      .catch((e) => toast(e.message, "error"))
      .finally(() => {
        this.inflight = false;
        if (this.queued != null) { const q = this.queued; this.queued = null; this.sendThrottle(q); }
      });
  }

  async toggleUnlock() {
    const on = !this.fader.unlocked;
    if (on && !(await confirmModal({
      title: t("bot.unlock_confirm.title"),
      text: t("bot.unlock_confirm.text"),
      confirmLabel: t("bot.unlock_confirm.ok"), danger: true,
    }))) return;
    this.fader.setUnlocked(on);
    if (!on && this.fader.value > (store.meta?.safety?.confirm_throttle_above ?? 12)) {
      this.sendThrottle(store.meta?.safety?.confirm_throttle_above ?? 12);
    }
    toggle(this.unlock, "on", on);
    setText(this.unlock, on ? t("bot.unlock_on") : t("bot.unlock_off"));
  }

  openProfile() {
    openProfileDrawer(this.slot, this.bot?.profile);
  }
}

function profileText(p) {
  if (!p) return "";
  const label = profileKindLabel(p.kind);
  if (p.kind === "constant") return `${label} · ${p.value}`;
  if (p.kind === "target") return `${label} · ${lapTime(p.target_ms)}`;
  if (p.kind === "rival") return `${label} · #${(p.rival_slot ?? 0) + 1} ${p.margin_pct >= 0 ? "+" : ""}${t("fmt.pct", { v: p.margin_pct })}`;
  if (p.kind === "corners") return `${label} · ${p.a_lat} m/s² · ${t("bot.cap", { n: p.cap })}`;
  if (p.kind === "brain") return [label, pilotLabel(p.pilot), t(`brain.lane.${p.lane?.policy}`), t(`brain.pit.${p.pit?.policy}`)].join(" · ");
  return label;
}

// the brain's pilots come from meta.pilots (a registry on the server): never a hard-coded list here
function pilotLabel(name) {
  const p = (store.meta?.pilots || []).find((x) => x.name === name);
  return p ? tOr(p.label_key, p.label) : name;
}

function brainIntent(b, state) {
  const args = { ...(b.intent_args || {}) };
  if (args.slot != null) args.name = state.cars.find((c) => c.slot === args.slot)?.name || t("bot.slot", { n: args.slot + 1 });
  const text = t(b.intent_key, args);
  return args.why ? `${text} · ${t(args.why)}` : text;
}

// ------------------------------------------------------------ profile settings

function openProfileDrawer(slot, current) {
  const profiles = store.meta?.profiles || {};
  let kind = current?.kind || "constant";
  const values = {};
  // deep copies: the editors change nested values (map points, brain sections), never meta's defaults
  for (const [k, p] of Object.entries(profiles)) values[k] = structuredClone({ ...p.defaults, ...(current?.kind === k ? current : {}) });
  const form = h("div", { style: { display: "flex", flexDirection: "column", gap: "16px" } });
  const seg = Segmented(Object.entries(profiles).map(([k, p]) => ({ value: k, label: profileKindLabel(k) })), kind, (k) => {
    kind = k;
    seg.set(k);
    renderForm();
  });
  seg.el.classList.add("full", "wrap");
  seg.el.setAttribute("aria-label", t("profile.drawer", { n: slot + 1 }));

  const num = (label, key, opts) => {
    const v = values[kind];
    const st = Stepper({ value: v[key], ...opts, label, onChange: (x) => (v[key] = x) });
    return h("label.field", {}, h("span", { text: label }), st.el);
  };
  const seconds = (label, key) => {
    const v = values[kind];
    const input = h("input.input.num", { value: (v[key] / 1000).toFixed(3), inputmode: "decimal" });
    input.addEventListener("change", () => {
      const x = parseFloat(input.value.replace(",", "."));
      if (x > 0) v[key] = Math.round(x * 1000);
      input.value = (v[key] / 1000).toFixed(3);
    });
    return h("label.field", {}, h("span", { text: label }), input);
  };

  // decimal value (m/s²...), between min and max
  const decimal = (label, key, min, max) => {
    const v = values[kind];
    const input = h("input.input.num", { value: v[key], inputmode: "decimal" });
    input.addEventListener("change", () => {
      const x = parseFloat(input.value.replace(",", "."));
      if (Number.isFinite(x)) v[key] = Math.max(min, Math.min(max, x));
      input.value = v[key];
    });
    return h("label.field", {}, h("span", { text: label }), input);
  };

  function renderForm() {
    const v = values[kind];
    const fields = [];
    if (kind === "constant") fields.push(num(t("profile.throttle"), "value", { min: 0, max: 15 }));
    if (kind === "target") fields.push(seconds(t("profile.target_time"), "target_ms"));
    if (kind === "corners") {
      fields.push(h("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: "10px" } },
        decimal(t("profile.a_lat"), "a_lat", 1, 40),
        num(t("profile.corner_margin"), "margin_pct", { min: 0, max: 50 }),
        num(t("profile.cap"), "cap", { min: 0, max: 15 })));
      fields.push(seconds(t("profile.fallback_time"), "target_ms"));
    }
    if (kind === "rival") {
      const drivers = (store.meta?.drivers || []).filter((d) => d.slot < 6 && d.slot !== slot);
      const sel = h("select.input", {}, drivers.map((d) => h("option", { value: d.slot, text: `#${d.slot + 1} ${d.name}` })));
      sel.value = v.rival_slot;
      sel.addEventListener("change", () => (v.rival_slot = parseInt(sel.value, 10)));
      fields.push(h("label.field", {}, h("span", { text: t("profile.rival_follow") }), sel));
      fields.push(num(t("profile.margin"), "margin_pct", { min: -20, max: 30 }));
    }
    if (kind === "target" || kind === "rival") {
      fields.push(h("div", { style: { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "10px" } },
        num(t("profile.start"), "start", { min: 0, max: 15 }), num(t("profile.min"), "min_value", { min: 0, max: 15 }), num(t("profile.max"), "max_value", { min: 0, max: 15 })));
    }
    if (kind === "map") fields.push(mapEditor(v));
    if (kind === "brain") fields.push(brainEditor(v, slot, current));
    else {
      fields.push(h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px" } },
        num(t("profile.lane_every"), "lane_every", { min: 0, max: 50 }),
        num(t("profile.ramp"), "ramp_s", { min: 0, max: 5 })));
    }
    const desc = {
      constant: t("profile.desc.constant"),
      target: t("profile.desc.target"),
      map: t("profile.desc.map"),
      rival: t("profile.desc.rival"),
      corners: t("profile.desc.corners"),
      brain: t("profile.desc.brain"),
    }[kind];
    form.replaceChildren(seg.el, h("p.dim", { text: desc, style: { margin: 0 } }), ...fields);
  }
  renderForm();

  openDrawer(t("profile.drawer", { n: slot + 1 }), form, [
    { label: t("common.cancel") },
    { label: t("common.save"), primary: true, run: () => run("bot.profile", { slot, profile: { ...values[kind], kind } }, t("profile.saved")) },
  ]);
}

const LANE_POLICIES = ["keep", "fastest", "overtake", "alternate"];
const PIT_POLICIES = ["off", "auto", "now"];
const GRID = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: "10px" };

// Race brain: pilot (from meta.pilots), lane policy, pit policy and their few parameters, plus the
// two one-shot test requests (they act on the running brain at once, timed by it)
function brainEditor(v, slot, current) {
  const pilots = store.meta?.pilots || [];
  const box = h("div", { style: { display: "flex", flexDirection: "column", gap: "14px" } });
  const field = (label, control) => h("label.field", {}, h("span", { text: label }), control);
  const select = (options, value, onChange) => {
    const sel = h("select.input", {}, options.map((o) => h("option", { value: o.value, text: o.label, disabled: !!o.disabled, title: o.title || "" })));
    sel.value = value;
    sel.addEventListener("change", () => onChange(sel.value));
    return sel;
  };
  // a number of obj[key], shown × scale (ms shown in s), clamped to [min, max]
  const number = (obj, spec, label) => {
    const scale = spec.scale ?? 1;
    const show = (x) => String(Math.round(x * scale * 1000) / 1000);
    const input = h("input.input.num", { value: show(obj[spec.key]), inputmode: "decimal" });
    input.addEventListener("change", () => {
      const x = parseFloat(input.value.replace(",", "."));
      if (Number.isFinite(x)) {
        const y = Math.max(spec.min, Math.min(spec.max, x)) / scale;
        obj[spec.key] = spec.whole ? Math.round(y) : y;
      }
      input.value = show(obj[spec.key]);
    });
    return field(label, input);
  };
  // pit zone override, in % of the lap from the line; both empty = the map's
  const zone = () => {
    const at = (i) => {
      const input = h("input.input.num", { value: v.pit.zone ? String(Math.round(v.pit.zone[i] * 1000) / 10) : "", inputmode: "decimal", placeholder: "–" });
      input.addEventListener("change", () => {
        const x = parseFloat(input.value.replace(",", "."));
        const z = v.pit.zone ? [...v.pit.zone] : [null, null];
        z[i] = Number.isFinite(x) ? Math.max(0, Math.min(99.9, x)) / 100 : null;
        v.pit.zone = z[0] != null && z[1] != null ? z : (z[0] == null && z[1] == null ? null : z);
      });
      return field(t(i ? "brain.param.zone_out" : "brain.param.zone_in"), input);
    };
    return h("div", {}, h("div.faint", { text: t("brain.param.zone"), style: { marginBottom: "6px" } }), h("div", { style: GRID }, at(0), at(1)));
  };
  const render = () => {
    const pilot = pilots.find((p) => p.name === v.pilot);
    const pilotSel = select(pilots.map((p) => ({
      value: p.name, disabled: !p.available, title: p.reason || "",
      label: p.available ? tOr(p.label_key, p.label) : t("brain.pilot_unavailable", { name: tOr(p.label_key, p.label) }),
    })), v.pilot, (name) => {
      v.pilot = name;
      v.pilot_params = { ...(pilots.find((p) => p.name === name)?.defaults || {}) };
      render();
    });
    const lane = [], pit = [];
    if (v.lane.policy === "alternate") lane.push(number(v.lane, { key: "every", min: 1, max: 50, whole: true }, t("brain.param.every")));
    if (v.lane.policy === "overtake") lane.push(number(v.lane, { key: "gap_m", min: 0.1, max: 5 }, t("brain.param.gap_m")));
    if (v.lane.policy !== "keep") {
      lane.push(number(v.lane, { key: "d_max_m", min: 0.5, max: 10 }, t("brain.param.d_max_m")),
        number(v.lane, { key: "latency_s", min: 0, max: 1 }, t("brain.param.latency_s")));
    }
    if (v.pit.policy === "auto") pit.push(number(v.pit, { key: "reserve_laps", min: 0, max: 10 }, t("brain.param.reserve_laps")));
    if (v.pit.policy !== "off") pit.push(number(v.pit, { key: "throttle", min: 1, max: 15, whole: true }, t("brain.param.throttle")));
    const live = current?.kind === "brain";
    const ask = (request, done) => run("bot.brain", { slot, request }, t(done)).catch(() => {});
    box.replaceChildren(...[
      field(t("brain.pilot"), pilotSel),
      h("div", { style: GRID }, ...(pilot?.params || []).map((spec) => number(v.pilot_params, spec, tOr(spec.label_key, spec.key)))),
      field(t("brain.lanes"), select(LANE_POLICIES.map((x) => ({ value: x, label: t(`brain.lane.${x}`) })), v.lane.policy, (x) => { v.lane.policy = x; render(); })),
      lane.length ? h("div", { style: GRID }, ...lane) : null,
      field(t("brain.pits"), select(PIT_POLICIES.map((x) => ({ value: x, label: t(`brain.pit.${x}`) })), v.pit.policy, (x) => { v.pit.policy = x; render(); })),
      pit.length ? h("div", { style: GRID }, ...pit) : null,
      v.pit.policy !== "off" ? zone() : null,
      h("div.field", {}, h("span", { text: t("brain.tests") }),
        h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
          h("button.btn.sm", { disabled: !live, onclick: () => ask("pit", "brain.pit_now_sent") }, t("brain.pit_now")),
          h("button.btn.sm", { disabled: !live, onclick: () => ask("lane", "brain.lane_now_sent") }, t("brain.lane_now"))),
        h("span.faint", { text: t(live ? "brain.tests_hint" : "brain.tests_save_first") })),
    ].filter(Boolean));
  };
  render();
  return box;
}

function mapEditor(v) {
  const list = h("div", { style: { display: "flex", flexDirection: "column", gap: "6px" } });
  const render = () => {
    list.replaceChildren(...v.points.map((p, i) => {
      const at = h("input.input.num", { value: Math.round(p[0] * 100), style: { width: "70px" } });
      at.addEventListener("change", () => { p[0] = Math.max(0, Math.min(99, parseInt(at.value, 10) || 0)) / 100; });
      const gas = h("input.input.num", { value: p[1], style: { width: "70px" } });
      gas.addEventListener("change", () => { p[1] = Math.max(0, Math.min(15, parseInt(gas.value, 10) || 0)); });
      return h("div", { style: { display: "flex", gap: "8px", alignItems: "center" } },
        h("span.faint", { text: t("profile.map.at"), style: { width: "14px" } }), at, h("span.dim", { text: t("profile.map.unit") }), gas,
        h("button.btn.ghost.sm", { onclick: () => { v.points.splice(i, 1); render(); } }, t("common.remove")));
    }));
  };
  render();
  return h("div.field", {}, h("span", { text: t("profile.map.title") }), list,
    h("button.btn.sm", { style: { alignSelf: "flex-start" }, onclick: () => { v.points.push([0.5, 8]); render(); } }, t("profile.map.add")));
}

// ------------------------------------------------------------ drawn track

class TrackMap {
  constructor(box, geo) {
    this.geo = geo;
    const pts = geo.points.map(([x, y]) => [x * 100, -y * 100]); // y axis pointing up
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const pad = 6;
    const minx = Math.min(...xs) - pad, miny = Math.min(...ys) - pad;
    const w = Math.max(...xs) - minx + pad, hgt = Math.max(...ys) - miny + pad;
    this.pts = pts;
    this.cum = [0];
    for (let i = 1; i <= pts.length; i++) {
      const a = pts[i - 1], b = pts[i % pts.length];
      this.cum.push(this.cum[i - 1] + Math.hypot(b[0] - a[0], b[1] - a[1]));
    }
    this.total = this.cum[this.cum.length - 1];
    this.off = (geo.lane_offset ?? 0.012) * 100;
    const d = `M${pts.map((p) => p.join(",")).join("L")}Z`;
    const lane = (k) => `M${pts.map((_, i) => this.at(this.cum[i] / this.total, k).join(",")).join("L")}Z`;
    this.cars = s("g");
    const svg = s("svg", { viewBox: `${minx} ${miny} ${w} ${hgt}` },
      s("path", { class: "surface", d, "stroke-width": this.off * 4.2 }),
      geo.pit ? s("path", { class: "pitzone", d: this.segment(geo.pit[0], geo.pit[1], 2.6) }) : null,
      s("path", { class: "lane", d: lane(-1) }),
      s("path", { class: "lane", d: lane(1) }),
      this.finishLine(geo.finish ?? 0),
      this.cars);
    box.replaceChildren(svg);
    this.dots = new Map();
  }

  // point at lap fraction f, offset by k lanes
  at(f, k = 0) {
    const target = ((f % 1) + 1) % 1 * this.total;
    let i = 1;
    while (i < this.cum.length - 1 && this.cum[i] < target) i++;
    const a = this.pts[(i - 1) % this.pts.length], b = this.pts[i % this.pts.length];
    const seg = this.cum[i] - this.cum[i - 1] || 1;
    const u = (target - this.cum[i - 1]) / seg;
    const x = a[0] + (b[0] - a[0]) * u, y = a[1] + (b[1] - a[1]) * u;
    const nx = -(b[1] - a[1]) / seg, ny = (b[0] - a[0]) / seg;
    return [x + nx * this.off * k, y + ny * this.off * k];
  }

  segment(f0, f1, k = 0) {
    const n = 40, out = [];
    const span = ((f1 - f0) % 1 + 1) % 1;
    for (let i = 0; i <= n; i++) out.push(this.at(f0 + (span * i) / n, k).join(","));
    return `M${out.join("L")}`;
  }

  finishLine(f) {
    const [x1, y1] = this.at(f, -2.2), [x2, y2] = this.at(f, 2.2);
    return s("line", { class: "finish", x1, y1, x2, y2 });
  }

  update(simCars, cars, stale = false) {
    toggle(this.cars, "stale", stale);
    const seen = new Set();
    for (const sc of simCars || []) {
      seen.add(sc.slot);
      let dot = this.dots.get(sc.slot);
      const car = cars.find((c) => c.slot === sc.slot);
      if (!dot) {
        const circle = s("circle", { r: 2.3 });
        const label = s("text", {});
        label.textContent = sc.slot + 1;
        dot = s("g", { class: "car" }, circle, label);
        this.cars.append(dot);
        this.dots.set(sc.slot, dot);
      }
      dot.firstChild.setAttribute("fill", car?.color || "#888");
      dot.lastChild.setAttribute("fill", inkOn(car?.color || "#888"));
      // s = lap fraction since the line; the drawn path starts at its own first point
      const [x, y] = this.at((this.geo.finish ?? 0) + sc.s, sc.lane === 0 ? -1 : 1);
      dot.setAttribute("transform", `translate(${x.toFixed(2)} ${y.toFixed(2)})`);
      toggle(dot, "off", sc.off);
    }
    for (const [slot, dot] of this.dots) {
      if (!seen.has(slot)) { dot.remove(); this.dots.delete(slot); }
    }
  }
}
