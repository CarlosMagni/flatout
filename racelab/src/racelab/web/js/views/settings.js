// Settings screen: the car setup through the CU keys (moved from System) and
// the external controllers (keyboard / gamepad) that drive a car on the
// manual path, exactly like the fader.
import { IDLE_STATES, h, setText } from "../dom.js";
import { t } from "../i18n.js";
import { botsUnavailable, readJson, store, writeJson } from "../store.js";
import { Segmented, Toggle, panel, run, toast } from "../ui/components.js";
import { firstPressedButton, hub, keyLabel, padList, readControllers, writeControllers } from "../input.js";

const LIVE_KEY = "racelab.liveprogram";
const LIVE_FIELDS = [
  { id: "speed", def: 10 },  // factory levels (manual): speed 10, brake 10, tank 7
  { id: "brake", def: 10 },
  { id: "fuel", def: 7 },
];

// a range input's filled part (CSS --fill), kept in step with its value
function paintFill(input) {
  const { min, max, value } = input;
  input.style.setProperty("--fill", `${((value - min) / (max - min)) * 100}%`);
}

// A level (1..10) as the CU's five lights show it while SPEED/BRAKE/FUEL is open (V339 state 7):
// the lights before steady, then the level's own light, steady for an even level, blinking for odd.
function LevelLights() {
  const leds = Array.from({ length: 5 }, () => h("i.light"));
  const el = h("div.lights.level-lights", { role: "img" }, leds);
  return {
    el,
    set(level) {
      const k = Math.ceil(level / 2) - 1;
      leds.forEach((led, i) => {
        led.classList.toggle("on", i <= k);
        led.classList.toggle("flash", i === k && level % 2 === 1);
      });
      el.setAttribute("aria-label", t("live.recal_leds", { level }));
    },
  };
}

export class SettingsView {
  constructor(root) {
    this.root = root;
    this.sig = null;
    this.liveDirty = false;
    this.list = readControllers();
    this._meter = null;
    this._capture = null;
    this._padsSig = "";
    this._laneSeen = [0, 0];
    this._estopSeen = [0, 0];
    const carPanel = this.buildCarSetup();
    const ctlPanel = this.buildControllers();
    root.append(h("div.screen.sys", {},
      h("h1.page", { text: t("nav.settings") }),
      h("div.sys-cols", {},
        h("div.sys-col", {}, carPanel),
        h("div.sys-col", {}, ctlPanel))));
    this.ctlPlayers.forEach((_, i) => this.refreshPlayer(i));
    this._meter = setInterval(() => this.paintMeters(), 80);
  }

  destroy() {
    clearInterval(this._meter);
    this._capture = null;
  }

  // ------------------------------------------------------------ car setup (CU keys)

  buildCarSetup() {
    const mem = readJson(LIVE_KEY, {});
    this.liveSliders = {};
    this.liveRecal = {};
    this.liveDirty = false;
    const rows = LIVE_FIELDS.map((f) => {
      const start = String(mem[f.id] ?? f.def);
      const value = h("output.live-val", { text: start });
      const slider = h("input.live-slider", {
        type: "range", min: "1", max: "10", step: "1", value: start,
        "aria-label": t(`garage.${f.id}`),
      });
      slider.addEventListener("input", () => { value.textContent = slider.value; paintFill(slider); sync(); });
      paintFill(slider);
      // re-sync: move the slider until these lights look like the CU's, then confirm
      const leds = LevelLights();
      const sel = h("input.recal-slider", { type: "range", min: "1", max: "10", step: "1", value: start,
        "aria-label": t("live.recal_tip") });
      const selVal = h("output.live-val", { text: start });
      sel.addEventListener("input", () => { leds.set(Number(sel.value)); selVal.textContent = sel.value; paintFill(sel); });
      leds.set(Number(start));
      const ok = h("button.recal-ok", { type: "button", title: t("live.recal_ok"), onclick: () => this.recalibrate(f.id, true) }, "\u2713");
      const no = h("button.recal-no", { type: "button", title: t("live.recal_cancel"), onclick: () => this.recalibrate(f.id, false) }, "\u2715");
      const edit = h("div.recal-edit", { hidden: true },
        h("span.recal-hint", { text: t("live.recal_match") }), leds.el, sel, selVal, ok, no);
      const recal = h("button.recal-btn", { type: "button", title: t("live.recal_tip"), "aria-label": t("live.recal_tip"), onclick: () => this.recalibrate(f.id) }, "\u21bb " + t("live.recal"));
      this.liveRecal[f.id] = { sel, edit, recal, leds, selVal };
      this.liveSliders[f.id] = slider;
      return h("div.live-field", {},
        h("div.live-row", {}, h("span.live-name", { text: t(`garage.${f.id}`) }), slider, value, recal), edit);
    });
    this.liveGo = h("button.btn.primary", { type: "button", disabled: true, onclick: () => this.liveProgram() }, t("live.go"));
    this.liveHint = h("p.live-hint");
    const sync = () => {
      const mem2 = readJson(LIVE_KEY, {});
      const dirty = LIVE_FIELDS.filter((f) => Number(this.liveSliders[f.id].value) !== (mem2[f.id] ?? f.def));
      this.liveDirty = dirty.length > 0;
      this.liveGo.disabled = !this.liveDirty;
      setText(this.liveHint, dirty.length
        ? dirty.map((f) => {
            const from = mem2[f.id] ?? f.def;
            const steps = (Number(this.liveSliders[f.id].value) - from + 10) % 10;
            return t("live.plan", { field: t(`garage.${f.id}`), from, to: this.liveSliders[f.id].value, n: steps });
          }).join("   \u00b7   ")
        : t("live.clean"));
    };
    this.liveSync = sync;
    sync();
    this.livePanel = panel(t("sys.live_program"), h("div.car-setup", {},
      ...rows,
      h("div.live-actions", {}, this.liveGo, this.liveHint),
      h("p.warn-line", { text: t("live.warn") }),
      h("p.note", { text: t("live.note") })));
    return this.livePanel;
  }

