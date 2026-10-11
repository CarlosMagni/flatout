// System screen: CU link, CU buttons, garage (car programming), raw console,
// API and log.
import { h, inkOn, setText, toggle, wallTime } from "../dom.js";
import { LANGUAGES, lang, locale, setLang, t } from "../i18n.js";
import { onEvent, request, store } from "../store.js";
import { Segmented, Stepper, Toggle, confirmModal, run, toast } from "../ui/components.js";
import { FirmwarePanel } from "./firmware.js";
import { ConnectChooser } from "./connect.js";

const IDLE_STATES = new Set(["idle", "grid", "finished"]);
const TRANSPORT = {
  ble: t("sys.transport.ble"), serial: t("sys.transport.serial"), sim: t("sys.transport.sim"), none: t("sys.transport.none"),
};
const LINK = {
  connected: { dot: "ok", label: t("sys.link.connected") },
  idle: { dot: "", label: t("sys.link.idle") },
  connecting: { dot: "warn", label: t("sys.link.connecting") },
  updating: { dot: "warn", label: t("sys.link.updating") },
  lost: { dot: "bad", label: t("sys.link.lost") },
  closed: { dot: "", label: t("sys.link.closed") },
};
const CU_BUTTONS = {
  race: [
    { id: "pace", label: "Pace car / ESC" },
    { id: "start", label: "Start / Enter" },
  ],
  menu: [
    { id: "speed", label: "Speed" },
    { id: "brake", label: "Brake" },
    { id: "fuel", label: "Fuel" },
    { id: "code", label: "Code" },
  ],
};
const GARAGE_FIELDS = [
  { key: "speed", label: t("garage.speed"), def: 10 },
  { key: "brake", label: t("garage.brake"), def: 10 },
  { key: "fuel", label: t("garage.fuel"), def: 15 },
];
const REFUSED = /^[GEFCB]/; // bootloader, firmware, CU settings, bots
const CONSOLE_MAX = 300;
const LOG_MAX = 200;
const GARAGE_KEY = "racelab.garage";

// minimal example with the real client (racelab/client); the URL is this page's
function pythonSnippet(url, token) {
  return `import asyncio
from racelab.client import RaceLabClient

async def main():
    async with RaceLabClient("${url}", agent="${t("api.snippet_agent")}"${token ? ', token="…"' : ""}) as c:
        await c.claim(0, free=True)        # ${t("api.snippet_claim")}
        for _ in range(40):
            await c.act(0, throttle=6)     # ${t("api.snippet_act")}
            await asyncio.sleep(0.25)

asyncio.run(main())`;
}

// ------------------------------------------------------------ small helpers

function panel(title, body, extra) {
  return h("section.panel.sys-panel", {},
    h("div.panel-head", {}, h("h2", { text: title }), h("span.spacer"), extra || null),
    h("div.panel-body", {}, body));
}

function kv(label) {
  const value = h("dd");
  return { row: h("div.kv", {}, h("dt", { text: label }), value), value };
}

function readGarage() {
  try { return JSON.parse(localStorage.getItem(GARAGE_KEY)) || {}; } catch { return {}; }
}
function writeGarage(data) {
  try { localStorage.setItem(GARAGE_KEY, JSON.stringify(data)); } catch { /* storage unavailable */ }
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = h("textarea", { "aria-hidden": "true", tabindex: -1 });
    area.value = text;
    Object.assign(area.style, { position: "fixed", opacity: "0", pointerEvents: "none" });
    document.body.append(area);
    area.select();
    try { document.execCommand("copy"); } catch { /* nothing to do */ }
    area.remove();
  }
  const before = button.textContent;
  button.textContent = t("common.copied");
  clearTimeout(button._t);
  button._t = setTimeout(() => { button.textContent = before; }, 1400);
}

// readable display of a raw frame (control bytes escaped)
const printable = (text) => String(text ?? "").replace(/[^\x20-\x7e]/g, (c) => `\\x${c.charCodeAt(0).toString(16).padStart(2, "0")}`);

const clock8 = (epoch = Date.now() / 1000) =>
  new Date(epoch * 1000).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

// ============================================================ view

