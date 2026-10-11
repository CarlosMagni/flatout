// CU choice: AppConnect Bluetooth, serial cable or virtual CU.
// Used on the welcome screen (no CU chosen yet) and in System → Connection.
import { h, setText, toggle } from "../dom.js";
import { t } from "../i18n.js";
import { refreshMeta, request } from "../store.js";
import { Toggle, run, toast } from "../ui/components.js";

const OPTIONS = [
  {
    transport: "ble",
    title: t("connect.ble.title"),
    text: t("connect.ble.text"),
  },
  {
    transport: "sim",
    title: t("connect.sim.title"),
    text: t("connect.sim.text"),
  },
  {
    transport: "serial",
    title: t("connect.serial.title"),
    text: t("connect.serial.text"),
  },
];

export class ConnectChooser {
  constructor({ compact = false } = {}) {
    this.compact = compact;
    this.busy = false;
    this.rows = new Map();
    this.results = h("div.cx-results.hidden");
    this.message = h("p.cx-message.hidden");
    this.stock = Toggle(false, null, t("connect.stock_aria"));
    const list = h("div.cx-list");
    for (const opt of OPTIONS) {
      const row = this.makeRow(opt);
      this.rows.set(opt.transport, row);
      list.append(row.el);
    }
    this.disconnect = h("button.btn.ghost.sm.hidden", { onclick: () => this.connect("none") }, t("connect.disconnect"));
    this.el = h(`div.cx${compact ? ".compact" : ""}`, {}, list, this.results, this.message, this.disconnect);
  }

  makeRow(opt) {
    const status = h("span.chip.hidden");
    const actions = h("div.cx-actions");
    if (opt.transport === "ble") {
      actions.append(
        h("button.btn.primary", { onclick: () => this.connect("ble") }, t("connect.connect")),
        h("button.btn", { onclick: () => this.scan("ble") }, t("connect.search")));
    } else if (opt.transport === "sim") {
      actions.append(
        h("button.btn", { onclick: () => this.connect("sim", "", this.stock.input.checked) }, t("connect.start")),
        h("label.cx-stock", {}, this.stock.el, h("span", { text: t("connect.stock") })));
    } else {
      actions.append(h("button.btn", { onclick: () => this.scan("serial") }, t("connect.search_ports")));
    }
    const el = h("div.cx-row", {},
      h("div.cx-text", {}, h("div.cx-title", {}, opt.title, status), h("div.cx-desc", { text: opt.text })),
      actions);
    return { el, status, actions };
  }

  update(state) {
    const link = state?.link || {};
    const sig = `${link.transport}|${link.state}|${this.busy}`;
    if (sig === this.sig) return;
    this.sig = sig;
    for (const [transport, row] of this.rows) {
      const current = link.transport === transport;
      toggle(row.el, "on", current);
      toggle(row.status, "hidden", !current);
      if (current) {
        const ok = link.state === "connected";
        row.status.className = `chip ${ok ? "go" : link.state === "lost" ? "stop" : "caution"}`;
        setText(row.status, ok ? t("connect.status_connected") : link.state === "lost" ? t("connect.status_lost") : t("connect.status_searching"));
      }
      row.actions.querySelectorAll("button").forEach((b) => (b.disabled = this.busy));
    }
    toggle(this.disconnect, "hidden", !this.compact || link.transport === "none");
  }

  async connect(transport, device = "", stock = false) {
    if (this.busy) return;
    this.busy = true;
    this.sig = null;
    try {
      await run("cu.connect", { transport, device, stock });
      await refreshMeta();
      this.hideResults();
    } catch {
      /* toast already shown */
    } finally {
      this.busy = false;
      this.sig = null;
    }
  }

  async scan(kind) {
    if (this.busy) return;
    this.busy = true;
    this.sig = null;
    toggle(this.results, "hidden", false);
    toggle(this.message, "hidden", true);
    this.results.replaceChildren(h("p.faint", { text: kind === "ble" ? t("connect.scanning_ble") : t("connect.scanning_ports") }));
    try {
      const res = await request("cu.scan", { timeout: 5 }, 30000);
      const devices = res.devices.filter((d) => d.transport === kind);
      // scan errors come from the server (English); Bluetooth ones mention "Bluetooth"
      const isBle = (e) => /bluetooth/i.test(e);
      const errors = kind === "ble" ? res.errors.filter(isBle) : res.errors.filter((e) => !isBle(e));
      this.results.replaceChildren(...(devices.length
        ? devices.map((d) => h("div.cx-device", {},
          h("div", {}, h("div", { text: d.label }), h("div.mono.faint", { text: d.device })),
          h("button.btn.primary.sm", { onclick: () => this.connect(d.transport, d.device) }, t("connect.connect"))))
        : [h("p.faint", { text: kind === "ble" ? t("connect.none_ble") : t("connect.none_serial") })]));
      if (errors.length) {
        setText(this.message, errors.join(" · "));
        toggle(this.message, "hidden", false);
      }
    } catch (e) {
      this.results.replaceChildren();
      toast(e.message, "error", 5000);
    } finally {
      this.busy = false;
      this.sig = null;
    }
  }

  hideResults() {
    toggle(this.results, "hidden", true);
    toggle(this.message, "hidden", true);
  }
}
