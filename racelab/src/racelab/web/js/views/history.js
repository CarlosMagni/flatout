// History screen: list of recorded sessions, then the detail of one session
// (standings + lap time chart).
import { clock, dateTime, gap, h, inkOn, lapTime, s, setText } from "../dom.js";
import { driverName, t } from "../i18n.js";
import { request, sessionLabel, store } from "../store.js";
import { toast } from "../ui/components.js";

const BEST_LAP_TYPES = new Set(["practice", "qualifying", "training"]);
const FALLBACK_COLORS = ["#e5383b", "#2f80ed", "#f2c94c", "#27ae60", "#f2994a", "#e0e0e0", "#9b51e0", "#828282"];

// ------------------------------------------------------------ utilities

// "20261010-171200-race" -> { epoch, type }
function parseId(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?:-(.+))?$/.exec(id || "");
  if (!m) return {};
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  return { epoch: d.getTime() / 1000, type: m[7] };
}

// `started` is an ISO date (from the recorder); dateTime() expects epoch seconds
function epochOf(started, id) {
  if (typeof started === "number") return started;
  if (typeof started === "string") {
    const ms = Date.parse(started);
    if (!Number.isNaN(ms)) return ms / 1000;
  }
  return parseId(id).epoch ?? null;
}

// the dictionary knows every built-in type, so labels also work before `meta` has
// arrived (direct load of a #/history/… URL)
const typeLabel = sessionLabel;

function driverInfo(slot) {
  return store.meta?.drivers?.find((d) => d.slot === slot);
}

