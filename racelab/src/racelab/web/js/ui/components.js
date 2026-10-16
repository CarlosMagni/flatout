// Components shared by the views.
import { h, s, setText, toggle } from "../dom.js";
import { t } from "../i18n.js";
import { request } from "../store.js";

// ------------------------------------------------------------ toasts

export function toast(text, kind = "info", ms = 3200) {
  const box = document.getElementById("toasts");
  const el = h(`div.toast.${kind}`, { text, role: kind === "error" ? "alert" : null });
  box.append(el);
  setTimeout(() => el.remove(), ms);
}

// runs a command and shows the error, if any
export async function run(op, args = {}, okText) {
  try {
    const result = await request(op, args);
    if (okText) toast(okText);
    return result;
  } catch (e) {
    toast(e.message, "error", 4500);
    throw e;
  }
}

// copies to the clipboard (with a fallback for plain http) and flashes "Copied" on the button
export async function copyText(text, button) {
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

// ------------------------------------------------------------ start lights

export function Lights() {
  const leds = Array.from({ length: 5 }, () => h("i.light"));
  const el = h("div.lights", { title: t("lights.title"), role: "img", "aria-label": t("lights.title") }, leds);
  return {
    el,
    update(cu, sessionState) {
      const lit = cu?.lights ?? 0;
      leds.forEach((led, i) => toggle(led, "on", i < lit));
      toggle(el, "blink", cu?.blink);
      // brief green at GO (phase 7)
      const go = cu?.phase === 7;
      toggle(el, "go", go);
      if (go) leds.forEach((led) => toggle(led, "on", true));
    },
  };
}

// ------------------------------------------------------------ fuel gauge

export function FuelGauge() {
  const segs = Array.from({ length: 15 }, () => h("i"));
  const bar = h("div.fuel-bar", {}, segs);
  const pit = h("span.pit-tag");
  const el = h("div.fuel", { role: "img" }, pit, bar);
  return {
    el,
    update(fuel, inPit, low = 3) {
      const v = fuel ?? 0;
      segs.forEach((seg, i) => toggle(seg, "on", fuel != null && i < v));
      toggle(bar, "low", fuel != null && v <= 5 && v > low);
      toggle(bar, "crit", fuel != null && v <= low);
      setText(pit, inPit ? "PIT" : "");
      el.title = fuel == null ? t("fuel.unknown") : t("fuel.level", { v });
      el.setAttribute("aria-label", el.title);
    },
  };
}

// ------------------------------------------------------------ fader 0..15

// Vertical fader with 16 notches. Above `limit` it has to be unlocked
// (safety notch: large throttle values require confirmation).
export function Fader({ onChange, limit = 12 }) {
  const ticks = h("div.ticks", {}, Array.from({ length: 16 }, () => h("i")));
  const fill = h("div.fill");
  const knob = h("div.knob");
  const limitEl = h("div.limit");
  const el = h("div.fader", { tabindex: 0, role: "slider", "aria-orientation": "vertical", "aria-valuemin": 0, "aria-valuemax": 15 },
    ticks, limitEl, fill, knob);
  let value = 0;
  let unlocked = false;
  let mode = "edit"; // edit | readonly | disabled
  let dragging = false;

  const max = () => (unlocked ? 15 : limit);
  const pad = 8;

  function render(v) {
    const h0 = el.clientHeight || 168;
    const span = h0 - 2 * pad;
    const y = (v / 15) * span;
    fill.style.height = `${y}px`;
    knob.style.bottom = `${pad + y}px`;
    const lim = ((15 - limit) / 15) * span;
    limitEl.style.height = `${lim}px`;
    limitEl.style.display = unlocked || mode !== "edit" ? "none" : "";
    el.setAttribute("aria-valuenow", v);
  }

  function fromPointer(ev) {
    const r = el.getBoundingClientRect();
    const span = r.height - 2 * pad;
    const y = r.bottom - pad - ev.clientY;
    return Math.max(0, Math.min(max(), Math.round((y / span) * 15)));
  }

  function set(v, user) {
    v = Math.max(0, Math.min(max(), v));
    if (v === value && !user) return;
    value = v;
    render(v);
    if (user) onChange?.(v);
  }

  el.addEventListener("pointerdown", (ev) => {
    if (mode !== "edit") return;
    dragging = true;
    el.setPointerCapture(ev.pointerId);
    set(fromPointer(ev), true);
  });
  el.addEventListener("pointermove", (ev) => { if (dragging) set(fromPointer(ev), true); });
  el.addEventListener("pointerup", () => { dragging = false; });
  el.addEventListener("pointercancel", () => { dragging = false; });
  el.addEventListener("keydown", (ev) => {
    if (mode !== "edit") return;
    const step = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1, PageUp: 3, PageDown: -3 }[ev.key];
    if (ev.key === "Home" || ev.key === "0") { set(0, true); ev.preventDefault(); }
    else if (step) { set(value + step, true); ev.preventDefault(); }
  });
  new ResizeObserver(() => render(value)).observe(el);

  return {
    el,
    get value() { return value; },
    get dragging() { return dragging; },
    set(v) { if (!dragging) set(v, false); },
    setMode(m) {
      mode = m;
      toggle(el, "readonly", m === "readonly");
      toggle(el, "disabled", m === "disabled");
      el.tabIndex = m === "edit" ? 0 : -1;
      el.setAttribute("aria-disabled", m === "edit" ? "false" : "true");
      render(value);
    },
    setUnlocked(on) { unlocked = on; render(value); },
    get unlocked() { return unlocked; },
  };
}

