// WebSocket connection to the RaceLab server + shared state.
// Same protocol as the RL agents (see docs/api.md).
import { driverName, t, tOr } from "./i18n.js";

const listeners = new Set();
const eventListeners = new Map(); // name -> Set(fn)
const pending = new Map();
let ws = null;
let nextId = 1;
let retry = 0;

export const store = {
  state: null,
  meta: null,
  log: [],
  online: false,
  client: null,
  quit: false,
};

export function onState(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function onEvent(name, fn) {
  if (!eventListeners.has(name)) eventListeners.set(name, new Set());
  eventListeners.get(name).add(fn);
  return () => eventListeners.get(name).delete(fn);
}

function emit(name, data, time) {
  for (const key of [name, "*"]) {
    for (const fn of eventListeners.get(key) || []) {
      try { fn(data, name, time); } catch (e) { console.error(e); }
    }
  }
}

function notify() {
  for (const fn of listeners) {
    try { fn(store.state); } catch (e) { console.error(e); }
  }
}

// ------------------------------------------------------------ browser storage + API token

// localStorage as JSON. The storage can be unavailable (private browsing): reads then give the
// fallback and writes are lost. `writeJson(key, null)` forgets the key.
export function readJson(key, fallback = null) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
export function writeJson(key, value) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* storage unavailable */ }
}

// Optional API token: a `?token=` in the URL is remembered for the next loads. The only place it
// is read; REST calls send it in a header (authHeaders), never in the URL.
let apiToken = null;
function token() {
  if (apiToken == null) {
    apiToken = new URLSearchParams(location.search).get("token") || "";
    try {
      if (apiToken) localStorage.setItem("racelab.token", apiToken);
      else apiToken = localStorage.getItem("racelab.token") || "";
    } catch { /* storage unavailable (private browsing) */ }
  }
  return apiToken;
}
export const authHeaders = () => (token() ? { Authorization: `Bearer ${token()}` } : {});

export function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams({ state_hz: "10" });
  if (token()) q.set("token", token());
  ws = new WebSocket(`${proto}://${location.host}/ws?${q}`);

  ws.onopen = () => { retry = 0; };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case "hello":
        store.client = msg.client;
        store.meta = localizeMeta(msg.meta);
        store.state = localizeState(msg.state);
        store.log = msg.log || [];
        store.online = true;
        emit("hello", msg);
        notify();
        break;
      case "state":
        store.state = localizeState(msg.data);
        notify();
        break;
      case "event":
        if (msg.name === "log") store.log = [...store.log.slice(-199), msg.data];
        emit(msg.name, msg.data, msg.t);
        break;
      case "raw":
        emit("raw", msg, msg.t);
        break;
      case "reply": {
        const p = pending.get(msg.id);
        if (!p) break;
        pending.delete(msg.id);
        clearTimeout(p.timer);
        // `code` + `params`: the reason of a refusal, for the page that words it (docs/api.md)
        msg.ok ? p.resolve(msg.result) : p.reject(Object.assign(new Error(msg.error || t("err.generic")), { code: msg.code, params: msg.params }));
        break;
      }
    }
  };
  ws.onclose = () => {
    if (store.quit) return; // RaceLab was stopped on purpose: do not reconnect
    const wasOnline = store.online;
    store.online = false;
    for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error(t("err.unreachable"))); }
    pending.clear();
    if (wasOnline) emit("offline", {});
    notify();
    retry = Math.min(retry + 1, 6);
    setTimeout(connect, 300 * retry);
  };
}

export function request(op, args = {}, timeout = 4000) {
  return new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      reject(new Error(t("err.unreachable")));
      return;
    }
    const id = nextId++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(t("err.no_reply")));
    }, timeout);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, op, ...args }));
  });
}

export function subscribe(args) {
  return request("subscribe", args);
}

// read helpers
export const isAdvanced = () => (store.state?.mode ?? store.meta?.mode) === "advanced";
export const driverOf = (slot) => store.meta?.drivers?.find((d) => d.slot === slot);
// Labels the server also sends: the dictionary wins, the server text is the fallback.
export const sessionLabel = (type) => tOr(`session.${type}.label`, store.meta?.session_types?.[type]?.label) || type || t("session.fallback");
export const sessionDescription = (type) => tOr(`session.${type}.description`, store.meta?.session_types?.[type]?.description) || "";
// Session-type facts sent by the server (modes.py SESSION_TYPES): the views never name a type.
export const sessionInfo = (type) => store.meta?.session_types?.[type];
// ranked by fastest lap (practice, qualifying, training) rather than by laps then time
export const rankedByBestLap = (type) => sessionInfo(type)?.ranking === "best_lap";

// The main counter of a session, the same for every view (they only choose the words):
//   idle       no session
//   lap        the lap the leader is on, out of `of` laps (null when there is no lap target:
//              elimination). Completed laps + 1 while racing, the completed count once finished:
//              the same convention for every lap-counted type, and an elimination happens at the
//              end of the lap shown
//   remaining  `seconds` left of a timed session
//   elapsed    `seconds` since the start (free practice, training: nothing to count down to)
export function sessionCounter(sess) {
  if (sess.state === "idle") return { kind: "idle" };
  if (sess.laps || sessionInfo(sess.type)?.ends_on === "last_car") {
    const lap = (sess.leader_lap || 0) + (sess.state === "finished" ? 0 : 1);
    return { kind: "lap", lap: Math.max(1, sess.laps ? Math.min(lap, sess.laps) : lap), of: sess.laps || null };
  }
  if (sess.duration_s != null) return { kind: "remaining", seconds: sess.remaining_s ?? sess.duration_s };
  return { kind: "elapsed", seconds: sess.elapsed_s };
}
// why bots cannot be turned on right now (null = they can), in the UI language;
// the server only says whether and roughly why (advanced_unavailable)
export function botsUnavailable(state) {
  if (!state?.advanced_unavailable) return null;
  const link = state.link || {};
  if (link.transport === "none") return t("mode.why.no_cu");
  if (link.bots_supported === false) return t("mode.why.old_cu", { version: link.version ?? "?" });
  return t("mode.why.wait");
}
export const profileLabel = (kind) => tOr(`profile.${kind}.label`, store.meta?.profiles?.[kind]?.label) || kind;

function localizeState(state) {
  for (const car of state?.cars || []) car.name = driverName(car.name, car.slot);
  return state;
}

function localizeMeta(meta) {
  for (const d of meta?.drivers || []) d.name = driverName(d.name, d.slot);
  return meta;
}

export async function refreshMeta() {
  store.meta = localizeMeta(await request("meta"));
  notify();
  return store.meta;
}
