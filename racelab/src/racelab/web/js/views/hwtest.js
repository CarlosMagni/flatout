// "Hardware test" panel (System screen): a bench to validate the stock CU's C command on a real track.
// Every timing and every limit lives on the server (test.* and link.bench ops): this file only
// asks, shows what came back, and records the tester's yes / no answers in a report.
import { h, setText, toggle } from "../dom.js";
import { lang, t } from "../i18n.js";
import { onEvent, request, store } from "../store.js";
import { Stepper, copyText, run, toast } from "../ui/components.js";

const IDLE_STATES = new Set(["idle", "grid", "finished"]);
const LONG = 12000; // request timeout of the ops that wait for the server
const BAR = 24;
// indicative verdict: a C frame must reach the CU within 750 ms, the heartbeat is 300 ms
const LATENCY_P95_MS = 150;
const LATENCY_MAX_MS = 400;
const DURATIONS = [0.2, 0.5, 1, 1.5, 2];
const BENCH_SIZES = [50, 100, 300];
const STEP_IDS = ["link_check", "latency", "pulse", "lane", "handback"];

// results survive leaving and coming back to the System screen (until the page is reloaded)
const memory = { results: {}, notes: "", saved: null, settings: { slot: 0, throttle: 3, ms: 1000, n: 100 } };

const now = () => new Date().toISOString();
const CHIPS = {
  todo: ["", () => t("hw.chip.todo")],
  running: ["caution", () => t("hw.chip.running")],
  wait: ["info", () => t("hw.chip.wait")],
  pass: ["go", () => t("hw.chip.pass")],
  fail: ["stop", () => t("hw.chip.fail")],
  aborted: ["caution", () => t("hw.chip.aborted")],
  refused: ["caution", () => t("hw.chip.refused")],
};

function histText(bins) {
  const max = Math.max(...bins.map((b) => b.count), 1);
  return bins.map((b) => `${b.from_ms.toFixed(1).padStart(7)} – ${b.to_ms.toFixed(1).padStart(7)} ms │${"█".repeat(Math.round((b.count / max) * BAR))} ${b.count}`).join("\n");
}

// PASS needs a connected CU that takes the stock C command
export function evaluateLink(state) {
  const l = state?.link || {};
  const base = {
    state: l.state, transport: l.transport, device: l.device ?? null, version: l.version ?? null,
    bot_transport: l.bot_transport ?? null, bots_supported: l.bots_supported ?? null, bot_module: l.bot_module ?? null,
  };
  let code = "ok";
  if (l.transport === "none") code = "no_cu";
  else if (l.state !== "connected") code = "not_connected";
  else if (l.bots_supported !== true) code = "no_bots";
  else if (l.bot_transport !== "C") code = "b_only";
  return { ...base, status: code === "ok" ? "pass" : "fail", code, explanation: t(`hw.link.${code}`, { version: l.version ?? "?" }), at: now() };
}

function latencyVerdict(r) {
  const worst = Math.max(r.max_ms ?? 0, r.c_ack?.max_ms ?? 0);
  if (!r.answered) return "no_answer";
  if (r.lost > 0) return "lost";
  if (r.p95_ms > LATENCY_P95_MS) return "slow";
  if (worst > LATENCY_MAX_MS) return "spikes";
  return "ok";
}

export class HwTestPanel {
  constructor() {
    this.state = null;
    this.running = null;
    this.phase = null;
    this.paused = null; // performance.now() when the hand-back pause started (display only)
    this.sig = null;
    this.steps = {};
    this.build();
    this.off = [onEvent("hwtest", (d) => this.onPhase(d))];
    this.renderAll();
  }

  destroy() {
    this.off.forEach((f) => f());
    clearInterval(this.tick);
  }

  hello() {
    this.sig = null;
  }

  // ------------------------------------------------------------ build