export class SystemView {
  constructor(root) {
    this.root = root;
    this.off = [];
    this.sig = null;
    this.metaRef = null;
    this.tracing = false;
    this.history = [];
    this.historyAt = -1;

    this.buildConnection();
    this.buildButtons();
    this.buildGarage();
    this.buildConsole();
    this.buildApi();
    this.buildLog();
    this.fwPanel = new FirmwarePanel();
    this.buildFoot();

    root.append(h("div.screen.sys", {},
      h("h1.page", { text: t("nav.system") }),
      h("div.sys-cols", {},
        h("div.sys-col", {}, this.connPanel, this.buttonsPanel, this.consolePanel),
        h("div.sys-col", {}, this.garagePanel, this.fwPanel.el, this.apiPanel)),
      this.logPanel,
      this.foot));

    this.off.push(onEvent("log", (entry) => this.addLog(entry)));
    this.off.push(onEvent("raw", (msg) => this.onRaw(msg)));
    this.applyMeta();
    if (store.state) this.update(store.state);
  }

  destroy() {
    this.off.forEach((f) => f());
    // the raw trace must not keep running once the screen is left
    if (this.tracing) request("subscribe", { raw: false }).catch(() => {});
  }

  hello() {
    // new WebSocket connection: the raw frame subscription is lost
    this.tracing = false;
    this.traceToggle.input.checked = false;
    this.metaRef = null;
    this.applyMeta();
    this.renderLog();
    this.fwPanel.hello();
  }

  // ------------------------------------------------------------ connection

  buildConnection() {
    this.dot = h("span.dot");
    this.stateText = h("span");
    const rows = {
      state: kv(t("sys.kv.state")), transport: kv(t("sys.kv.transport")), device: kv(t("sys.kv.device")), version: kv(t("sys.kv.cu_fw")),
      bots: kv(t("sys.kv.bots_fw")), rtt: kv(t("sys.kv.latency")), rate: kv(t("sys.kv.rate")), queue: kv(t("sys.kv.queue")),
      error: kv(t("sys.kv.error")),
    };
    rows.state.value.append(this.dot, this.stateText);
    rows.state.value.classList.add("with-dot");
    this.botsChip = h("span.chip");
    rows.bots.value.append(this.botsChip);
    this.c = rows;
    this.drop = h("button.btn.sm", { type: "button", onclick: () => run("sim.drop_link", { seconds: 3 }).catch(() => {}) }, t("sys.drop"));
    this.simBox = h("div.sim-box.hidden", {}, this.drop,
      h("p.note", { text: t("sys.drop_note") }));
    this.chooser = new ConnectChooser({ compact: true });
    this.connPanel = panel(t("sys.connection"), [
      h("dl.kv-list", {}, ...Object.values(rows).map((r) => r.row)), this.simBox,
      h("div.eyebrow", { text: t("sys.change_cu"), style: { margin: "18px 0 10px" } }), this.chooser.el]);
  }

  updateLink(state) {
    const online = store.online && state;
    const link = state?.link || {};
    let info;
    if (!online) info = { dot: "bad", label: t("topbar.server_down") };
    else info = LINK[link.state] || { dot: "", label: link.state || "—" };
    const cls = `dot ${info.dot}`.trim();
    if (this.dot.className !== cls) this.dot.className = cls;
    setText(this.stateText, info.label);
    setText(this.c.transport.value, TRANSPORT[link.transport] || link.transport || "—");
    setText(this.c.device.value, link.device || "—");
    setText(this.c.version.value, link.version ? `v${link.version}` : "—");

    const bots = link.bots_supported;
    const botsCls = bots === true ? "chip go" : bots === false ? "chip caution" : "chip";
    if (this.botsChip.className !== botsCls) this.botsChip.className = botsCls;
    setText(this.botsChip, bots === true ? t("sys.bots_detected") : bots === false ? t("sys.bots_stock") : t("sys.bots_unknown"));

    const rtt = link.rtt_ms;
    setText(this.c.rtt.value, rtt != null ? `${Math.round(rtt)} ms` : "—");
    const rx = link.rx_per_s;
    const tx = link.tx_per_s;
    setText(this.c.rate.value, rx != null ? t("sys.kv.rate_value", { rx: Math.round(rx), tx: Math.round(tx ?? 0) }) : "—");
    setText(this.c.queue.value, link.queue != null ? String(link.queue) : "—");
    setText(this.c.error.value, link.error || t("sys.kv.error_none"));
    toggle(this.c.error.value, "faint", !link.error);
    toggle(this.c.error.value, "bad-text", !!link.error);

    const sim = link.transport === "sim";
    toggle(this.simBox, "hidden", !sim);
    if (state) this.chooser.update(state);
    this.drop.disabled = !online || link.state !== "connected";
  }

  // ------------------------------------------------------------ CU buttons

