// "CU firmware" panel (System screen): .HMF image choice, import, explicit
// confirmation, flash progress, recovery mode.
import { IDLE_STATES, clock, h, setText, toggle } from "../dom.js";
import { t, tOr } from "../i18n.js";
import { authHeaders, request, store } from "../store.js";
import { confirmModal, openDrawer, run, toast } from "../ui/components.js";

// the server knows images by SHA-256: its verdict, in the UI language when we have it
const warningOf = (img) => (img.issue ? tOr(`fw.issue.${img.issue}`, img.warning) : img.warning);
const STAGES = {
  prepare: t("fw.stage.prepare"),
  bootloader: t("fw.stage.bootloader"),
  write: t("fw.stage.write"),
  reboot: t("fw.stage.reboot"),
  verifying: t("fw.stage.verifying"),
};
// seconds per line, measured on the principle of carreralib.fw (BLE: 5 F blocks + 1 E per line)
const SECONDS_PER_LINE = { ble: 0.16, serial: 0.06, sim: 0.03 };

function kib(bytes) {
  return t("fmt.kib", { n: Math.round(bytes / 1024) });
}

// "Type <word> to confirm" with the word in bold, wherever the language puts it
function typeConfirmParts(word) {
  const [before, after] = t("fw.type_confirm", { word: "{word}" }).split("{word}");
  return [before, h("b", { text: word }), after];
}

// firmware images found in the project folder (server value "project"; others are "imported")
const isLocalSource = (source) => source === "project";

export class FirmwarePanel {
  constructor() {
    this.images = [];
    this.selected = null;
    this.current = h("div.fw-current");
    this.list = h("div.fw-list", { role: "radiogroup", "aria-label": t("fw.image_aria") });
    this.file = h("input", { type: "file", accept: ".hmf,.HMF", class: "hidden" });
    this.file.addEventListener("change", () => this.upload());
    this.importBtn = h("button.btn.sm", { onclick: () => this.file.click() }, t("fw.import"));
    this.flashBtn = h("button.btn.primary", { onclick: () => this.confirm(false) }, t("fw.update"));
    this.recoverBtn = h("button.btn.danger.hidden", { onclick: () => this.confirm(true) }, t("fw.recovery"));
    this.why = h("p.note.hidden");
    // images are built from source (firmware/build.py), not stored in the repository
    this.buildBtn = h("button.btn.sm", { onclick: () => this.build() }, t("fw.build"));
    this.buildNote = h("p.note");
    this.buildBox = h("div.fw-build.hidden", {}, this.buildNote, this.buildBtn);
    this.progress = this.buildProgress();
    // advanced option: bots do not need a firmware update, so the panel stays folded
    // unless an update is running or the CU is waiting in its bootloader
    this.el = h("details.panel.sys-panel.fw.adv", {},
      h("summary.adv-summary", {},
        h("span.adv-text", {}, h("h2", { text: t("fw.adv_title") }), h("span.faint", { text: t("fw.adv_sub") }))),
      h("div.panel-body", {},
        h("p.note.adv-note", { text: t("fw.adv_note") }),
        h("div.panel-head.fw-head", {}, h("h2", { text: t("fw.title") }), h("span.spacer"), this.importBtn, this.file),
        this.current,
        this.buildBox,
        this.list,
        this.progress.el,
        h("div.fw-actions", {}, this.flashBtn, this.recoverBtn),
        this.why,
        h("p.note", { text: t("fw.note") })));
    this.load();
  }

  hello() {
    this.load();
  }

  async load() {
    try {
      this.images = await request("firmware.images");
    } catch {
      return;
    }
    if (!this.images.some((i) => i.id === this.selected)) {
      this.selected = (this.images.find((i) => /bot/i.test(i.name)) || this.images[0])?.id ?? null;
    }
    this.renderList();
    if (this.state) this.update(this.state, true);
  }

  renderList() {
    if (!this.images.length) {
      this.list.replaceChildren(h("p.faint.placeholder", {
        text: t("fw.no_image"),
      }));
      return;
    }
    this.list.replaceChildren(...this.images.map((img) => {
      const item = h("button.fw-item", {
        type: "button", role: "radio", "aria-checked": img.id === this.selected ? "true" : "false",
        onclick: () => { this.selected = img.id; this.renderList(); if (this.state) this.update(this.state, true); },
      },
      h("span.type-dot"),
      h("span.type-text", {},
        h("span.type-label", { text: img.label }),
        h("span.type-desc", {},
          `${t("fw.lines", { n: img.lines })} · ${kib(img.size)} · ${isLocalSource(img.source) ? t("fw.source_local") : t("fw.source_imported")} · `,
          img.tag ? h("span.mono", { text: img.tag }) : null, img.tag ? " · " : null,
          h("span.mono", { text: img.sha256.slice(0, 12) }), " · ",
          h(`span.fw-status.${img.status}`, { text: t(`fw.status.${img.status}`) })),
        warningOf(img) ? h("span.fw-warn", { text: warningOf(img) }) : null));
      toggle(item, "on", img.id === this.selected);
      return item;
    }));
  }

