// Shell: router, top bar, keyboard shortcuts.
import { h, setText, toggle } from "./dom.js";
import { t, translateStatic } from "./i18n.js";
import { botsUnavailable, connect, onEvent, onState, refreshMeta, store } from "./store.js";
import { run, toast } from "./ui/components.js";
import { RaceView } from "./views/race.js";
import { SetupView } from "./views/setup.js";
import { HistoryView } from "./views/history.js";
import { SystemView } from "./views/system.js";
import { ScreenView } from "./views/screen.js";

// `ecran` is the former (French) name of the big-screen route, kept for old bookmarks
const ROUTES = { race: RaceView, setup: SetupView, history: HistoryView, system: SystemView, screen: ScreenView, ecran: ScreenView };
const viewRoot = document.getElementById("view");
translateStatic(); // static texts of index.html
let current = null;
let currentName = null;

function route() {
  const [name, ...rest] = (location.hash.replace(/^#\/?/, "") || "race").split("/");
  const View = ROUTES[name] || RaceView;
  const key = ROUTES[name] ? name : "race";
  document.querySelectorAll("#tabs a").forEach((a) => toggle(a, "active", a.dataset.route === key));
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
const modeSwitch = document.getElementById("mode-switch");
const estopBtn = document.getElementById("estop");
let offlineBanner = null;
const fwBanner = h("a.fw-banner.hidden", { href: "#/system" });
document.body.append(fwBanner);

function updateTopbar(state) {
  if (!store.online || !state) {
    pillDot.className = "dot bad";
    setText(pillLabel, t("topbar.server_down"));
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
  pill.title = link.error ? t("topbar.last_error", { error: link.error }) : t("topbar.link_title");
  pill.href = link.transport === "none" ? "#/race" : "#/system";

  // single mode selector: "with bots" only if the CU can take software drivers
  const busy = !["idle", "grid", "finished"].includes(state.session?.state);
  const why = botsUnavailable(state);
  modeSwitch.querySelectorAll("button").forEach((b) => {
    toggle(b, "on", b.dataset.mode === state.mode);
    const advanced = b.dataset.mode === "advanced";
    b.disabled = busy || (advanced && !!why && state.mode !== "advanced");
    b.title = busy ? t("mode.busy")
      : advanced
        ? (why ? t("mode.advanced_unavailable", { why }) : t("mode.advanced_hint"))
        : t("mode.classic_hint");
  });

  // firmware update: banner on every screen until it is finished
  const fw = state.firmware || {};
  const flashing = fw.state === "running" || fw.state === "verifying";
  toggle(fwBanner, "hidden", !flashing);
  toggle(document.body, "fw-on", flashing);
  if (flashing) {
    setText(fwBanner, fw.state === "verifying"
      ? t("topbar.fw_verifying")
      : t("topbar.fw_progress", { done: fw.done, total: fw.total }));
  }

  const advanced = state.mode === "advanced";
  toggle(estopBtn, "hidden", !advanced || link.transport === "none");
  const live = (state.bots || []).some((b) => b.active);
  toggle(estopBtn, "live", live && !state.estop);
  toggle(estopBtn, "engaged", !!state.estop);
  setText(estopBtn, state.estop ? t("estop.engaged") : t("estop.stop"));
  estopBtn.title = state.estop ? t("estop.title_engaged") : t("estop.title");
}

modeSwitch.addEventListener("click", (ev) => {
  const mode = ev.target.closest("button")?.dataset.mode;
  if (mode && mode !== store.state?.mode) {
    run("mode.set", { mode }, mode === "advanced" ? t("mode.set_advanced") : t("mode.set_classic")).catch(() => {});
  }
});

function estop() {
  if (!store.state || store.state.mode !== "advanced") return;
  if (store.state.estop) run("estop.clear").catch(() => {});
  else run("estop").catch(() => {});
}
estopBtn.addEventListener("click", estop);

// Escape = emergency stop (never the release: releasing is a deliberate mouse click).
// Not while typing text, where Escape means cancel.
document.addEventListener("keydown", (ev) => {
  const typing = ev.target.closest?.("input[type=text], input:not([type]), textarea, select");
  if (ev.key === "Escape" && !typing && !document.querySelector(".drawer, .modal") && store.state?.mode === "advanced" && !store.state.estop) {
    run("estop").catch(() => {});
  }
});

// ------------------------------------------------------------ startup

onState((state) => {
  updateTopbar(state);
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