  build() {
    this.stopBtn = h("button.btn.danger.lg", { type: "button", onclick: () => this.stop() }, t("estop.stop"));
    this.bar = h("div.hw-bar", {},
      h("div.hw-warn", { role: "alert" }, h("strong", { text: t("hw.warn.title") }), h("span", { text: t("hw.warn.text") })),
      this.stopBtn);

    this.slotSel = h("select.input", { "aria-label": t("hw.slot") });
    for (let i = 0; i < 6; i++) this.slotSel.append(h("option", { value: i, text: t("hw.slot_option", { n: i + 1 }) }));
    this.slotSel.value = memory.settings.slot;
    this.slotSel.addEventListener("change", () => { memory.settings.slot = Number(this.slotSel.value); });
    this.throttle = Stepper({ value: memory.settings.throttle, min: 1, max: 6, onChange: (v) => { memory.settings.throttle = v; } });
    this.duration = h("select.input", { "aria-label": t("hw.duration") });
    for (const s of DURATIONS) this.duration.append(h("option", { value: s * 1000, text: t("hw.seconds", { s }) }));
    this.duration.value = memory.settings.ms;
    this.duration.addEventListener("change", () => { memory.settings.ms = Number(this.duration.value); });
    this.setup = h("div.hw-setup", {},
      h("label", {}, h("span", { text: t("hw.slot") }), this.slotSel),
      h("label", {}, h("span", { text: t("hw.throttle") }), this.throttle.el),
      h("label", {}, h("span", { text: t("hw.duration") }), this.duration));

    this.why = h("p.note.hw-why.hidden");
    this.modeBtn = h("button.btn.sm.hidden", { type: "button", onclick: () => run("mode.set", { mode: "advanced" }, t("mode.set_advanced")).catch(() => {}) }, t("hw.switch_mode"));

    this.list = h("ol.hw-steps");
    this.step("link_check", t("hw.step.link.title"), t("hw.step.link.desc"), () => this.runLink(), t("hw.step.link.run"));
    this.step("latency", t("hw.step.latency.title"), t("hw.step.latency.desc"), () => this.runLatency(), t("hw.step.latency.run"), this.sizeSelect());
    this.step("pulse", t("hw.step.pulse.title"), t("hw.step.pulse.desc"), () => this.runMove("pulse"), t("hw.step.pulse.run"));
    this.step("lane", t("hw.step.lane.title"), t("hw.step.lane.desc"), () => this.runMove("lane"), t("hw.step.lane.run"));
    this.step("handback", t("hw.step.handback.title"), t("hw.step.handback.desc"), () => this.runHandback(), t("hw.step.handback.run"));

    this.notes = h("textarea.input.hw-notes", { rows: 3, placeholder: t("hw.notes_placeholder"), "aria-label": t("hw.notes") });
    this.notes.value = memory.notes;
    this.notes.addEventListener("input", () => { memory.notes = this.notes.value; });
    this.copyBtn = h("button.btn.primary", { type: "button", onclick: () => this.copyReport() }, t("hw.copy"));
    this.saved = h("p.note.hw-saved");

    this.el = h("section.panel.sys-panel.hw", {},
      h("div.panel-head", {}, h("h2", { text: t("hw.title") })),
      h("div.panel-body", {},
        this.bar,
        h("p.note", { text: t("hw.intro") }),
        this.setup, this.why, this.modeBtn,
        this.list,
        h("div.hw-report", {},
          h("h3", { text: t("hw.report.title") }),
          h("p.note", { text: t("hw.report.desc") }),
          this.notes,
          h("div.hw-actions", {}, this.copyBtn),
          this.saved)));
  }

  sizeSelect() {
    this.nSel = h("select.input", { "aria-label": t("hw.samples") });
    for (const n of BENCH_SIZES) this.nSel.append(h("option", { value: n, text: t("hw.samples_n", { n }) }));
    this.nSel.value = memory.settings.n;
    this.nSel.addEventListener("change", () => { memory.settings.n = Number(this.nSel.value); });
    return this.nSel;
  }

  step(id, title, desc, onRun, runLabel, extra) {
    const chip = h("span.chip");
    const body = h("div.hw-body");
    const button = h("button.btn", { type: "button", onclick: onRun }, runLabel);
    const el = h("li.hw-step", {},
      h("div.hw-step-head", {}, h("span.hw-n", { text: String(STEP_IDS.indexOf(id) + 1) }), h("h3", { text: title }), chip),
      h("p.note", { text: desc }),
      h("div.hw-run", {}, extra || null, button),
      body);
    this.list.append(el);
    this.steps[id] = { el, chip, body, button };
  }

  // ------------------------------------------------------------ state