// Optional API token (same source as store.js), always in a header, never in the URL.
function authHeaders() {
  try {
    const token = localStorage.getItem("racelab.token");
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}

// `sessions.get {id}` cannot go through the WebSocket: the `id` argument would overwrite
// the request identifier (see store.request). So the REST route is used instead.
async function fetchSession(id) {
  let res;
  try {
    res = await fetch(`/api/sessions/${encodeURIComponent(id)}`, { headers: authHeaders() });
  } catch {
    throw new Error(t("err.unreachable"));
  }
  if (res.status === 404) throw new Error(t("history.err_missing"));
  if (!res.ok) throw new Error(t("err.server", { status: res.status }));
  return res.json();
}
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

function stateBlock(kind, title, text, action) {
  return h("div.empty", { role: kind === "error" ? "alert" : "status" },
    h("h3", { text: title }),
    text ? h("p", { text }) : null,
    action || null);
}

// ============================================================ view

export class HistoryView {
  constructor(root, parts = []) {
    this.root = root;
    this.seq = 0;
    this.status = "idle";
    this.chart = null;
    this.wrap = h("div.screen.screen-narrow.history");
    root.append(this.wrap);
    this.route(parts);
  }

  destroy() {
    this.seq++;
    this.chart?.destroy();
  }

  route(parts = []) {
    this.id = parts[0] ? decodeURIComponent(parts[0]) : null;
    this.load();
  }

  hello() {
    if (this.status !== "ready" || !this.hadMeta) this.load();
  }

  load() {
    this.hadMeta = !!store.meta;
    this.chart?.destroy();
    this.chart = null;
    if (this.id) this.showDetail(this.id);
    else this.showList();
    window.scrollTo(0, 0);
  }

  // ------------------------------------------------------------ list

  async showList() {
    const token = ++this.seq;
    this.status = "loading";
    const body = h("div", {}, stateBlock("loading", t("common.loading")));
    this.wrap.replaceChildren(h("h1.page", { text: t("nav.history") }), h("section.panel.hist-list", {}, body));
    if (!store.online) return; // the list loads on `hello` (see hello())
    try {
      const rows = await request("sessions.list");
      if (token !== this.seq) return;
      this.status = "ready";
      this.renderList(body, Array.isArray(rows) ? rows : []);
    } catch (e) {
      if (token !== this.seq) return;
      this.status = "error";
      body.replaceChildren(stateBlock("error", t("history.unavailable"), e.message,
        h("button.btn", { type: "button", onclick: () => this.showList() }, t("common.retry"))));
    }
  }

  renderList(body, rows) {
    if (!rows.length) {
      body.replaceChildren(stateBlock("empty", t("history.empty.title"), t("history.empty.text"),
        h("a.btn.primary", { href: "#/setup" }, t("history.empty.cta"))));
      return;
    }
    const items = rows.map((r) => ({ ...r, epoch: epochOf(r.started, r.id) }));
    items.sort((a, b) => (b.epoch ?? 0) - (a.epoch ?? 0));
    const showLaps = items.some((r) => num(r.laps ?? r.total_laps) != null);
    const list = h("div.hist-rows" + (showLaps ? ".with-laps" : ""));
    list.append(h("div.hrow.head", {},
      h("div", { text: t("history.col.date") }), h("div", { text: t("history.col.session") }), h("div.r", { text: t("history.col.duration") }),
      h("div", { text: t("history.col.winner") }), showLaps ? h("div.r", { text: t("history.col.laps") }) : null));
    for (const r of items) {
      const type = r.type || parseId(r.id).type;
      const driver = r.winner_slot != null ? driverInfo(r.winner_slot) : null;
      const winner = r.winner
        ? h("div.h-winner", {},
          driver && driver.name === r.winner ? h("span.swatch", { style: { background: driver.color } }) : null,
          h("span", { text: r.winner }))
        : h("div.faint", { text: "—" });
      const laps = num(r.laps ?? r.total_laps);
      list.append(h("a.hrow.link", { href: `#/history/${encodeURIComponent(r.id)}` },
        h("div.dim.num", { text: r.epoch != null ? dateTime(r.epoch) : "—" }),
        h("div.h-title", {}, h("span.name", { text: r.title || typeLabel(type) }), r.title ? h("span.faint", { text: typeLabel(type) }) : null,
          r.winner ? h("span.faint.h-winner-m", { text: t("history.winner", { name: r.winner }) }) : null),
        h("div.r.num", { text: r.duration_s != null ? clock(r.duration_s) : "—" }),
        winner,
        showLaps ? h("div.r.num", { text: laps ?? "—" }) : null));
    }
    body.replaceChildren(list);
    const head = this.wrap.querySelector("h1.page");
    if (head) head.append(h("span.count", { text: t("history.count", { n: items.length }) }));
  }

  // ------------------------------------------------------------ detail

  async showDetail(id) {
    const token = ++this.seq;
    this.status = "loading";
    const back = h("a.btn.ghost.sm.back", { href: "#/history" }, t("history.back"));
    this.wrap.replaceChildren(back, h("section.panel", {}, stateBlock("loading", t("common.loading"))));
    try {
      const doc = await fetchSession(id);
      if (token !== this.seq) return;
      this.status = "ready";
      this.renderDetail(id, back, doc);
    } catch (e) {
      if (token !== this.seq) return;
      this.status = "error";
      this.wrap.replaceChildren(back, h("section.panel", {}, stateBlock("error", t("history.not_found"), e.message,
        h("div.quick", {},
          h("button.btn", { type: "button", onclick: () => this.showDetail(id) }, t("common.retry")),
          h("a.btn.ghost", { href: "#/history" }, t("history.back_to_list"))))));
    }
  }

  renderDetail(id, back, doc) {
    const data = normalize(doc, id);
    const best = data.best;
    const bestCar = best ? data.cars.find((c) => c.slot === best.slot) : null;

    const stats = h("dl.hist-stats", {},
      stat(t("history.stat.date"), data.epoch != null ? dateTime(data.epoch) : "—"),
      stat(t("history.stat.duration"), data.duration_s != null ? clock(data.duration_s) : "—"),
      stat(data.bestMode ? t("history.stat.laps_total") : t("history.stat.laps"), data.laps != null ? String(data.laps) : "—"),
      stat(t("history.stat.best"), best ? lapTime(best.ms) : "—", best ? "sb" : "",
        best ? [h("span.swatch", { style: { background: bestCar?.color || "#888" } }), h("span", { text: bestCar?.name || `#${best.slot + 1}` }), h("span.faint", { text: t("race.best_lap_on", { n: best.lap }) })] : null));

    const exportLink = h("a.btn.sm", {
      href: `/api/sessions/${encodeURIComponent(id)}`,
      download: `${id}.json`,
      onclick: (ev) => exportJson(ev, id),
    }, t("history.export"));

    const head = h("header.hist-head", {},
      h("div", {}, h("div.eyebrow", { text: typeLabel(data.type) }),
        h("h1.page", { text: data.title }),
        data.partial ? h("span.chip.caution", { text: t("history.partial") }) : null),
      exportLink);

    this.wrap.replaceChildren(back, head, stats);
    if (data.cars.length) this.wrap.append(classification(data));
    this.wrap.append(this.chartPanel(data));
    this.chart?.render();
  }

  chartPanel(data) {
    const panel = h("section.panel.chart-panel", {}, h("div.panel-head", {}, h("h2", { text: t("history.chart.title") }), h("span.spacer"), h("span.faint.small", { text: t("history.chart.seconds") })));
    if (!data.series.length) {
      panel.append(stateBlock("empty", t("history.chart.empty.title"), t("history.chart.empty.text")));
      return panel;
    }
    const host = h("div.chart");
    const legend = h("div.legend", { role: "group", "aria-label": t("history.chart.legend") });
    const key = h("p.chart-key.faint.small");
    panel.append(h("div.panel-body", {}, host, legend, key));
    this.chart = new LapChart(host, data, key);
    for (const ser of data.series) {
      const btn = h("button.lg-item", {
        type: "button", "aria-pressed": "true", title: t("history.chart.toggle"),
        onclick: () => {
          const on = this.chart.toggle(ser.slot);
          btn.setAttribute("aria-pressed", on ? "true" : "false");
        },
      }, h("span.swatch", { style: { background: ser.color } }), h("span", { text: ser.name }));
      legend.append(btn);
    }
    if (data.series.length < 2) legend.classList.add("single");
    return panel;
  }
}

function stat(label, value, cls = "", extra = null) {
  return h("div.stat", {}, h("dt.eyebrow", { text: label }),
    h("dd", {}, h(`span.value.num${cls ? "." + cls : ""}`, { text: value }), extra ? h("span.who-line", {}, extra) : null));
}

// the export goes through a link; if an API token is needed it is sent in a
// header rather than in the URL
async function exportJson(ev, id) {
  const headers = authHeaders();
  if (!headers.Authorization) return; // no token: the plain link is enough
  ev.preventDefault();
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(id)}`, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const url = URL.createObjectURL(await res.blob());
    const a = h("a", { href: url, download: `${id}.json` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch (e) {
    toast(t("history.export_failed", { error: e.message }), "error", 4500);
  }
}

// ============================================================ file normalization

// The file is {meta, events, results}. `results` is null until the session is
// finished (autosave): laps and standings are then rebuilt from the `lap` events.
function normalize(doc, id) {
  const meta = doc?.meta || {};
  const results = doc?.results || null;
  const session = results?.session || {};
  const type = session.type || meta.type || meta.session?.type || parseId(id).type || null;
  const title = session.title || meta.title || meta.session?.title || typeLabel(type);

  let laps = Array.isArray(results?.laps) ? results.laps : null;
  let partial = false;
  if (!laps) {
    partial = true;
    laps = (doc?.events || []).filter((e) => e?.name === "lap" && e.data).map((e) => ({ ...e.data }));
  }
  laps = laps.filter((l) => l && l.slot != null && num(l.ms) != null);

  const metaDrivers = new Map((meta.drivers || []).map((d) => [d.slot, d]));
  const identity = (slot, fromClass) => {
    const d = fromClass || metaDrivers.get(slot) || driverInfo(slot);
    return {
      name: (d?.name && driverName(d.name, slot)) || (slot >= 6 ? ["Ghost", "Pace car"][slot - 6] : t("driver.n", { n: slot + 1 })),
      color: d?.color || FALLBACK_COLORS[slot % FALLBACK_COLORS.length],
    };
  };

  // laps per car
  const bySlot = new Map();
  for (const l of laps) {
    if (!bySlot.has(l.slot)) bySlot.set(l.slot, []);
    bySlot.get(l.slot).push(l);
  }
  for (const list of bySlot.values()) list.sort((a, b) => a.lap - b.lap);

  const total = (slot) => {
    const list = bySlot.get(slot) || [];
    const last = list[list.length - 1];
    if (last && num(last.t) != null) return last.t * 1000;
    return list.length ? list.reduce((acc, l) => acc + l.ms, 0) : null;
  };
  const bestOf = (slot) => {
    const times = (bySlot.get(slot) || []).filter((l) => !l.neutralized).map((l) => l.ms);
    return times.length ? Math.min(...times) : null;
  };

  // standings
  let cars;
  const bestMode = BEST_LAP_TYPES.has(type);
  if (Array.isArray(results?.classification) && results.classification.length) {
    cars = results.classification.map((c) => ({ ...c }));
  } else {
    cars = [...bySlot.keys()].map((slot) => ({
      slot, laps: bySlot.get(slot).length, best_ms: bestOf(slot), derived: true,
    }));
    cars.sort((a, b) => (bestMode
      ? (a.best_ms ?? Infinity) - (b.best_ms ?? Infinity)
      : b.laps - a.laps || (total(a.slot) ?? Infinity) - (total(b.slot) ?? Infinity)));
    cars.forEach((c, i) => { c.position = i + 1; });
  }
  cars.sort((a, b) => (a.position ?? 99) - (b.position ?? 99));
  for (const c of cars) {
    Object.assign(c, identity(c.slot, c.name && c.color ? c : null));
    if (c.laps == null) c.laps = bySlot.get(c.slot)?.length ?? 0;
    if (c.best_ms == null) c.best_ms = bestOf(c.slot);
    c.total_ms = total(c.slot);
  }

  // session best lap
  let best = session.best && num(session.best.ms) != null ? session.best : null;
  if (!best) {
    for (const l of laps) {
      if (!l.neutralized && (!best || l.ms < best.ms)) best = { slot: l.slot, ms: l.ms, lap: l.lap };
    }
  }

  // chart series: in standings order
  const order = cars.map((c) => c.slot);
  for (const slot of bySlot.keys()) if (!order.includes(slot)) order.push(slot);
  const series = order.filter((slot) => bySlot.has(slot)).map((slot) => ({
    slot, ...identity(slot, cars.find((c) => c.slot === slot)),
    laps: bySlot.get(slot).map((l) => ({ lap: l.lap, ms: l.ms, neutralized: !!l.neutralized })),
  }));

  return {
    id, type, title, partial, bestMode, cars, series, best,
    epoch: epochOf(meta.started, id),
    duration_s: num(session.elapsed_s),
    laps: cars.length
      ? (bestMode ? cars.reduce((acc, c) => acc + (c.laps || 0), 0) : cars[0].laps)
      : (num(session.leader_lap) ?? null),
  };
}

// ============================================================ standings

function classification(data) {
  const leader = data.cars[0];
  const mode = data.bestMode ? "bestmode" : "lapsmode";
  const head = h("div.hrow.head", {},
    h("div.pos", { text: t("history.class.pos") }), h("div", { text: t("history.class.driver") }), h("div.r", { text: t("history.class.laps") }),
    h("div.r", { text: t("history.class.best") }), data.bestMode ? null : h("div.r", { text: t("history.class.time") }), h("div.r", { text: t("history.class.gap") }));
  const panel = h(`section.panel.hclass.${mode}`, {}, h("div.panel-head", {}, h("h2", { text: t("history.class.title") })), head);
  for (const c of data.cars) {
    const isBest = data.best && data.best.slot === c.slot && c.best_ms === data.best.ms;
    const carno = h("span.carno", { text: c.slot + 1 });
    carno.style.color = inkOn(c.color);
    carno.style.setProperty("--car", c.color);
    const row = h("div.hrow", {},
      h("div.pos", { text: c.position ?? "—" }),
      h("div.driver", {}, carno, h("span.name", { text: c.name }),
        c.eliminated ? h("span.chip.stop", { text: t("car.eliminated") }) : null),
      h("div.r.laps", { text: c.laps ?? "—" }),
      h("div.r.time" + (isBest ? ".sb" : ""), { text: lapTime(c.best_ms) }),
      data.bestMode ? null : h("div.r.time", { text: c.total_ms != null ? lapTime(c.total_ms) : "—" }),
      h("div.r.time.dim", { text: gapText(c, leader, data.bestMode) }));
    row.style.setProperty("--car", c.color);
    panel.append(row);
  }
  return panel;
}

function gapText(c, leader, bestMode) {
  if (c === leader) return "";
  if (bestMode) return c.best_ms != null && leader.best_ms != null ? gap(c.best_ms - leader.best_ms) : "—";
  if (c.gap_laps) return gap(null, c.gap_laps);
  if (num(c.gap_ms) != null) return gap(c.gap_ms);
  if (leader.laps != null && c.laps != null && leader.laps > c.laps) return gap(null, leader.laps - c.laps);
  if (c.total_ms != null && leader.total_ms != null) return gap(Math.max(0, c.total_ms - leader.total_ms));
  return "—";
}

// ============================================================ lap time chart

const NICE_STEPS = [50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000];

// Robust vertical range: laps outside [0.6, 1.5] × median (off-track, neutralization)
// do not set the scale; they are clipped at the edge with a marker.
// (A 5-95 % percentile would always discard the first laps, which are legitimately slow.)
function computeRange(values) {
  const v = values.slice().sort((a, b) => a - b);
  const median = v[Math.floor((v.length - 1) / 2)];
  let base = v.filter((x) => x >= median * 0.6 && x <= median * 1.5);
  if (!base.length) base = v;
  let lo = base[0];
  let hi = base[base.length - 1];
  const span = Math.max(hi - lo, 300);
  lo -= span * 0.12;
  hi += span * 0.12;
  const raw = (hi - lo) / 5;
  const step = NICE_STEPS.find((x) => x >= raw) || 600000;
  return { min: Math.max(0, Math.floor(lo / step) * step), max: Math.ceil(hi / step) * step, step };
}

function tickLabel(ms, step) {
  const sec = ms / 1000;
  const decimals = step >= 1000 ? 0 : step % 100 === 0 ? 1 : 2;
  if (sec >= 60) {
    const m = Math.floor(sec / 60);
    const rest = (sec - m * 60).toFixed(decimals);
    return `${m}:${rest.padStart(decimals ? 3 + decimals : 2, "0")}`;
  }
  return sec.toFixed(decimals);
}

class LapChart {
  constructor(host, data, keyEl) {
    this.host = host;
    this.data = data;
    this.keyEl = keyEl;
    this.hidden = new Set();
    this.tip = h("div.chart-tip.hidden", { role: "presentation" });
    this.points = [];
    this.width = 0;
    this.raf = 0;
    this.ro = new ResizeObserver(() => {
      cancelAnimationFrame(this.raf);
      this.raf = requestAnimationFrame(() => {
        const w = Math.round(this.host.clientWidth);
        if (w && w !== this.width) this.render();
      });
    });
    this.ro.observe(host);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.ro.disconnect();
  }

  // hides / shows a series (never the last visible one)
  toggle(slot) {
    if (this.hidden.has(slot)) this.hidden.delete(slot);
    else if (this.data.series.length - this.hidden.size > 1) this.hidden.add(slot);
    this.render();
    return !this.hidden.has(slot);
  }

  render() {
    const width = Math.round(this.host.clientWidth);
    if (!width) return;
    this.width = width;
    const W = Math.max(260, width);
    const H = W < 520 ? 240 : 300;
    const m = { l: 44, r: 12, t: 16, b: 30 };
    const plotW = W - m.l - m.r;
    const plotH = H - m.t - m.b;
    const series = this.data.series.filter((x) => !this.hidden.has(x.slot));
    const values = series.flatMap((x) => x.laps.map((l) => l.ms));
    const range = computeRange(values.length ? values : [5000]);
    const maxLap = Math.max(2, ...series.flatMap((x) => x.laps.map((l) => l.lap)));
    const pad = 8;
    const X = (lap) => m.l + pad + ((lap - 1) / (maxLap - 1)) * (plotW - 2 * pad);
    const Y = (ms) => m.t + ((range.max - clampNum(ms, range.min, range.max)) / (range.max - range.min)) * plotH;

    const svg = s("svg", { class: "lapchart", viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img",
      "aria-label": t("history.chart.aria", { names: series.map((x) => x.name).join(", ") }) });

    // grid and vertical axis
    for (let v = range.min; v <= range.max + 1; v += range.step) {
      const y = Y(v);
      svg.append(s("line", { class: "grid", x1: m.l, x2: W - m.r, y1: y, y2: y }));
      const label = s("text", { class: "ylab", x: m.l - 8, y });
      label.textContent = tickLabel(v, range.step);
      svg.append(label);
    }
    // horizontal axis: lap numbers
    const target = Math.max(2, Math.floor(plotW / 56));
    const xStep = [1, 2, 5, 10, 20, 25, 50, 100, 200, 500].find((st) => Math.ceil(maxLap / st) <= target) || 1000;
    svg.append(s("line", { class: "axis", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }));
    for (let lap = xStep === 1 ? 1 : xStep; lap <= maxLap; lap += xStep) {
      const label = s("text", { class: "xlab", x: X(lap), y: H - m.b + 17 });
      label.textContent = lap;
      svg.append(label);
    }
    if (xStep !== 1) {
      const first = s("text", { class: "xlab", x: X(1), y: H - m.b + 17 });
      first.textContent = 1;
      svg.append(first);
    }

    // series
    const showDots = maxLap <= 40;
    const pts = [];
    let hasNeutral = false;
    let hasClipped = false;
    const best = this.data.best;
    for (const ser of series) {
      const d = ser.laps.map((l, i) => `${i ? "L" : "M"}${X(l.lap).toFixed(1)},${Y(l.ms).toFixed(1)}`).join("");
      svg.append(s("path", { class: "line", d, stroke: ser.color }));
    }
    for (const ser of series) {
      for (const l of ser.laps) {
        const x = X(l.lap);
        const y = Y(l.ms);
        const above = l.ms > range.max;
        const below = l.ms < range.min;
        pts.push({ x, y, ser, lap: l });
        if (above || below) {
          // off the scale: triangle marker at the edge (offset per series so they do not overlap)
          hasClipped = true;
          const mx = x + (series.indexOf(ser) - (series.length - 1) / 2) * 7;
          const d = above
            ? `M${mx - 3.5},${m.t}L${mx + 3.5},${m.t}L${mx},${m.t - 7}Z`
            : `M${mx - 3.5},${m.t + plotH}L${mx + 3.5},${m.t + plotH}L${mx},${m.t + plotH + 7}Z`;
          if (l.neutralized) {
            hasNeutral = true;
            svg.append(s("path", { class: "clip hollow", d, fill: "none", stroke: ser.color, "stroke-width": 1.5, "stroke-linejoin": "round" }));
          } else {
            svg.append(s("path", { class: "clip", d, fill: ser.color }));
          }
        } else if (l.neutralized) {
          hasNeutral = true;
          svg.append(s("circle", { class: "pt hollow", cx: x, cy: y, r: 3.5, stroke: ser.color }));
        } else if (showDots) {
          svg.append(s("circle", { class: "pt", cx: x, cy: y, r: 2.75, fill: ser.color }));
        }
      }
    }
    if (best) {
      const p = pts.find((q) => q.ser.slot === best.slot && q.lap.lap === best.lap);
      if (p) svg.append(s("circle", { class: "ring-best", cx: p.x, cy: p.y, r: 6.5 }));
    }
    this.ring = s("circle", { class: "ring-hover hidden", r: 6 });
    svg.append(this.ring);
    this.points = pts;
    this.geometry = { W, H, m };

    const touch = (ev) => this.hover(ev, svg);
    svg.addEventListener("pointermove", touch);
    svg.addEventListener("pointerdown", touch);
    svg.addEventListener("pointerleave", () => this.leave());

    this.host.replaceChildren(svg, this.tip);
    this.leave();

    const key = [];
    if (hasNeutral) key.push(t("history.chart.key_neutral"));
    if (best) key.push(t("history.chart.key_best"));
    if (hasClipped) key.push(t("history.chart.key_clipped"));
    setText(this.keyEl, key.join(" · "));
  }

  hover(ev, svg) {
    const box = svg.getBoundingClientRect();
    if (!box.width) return;
    const k = this.geometry.W / box.width;
    const px = (ev.clientX - box.left) * k;
    const py = (ev.clientY - box.top) * k;
    let nearest = null;
    let dist = 30 * 30;
    for (const p of this.points) {
      const d = (p.x - px) ** 2 + (p.y - py) ** 2;
      if (d < dist) { dist = d; nearest = p; }
    }
    if (!nearest) { this.leave(); return; }
    const { ser, lap } = nearest;
    this.ring.setAttribute("cx", nearest.x);
    this.ring.setAttribute("cy", nearest.y);
    this.ring.setAttribute("stroke", ser.color);
    this.ring.classList.remove("hidden");
    const parts = [
      h("span.swatch", { style: { background: ser.color } }),
      h("span.dim", { text: ser.name }),
      h("span.num", { text: t("history.chart.tip_lap", { n: lap.lap, time: lapTime(lap.ms) }) }),
    ];
    if (lap.neutralized) parts.push(h("span.faint", { text: t("history.chart.tip_neutral") }));
    this.tip.replaceChildren(...parts);
    this.tip.classList.remove("hidden");
    const hostW = this.host.clientWidth;
    // box is in screen px, left/top in the page's own (zoomed on wide screens, style.css)
    const z = svg.currentCSSZoom || 1;
    const left = Math.min(hostW - this.tip.offsetWidth / 2 - 4, Math.max(this.tip.offsetWidth / 2 + 4, nearest.x / k / z));
    this.tip.style.left = `${left}px`;
    this.tip.style.top = `${nearest.y / k / z}px`;
  }

  leave() {
    this.ring?.classList.add("hidden");
    this.tip.classList.add("hidden");
  }
}

function clampNum(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}