  renderBuild(info) {
    if (!info) return;
    const building = info.state === "building";
    const show = info.available && (building || info.state === "failed" || (info.missing || []).length > 0);
    toggle(this.buildBox, "hidden", !show);
    this.buildBtn.disabled = building;
    let text = t("fw.build_missing", { tags: (info.missing || []).join(", ") });
    if (building) text = t("fw.build_running");
    else if (info.state === "failed") text = t("fw.build_failed", { log: (info.log || "").split("\n").slice(-3).join(" ") });
    setText(this.buildNote, text);
    if (info.state === "done" && this._buildWas === "building") {
      toast(t("fw.build_done"));
      this.load();
    }
    this._buildWas = info.state;
  }

  async build() {
    try {
      this.renderBuild(await run("firmware.build"));
    } catch {
      /* the error is already shown by run() */
    }
  }

  async upload() {
    const file = this.file.files?.[0];
    this.file.value = "";
    if (!file) return;
    try {
      const res = await fetch(`/api/firmware/upload?name=${encodeURIComponent(file.name)}`, {
        method: "POST", body: await file.arrayBuffer(), headers: authHeaders(),
      });
      const body = await res.json();
      if (!body.ok) throw new Error(body.error || t("fw.import_refused"));
      this.selected = body.result.id;
      toast(t("fw.imported", { n: body.result.lines }));
      await this.load();
    } catch (e) {
      toast(e.message, "error", 5000);
    }
  }

  // ------------------------------------------------------------ progress

  buildProgress() {
    const stage = h("div.fw-stage");
    const fill = h("div.fw-fill");
    const bar = h("div.fw-bar", { role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100 }, fill);
    const detail = h("div.fw-detail.num");
    const abort = h("button.btn.danger.sm", {
      onclick: async () => {
        const ok = await confirmModal({
          title: t("fw.abort_confirm.title"),
          text: t("fw.abort_confirm.text"),
          confirmLabel: t("fw.abort_confirm.ok"), cancelLabel: t("fw.abort_confirm.cancel"), danger: true,
        });
        if (ok) run("firmware.abort").catch(() => {});
      },
    }, t("fw.abort"));
    const el = h("div.fw-progress.hidden", {}, h("div.fw-row", {}, stage, abort), bar, detail);
    return { el, stage, fill, bar, detail, abort };
  }

  update(state, force = false) {
    this.renderBuild(state.firmware_build);
    this.state = state;
    const fw = state.firmware || { state: "idle" };
    const link = state.link || {};
    const sig = JSON.stringify([fw, link.state, link.version, link.bot_module, state.session?.state, this.selected]);
    if (!force && sig === this.sig) return;
    this.sig = sig;

    // what the firmware is (bots module or not), whether or not bots are available
    const fwLabel = { true: t("fw.bots_detected"), false: t("fw.bots_absent") }[link.bot_module] ?? t("fw.bots_unknown");
    this.current.replaceChildren(
      h("span.dot." + (link.state === "connected" ? "ok" : link.state === "updating" ? "warn" : "bad")),
      h("span", {}, link.state === "connected"
        ? t("fw.cu_version", { version: link.version ?? "?", bots: fwLabel })
        : link.state === "updating" ? t("fw.updating") : t("fw.cu_disconnected")));

    const busy = fw.state === "running" || fw.state === "verifying";
    const sessionBusy = !IDLE_STATES.has(state.session?.state);
    const connected = link.state === "connected";
    const stuck = !!fw.stuck; // the server's call: a failed update, or a CU that answers without a version
    if ((busy || stuck) && !this.opened) {
      this.opened = true; // unfold once; the user may fold it again
      this.el.open = true;
    }
    const image = this.images.find((i) => i.id === this.selected);

    const defective = image?.status === "defective"; // refused by the server too
    this.flashBtn.disabled = busy || sessionBusy || !connected || !image || defective;
    toggle(this.recoverBtn, "hidden", !stuck || busy);
    this.recoverBtn.disabled = !image || sessionBusy || defective;
    this.importBtn.disabled = busy;
    this.list.querySelectorAll("button").forEach((b) => (b.disabled = busy));
    let why = "";
    if (defective) why = t("fw.why.defective");
    else if (sessionBusy) why = t("fw.why.session");
    else if (!connected && !busy && !stuck) why = t("fw.why.connect");
    else if (stuck && !busy) why = t("fw.why.stuck");
    setText(this.why, why);
    toggle(this.why, "hidden", !why);

    this.renderProgress(fw, link);
  }