  update(state) {
    this.state = state;
    const link = state.link || {};
    const connected = store.online && link.state === "connected";
    const idle = IDLE_STATES.has(state.session?.state);
    const advanced = state.mode === "advanced";
    const busy = !!state.hwtest || !!this.running;
    const bots = (state.bots || []).filter((b) => b.active).length;
    let why = null;
    let needMode = false;
    if (!connected) why = t("hw.why.link");
    else if (link.bots_supported !== true) why = t("hw.why.bots");
    else if (!advanced) { why = t("hw.why.mode"); needMode = true; }
    else if (!idle) why = t("hw.why.session");
    else if (state.estop) why = t("hw.why.estop");
    const handbackWhy = why || (link.bot_transport !== "C" ? t("hw.why.transport") : bots ? t("hw.why.bots_active") : null);
    const sig = [connected, why, handbackWhy, busy, state.estop, needMode, this.running].join("|");
    if (sig === this.sig) return;
    this.sig = sig;

    setText(this.stopBtn, state.estop ? t("hw.release_stop") : t("estop.stop"));
    toggle(this.why, "hidden", !(why || handbackWhy));
    setText(this.why, why || handbackWhy || "");
    toggle(this.modeBtn, "hidden", !needMode);
    this.steps.link_check.button.disabled = !store.online || busy;
    this.steps.latency.button.disabled = !connected || busy;
    for (const id of ["pulse", "lane"]) this.steps[id].button.disabled = !!why || busy;
    this.steps.handback.button.disabled = !!handbackWhy || busy;
    for (const id of STEP_IDS) toggle(this.steps[id].el, "running", this.running === id);
    this.renderAll();
  }

  stop() {
    run(this.state?.estop ? "estop.clear" : "estop").catch(() => {});
  }

  // ------------------------------------------------------------ steps

  setResult(id, result) {
    memory.results[id] = { ...result, at: now() };
    this.sig = null;
    this.renderStep(id);
    if (this.state) this.update(this.state);
  }

  async begin(id, op, args, timeout) {
    this.running = id;
    this.sig = null;
    this.renderStep(id);
    if (this.state) this.update(this.state);
    try {
      return await request(op, args, timeout);
    } finally {
      this.running = null;
      this.paused = null;
      clearInterval(this.tick);
    }
  }

  runLink() {
    this.setResult("link_check", evaluateLink(this.state));
  }

  async runLatency() {
    try {
      const r = await this.begin("latency", "link.bench", { n: memory.settings.n }, 60000);
      const verdict = latencyVerdict(r);
      this.setResult("latency", { status: verdict === "ok" ? "pass" : "fail", verdict, ...r });
    } catch (e) {
      this.refused("latency", e);
    }
  }

  async runMove(id) {
    const { slot, throttle, ms } = memory.settings;
    try {
      const r = await this.begin(id, `test.${id}`, { slot, throttle, ms }, LONG);
      const ok = r.acked && !r.aborted && (id === "pulse" || r.lane_sent);
      const status = r.aborted ? "aborted" : !r.acked ? "fail" : ok ? "wait" : "fail";
      this.setResult(id, { status, observed: null, ...r });
    } catch (e) {
      this.refused(id, e);
    }
  }

  async runHandback() {
    const { slot, throttle } = memory.settings;
    try {
      const r = await this.begin("handback", "test.handback", { slot, throttle }, LONG);
      const status = r.aborted ? "aborted" : !r.acked ? "fail" : "wait";
      this.setResult("handback", { status, observed: null, ...r });
    } catch (e) {
      this.refused("handback", e);
    }
  }

  refused(id, error) {
    toast(error.message, "error", 5000);
    this.setResult(id, { status: "refused", error: error.message });
  }

  answer(id, yes) {
    const r = memory.results[id];
    if (!r) return;
    this.setResult(id, { ...r, observed: yes, status: yes ? "pass" : "fail" });
  }

  onPhase(data) {
    this.phase = data;
    if (data.kind === "handback" && data.phase === "paused") {
      this.paused = performance.now();
      clearInterval(this.tick);
      this.tick = setInterval(() => this.renderStep("handback"), 100);
    }
    if (data.phase === "done") {
      clearInterval(this.tick);
      this.paused = null;
    }
    this.renderStep(data.kind === "bench" ? "latency" : data.kind);
  }

  // ------------------------------------------------------------ rendering

  renderAll() {
    for (const id of STEP_IDS) this.renderStep(id);
  }

  renderStep(id) {
    const s = this.steps[id];
    if (!s) return;
    const r = memory.results[id];
    const status = this.running === id ? "running" : r?.status || "todo";
    const [cls, label] = CHIPS[status];
    s.chip.className = `chip ${cls}`.trim();
    setText(s.chip, label());
    const parts = [];
    if (this.running === id) parts.push(this.runningNote(id));
    else if (r) parts.push(...this.resultParts(id, r));
    s.body.replaceChildren(...parts.filter(Boolean));
    toggle(s.body, "hidden", !parts.length);
  }

  runningNote(id) {
    if (id === "handback" && this.paused != null) {
      const s = ((performance.now() - this.paused) / 1000).toFixed(1);
      return h("p.hw-timer", { text: t("hw.handback.timer", { s }) });
    }
    return h("p.note", { text: t(id === "latency" ? "hw.latency.running" : "hw.running") });
  }