  async recalibrate(field, confirm) {
    const r = this.liveRecal[field];
    if (confirm === undefined) {
      r.edit.hidden = !r.edit.hidden;
      r.recal.classList.toggle("on", !r.edit.hidden);
      if (!r.edit.hidden) {
        r.sel.value = this.liveSliders[field].value;
        paintFill(r.sel);
        r.leds.set(Number(r.sel.value));
        r.selVal.textContent = r.sel.value;
        await run("cu.press", { button: field });  // the CU shows that level display
        toast(t("live.recal_open"));
      }
      return;
    }
    if (confirm) {
      const mem = readJson(LIVE_KEY, {});
      mem[field] = Number(r.sel.value);
      writeJson(LIVE_KEY, mem);
      toast(t("live.recal_done", { field: t(`garage.${field}`), level: r.sel.value }));
    }
    r.edit.hidden = true;
    r.recal.classList.remove("on");
    await run("cu.press", { button: "pace" });     // leave the display, nothing validated
    this.liveSync();
    this.sig = null;
    if (store.state) this.update(store.state);
  }

  async liveProgram() {
    const mem = readJson(LIVE_KEY, {});
    const dirty = LIVE_FIELDS.filter((f) => Number(this.liveSliders[f.id].value) !== (mem[f.id] ?? f.def));
    if (!dirty.length) return;
    this.liveGo.disabled = true;
    for (const f of dirty) {
      const current = mem[f.id] ?? f.def;
      const level = Number(this.liveSliders[f.id].value);
      try {
        await run("car.program_live", { field: f.id, level, current },
          t("live.done", { field: t(`garage.${f.id}`), from: current, to: level }));
        mem[f.id] = level;
        writeJson(LIVE_KEY, mem);
      } catch { break; /* toast already shown */ }
    }
    this.liveSync();
    this.sig = null;  // force the next update to re-evaluate the button state
    if (store.state) this.update(store.state);
  }

  // ------------------------------------------------------------ controllers

  buildControllers() {
    this.ctlNote = h("p.warn-line", { hidden: true });
    this.ctlPlayers = [0, 1].map((i) => this.buildPlayer(i));
    this.ctlPanel = panel(t("ctl.panel"), h("div.controllers", {},
      this.ctlNote,
      ...this.ctlPlayers.map((p) => p.el),
      h("p.note", { text: t("ctl.note") })));
    return this.ctlPanel;
  }