  renderProgress(fw, link) {
    const p = this.progress;
    const shown = fw.state !== "idle";
    toggle(p.el, "hidden", !shown);
    if (!shown) return;
    const pct = fw.total ? Math.round((fw.done / fw.total) * 100) : 0;
    p.fill.style.width = `${fw.state === "done" ? 100 : pct}%`;
    p.bar.setAttribute("aria-valuenow", pct);
    p.el.className = `fw-progress ${fw.state}`;
    toggle(p.abort, "hidden", fw.state !== "running");
    if (fw.state === "done") {
      const r = fw.result || {};
      setText(p.stage, t("fw.done"));
      const bots = r.bot_module ? t("fw.bots_detected") : r.bot_module === false ? t("fw.bots_absent") : t("fw.bots_unknown");
      setText(p.detail, `${t("fw.cu_version", { version: r.version ?? "?", bots })} · ${fw.image?.name ?? ""}`);
    } else if (fw.state === "failed") {
      setText(p.stage, t("fw.failed"));
      setText(p.detail, fw.error || "");
    } else {
      setText(p.stage, STAGES[fw.state === "verifying" ? "verifying" : fw.stage] || t("fw.stage.default"));
      const eta = fw.eta_s != null ? t("fw.time_left", { time: clock(fw.eta_s) }) : "";
      setText(p.detail, fw.state === "verifying"
        ? t("fw.sent_waiting")
        : t("fw.progress", { done: fw.done, total: fw.total, pct, eta }));
    }
  }

  // ------------------------------------------------------------ confirmation

  confirm(recovery) {
    const image = this.images.find((i) => i.id === this.selected);
    if (!image) return;
    const word = store.meta?.firmware_confirm; // the confirmation word is defined by the server
    if (!word) return;
    const transport = this.state?.link?.transport || "ble";
    const duration = clock(Math.max(5, image.lines * (SECONDS_PER_LINE[transport] ?? 0.16) + 6));
    const input = h("input.input.fw-confirm", { placeholder: word, autocomplete: "off", spellcheck: "false", "aria-label": t("fw.type_word", { word }) });
    const go = h("button.btn.danger", { disabled: true }, recovery ? t("fw.restart_flash") : t("fw.start_update"));
    input.addEventListener("input", () => { go.disabled = input.value.trim().toUpperCase() !== word; });
    const content = h("div.fw-sheet", {},
      warningOf(image) ? h("p.fw-warn.fw-warn-block", { text: warningOf(image) }) : null,
      h("div.fw-summary", {},
        h("div.eyebrow", { text: recovery ? t("fw.recovery_eyebrow") : t("fw.image_eyebrow") }),
        h("div.fw-name", { text: image.label }),
        h("div.dim", { text: t("fw.estimate", { lines: t("fw.lines", { n: image.lines }), duration, transport: transport === "sim" ? t("fw.virtual_cu") : transport.toUpperCase() }) })),
      h("ul.fw-checklist", {},
        h("li", { text: t("fw.check.bots") }),
        h("li", { text: t("fw.check.power") }),
        h("li", { text: recovery ? t("fw.check.recovery") : t("fw.check.normal") }),
        h("li", { text: t("fw.check.safe") })),
      h("label.field", {}, h("span", {}, ...typeConfirmParts(word)), input));
    const close = openDrawer(recovery ? t("fw.drawer_recovery") : t("fw.drawer_update"), content, []);
    const footer = h("footer", {}, h("button.btn", { onclick: () => close() }, t("common.cancel")), go);
    content.closest(".drawer").append(footer);
    go.addEventListener("click", async () => {
      go.disabled = true;
      try {
        await run("firmware.flash", { image: image.id, confirm: input.value.trim(), recovery });
        close();
      } catch {
        go.disabled = false;
      }
    });
    setTimeout(() => input.focus(), 50);
  }
}
