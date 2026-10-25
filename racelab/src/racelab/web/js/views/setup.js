// Session screen: pick a type, set its parameters, put it on the grid, then
// manage the drivers (name, color, car).
import { STATE_LABEL, h, inkOn, setText, toggle } from "../dom.js";
import { t } from "../i18n.js";
import { refreshMeta, sessionDescription, sessionLabel, store } from "../store.js";
import { Segmented, Stepper, Toggle, run } from "../ui/components.js";

const IDLE_STATES = new Set(["idle", "grid", "finished"]);
const DURATION_REQUIRED = new Set(["qualifying", "endurance"]);
const DEFAULT_MINUTES = { practice: 10, qualifying: 5, endurance: 30 };
const SAVE_DELAY_MS = 400;
const NAME_MAX = 24;
const CAR_MAX = 40;
const SPECIAL_NAMES = { 6: "Ghost", 7: "Pace car" };

// Car colors on offer. The first six match the server's default colors; the
// purple and the green stay distinct from the "timing" purple / green of the UI.
const PALETTE = [
  { hex: "#e5383b", name: t("color.red") },
  { hex: "#f2994a", name: t("color.orange") },
  { hex: "#f2c94c", name: t("color.yellow") },
  { hex: "#27ae60", name: t("color.green") },
  { hex: "#2bb3c0", name: t("color.turquoise") },
  { hex: "#2f80ed", name: t("color.blue") },
  { hex: "#9b51e0", name: t("color.purple") },
  { hex: "#e86a9f", name: t("color.pink") },
  { hex: "#e0e0e0", name: t("color.white") },
  { hex: "#828282", name: t("color.gray") },
];

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const laps = (n) => t("unit.laps", { n });
const isAdvanced = () => (store.state?.mode ?? store.meta?.mode) === "advanced";

// mirror of the server's format_duration(): 90 -> "1 min 30 s"
function formatDuration(seconds) {
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return [hours && `${hours} h`, minutes && `${minutes} min`, secs && `${secs} s`].filter(Boolean).join(" ") || "0 s";
}

// mirror of SessionSettings.default_title() (the server builds the real title)
function defaultTitle(key, label, v, durationS) {
  let detail = "";
  if (key === "race" || key === "rival") detail = laps(v.laps);
  else if (durationS != null && (key === "practice" || key === "qualifying" || key === "endurance")) detail = formatDuration(durationS);
  else if (key === "elimination") detail = v.elim_every === 1 ? t("setup.title_detail.every_lap") : t("setup.title_detail.every_n_laps", { n: v.elim_every });
  else if (key === "training") detail = t("setup.title_detail.episodes", { n: v.episode_laps });
  return detail ? `${label} · ${detail}` : label;
}

// Radio button group: a single tab stop, arrow keys to change.
function radioGroup(container) {
  container.addEventListener("keydown", (ev) => {
    const current = ev.target.closest?.('[role="radio"]');
    if (!current) return;
    const items = [...container.querySelectorAll('[role="radio"]:not(:disabled)')];
    const i = items.indexOf(current);
    let next = null;
    if (ev.key === "ArrowRight" || ev.key === "ArrowDown") next = items[(i + 1) % items.length];
    else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") next = items[(i - 1 + items.length) % items.length];
    else if (ev.key === "Home") next = items[0];
    else if (ev.key === "End") next = items[items.length - 1];
    if (!next) return;
    ev.preventDefault();
    next.focus();
    next.click();
  });
}

function setRadios(container, selected) {
  for (const btn of container.querySelectorAll('[role="radio"]')) {
    const on = btn === selected;
    btn.setAttribute("aria-checked", on ? "true" : "false");
    btn.tabIndex = on ? 0 : -1;
  }
}

// labelled field for a composite control (a <label> cannot wrap a stepper:
// it would activate the first button)
let fieldSeq = 0;
function group(label, control, extra) {
  const id = `setup-f${++fieldSeq}`;
  const el = h("div.field", { role: "group", "aria-labelledby": id },
    h("span", { id, text: label }), control, extra);
  return el;
}

// ============================================================ view

export class SetupView {
  constructor(root) {
    this.root = root;
    this.built = false;
    this.dirty = new Set();
    this.timer = null;
    this.saving = false;
    this.again = false;
    this.rows = [];
    this.sig = null;
    this.page = h("div.screen.screen-narrow.setup");
    this.page.append(h("h1.page", { text: t("setup.title") }), h("p.dim.placeholder", { text: t("setup.connecting") }));
    root.append(this.page);
    if (store.meta) this.build();
  }