  buildPlayer(i) {
    const save = () => { writeControllers(this.list); hub.setPlayers(this.list); };
    const p = this.list[i];

    const enable = Toggle(!!p.enabled, (on) => { p.enabled = on; save(); this.refreshPlayer(i); }, t("ctl.player", { n: i + 1 }));
    const slotSel = h("select.ctl-sel", {}, ...Array.from({ length: 6 }, (_, n) => h("option", { value: String(n) }, String(n + 1))));
    slotSel.value = String(p.slot);
    slotSel.addEventListener("change", () => { p.slot = Number(slotSel.value); save(); });
    const typeSeg = Segmented([
      { value: "keyboard", label: t("ctl.keyboard") },
      { value: "gamepad", label: t("ctl.gamepad") },
    ], p.type, (v) => { p.type = v; save(); this.refreshPlayer(i); }, t("ctl.type"));

    // keyboard: capture the next pressed key
    const keyRow = (labelKey, prop) => {
      const btn = h("button.ctl-capture");
      const paint = () => setText(btn, keyLabel(p[prop]));
      paint();
      btn.addEventListener("click", () => this.captureKey(btn, paint, (code) => { p[prop] = code; save(); paint(); }));
      return h("div.ctl-row", {}, h("span.ctl-label", { text: t(labelKey) }), btn);
    };
    const kmSeg = Segmented([
      { value: "hold", label: t("ctl.km_hold") },
      { value: "updown", label: t("ctl.km_updown") },
    ], p.key_mode || "hold", (v) => { p.key_mode = v; save(); this.refreshPlayer(i); }, t("ctl.keymode"));
    const downRow = keyRow("ctl.down", "down");
    const kb = h("div.ctl-block", {},
      h("div.ctl-row", {}, h("span.ctl-label", { text: t("ctl.keymode") }), kmSeg.el),
      keyRow("ctl.up", "up"),
      downRow,
      keyRow("ctl.lane", "lane"),
      h("div.ctl-row", {}, h("span.ctl-label", { text: t("ctl.estop") }),
        h("span.ctl-val.ctl-fixed", { title: t("ctl.estop_fixed_tip") }, t("ctl.estop_fixed"))),
      h("p.note", { text: t("ctl.km_note") }));

    // gamepad: device, throttle axis (auto-detect captures the rest value), buttons
    const padSel = h("select.ctl-sel");
    padSel.addEventListener("change", () => { p.pad = padSel.value; save(); });
    const axisOut = h("span.ctl-val");
    const detectBtn = h("button.ctl-capture", {}, t("ctl.detect"));
    detectBtn.addEventListener("click", () => {
      if (hub.detectAxis(i)) setText(detectBtn, t("ctl.detect_wait"));
      else toast(t("ctl.no_pad"), "warn");
    });
    const invert = Toggle(!!p.invert, (on) => { p.invert = on; save(); }, t("ctl.invert"));
    const btnRow = (labelKey, prop) => {
      const btn = h("button.ctl-capture", { text: String(p[prop]) });
      const paint = () => setText(btn, String(p[prop]));
      btn.addEventListener("click", () => this.capturePadButton(btn, paint, (n) => { p[prop] = n; save(); paint(); }));
      return h("div.ctl-row", {}, h("span.ctl-label", { text: t(labelKey) }), btn);
    };
    const pad = h("div.ctl-block", {},
      h("div.ctl-row", {}, h("span.ctl-label", { text: t("ctl.pad") }), padSel),
      h("div.ctl-row", {}, h("span.ctl-label", { text: t("ctl.detect") }), detectBtn, axisOut),
      h("div.ctl-row", {}, h("span.ctl-label", { text: t("ctl.invert") }), invert.el),
      btnRow("ctl.lane", "laneButton"),
      btnRow("ctl.estop", "estopButton"));

    // shared sliders
    const slider = (labelKey, prop, min, max, step, fmt) => {
      const val = h("span.ctl-val", { text: fmt(p[prop]) });
      const input = h("input.ctl-range", { type: "range", min: String(min), max: String(max), step: String(step), value: String(p[prop]), "aria-label": t(labelKey) });
      input.addEventListener("input", () => { p[prop] = Number(input.value); setText(val, fmt(input.value)); save(); });
      return h("div.ctl-row", {}, h("span.ctl-label", { text: t(labelKey) }), input, val);
    };
    const dzRow = slider("ctl.deadzone", "deadzone", 0, 0.4, 0.01, (v) => `${Math.round(v * 100)}%`);
    const curveRow = slider("ctl.curve", "curve", 0.6, 2, 0.1, (v) => Number(v).toFixed(1));
    const rampRow = slider("ctl.ramp", "ramp_ms", 100, 1500, 50, (v) => `${v} ms`);

    // live test meter
    const meterFill = h("i");
    const meterVal = h("span.ctl-val");
    const laneDot = h("i.ctl-dot", { title: t("ctl.lane") });
    const estopDot = h("i.ctl-dot", { title: t("ctl.estop") });
    const meter = h("div.ctl-row.ctl-test", {},
      h("span.ctl-label", { text: t("ctl.test") }),
      h("div.ctl-bar", {}, meterFill), meterVal, laneDot, estopDot);

    const body = h("div.ctl-body", {}, kb, pad, dzRow, curveRow, rampRow, meter, h("p.note", { text: t("ctl.test_hint") }));
    const el = h("section.ctl-player", {},
      h("div.ctl-head", {}, enable.el, h("span.ctl-name", { text: t("ctl.player", { n: i + 1 }) }), h("span.ctl-label", { text: t("garage.car") }), slotSel, typeSeg.el),
      body);
    return { el, body, kb, pad, rampRow, dzRow, curveRow, meterFill, meterVal, laneDot, estopDot, detectBtn, axisOut, padSel, typeSeg, kmSeg, downRow };
  }