  buildButtons() {
    this.cuButtons = [];
    const make = (b) => {
      const el = h("button.btn", { type: "button", onclick: () => run("cu.press", { button: b.id }).catch(() => {}) }, b.label);
      this.cuButtons.push(el);
      return el;
    };
    this.buttonsPanel = panel(t("sys.cu_buttons"), [
      h("div.cu-pad", {},
        h("div.cu-row.two", {}, ...CU_BUTTONS.race.map(make)),
        h("div.cu-row.four", {}, ...CU_BUTTONS.menu.map(make))),
      h("p.note", { text: t("sys.buttons_note") }),
    ]);
  }

  // ------------------------------------------------------------ garage

  buildGarage() {
    this.garageRows = [];
    const memory = readGarage();
    const rows = h("div.g-rows");
    for (let slot = 0; slot < 6; slot++) {
      const row = new GarageRow(slot, memory[slot] || {}, (r) => this.program(r));
      this.garageRows.push(row);
      rows.append(row.el);
    }
    this.garagePanel = panel(t("sys.garage"), h("div.garage", {},
      h("div.g-head", { "aria-hidden": "true" },
        h("span", { text: t("garage.car") }), ...GARAGE_FIELDS.map((f) => h("span", { text: f.label })), h("span")),
      rows,
      h("p.warn-line", { text: t("garage.warn") }),
      h("p.note", { text: t("garage.note") })));
  }

  async program(row) {
    const payload = row.payload();
    if (!Object.keys(payload).length) return;
    row.button.disabled = true;
    try {
      await run("car.program", { slot: row.slot, ...payload }, t("garage.programmed", { n: row.slot + 1 }));
      row.saved();
      const memory = readGarage();
      memory[row.slot] = row.values();
      writeGarage(memory);
    } catch { /* toast already shown */ }
    this.sig = null;
    if (store.state) this.update(store.state);
  }

  // ------------------------------------------------------------ console