  destroy() {
    // an edit waiting to be saved must not be lost when leaving the screen
    if (this.timer) this.flush();
  }

  hello() {
    if (!this.built) {
      if (store.meta) this.build();
      return;
    }
    this.sig = null;
    if (this.dirty.size || this.saving || this.timer) return; // do not overwrite what is being typed
    for (const d of store.meta?.drivers || []) this.rows[d.slot]?.load(d);
    this.refreshRival();
  }

  update(state) {
    if (!this.built) {
      if (!store.meta) return;
      this.build();
    }
    this.patch(state);
  }

  // ------------------------------------------------------------ construction

  build() {
    const meta = store.meta;
    this.types = Object.entries(meta.session_types || {}).map(([key, info]) => ({ key, ...info }));
    const last = meta.session || {};
    this.v = {
      type: null,
      laps: clamp(last.laps ?? 20, 1, 999),
      minutes: last.duration_s ? clamp(Math.round(last.duration_s / 60), 1, 600) : null,
      limited: last.duration_s != null,
      elim_every: clamp(last.elim_every ?? 3, 1, 20),
      episode_laps: clamp(last.episode_laps ?? 5, 1, 100),
      rival_slot: last.rival_slot ?? null,
      rival_margin_pct: clamp(last.rival_margin_pct ?? 3, -20, 30),
      title: "",
    };

    this.buildTypes();
    this.buildParams();
    this.buildDrivers();

    this.page.replaceChildren(
      h("h1.page", { text: t("setup.title") }),
      h("div.setup-grid", {}, this.typesPanel, this.paramsPanel),
      this.driversPanel);
    this.built = true;

    const allowed = this.types.filter((type) => this.allowed(type));
    const wanted = this.types.find((type) => type.key === last.type && this.allowed(type)) || allowed[0] || this.types[0];
    if (wanted) this.selectType(wanted.key);
    this.refreshRival();
    if (store.state) this.patch(store.state);
  }

  allowed(type) {
    return !type.advanced_only || isAdvanced();
  }

  currentType() {
    return this.types.find((type) => type.key === this.v.type);
  }

  // ---- session types

  buildTypes() {
    this.typeList = h("div.type-list", { role: "radiogroup", "aria-label": t("setup.types") });
    this.typeItems = new Map();
    for (const type of this.types) {
      const chip = h("span.chip.hidden", { text: t("setup.advanced_only") });
      const item = h("button.type-item", {
        type: "button", role: "radio", "aria-checked": "false", tabindex: -1,
        onclick: () => { if (!item.disabled) this.selectType(type.key); },
      },
      h("span.type-dot", { "aria-hidden": "true" }),
      h("span.type-text", {}, h("span.type-label", { text: sessionLabel(type.key) }), h("span.type-desc", { text: sessionDescription(type.key) })),
      chip);
      this.typeItems.set(type.key, { item, chip });
      this.typeList.append(item);
    }
    radioGroup(this.typeList);
    this.typesPanel = h("section.panel.types", {},
      h("div.panel-head", {}, h("h2", { text: t("setup.types") })),
      this.typeList);
  }

  selectType(key) {
    this.v.type = key;
    for (const [k, { item }] of this.typeItems) {
      toggle(item, "on", k === key);
    }
    setRadios(this.typeList, this.typeItems.get(key)?.item);
    // if nothing is keyboard-selectable (everything disabled), a tab stop is kept
    this.applyParams();
  }

  // ---- parameters

