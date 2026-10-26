// Shell: router, top bar, keyboard shortcuts.
import { h, setText, toggle } from "./dom.js";
import { t, translateStatic } from "./i18n.js";
import { botsUnavailable, connect, onEvent, onState, refreshMeta, request, store } from "./store.js";
import { estopButton, noteEstop, run, toast } from "./ui/components.js";
import { RaceView } from "./views/race.js";
import { SetupView } from "./views/setup.js";
import { HistoryView } from "./views/history.js";
import { SystemView } from "./views/system.js";
import { SettingsView } from "./views/settings.js";
import { hub } from "./input.js";
import { ScreenView } from "./views/screen.js";

// `ecran` is the former (French) name of the big-screen route, kept for old bookmarks
const ROUTES = { race: RaceView, setup: SetupView, history: HistoryView, settings: SettingsView, system: SystemView, screen: ScreenView, ecran: ScreenView };
const viewRoot = document.getElementById("view");
translateStatic(); // static texts of index.html
let current = null;
let currentName = null;

function route() {
  const [name, ...rest] = (location.hash.replace(/^#\/?/, "") || "race").split("/");
  const View = ROUTES[name] || RaceView;
  const key = ROUTES[name] ? name : "race";
  document.querySelectorAll("#tabs a").forEach((a) => {
    toggle(a, "active", a.dataset.route === key);
    if (a.dataset.route === key) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  // phone: the tab strip scrolls sideways; after this turn, once the top bar is shown again (hidden on the big screen)
  setTimeout(() => document.querySelector("#tabs a.active")?.scrollIntoView?.({ inline: "center", block: "nearest" }), 0);
  if (currentName === key && current?.route) {
    current.route(rest);
    return;
  }
  current?.destroy?.();
  viewRoot.replaceChildren();
  currentName = key;
  current = new View(viewRoot, rest);
  if (store.state) current.update?.(store.state);
  window.scrollTo(0, 0);
}

// ------------------------------------------------------------ top bar

const pill = document.getElementById("link-pill");
const pillDot = pill.querySelector(".dot");
const pillLabel = pill.querySelector(".label");
const botsBadge = pill.querySelector(".bots-badge");
const modeSwitch = document.getElementById("mode-switch");
const estopBtn = document.getElementById("estop");
let offlineBanner = null;
const fwBanner = h("a.fw-banner.hidden", { href: "#/system" });
document.body.append(fwBanner);

function updateTopbar(state) {
  if (!store.online || !state) {
    pillDot.className = "dot bad";
    setText(pillLabel, t("topbar.server_down"));
    pill.setAttribute("aria-label", pillLabel.textContent);
    if (!offlineBanner) {
      offlineBanner = h("div.offline", { text: t("topbar.offline_banner") });
      document.body.append(offlineBanner);
    }
    return;
  }
  offlineBanner?.remove();
  offlineBanner = null;

  const link = state.link;
  const transport = {
    ble: t("topbar.transport.ble"), serial: t("topbar.transport.serial"), sim: t("topbar.transport.sim"),
  }[link.transport] || link.transport;
  if (link.transport === "none") {
    pillDot.className = "dot";
    setText(pillLabel, t("topbar.no_cu"));
  } else if (link.state === "connected") {
    pillDot.className = `dot ${link.bots_supported === false && state.mode === "advanced" ? "warn" : "ok"}`;
    const suffix = link.bots_supported ? t("topbar.suffix_bots") : link.bots_supported === false ? t("topbar.suffix_stock") : "";
    setText(pillLabel, t("topbar.cu", { transport, version: link.version ?? "?", suffix }));
  } else {
    pillDot.className = `dot ${link.state === "lost" ? "bad" : "warn"}`; // updating / connecting: orange
    setText(pillLabel, { lost: t("topbar.lost"), updating: t("topbar.updating") }[link.state] || t("topbar.searching"));
  }
  pill.setAttribute("aria-label", pillLabel.textContent); // the label is hidden on narrow screens
  pill.title = link.error ? t("topbar.last_error", { error: link.error }) : t("topbar.link_title");
  pill.href = link.transport === "none" ? "#/race" : "#/system";

  // single mode selector: "with bots" only if the CU can take software drivers
  const busy = !["idle", "grid", "finished"].includes(state.session?.state);
  const why = botsUnavailable(state);
  modeSwitch.querySelectorAll("button").forEach((b) => {
    toggle(b, "on", b.dataset.mode === state.mode);
    b.setAttribute("aria-pressed", b.dataset.mode === state.mode ? "true" : "false");
    const advanced = b.dataset.mode === "advanced";
    b.disabled = busy || (advanced && !!why && state.mode !== "advanced");
    b.title = busy ? t("mode.busy")
      : advanced
        ? (why ? t("mode.advanced_unavailable", { why }) : t("mode.advanced_hint"))
        : t("mode.classic_hint");
  });

  // firmware update, then a new RaceLab version: banner on every screen (System has both)
  const fw = state.firmware || {};
  const flashing = fw.state === "running" || fw.state === "verifying";
  const newVersion = !!state.update?.available;
  toggle(fwBanner, "hidden", !flashing && !newVersion);
  toggle(document.body, "fw-on", flashing || newVersion);
  if (flashing) {
    setText(fwBanner, fw.state === "verifying"
      ? t("topbar.fw_verifying")
      : t("topbar.fw_progress", { done: fw.done, total: fw.total }));
  } else if (newVersion) {
    setText(fwBanner, t("topbar.update_available"));
  }

  const advanced = state.mode === "advanced";
  toggle(botsBadge, "hidden", !advanced);
  toggle(estopBtn, "hidden", !advanced || link.transport === "none");
  const live = (state.bots || []).some((b) => b.active);
  toggle(estopBtn, "live", live && !state.estop);
  toggle(estopBtn, "engaged", !!state.estop);
  noteEstop(!!state.estop);
  estopBtn.setAttribute("aria-pressed", state.estop ? "true" : "false");
  setText(estopBtn, state.estop ? t("estop.engaged") : t("estop.stop"));
  estopBtn.title = state.estop ? t("estop.title_engaged") : t("estop.title");
}

modeSwitch.addEventListener("click", (ev) => {
  const mode = ev.target.closest("button")?.dataset.mode;
  if (mode && mode !== store.state?.mode) {
    run("mode.set", { mode }, mode === "advanced" ? t("mode.set_advanced") : t("mode.set_classic")).catch(() => {});
  }
});

estopBtn.addEventListener("click", () => estopButton()); // engage at once; release asks first (components.js)

// Escape = emergency stop, ALWAYS: under a drawer or a dialog (which also close), in a text field,
// in a select. Engage only: releasing is a deliberate click on STOP. If it is already on, say so.
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape" || store.state?.mode !== "advanced") return;
  if (!store.state.estop) run("estop").catch(() => {});
  else if (!ev.repeat) toast(t("estop.already"), "stop");
});

// Space = start / pause / resume the session, on the race page and on the big screen. Not on a
// control (Space presses it) nor while typing; a press right after a change is a double tap.
const SPACE_OPS = { grid: "race.start", running: "race.pause", paused: "race.resume" };
const SPACE_QUIET_MS = 2000;
let sessionState = null;
let sessionChangedAt = -Infinity;
document.addEventListener("keydown", (ev) => {
  if (ev.key !== " " || ev.repeat || !(current instanceof RaceView || current instanceof ScreenView)) return;
  if (ev.target.closest("input, textarea, select, button, a, summary, [tabindex], .fader") || document.querySelector(".drawer, .modal")) return;
  const op = SPACE_OPS[store.state?.session.state];
  if (!op) return;
  ev.preventDefault();
  if (performance.now() - sessionChangedAt >= SPACE_QUIET_MS) run(op).catch(() => {});
});

// external controllers: keyboard / gamepad drive a slot on the manual path
// (advanced mode: the bot commands are what actually moves the car)
hub.setSinks({
  throttle: (slot, value) => { if (store.state?.mode === "advanced") request("bot.throttle", { slot, value }).catch(() => {}); },
  lane: (slot) => { if (store.state?.mode === "advanced") request("bot.lane", { slot }).catch(() => {}); },
  estop: () => { if (store.state?.mode === "advanced") run("estop").catch(() => {}); },
  status: (key) => toast(t(key), "warn"),
});
hub.start();

// ------------------------------------------------------------ startup

onState((state) => {
  const sess = state?.session.state;
  if (sess && sess !== sessionState) {
    if (sessionState) sessionChangedAt = performance.now();
    sessionState = sess;
  }
  updateTopbar(state);
  if (state) hub.setPaused(!!state.estop);
  if (state) current?.update?.(state);
});
onEvent("hello", () => current?.hello?.());
onEvent("connection", async () => {
  try { await refreshMeta(); } catch { /* reconnection in progress */ }
  current?.hello?.();
});
onEvent("offline", () => toast(t("toast.offline"), "error"));
onEvent("false_start", () => toast(t("state.false_start"), "error", 5000));
window.addEventListener("hashchange", route);

// shutdown requested from System: end screen, no more reconnecting
export function showQuitScreen() {
  store.quit = true;
  document.body.append(h("div.quit-screen", {},
    h("div", {}, h("h2", { text: t("quit.title") }),
      h("p", { text: t("quit.text") }))));
}
window.racelabQuit = showQuitScreen;

route();
connect();