  question(id, text) {
    return h("div.hw-ask", {}, h("p", { text }),
      h("button.btn.go", { type: "button", onclick: () => this.answer(id, true) }, t("hw.yes")),
      h("button.btn", { type: "button", onclick: () => this.answer(id, false) }, t("hw.no")));
  }

  resultParts(id, r) {
    if (r.status === "refused") return [h("p.note", { text: t("hw.refused", { error: r.error }) })];
    if (id === "link_check") return this.linkParts(r);
    if (id === "latency") return this.latencyParts(r);
    const out = [];
    if (r.aborted) out.push(h("p.note", { text: t(`hw.aborted.${r.aborted}`) }));
    else if (!r.acked) out.push(h("p.note", { text: t("hw.not_acked", { gate: r.gate ? t(`gate.${r.gate}`) : "—" }) }));
    else if (id === "handback") {
      out.push(h("p.note", { text: t("hw.handback.done", { throttle: r.throttle, ms: r.paused_ms }) }));
    } else {
      out.push(h("p.note", { text: t("hw.move.done", { throttle: r.applied_peak, ms: r.held_ms }) }));
    }
    if (r.status === "wait") out.push(this.question(id, t(`hw.ask.${id}`)));
    else if (r.observed != null) out.push(h("p.note", { text: r.observed ? t("hw.answered_yes") : t("hw.answered_no") }));
    return out;
  }

  linkParts(r) {
    const rows = [
      [t("sys.kv.transport"), r.transport ?? "—"], [t("sys.kv.cu_fw"), r.version ? `v${r.version}` : "—"],
      [t("hw.link.bot_transport"), r.bot_transport ?? t("hw.link.none")], [t("hw.link.bots_supported"), String(r.bots_supported)],
    ];
    return [
      h("dl.kv-list", {}, ...rows.map(([k, v]) => h("div.kv", {}, h("dt", { text: k }), h("dd", { text: v })))),
      h("p.note", { text: r.explanation })];
  }

  latencyParts(r) {
    const cell = (label, v) => h("div.hw-stat", {}, h("span", { text: label }), h("b", { text: v == null ? "—" : `${v.toFixed ? v.toFixed(1) : v} ms` }));
    const stats = [cell("min", r.min_ms), cell(t("hw.median"), r.median_ms), cell("p95", r.p95_ms), cell("max", r.max_ms), cell(t("hw.jitter"), r.jitter_ms)];
    const out = [h("div.hw-stats", {}, ...stats), h("p.note", { text: t("hw.latency.counts", { answered: r.answered, n: r.n, lost: r.lost }) })];
    if (r.timed_out) out.push(h("p.note", { text: t("hw.latency.timed_out") }));
    out.push(h("p.note", { text: r.c_ack
      ? t("hw.latency.c_ack", { median: r.c_ack.median_ms, p95: r.c_ack.p95_ms, max: r.c_ack.max_ms, n: r.c_ack.n })
      : t("hw.latency.no_c_ack") }));
    if (r.histogram?.length) out.push(h("pre.hw-hist", { text: histText(r.histogram), "aria-label": t("hw.histogram") }));
    if (r.verdict) out.push(h("p.note", { text: t(`hw.latency.${r.verdict}`, { p95: LATENCY_P95_MS, max: LATENCY_MAX_MS }) }));
    return out;
  }

  // ------------------------------------------------------------ report

  buildReport() {
    const l = this.state?.link || {};
    const steps = {};
    for (const id of STEP_IDS) steps[id] = memory.results[id] ?? { status: "not_run" };
    return {
      format: 1, kind: "racelab-hardware-test", created: now(), language: lang,
      racelab: store.meta?.version ?? null, settings: { ...memory.settings }, notes: memory.notes.trim(),
      link: Object.fromEntries(["state", "transport", "device", "version", "bots_supported", "bot_transport", "bot_module", "rtt_ms"].map((k) => [k, l[k] ?? null])),
      steps,
    };
  }

  async copyReport() {
    const report = this.buildReport();
    let text = JSON.stringify(report, null, 2);
    this.copyBtn.disabled = true;
    try {
      const saved = await request("test.report", { report });
      text = JSON.stringify(saved.report, null, 2);
      memory.saved = saved.path;
    } catch (e) {
      memory.saved = null;
      toast(t("hw.save_failed", { error: e.message }), "error", 5000);
    }
    await copyText(text, this.copyBtn);
    this.copyBtn.disabled = false;
    setText(this.saved, memory.saved ? t("hw.saved", { path: memory.saved }) : "");
  }
}