  buildParams() {
    const f = (this.fields = {});

    // laps
    this.lapsStepper = Stepper({ label: t("setup.laps"), value: this.v.laps, min: 1, max: 999, onChange: (x) => this.setValue("laps", x) });
    f.laps = group(t("setup.laps"), h("div.control", {}, this.lapsStepper.el, h("span.dim", { text: t("setup.unit_laps") })));

    // duration (optional for practice)
    this.durMode = Segmented(
      [{ value: "free", label: t("setup.free") }, { value: "fixed", label: t("setup.fixed") }],
      this.v.limited ? "fixed" : "free",
      (val) => {
        this.v.limited = val === "fixed";
        this.paintDuration();
        this.applyParams();
      }, t("setup.duration"));
    this.durMode.buttons.forEach((b) => b.setAttribute("aria-pressed", "false"));
    this.durStepper = Stepper({
      label: t("setup.duration"),
      value: this.v.minutes ?? DEFAULT_MINUTES.practice, min: 1, max: 600,
      onChange: (x) => { this.v.minutes = x; this.applyParams(); },
    });
    this.durControl = h("div.control", {}, this.durStepper.el, h("span.dim", { text: t("setup.unit_min") }));
    f.duration = group(t("setup.duration"), h("div.stack", {}, this.durMode.el, this.durControl));

    // elimination
    this.elimStepper = Stepper({ label: t("setup.elim"), value: this.v.elim_every, min: 1, max: 20, onChange: (x) => this.setValue("elim_every", x) });
    f.elim = group(t("setup.elim"), h("div.control", {}, this.elimStepper.el, h("span.dim", { text: t("setup.unit_leader_laps") })));

    // training
    this.epiStepper = Stepper({ label: t("setup.episode"), value: this.v.episode_laps, min: 1, max: 100, onChange: (x) => this.setValue("episode_laps", x) });
    f.episode = group(t("setup.episode"), h("div.control", {}, this.epiStepper.el, h("span.dim", { text: t("setup.unit_laps") })));

    // against the bot
    this.rivalSelect = h("select.input", {
      onchange: () => { this.v.rival_slot = parseInt(this.rivalSelect.value, 10); this.applyParams(); },
    });
    f.rival = h("label.field", {}, h("span", { text: t("setup.rival_pace") }), this.rivalSelect);
    this.marginStepper = Stepper({ label: t("setup.margin"), value: this.v.rival_margin_pct, min: -20, max: 30, onChange: (x) => this.setValue("rival_margin_pct", x) });
    f.margin = group(t("setup.margin"), h("div.control", {}, this.marginStepper.el, h("span.dim", { text: t("setup.unit_margin") })));


    // title
    this.titleInput = h("input.input", { type: "text", maxlength: 80, autocomplete: "off" });
    this.titleInput.addEventListener("input", () => { this.v.title = this.titleInput.value; });
    f.title = h("label.field.wide", {}, h("span", { text: t("setup.title_field") }), this.titleInput);

    this.fieldsBox = h("div.params", {}, f.laps, f.duration, f.elim, f.episode, f.rival, f.margin, f.title);

    this.hint = h("p.setup-hint");
    this.busyText = h("span");
    this.goRace = h("a.btn", { href: "#/race" }, t("setup.go_race"));
    this.busyBox = h("div.notice.hidden", { role: "status" }, h("span.dot.warn"), this.busyText, this.goRace);
    this.submit = h("button.btn.primary.lg", { type: "button", onclick: () => this.go() }, t("setup.submit"));
    this.submitNote = h("span.dim.small");
    this.paramsPanel = h("section.panel.params-panel", {},
      h("div.panel-head", {}, h("h2", { text: t("setup.settings") })),
      h("div.panel-body", {},
        this.busyBox,
        this.fieldsBox,
        this.hint,
        // fuel is not a setting: the CU's fuel switch decides, nothing on the serial line can change it
        h("p.setup-hint.dim", { text: t("setup.fuel_cu") }),
        h("div.setup-actions", {}, this.submit, this.submitNote)));
    this.paintDuration();
  }

  setValue(key, value) {
    this.v[key] = value;
    this.applyParams();
  }

  paintDuration() {
    const key = this.v.type;
    const required = DURATION_REQUIRED.has(key);
    const fixed = required || this.v.limited;
    this.durMode.set(this.v.limited ? "fixed" : "free");
    this.durMode.buttons.forEach((b) => b.setAttribute("aria-pressed", b.classList.contains("on") ? "true" : "false"));
    toggle(this.durMode.el, "hidden", required);
    toggle(this.durControl, "hidden", !fixed);
  }

  durationMinutes() {
    return this.v.minutes ?? DEFAULT_MINUTES[this.v.type] ?? 10;
  }

