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

export function connect() {
  const params = new URLSearchParams(location.search);
  let token = params.get("token") || "";
  try {
    if (token) localStorage.setItem("racelab.token", token);
    else token = localStorage.getItem("racelab.token") || "";
  } catch { /* storage unavailable (private browsing) */ }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams({ state_hz: "10" });
  if (token) q.set("token", token);
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
        msg.ok ? p.resolve(msg.result) : p.reject(new Error(msg.error || t("err.generic")));
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
export const isAdvanced = () => store.state?.mode === "advanced";
export const driverOf = (slot) => store.meta?.drivers?.find((d) => d.slot === slot);
// Labels the server also sends: the dictionary wins, the server text is the fallback.
export const sessionLabel = (type) => tOr(`session.${type}.label`, store.meta?.session_types?.[type]?.label) || type || t("session.fallback");
export const sessionDescription = (type) => tOr(`session.${type}.description`, store.meta?.session_types?.[type]?.description) || "";
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