// ------------------------------------------------------------ sparkline

export function Spark(points = 90) {
  const area = s("path", { class: "area" });
  const line = s("path");
  const el = s("svg", { class: "spark", viewBox: `0 0 ${points - 1} 16`, preserveAspectRatio: "none" }, area, line);
  const data = [];
  return {
    el,
    push(v) {
      data.push(v ?? 0);
      if (data.length > points) data.shift();
      const offset = points - data.length;
      const pts = data.map((d, i) => `${i + offset},${(16 - (d / 15) * 15).toFixed(2)}`);
      if (!pts.length) return;
      line.setAttribute("d", `M${pts.join("L")}`);
      area.setAttribute("d", `M${offset},16L${pts.join("L")}L${points - 1},16Z`);
    },
  };
}

// ------------------------------------------------------------ confirmation modal

// Replaces confirm(): returns a promise (true = confirmed). Escape cancels,
// Enter confirms; for a risky action the focus starts on "Cancel".
export function confirmModal({ title, text, confirmLabel = t("common.confirm"), cancelLabel = t("common.cancel"), danger = false }) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    const close = (value) => {
      document.removeEventListener("keydown", onKey, true);
      backdrop.remove();
      previous?.focus?.();
      resolve(value);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); close(false); }
      else if (ev.key === "Enter" && document.activeElement?.tagName !== "BUTTON") { ev.preventDefault(); close(true); }
      else if (ev.key === "Tab") {
        // focus stays inside the modal
        const items = [cancel, ok];
        const i = items.indexOf(document.activeElement);
        ev.preventDefault();
        items[(i + (ev.shiftKey ? items.length - 1 : 1)) % items.length].focus();
      }
    };
    const cancel = h("button.btn", { type: "button", onclick: () => close(false) }, cancelLabel);
    const ok = h(`button.btn.${danger ? "danger" : "primary"}`, { type: "button", onclick: () => close(true) }, confirmLabel);
    const box = h("div.modal", { role: "alertdialog", "aria-modal": "true", "aria-labelledby": "modal-title" },
      h("h3", { id: "modal-title", text: title }),
      text ? h("p", { text }) : null,
      h("div.modal-actions", {}, cancel, ok));
    const backdrop = h("div.modal-backdrop", { onclick: (ev) => { if (ev.target === backdrop) close(false); } }, box);
    document.addEventListener("keydown", onKey, true);
    document.body.append(backdrop);
    (danger ? cancel : ok).focus();
  });
}