  // shows the fields useful to the chosen type, updates the help text and the automatic title
  applyParams() {
    const type = this.currentType();
    if (!type) return;
    const params = new Set(type.params || []);
    const f = this.fields;
    toggle(f.laps, "hidden", !params.has("laps"));
    toggle(f.duration, "hidden", !params.has("duration_s"));
    toggle(f.elim, "hidden", !params.has("elim_every"));
    toggle(f.episode, "hidden", !params.has("episode_laps"));
    toggle(f.rival, "hidden", !params.has("rival_slot"));
    toggle(f.margin, "hidden", !params.has("rival_margin_pct"));
    if (params.has("duration_s")) {
      if (this.durStepper.value !== this.durationMinutes()) this.durStepper.set(this.durationMinutes());
      this.paintDuration();
    }
    this.titleInput.placeholder = defaultTitle(type.key, sessionLabel(type.key), this.v, this.durationSeconds(type));
    setText(this.hint, this.hintText(type));
  }

  durationSeconds(type) {
    if (!(type.params || []).includes("duration_s")) return null;
    if (!DURATION_REQUIRED.has(type.key) && !this.v.limited) return null;
    return this.durationMinutes() * 60;
  }

  rivalName() {
    const d = this.driverModels().find((m) => m.slot === this.v.rival_slot);
    return d?.name || t("setup.a_driver");
  }

  hintText(type) {
    const v = this.v;
    const lights = type.uses_lights ? t("setup.lights.cu") : t("setup.lights.none");
    const mins = formatDuration(this.durationMinutes() * 60);
    switch (type.key) {
      case "practice":
        return t("setup.hint.practice", { lights, limit: v.limited ? t("setup.hint.practice_limited", { mins }) : t("setup.hint.practice_free") });
      case "qualifying":
        return t("setup.hint.qualifying", { lights, mins });
      case "race":
        return t("setup.hint.race", { lights, laps: laps(v.laps) });
      case "endurance":
        return t("setup.hint.endurance", { lights, mins });
      case "elimination":
        return t("setup.hint.elimination", {
          lights,
          when: v.elim_every === 1 ? t("setup.hint.elim_every_lap") : t("setup.hint.elim_every_n", { n: v.elim_every }),
        });
      case "rival": {
        const m = v.rival_margin_pct;
        const pace = m === 0 ? t("setup.hint.pace_same") : t(m > 0 ? "setup.hint.pace_slower" : "setup.hint.pace_faster", { pct: Math.abs(m) });
        return t("setup.hint.rival", { lights, laps: laps(v.laps), name: this.rivalName(), pace });
      }
      case "training":
        return t("setup.hint.training", { lights, laps: laps(v.episode_laps) });
      default:
        return sessionDescription(type.key);
    }
  }

  // settings sent to the server (all keys; unused ones stay neutral)
  settings() {
    const type = this.currentType();
    const v = this.v;
    return {
      type: type.key,
      laps: v.laps,
      duration_s: this.durationSeconds(type),
      elim_every: v.elim_every,
      episode_laps: v.episode_laps,
      rival_slot: this.enabledSlots().includes(v.rival_slot) ? v.rival_slot : (this.enabledSlots()[0] ?? null),
      rival_margin_pct: v.rival_margin_pct,
      // empty: send the automatic title in the UI language (the server's default is English)
      title: v.title.trim() || this.titleInput.placeholder,
    };
  }

  async go() {
    const type = this.currentType();
    if (!type || this.submit.disabled) return;
    this.submit.disabled = true;
    try {
      await run("race.setup", { settings: this.settings() });
      location.hash = "#/race";
    } catch { /* toast already shown */ }
    this.sig = null;
    if (store.state) this.patch(store.state);
  }

  // ---- state (10 Hz: the DOM is only touched when something changes)

  patch(state) {
    const sess = state.session || {};
    const busy = !IDLE_STATES.has(sess.state);
    const advanced = state.mode === "advanced";
    const sig = `${sess.state}|${advanced}|${state.link?.state}|${this.enabledSlots().length}`;
    if (sig === this.sig) return;
    this.sig = sig;

    for (const type of this.types) {
      const { item, chip } = this.typeItems.get(type.key);
      const locked = !this.allowed(type);
      item.disabled = locked;
      toggle(chip, "hidden", !locked);
    }
    const current = this.currentType();
    if (!current || !this.allowed(current)) {
      const fallback = this.types.find((type) => this.allowed(type));
      if (fallback) this.selectType(fallback.key);
    }

    toggle(this.busyBox, "hidden", !busy);
    setText(this.busyText, t("setup.busy", {
      title: sess.title || t("setup.untitled"),
      state: (STATE_LABEL[sess.state] || sess.state || "").toLowerCase(),
    }));

    const noDriver = this.enabledSlots().length === 0;
    this.submit.disabled = busy || noDriver;
    let note = "";
    if (busy) note = "";
    else if (noDriver) note = t("setup.note.no_driver");
    else if (sess.state === "grid") note = t("setup.note.replace");
    else if (state.link && state.link.state !== "connected") note = t("setup.note.no_link");
    setText(this.submitNote, note);
  }