  refreshPlayer(i) {
    const ui = this.ctlPlayers[i];
    if (!ui) return;
    const p = this.list[i];
    ui.body.hidden = !p.enabled;
    ui.kb.hidden = p.type !== "keyboard";
    ui.pad.hidden = p.type !== "gamepad";
    ui.rampRow.hidden = p.type !== "keyboard";
    ui.typeSeg.set(p.type);
    ui.kmSeg.set(p.key_mode || "hold");
    ui.downRow.hidden = (p.key_mode || "hold") !== "updown";
    ui.dzRow.hidden = p.type !== "gamepad";       // dead zone and curve are
    ui.curveRow.hidden = p.type !== "gamepad";    // stick things, not key things
    setText(ui.axisOut, `${t("ctl.axis", { n: p.axis })}  (${Number(p.rest).toFixed(2)})`);
    this.paintPads(i);
  }

  paintPads(i) {
    const ui = this.ctlPlayers[i];
    if (!ui) return;
    const current = this.list[i].pad;
    const pads = padList();
    ui.padSel.replaceChildren(
      h("option", { value: "" }, t("ctl.any_pad")),
      ...pads.map((g) => h("option", { value: g.id }, g.id.slice(0, 28))));
    ui.padSel.value = current && pads.some((g) => g.id === current) ? current : "";
  }

  captureKey(btn, repaint, cb) {
    if (this._capture) this._capture();
    setText(btn, t("ctl.press_key"));
    const onKey = (ev) => { ev.preventDefault(); ev.stopPropagation(); cleanup(); cb(ev.code); };
    const cleanup = () => {
      removeEventListener("keydown", onKey, true);
      if (this._capture === cleanup) this._capture = null;
      repaint();
    };
    addEventListener("keydown", onKey, true);
    this._capture = cleanup;
    setTimeout(() => { if (this._capture === cleanup) cleanup(); }, 6000);
  }

  capturePadButton(btn, repaint, cb) {
    if (this._capture) this._capture();
    setText(btn, t("ctl.press_btn"));
    const started = performance.now();
    const done = (hit) => {
      clearInterval(iv);
      repaint();
      if (hit) cb(hit.button);
    };
    const iv = setInterval(() => {
      const hit = firstPressedButton();
      if (hit || performance.now() - started > 6000) done(hit);
    }, 60);
    this._capture = () => { clearInterval(iv); repaint(); };
  }

  paintMeters() {
    this.ctlPlayers.forEach((ui, i) => {
      const v = hub.live[i] ?? 0;
      setText(ui.meterVal, `${v}/15`);
      ui.meterFill.style.width = `${Math.round((v / 15) * 100)}%`;
      if ((hub.laneCount[i] || 0) !== this._laneSeen[i]) {
        this._laneSeen[i] = hub.laneCount[i];
        ui.laneDot.classList.add("on");
        setTimeout(() => ui.laneDot.classList.remove("on"), 250);
      }
      if ((hub.estopCount[i] || 0) !== this._estopSeen[i]) {
        this._estopSeen[i] = hub.estopCount[i];
        ui.estopDot.classList.add("on");
        setTimeout(() => ui.estopDot.classList.remove("on"), 800);
      }
    });
  }

  // ------------------------------------------------------------ state

  update(state) {
    const connected = state.link.state === "connected";
    const busy = !IDLE_STATES.has(state.session.state);
    const advanced = state.mode === "advanced";
    const why = botsUnavailable(state);   // reason bots cannot run here (null: they can)
    const padsSig = padList().map((g) => g.id).join("|");
    const sig = `${state.mode}|${connected}|${busy}|${this.liveDirty}|${padsSig}|${hub.detect?.player ?? ""}|${why ?? ""}`;
    if (sig === this.sig) return;
    this.sig = sig;

    this.liveGo.disabled = !connected || busy || !this.liveDirty;
    this.livePanel.classList.toggle("locked", busy);

    // the controllers drive through the software drivers: usable only with a
    // bot-capable CU, connected, in advanced mode
    const drivable = advanced && connected && !why;
    this.ctlNote.hidden = drivable;
    if (!drivable) {
      setText(this.ctlNote, why ? t("mode.advanced_unavailable", { why }) : (advanced ? t("ctl.offline") : t("ctl.why_hint")));
    }
    this.ctlPlayers.forEach((ui, i) => {
      ui.el.classList.toggle("locked", !drivable);
      if (padsSig !== this._padsSig) this.paintPads(i);
      if (ui.detectBtn) setText(ui.detectBtn, hub.detect?.player === i ? t("ctl.detect_wait") : t("ctl.detect"));
      setText(ui.axisOut, `${t("ctl.axis", { n: this.list[i].axis })}  (${Number(this.list[i].rest).toFixed(2)})`);
    });
    this._padsSig = padsSig;
  }
}