// ------------------------------------------------------------ side drawer

export function openDrawer(title, content, actions = []) {
  const close = () => { backdrop.remove(); drawer.remove(); document.removeEventListener("keydown", onKey, true); };
  const onKey = (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); close(); } };
  const backdrop = h("div.drawer-backdrop", { onclick: close });
  const drawer = h("aside.drawer", { role: "dialog", "aria-modal": "true", "aria-label": title },
    h("header", {}, h("h3", { text: title }), h("button.btn.ghost.sm", { onclick: close, "aria-label": t("common.close") }, t("common.close"))),
    h("div.content", {}, content),
    actions.length ? h("footer", {}, actions.map((a) => {
      const b = h(`button.btn${a.primary ? ".primary" : ""}`, { text: a.label });
      b.addEventListener("click", async () => {
        try { if ((await a.run?.()) !== false) close(); } catch { /* toast already shown */ }
      });
      return b;
    })) : null);
  document.addEventListener("keydown", onKey, true);
  document.body.append(backdrop, drawer);
  return close;
}

// ------------------------------------------------------------ fields

export function Stepper({ value = 0, min = 0, max = 99, step = 1, onChange }) {
  const input = h("input", { type: "text", inputmode: "numeric", value });
  const clamp = (v) => Math.max(min, Math.min(max, v));
  const commit = (v) => {
    v = clamp(Number.isFinite(v) ? v : min);
    input.value = v;
    onChange?.(v);
  };
  input.addEventListener("change", () => commit(parseInt(input.value, 10)));
  const el = h("div.stepper", {},
    h("button", { type: "button", "aria-label": t("stepper.minus"), onclick: () => commit((parseInt(input.value, 10) || 0) - step) }, "−"),
    input,
    h("button", { type: "button", "aria-label": t("stepper.plus"), onclick: () => commit((parseInt(input.value, 10) || 0) + step) }, "+"));
  return { el, get value() { return parseInt(input.value, 10); }, set(v) { input.value = v; } };
}

export function Toggle(checked, onChange, label) {
  const input = h("input", { type: "checkbox", "aria-label": label || "" });
  input.checked = !!checked;
  input.addEventListener("change", () => onChange?.(input.checked));
  return { el: h("label.toggle", {}, input, h("i")), input };
}

export function Segmented(options, value, onChange) {
  const buttons = options.map((o) => h("button", { type: "button", text: o.label, title: o.title || "", dataset: { value: o.value } }));
  const el = h("div.seg", { role: "group" }, buttons);
  const set = (v) => buttons.forEach((b) => {
    const on = b.dataset.value === String(v);
    toggle(b, "on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
  buttons.forEach((b) => b.addEventListener("click", () => onChange?.(b.dataset.value)));
  set(value);
  return { el, set, buttons };
}

// ------------------------------------------------------------ connector cards

// One chip per connector: name, status and an "Open" link to the connector's own page
// (new tab, never an iframe). Text comes from the connector: set as text only (escaped).
// The server only accepts http(s) links; checked again here before making a link.
export function fillConnectors(el, list) {
  const sig = JSON.stringify(list || []);
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.replaceChildren(...(list || []).map((c) => {
    const safe = typeof c.ui_url === "string" && /^https?:\/\//i.test(c.ui_url);
    return h("span.chip.connector", { title: c.agent }, h("span.connector-name", { text: c.name }),
      c.status ? h("span.connector-status", { text: c.status }) : null,
      safe ? h("a", { href: c.ui_url, target: "_blank", rel: "noopener", text: t("connector.open") }) : null);
  }));
}