  // ------------------------------------------------------------ drivers

  buildDrivers() {
    const drivers = (store.meta.drivers || []).slice().sort((a, b) => a.slot - b.slot);
    this.rows = [];
    const main = h("div.drv-list");
    const extra = h("div.drv-list");
    for (const d of drivers) {
      const row = new DriverRow(d, (r) => this.touch(r));
      this.rows[d.slot] = row;
      (d.slot < 6 ? main : extra).append(row.el);
    }
    const specialOn = drivers.some((d) => d.slot >= 6 && d.enabled);
    this.saveState = h("span.save-state", { role: "status", "aria-live": "polite" });
    this.retry = h("button.btn.ghost.sm.hidden", { type: "button", onclick: () => this.flush() }, t("common.retry"));
    this.driversPanel = h("section.panel.drivers", {},
      h("div.panel-head", {}, h("h2", { text: t("setup.drivers") }), h("span.spacer"), this.saveState, this.retry),
      h("div.drv-head", { "aria-hidden": "true" },
        h("span"), h("span", { text: t("setup.col.no") }), h("span", { text: t("setup.col.name") }), h("span", { text: t("setup.col.car") }), h("span", { text: t("setup.col.color") })),
      main,
      extra.childElementCount
        ? h("details.drv-extra", { open: specialOn },
          h("summary", {}, h("span", { text: t("setup.special") }), h("span.faint", { text: t("setup.special_desc") })),
          extra)
        : null);
    this.setSave("idle");
  }

  driverModels() {
    return this.rows.filter(Boolean).map((r) => ({ slot: r.slot, ...r.model }));
  }

  enabledSlots() {
    return this.rows.filter((r) => r && r.slot < 6 && r.model.enabled).map((r) => r.slot);
  }

  refreshRival() {
    if (!this.rivalSelect) return;
    const slots = this.enabledSlots();
    this.rivalSelect.replaceChildren(...slots.map((s) =>
      h("option", { value: s, text: `${s + 1} · ${this.rows[s].model.name}` })));
    if (!slots.includes(this.v.rival_slot)) this.v.rival_slot = slots[0] ?? null;
    if (this.v.rival_slot != null) this.rivalSelect.value = String(this.v.rival_slot);
    this.rivalSelect.disabled = slots.length === 0;
    if (this.currentType()) this.applyParams();
  }

  touch(row) {
    this.dirty.add(row.slot);
    this.setSave(row.valid() ? "pending" : "invalid");
    this.refreshRival();
    this.sig = null;
    if (store.state) this.patch(store.state);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.flush(), SAVE_DELAY_MS);
  }

  async flush() {
    clearTimeout(this.timer);
    this.timer = null;
    if (this.saving) { this.again = true; return; }
    const rows = [...this.dirty].map((s) => this.rows[s]).filter((r) => r && r.valid());
    if (!rows.length) {
      this.setSave(this.dirty.size ? "invalid" : "idle");
      return;
    }
    const sent = rows.map((row) => ({ row, payload: row.payload() }));
    rows.forEach((r) => this.dirty.delete(r.slot));
    this.saving = true;
    this.setSave("saving");
    let failed = false;
    try {
      await run("drivers.update", { drivers: sent.map((s) => s.payload) });
      sent.forEach(({ row, payload }) => row.markSaved(payload));
      await refreshMeta().catch(() => {});
    } catch {
      failed = true;
      sent.forEach(({ row }) => this.dirty.add(row.slot));
    }
    this.saving = false;
    if (this.again) {
      this.again = false;
      this.flush();
    } else {
      this.setSave(failed ? "error" : this.dirty.size ? "invalid" : "saved");
    }
  }

  setSave(kind) {
    const text = {
      idle: t("setup.save.idle"),
      pending: t("setup.save.pending"),
      saving: t("setup.save.saving"),
      saved: t("setup.save.saved"),
      invalid: t("setup.save.invalid"),
      error: t("setup.save.error"),
    }[kind];
    setText(this.saveState, text);
    this.saveState.className = `save-state ${kind}`;
    toggle(this.retry, "hidden", kind !== "error");
  }
}