  buildConsole() {
    this.log = h("div.console-log", { role: "log", "aria-label": t("console.frames"), tabindex: 0, "data-empty": t("console.empty") });
    this.input = h("input.input.mono", {
      type: "text", autocomplete: "off", autocapitalize: "off", spellcheck: "false",
      placeholder: t("console.placeholder"), "aria-label": t("console.input_aria"),
    });
    this.input.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
        if (!this.history.length) return;
        ev.preventDefault();
        this.historyAt = ev.key === "ArrowUp"
          ? Math.min(this.history.length - 1, this.historyAt + 1)
          : Math.max(-1, this.historyAt - 1);
        this.input.value = this.historyAt < 0 ? "" : this.history[this.history.length - 1 - this.historyAt];
      }
    });
    this.sendBtn = h("button.btn", { type: "submit" }, t("console.send"));
    const form = h("form.console-form", { onsubmit: (ev) => { ev.preventDefault(); this.send(); } }, this.input, this.sendBtn);
    this.traceToggle = Toggle(false, (on) => this.setTrace(on), t("console.trace"));
    const trace = h("div.switch-row", {}, this.traceToggle.el,
      h("span.switch-text", { onclick: () => this.traceToggle.input.click() },
        h("span", { text: t("console.trace") }),
        h("span.dim", { text: t("console.trace_desc") })));
    const clear = h("button.btn.ghost.sm", { type: "button", onclick: () => this.log.replaceChildren() }, t("console.clear"));
    this.consolePanel = panel(t("sys.console"), [
      form,
      h("div.console-tools", {}, trace, clear),
      this.log,
      h("p.note", { text: t("console.refused_note") }),
    ]);
  }

  line(dir, text, kind = "") {
    const stick = this.log.scrollHeight - this.log.scrollTop - this.log.clientHeight < 28;
    this.log.append(h(`div.cl${kind ? "." + kind : ""}`, {},
      h("span.t", { text: clock8() }), h("span.d", { text: dir }),
      h("span.x", { text: kind === "err" || kind === "note" ? text : printable(text) })));
    while (this.log.childElementCount > CONSOLE_MAX) this.log.firstChild.remove();
    if (stick) this.log.scrollTop = this.log.scrollHeight;
  }

  async send() {
    const frame = this.input.value.trim();
    if (!frame) return;
    if (this.history[this.history.length - 1] !== frame) this.history.push(frame);
    this.historyAt = -1;
    this.line("→", frame, "out");
    this.input.value = "";
    if (REFUSED.test(frame)) {
      this.line("!", t("console.refused"), "err");
      return;
    }
    this.sendBtn.disabled = true;
    try {
      const reply = await request("console.send", { frame });
      this.line("←", reply == null || reply === "" ? t("console.no_reply") : String(reply), "in");
    } catch (e) {
      this.line("!", e.message, "err");
    }
    this.sendBtn.disabled = false;
    this.input.focus();
  }

  async setTrace(on) {
    try {
      await request("subscribe", { raw: on });
      this.tracing = on;
      if (on) this.line("·", t("console.trace_on"), "note");
    } catch (e) {
      toast(e.message, "error", 4500);
      this.traceToggle.input.checked = this.tracing;
    }
  }

  onRaw(msg) {
    if (!this.tracing) return;
    this.line(msg.dir === "rx" ? "rx" : "tx", msg.data, msg.dir === "rx" ? "rx" : "tx");
  }

  // ------------------------------------------------------------ API

  buildApi() {
    const ws = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
    const rest = `${location.origin}/api`;
    const copyRow = (label, text) => {
      const btn = h("button.btn.ghost.sm", { type: "button", "aria-label": t("api.copy_aria", { label }) }, t("common.copy"));
      btn.addEventListener("click", () => copyText(text, btn));
      return h("div.copy-row", {}, h("span.eyebrow", { text: label }), h("div.copy-line", {}, h("code.mono", { text }), btn));
    };
    this.wsUrl = ws;
    this.snippet = pythonSnippet(ws, false);
    this.pyCode = h("code.mono", { text: this.snippet });
    const pyBtn = h("button.btn.ghost.sm", { type: "button", "aria-label": t("api.python_copy_aria") }, t("common.copy"));
    pyBtn.addEventListener("click", () => copyText(this.snippet, pyBtn));
    this.apiMode = h("span");
    this.apiDeadman = h("span");
    this.apiAuth = h("span");
    this.apiAuthItem = h("li.hidden", {}, this.apiAuth);
    this.apiPanel = panel(t("sys.api"), [
      copyRow("WebSocket", ws),
      copyRow("REST", rest),
      h("div.copy-row", {}, h("div.copy-head", {}, h("span.eyebrow", { text: t("api.python") }), pyBtn),
        h("pre.code", {}, this.pyCode)),
      h("ul.api-notes", {},
        h("li", {}, this.apiMode),
        h("li", {}, this.apiDeadman),
        this.apiAuthItem),
    ]);
  }

  // ------------------------------------------------------------ log

  buildLog() {
    this.feed = h("ul.feed.log");
    this.logPanel = h("section.panel.sys-panel", {}, h("div.panel-head", {}, h("h2", { text: t("sys.log") })), this.feed);
    this.renderLog();
  }

  renderLog() {
    const entries = (store.log || []).slice(-LOG_MAX).reverse();
    this.feed.replaceChildren(...entries.map((e) => this.logItem(e)));
    if (!entries.length) this.feed.append(h("li.faint", {}, h("span.t"), h("span", { text: t("log.empty") })));
  }

  logItem(entry) {
    const cls = { warn: "warn", error: "bad" }[entry.level] || "";
    return h("li", { title: new Date(entry.t * 1000).toLocaleString(locale) },
      h("span.t", { text: wallTime(entry.t) }), h(`span${cls ? "." + cls : ""}`, { text: entry.text }));
  }

  addLog(entry) {
    if (!entry) return;
    if (this.feed.firstChild?.classList.contains("faint")) this.feed.replaceChildren();
    this.feed.prepend(this.logItem(entry));
    while (this.feed.childElementCount > LOG_MAX) this.feed.lastChild.remove();
  }

  // ------------------------------------------------------------ meta and state

  applyMeta() {
    const meta = store.meta;
    if (!meta) return;
    this.metaRef = meta;
    const ms = meta.safety?.deadman_ms ?? 600;
    setText(this.apiDeadman, t("api.deadman", { ms }));
    this.snippet = pythonSnippet(this.wsUrl, !!meta.api?.auth);
    setText(this.pyCode, this.snippet);
    toggle(this.apiAuthItem, "hidden", !meta.api?.auth);
    setText(this.apiAuth, t("api.auth"));

    setText(this.footVersion, `RaceLab ${meta.version ? "v" + meta.version : ""}`.trim());
    setText(this.footConfig, meta.config_file || t("foot.config_none"));
    setText(this.footData, meta.data_dir || "—");

    this.garageRows.forEach((row, slot) => {
      const d = meta.drivers?.find((x) => x.slot === slot);
      row.setDriver(d?.name || t("driver.n", { n: slot + 1 }), d?.color || "#888888");
    });
  }

  async quit() {
    const ok = await confirmModal({
      title: t("foot.quit_confirm.title"),
      text: t("foot.quit_confirm.text"),
      confirmLabel: t("foot.quit_confirm.ok"), danger: true,
    });
    if (!ok) return;
    try {
      await run("app.quit");
      window.racelabQuit?.();
    } catch { /* toast already shown */ }
  }

  // Footer: version, config, data folder, language selector and Quit. Built once (the
  // selector must work even while the server is unreachable); applyMeta() fills the texts.
  buildFoot() {
    this.footVersion = h("span");
    this.footConfig = h("span.mono", { text: "—" });
    this.footData = h("span.mono", { text: "—" });
    this.langSwitch = Segmented(LANGUAGES, lang, (next) => setLang(next));
    this.langSwitch.el.classList.add("lang-switch");
    this.langSwitch.el.setAttribute("aria-label", t("lang.label"));
    this.foot = h("footer.sys-foot", {},
      this.footVersion,
      h("span", {}, t("foot.config"), this.footConfig),
      h("span", {}, t("foot.data"), this.footData),
      h("div.foot-actions", {}, this.langSwitch.el,
        h("button.btn.danger.sm.quit-btn", { type: "button", onclick: () => this.quit() }, t("foot.quit"))));
  }

  update(state) {
    this.state = state;
    if (this.metaRef !== store.meta) this.applyMeta();
    this.updateLink(state);
    this.fwPanel.update(state);

    const sess = state.session || {};
    const busy = !IDLE_STATES.has(sess.state);
    const connected = store.online && state.link?.state === "connected";
    const advanced = state.mode === "advanced";
    const sig = `${state.mode}|${busy}|${connected}`;
    if (sig === this.sig) return;
    this.sig = sig;

    this.cuButtons.forEach((b) => { b.disabled = !connected; });
    this.garageRows.forEach((row) => row.setEnabled(connected && !busy));
    this.garagePanel.classList.toggle("locked", busy);

    toggle(this.consolePanel, "hidden", !advanced);
    this.sendBtn.disabled = !connected;
    if (!advanced && this.tracing) {
      this.tracing = false;
      this.traceToggle.input.checked = false;
      request("subscribe", { raw: false }).catch(() => {});
    }

    setText(this.apiMode, advanced ? t("api.mode_advanced") : t("api.mode_classic"));
  }
}

