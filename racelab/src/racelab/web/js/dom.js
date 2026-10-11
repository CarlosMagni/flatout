// Small DOM helpers + formats. No framework: views build their DOM once and
// then only patch what changes.
import { locale, t } from "./i18n.js";

export function h(tag, props = {}, ...children) {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name || "div");
  if (classes.length) el.className = classes.join(" ");
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className += (el.className ? " " : "") + v;
    else if (k === "style" && typeof v === "object") {
      for (const [prop, val] of Object.entries(v)) {
        if (prop.startsWith("--")) el.style.setProperty(prop, val);
        else el.style[prop] = val;
      }
    }
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else if (k === "html") el.innerHTML = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export const svgNS = "http://www.w3.org/2000/svg";
export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  for (const c of children.flat()) if (c) el.append(c);
  return el;
}

// only writes to the DOM when the value changes (avoids reflows at 10 Hz)
export function setText(el, text) {
  const str = text == null ? "" : String(text);
  if (el.textContent !== str) el.textContent = str;
}
export function toggle(el, cls, on) {
  if (el.classList.contains(cls) !== !!on) el.classList.toggle(cls, !!on);
}

// ------------------------------------------------------------ formats

export function lapTime(ms) {
  if (ms == null) return "—";
  const total = Math.round(ms);
  const m = Math.floor(total / 60000);
  const sec = ((total % 60000) / 1000).toFixed(3);
  return m ? `${m}:${sec.padStart(6, "0")}` : sec;
}

export function clock(seconds) {
  if (seconds == null) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const hh = Math.floor(total / 3600), mm = Math.floor((total % 3600) / 60), ss = total % 60;
  const p = (n) => String(n).padStart(2, "0");
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${p(mm)}:${p(ss)}`;
}

export function gap(ms, laps) {
  if (laps) return t("fmt.gap_laps", { n: laps });
  if (ms == null) return "—";
  return `+${(ms / 1000).toFixed(3)}`;
}

export function wallTime(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  return d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
}

export function dateTime(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  return d.toLocaleString(locale, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

// readable text color on top of a car color
export function inkOn(hex) {
  const n = parseInt((hex || "#888888").slice(1), 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? "#0b0c0e" : "#ffffff";
}

// the language is fixed per page load, so these maps are built once
export const STATE_LABEL = {
  idle: t("state.idle"),
  grid: t("state.grid"),
  starting: t("state.starting"),
  running: t("state.running"),
  paused: t("state.paused"),
  finishing: t("state.finishing"),
  finished: t("state.finished"),
};
export const STATE_CHIP = {
  idle: "", grid: "info", starting: "caution", running: "go",
  paused: "stop", finishing: "sb", finished: "",
};
export const GATE_LABEL = {
  estop: t("gate.estop"),
  link: t("gate.link"),
  lights: t("gate.lights"),
  session: t("gate.session"),
  finished: t("gate.finished"),
  agent: t("gate.agent"),
  deadman: t("gate.deadman"),
};