// ============================================================ driver row

class DriverRow {
  constructor(driver, onChange) {
    this.slot = driver.slot;
    this.onChange = onChange;
    const label = SPECIAL_NAMES[driver.slot] || t("driver.n", { n: driver.slot + 1 });
    this.label = label;
    this.customColor = PALETTE.some((p) => p.hex === driver.color) ? null : driver.color;

    this.toggle = Toggle(driver.enabled, (on) => { this.model.enabled = on; this.paint(); this.onChange(this); }, t("driver.enable", { label }));
    this.carno = h("span.carno", { text: driver.slot + 1 });
    this.name = h("input.input", { type: "text", maxlength: NAME_MAX, autocomplete: "off", spellcheck: "false", "aria-label": t("driver.aria_name", { label }) });
    this.car = h("input.input", { type: "text", maxlength: CAR_MAX, autocomplete: "off", placeholder: t("driver.car_placeholder"), "aria-label": t("driver.aria_car", { label }) });
    this.swatches = h("div.swatches", { role: "radiogroup", "aria-label": t("driver.aria_color", { label }) });
    radioGroup(this.swatches);

    this.name.addEventListener("input", () => {
      this.model.name = this.name.value;
      this.paintName();
      this.onChange(this);
    });
    this.name.addEventListener("change", () => {
      // field left empty: go back to the last saved name
      if (!this.name.value.trim()) {
        this.name.value = this.saved.name;
        this.model.name = this.saved.name;
        this.paintName();
        this.onChange(this);
      }
    });
    this.car.addEventListener("input", () => { this.model.car = this.car.value; this.onChange(this); });

    this.el = h("div.drv-row", {}, this.toggle.el, this.carno, this.name, this.car, this.swatches);
    this.load(driver);
  }

  load(driver) {
    this.saved = { name: driver.name, color: driver.color, enabled: !!driver.enabled, car: driver.car || "" };
    this.model = { ...this.saved };
    this.toggle.input.checked = this.model.enabled;
    this.name.value = this.model.name;
    this.car.value = this.model.car;
    this.renderSwatches();
    this.paintName();
    this.paint();
  }

  renderSwatches() {
    const options = PALETTE.slice();
    const custom = this.customColor || (PALETTE.some((p) => p.hex === this.model.color) ? null : this.model.color);
    if (custom) {
      this.customColor = custom;
      options.push({ hex: custom, name: t("color.current") });
    }
    this.swatches.replaceChildren(...options.map((c) => {
      const btn = h("button.sw", {
        type: "button", role: "radio", "aria-checked": "false", tabindex: -1,
        title: c.name, "aria-label": c.name, dataset: { hex: c.hex },
        onclick: () => this.pick(c.hex),
      });
      btn.style.setProperty("--c", c.hex);
      return btn;
    }));
    this.paintSwatches();
  }

  pick(hex) {
    if (hex === this.model.color) return;
    this.model.color = hex;
    this.paint();
    this.onChange(this);
  }

  paintSwatches() {
    let selected = null;
    for (const btn of this.swatches.children) {
      const on = btn.dataset.hex === this.model.color;
      toggle(btn, "on", on);
      if (on) selected = btn;
    }
    setRadios(this.swatches, selected || this.swatches.firstElementChild);
  }

  paint() {
    this.el.style.setProperty("--car", this.model.color);
    this.carno.style.color = inkOn(this.model.color);
    toggle(this.el, "off", !this.model.enabled);
    this.paintSwatches();
  }

  paintName() {
    if (this.valid()) this.name.removeAttribute("aria-invalid");
    else this.name.setAttribute("aria-invalid", "true");
  }

  valid() {
    return this.model.name.trim().length > 0;
  }

  payload() {
    return {
      slot: this.slot,
      name: this.model.name.trim().slice(0, NAME_MAX),
      color: this.model.color,
      enabled: this.model.enabled,
      car: this.model.car.trim(),
    };
  }

  markSaved(payload) {
    this.saved = { name: payload.name, color: payload.color, enabled: payload.enabled, car: payload.car };
  }
}
