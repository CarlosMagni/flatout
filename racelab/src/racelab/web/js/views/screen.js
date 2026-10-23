// Big screen: readable from several meters away (TV or laptop next to the track).
// Everything is sized on the screen height; F = fullscreen.
import { STATE_LABEL, clock, gap, h, inkOn, lapTime, setText, toggle } from "../dom.js";
import { t } from "../i18n.js";
import { onEvent, sessionLabel, store } from "../store.js";
import { estopButton, run } from "../ui/components.js";

const LAP_TYPES = new Set(["race", "rival"]);
const TIMED = new Set(["endurance", "qualifying"]);
const MAX_ROWS = 8;

export class ScreenView {
  constructor(root) {
    this.root = root;
    this.rows = new Map();
    this.lastLaps = new Map();
    document.body.classList.add("bigscreen");

    this.title = h("div.bs-title");
    this.counter = h("div.bs-counter");
    this.counterLabel = h("div.bs-label");
    this.clock = h("div.bs-clock");
    this.state = h("div.bs-state");
    this.lightsSmall = this.makeLights("bs-lights-small");
    this.header = h("header.bs-head", {},
      h("div.bs-head-left", {}, this.title, this.state),
      h("div.bs-head-center", {}, this.counter, this.counterLabel),
      h("div.bs-head-right", {}, this.lightsSmall.el, this.clock));

    // column labels share the row grid (see .bs-row / .bs-cols in style.css)
    this.cols = h("div.bs-cols", { "aria-hidden": "true" },
      h("div.c", { text: t("race.col.pos") }), h("div"), h("div", { text: t("race.col.driver") }),
      h("div.r", { text: t("race.col.laps") }), h("div.r", { text: t("race.col.last") }), h("div.r", { text: t("race.col.gap") }),
      this.fuelCol = h("div.r", { text: t("race.col.fuel") }));
    this.list = h("div.bs-list");
    this.board = h("div.bs-board", {}, this.cols, this.list);
    this.empty = h("div.bs-empty.hidden", {}, h("div", { text: t("screen.waiting") }),
      h("div.bs-empty-sub", { text: t("screen.waiting_sub") }));

    // big lights in the center during the start procedure
    this.lightsBig = this.makeLights("bs-lights-big");
    this.banner = h("div.bs-banner.hidden");
    this.overlay = h("div.bs-overlay.hidden", {}, this.lightsBig.el, this.banner);
    this.podium = h("div.bs-podium.hidden");

    // STOP on the big screen: a small pill that stays visible while a bot drives (engage only: a stray
    // click can never release it), and a button with the controls (same op as the top bar: engage or release)
    this.stopPill = h("button.bs-stop.hidden", { type: "button", onclick: () => this.engageStop() });
    this.stopBtn = h("button.btn.danger.hidden", { type: "button", onclick: () => this.toggleStop() });
    this.exit = h("div.bs-controls", {}, this.stopBtn,
      h("button.btn", { onclick: () => this.fullscreen() }, t("screen.fullscreen")),
      h("a.btn", { href: "#/race" }, t("screen.exit")));
    this.el = h("div.bs", {}, this.header, this.board, this.empty, this.podium, this.overlay, this.stopPill, this.exit);
    root.append(this.el);

    // the buttons only show up when the mouse moves
    this.onMove = () => {
      this.el.classList.add("show-controls");
      clearTimeout(this.hideTimer);
      this.hideTimer = setTimeout(() => this.el.classList.remove("show-controls"), 2500);
    };
    this.onKey = (ev) => {
      if ((ev.key === "f" || ev.key === "F") && !ev.target.closest("input, textarea")) this.fullscreen();
    };
    document.addEventListener("mousemove", this.onMove);
    document.addEventListener("keydown", this.onKey);
    this.offEvent = onEvent("lap", (d) => this.flash(d.slot));
  }

  destroy() {
    document.body.classList.remove("bigscreen");
    document.removeEventListener("mousemove", this.onMove);
    document.removeEventListener("keydown", this.onKey);
    clearTimeout(this.hideTimer);
    this.offEvent();
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  }

  engageStop() {
    if (store.state?.mode === "advanced" && !store.state.estop) run("estop").catch(() => {});
  }

  toggleStop() {
    estopButton(); // engage at once; release asks first, never right after engaging
  }