// ============================================================ garage row

class GarageRow {
  constructor(slot, memory, onProgram) {
    this.slot = slot;
    this.dirty = new Set();
    this.enabled = false;
    this.carno = h("span.carno", { text: slot + 1 });
    this.name = h("span.name", { text: t("driver.n", { n: slot + 1 }) });
    this.steppers = {};
    const cells = GARAGE_FIELDS.map((f) => {
      const st = Stepper({
        value: memory[f.key] ?? f.def, min: 0, max: 15,
        onChange: () => { this.dirty.add(f.key); this.sync(); },
      });
      const input = st.el.querySelector("input");
      input.setAttribute("aria-label", t("garage.aria_cell", { label: f.label, n: slot + 1 }));
      const [minus, plus] = st.el.querySelectorAll("button");
      minus.setAttribute("aria-label", t("garage.aria_minus", { label: f.label }));
      plus.setAttribute("aria-label", t("garage.aria_plus", { label: f.label }));
      this.steppers[f.key] = st;
      return h("div.g-cell", {}, h("span.g-label", { text: f.label }), st.el);
    });
    this.button = h("button.btn.sm", { type: "button", onclick: () => onProgram(this) }, t("garage.program"));
    this.el = h("div.g-row", {}, h("div.driver", {}, this.carno, this.name), ...cells, this.button);
    this.sync();
  }

  setDriver(name, color) {
    setText(this.name, name);
    this.el.style.setProperty("--car", color);
    this.carno.style.color = inkOn(color);
  }

  setEnabled(on) {
    this.enabled = on;
    this.sync();
  }

  values() {
    return Object.fromEntries(GARAGE_FIELDS.map((f) => [f.key, this.steppers[f.key].value]));
  }

  // only the values that were touched are sent to the car
  payload() {
    const all = this.values();
    return Object.fromEntries([...this.dirty].map((k) => [k, all[k]]));
  }

  saved() {
    this.dirty.clear();
    this.sync();
  }

  sync() {
    this.button.disabled = !this.enabled || this.dirty.size === 0;
    this.button.title = !this.enabled
      ? t("garage.unavailable")
      : this.dirty.size ? t("garage.program_changed") : t("garage.change_first");
  }
}