  updateStop(state) {
    const advanced = state.mode === "advanced" && state.link.transport !== "none";
    const live = (state.bots || []).some((b) => b.active);
    const engaged = !!state.estop;
    toggle(this.stopPill, "hidden", !advanced || !(live || engaged));
    toggle(this.stopPill, "engaged", engaged);
    setText(this.stopPill, engaged ? t("estop.engaged") : t("estop.stop"));
    this.stopPill.title = engaged ? t("estop.title_engaged") : t("estop.title");
    this.stopPill.setAttribute("aria-pressed", engaged ? "true" : "false");
    this.stopPill.disabled = engaged; // released from the controls button (or the top bar), never by a stray click here
    toggle(this.stopBtn, "hidden", !advanced);
    setText(this.stopBtn, engaged ? t("hw.release_stop") : t("estop.stop"));
    this.stopBtn.setAttribute("aria-pressed", engaged ? "true" : "false");
  }

  fullscreen() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else document.documentElement.requestFullscreen?.().catch(() => {});
  }

  makeLights(cls) {
    const leds = Array.from({ length: 5 }, () => h("i"));
    return { el: h(`div.${cls}`, {}, leds), leds };
  }

  paintLights(lights, cu) {
    const go = cu.phase === 7;
    lights.leds.forEach((led, i) => toggle(led, "on", go || i < cu.lights));
    toggle(lights.el, "go", go);
    toggle(lights.el, "blink", cu.blink);
  }

  update(state) {
    const sess = state.session;
    this.updateStop(state);
    setText(this.title, sess.state === "idle" ? "RaceLab" : sess.title || (sess.type ? sessionLabel(sess.type) : ""));
    setText(this.state, sess.false_start ? t("state.false_start") : STATE_LABEL[sess.state] || "");
    this.state.className = `bs-state ${sess.false_start ? "stop" : sess.state}`;

    // main counter: leader lap, remaining time or elapsed time
    if (LAP_TYPES.has(sess.type) && sess.laps) {
      const lap = Math.min(Math.max((sess.leader_lap || 0) + (sess.state === "finished" ? 0 : 1), 1), sess.laps);
      this.counter.replaceChildren(String(lap), h("span", { text: `/${sess.laps}` }));
      setText(this.counterLabel, t("screen.lap"));
    } else if (TIMED.has(sess.type) || sess.duration_s) {
      setText(this.counter, clock(sess.remaining_s ?? sess.duration_s));
      setText(this.counterLabel, t("screen.remaining"));
    } else if (sess.state !== "idle") {
      setText(this.counter, String((sess.leader_lap || 0)));
      setText(this.counterLabel, t("screen.laps"));
    } else {
      setText(this.counter, "");
      setText(this.counterLabel, "");
    }
    setText(this.clock, sess.state === "idle" ? "" : clock(sess.elapsed_s));
    this.paintLights(this.lightsSmall, state.cu);

    // start procedure, false start, checkered flag: shown big in the center. A finished (or reset)
    // session leaves the CU armed on purpose (five lights hold the cars): not a start, the podium stays
    const starting = sess.state === "starting"
      || (state.cu.phase >= 1 && state.cu.phase <= 7 && !["running", "finished", "idle"].includes(sess.state));
    let banner = "", bannerKind = "";
    if (sess.false_start || state.cu.phase >= 8) { banner = t("state.false_start"); bannerKind = "stop"; }
    else if (sess.state === "finishing") { banner = t("state.finishing"); bannerKind = "checkered"; }
    else if (sess.state === "paused") { banner = t("state.paused"); bannerKind = "caution"; }
    banner = banner.toUpperCase();
    toggle(this.overlay, "hidden", !starting && !banner);
    toggle(this.lightsBig.el, "hidden", !starting);
    this.paintLights(this.lightsBig, state.cu);
    toggle(this.banner, "hidden", !banner);
    setText(this.banner, banner);
    this.banner.className = `bs-banner ${bannerKind || "stop"}${banner ? "" : " hidden"}`;

    const cars = state.cars.filter((c) => c.enabled || c.laps > 0).slice(0, MAX_ROWS);
    const idle = sess.state === "idle";
    toggle(this.empty, "hidden", !idle);
    toggle(this.board, "hidden", idle || sess.state === "finished");
    this.renderPodium(state, sess.state === "finished");
    if (idle) return;
    this.board.style.setProperty("--n", Math.max(cars.length, 4));
    const fuel = state.cu.pit_lane || state.cu.fuel_mode;
    toggle(this.fuelCol, "invisible", !fuel);
    const order = cars.map((car) => this.fillRow(car, sess, fuel));
    for (const [slot, row] of this.rows) {
      if (!cars.some((c) => c.slot === slot)) { row.el.remove(); this.rows.delete(slot); }
    }
    const current = [...this.list.children];
    if (current.length !== order.length || current.some((el, i) => el !== order[i])) {
      const before = new Map(current.map((el) => [el, el.getBoundingClientRect().top]));
      this.list.replaceChildren(...order);
      for (const el of order) {
        const top = before.get(el);
        const dy = top == null ? 0 : top - el.getBoundingClientRect().top;
        if (dy) el.animate([{ transform: `translateY(${dy}px)` }, { transform: "none" }], { duration: 500, easing: "cubic-bezier(.2,.7,.2,1)" });
      }
    }
  }

  fillRow(car, sess, fuelShown) {
    let r = this.rows.get(car.slot);
    if (!r) {
      r = {
        pos: h("div.bs-pos"), no: h("div.bs-no"), name: h("div.bs-name"), tag: h("div.bs-tag"),
        laps: h("div.bs-laps"), last: h("div.bs-last"), gap: h("div.bs-gap"),
        fuel: h("div.bs-fuel", {}, Array.from({ length: 15 }, () => h("i"))),
      };
      r.el = h("div.bs-row", {}, r.pos, r.no, h("div.bs-who", {}, r.name, r.tag), r.laps, r.last, r.gap, r.fuel);
      this.rows.set(car.slot, r);
    }
    r.el.style.setProperty("--car", car.color);
    r.no.style.color = inkOn(car.color);
    setText(r.pos, car.position ?? "–");
    setText(r.no, car.slot + 1);
    setText(r.name, car.name);
    const tag = car.eliminated ? t("car.eliminated") : car.finished ? t("car.finished") : car.pit ? t("car.pit")
      : car.controller === "api" ? t("bot.driver.api") : car.controller === "bot" ? t("bot.tag") : "";
    setText(r.tag, tag.toUpperCase());
    r.tag.className = `bs-tag ${car.eliminated ? "stop" : car.pit ? "caution" : car.finished ? "done" : ""}`;
    toggle(r.el, "out", car.eliminated);
    setText(r.laps, car.laps);
    setText(r.last, lapTime(car.last_ms));
    toggle(r.last, "sb", car.last_sb);
    toggle(r.last, "pb", car.last_pb && !car.last_sb);
    const best = sess.type === "practice" || sess.type === "qualifying" || sess.type === "training";
    setText(r.gap, car.position === 1 ? (best ? lapTime(car.best_ms) : "") : gap(car.gap_ms, car.gap_laps));
    toggle(r.fuel, "hidden", !fuelShown || car.fuel == null);
    const v = car.fuel ?? 0;
    [...r.fuel.children].forEach((seg, i) => toggle(seg, "on", i < v));
    toggle(r.fuel, "low", v <= 5);
    toggle(r.fuel, "crit", v <= 3);
    return r.el;
  }

  flash(slot) {
    const r = this.rows.get(slot);
    if (!r) return;
    r.el.classList.remove("flash");
    void r.el.offsetWidth;
    r.el.classList.add("flash");
  }

  renderPodium(state, finished) {
    toggle(this.podium, "hidden", !finished);
    if (!finished) { this.podiumSig = null; return; }
    const top = state.cars.filter((c) => c.position).sort((a, b) => a.position - b.position).slice(0, 3);
    const sig = top.map((c) => `${c.slot}:${c.position}`).join("|");
    if (sig === this.podiumSig) return;
    this.podiumSig = sig;
    const steps = [top[1], top[0], top[2]].map((car, i) => car ? h(`div.bs-step.p${[2, 1, 3][i]}`, {},
      h("div.bs-step-no", { text: car.slot + 1, style: { background: car.color, color: inkOn(car.color) } }),
      h("div.bs-step-name", { text: car.name }),
      h("div.bs-step-best", { text: car.best_ms ? t("screen.best", { time: lapTime(car.best_ms) }) : "" }),
      h("div.bs-step-block", { text: [2, 1, 3][i] })) : h("div"));
    this.podium.replaceChildren(h("div.bs-podium-title", { text: t("screen.podium") }), h("div.bs-steps", {}, steps));
  }
}
