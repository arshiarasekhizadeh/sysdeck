"use strict";
// Value transitions repaint constantly (≈80% of a core); opt in with ?anim=1.
if (new URLSearchParams(location.search).get("anim") === "1") document.documentElement.classList.add("anim");
/* SYSDECK front-end: SSE client + canvas charts + DOM panels. */

const $ = (id) => document.getElementById(id);
const C = {
  cyan: "#3fd0ff", violet: "#a78bfa", pink: "#ff5fb7", amber: "#ffc04d", orange: "#ff8a4c",
  green: "#3ee8a5", blue: "#6aa8ff", red: "#ff4d6d", dim: "#8d96aa", faint: "#545d70",
};
const rgba = (hex, a) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
const GiB = 1024 ** 3;
/* ?privacy=1 hides identifying details (host name, IPs, project folders) for screen sharing */
const PRIVACY = new URLSearchParams(location.search).get("privacy") === "1";
const maskIP = (ip) => (PRIVACY ? String(ip).replace(/[0-9a-f]+/gi, "•••") : ip);
const isRunner = (n) => /^(ollama|llama)/.test(n || "");

/* ───────────── formatting ───────────── */
function fmtBytes(b, d = 1) {
  if (b == null) return "—";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i < 2 ? 0 : d)} ${u[i]}`;
}
function fmtRate(b) {
  if (b == null) return ["—", ""];
  const u = ["B/s", "KB/s", "MB/s", "GB/s"];
  let i = 0;
  while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
  return [b.toFixed(i > 0 && b < 10 ? 1 : 0), u[i]];
}
const rateHTML = (b) => { const [v, u] = fmtRate(b); return `${v}<small>${u}</small>`; };
function fmtDur(s) {
  if (s == null) return "—";
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `up ${d}d ${h}h` : h ? `up ${h}h ${m}m` : `up ${m}m`;
}
const hhmmss = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });

/* heat colour for per-core load: cyan → violet → pink → red */
const HEAT = [[0, C.cyan], [45, C.violet], [75, C.pink], [100, C.red]];
function heat(v) {
  for (let i = 1; i < HEAT.length; i++) {
    const [p1, c1] = HEAT[i];
    if (v <= p1) {
      const [p0, c0] = HEAT[i - 1];
      const t = (v - p0) / (p1 - p0);
      const a = parseInt(c0.slice(1), 16), b = parseInt(c1.slice(1), 16);
      const ch = (s) => Math.round(((a >> s) & 255) + (((b >> s) & 255) - ((a >> s) & 255)) * t);
      return `rgb(${ch(16)},${ch(8)},${ch(0)})`;
    }
  }
  return C.red;
}

/* ───────────── ring gauge ───────────── */
let ringSeq = 0;
function makeRing(el, { label = "", color = C.cyan, color2 = C.violet, ticks = true } = {}) {
  const small = el.classList.contains("small");
  const sw = small ? 8 : 7;
  const r = 50 - sw / 2 - (ticks ? 7 : 1);
  const c = 2 * Math.PI * r;
  const id = `rg${ringSeq++}`;
  const tickC = 2 * Math.PI * 48;
  el.innerHTML = `<svg viewBox="0 0 100 100">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="${color}"/><stop offset="1" stop-color="${color2}"/></linearGradient></defs>
    ${ticks ? `<circle class="r-ticks" cx="50" cy="50" r="48" fill="none" stroke-width="3" stroke-dasharray="0.7 ${(tickC / 60 - 0.7).toFixed(3)}"/>` : ""}
    <circle class="r-track" cx="50" cy="50" r="${r}" fill="none" stroke-width="${sw}"/>
    <circle class="r-val" cx="50" cy="50" r="${r}" fill="none" stroke="url(#${id})" stroke-width="${sw}"
      stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${c}" transform="rotate(-90 50 50)"
      style="--glow:${rgba(color2, 0.6)}"/>
  </svg><div class="r-label"><b>—</b><span>${label}</span></div>`;
  const val = el.querySelector(".r-val"), b = el.querySelector("b");
  let last = "";
  return {
    set(frac, html) {
      val.style.strokeDashoffset = String(c * (1 - clamp(frac || 0)));
      if (html != null && html !== last) { b.innerHTML = html; last = html; }
    },
  };
}

/* ───────────── history store ───────────── */
const KEYS = ["cpu", "mem", "gpu", "vram", "rx", "tx", "rd", "wr", "tps"];
const H = Object.fromEntries(KEYS.map((k) => [k, []]));
let Hcount = 0;
let lastTick = performance.now();
function pushHistory(s) {
  const v = {
    cpu: s.cpu?.total ?? 0, mem: s.mem?.percent ?? 0,
    gpu: s.gpu?.util ?? 0, vram: s.gpu?.vram_pct ?? 0,
    rx: s.io?.rx ?? 0, tx: s.io?.tx ?? 0, rd: s.io?.rd ?? 0, wr: s.io?.wr ?? 0,
    tps: s.llm?.live_tps ?? 0,
  };
  for (const k of KEYS) { H[k].push(v[k]); if (H[k].length > 300) H[k].shift(); }
  Hcount++;
  lastTick = performance.now();
}

/* ───────────── scrolling area chart ───────────── */
class Chart {
  constructor(canvas, opts) {
    this.cv = canvas;
    this.ctx = canvas.getContext("2d");
    this.o = { n: 120, max: null, floor: 1, mirror: false, fmt: null, ...opts };
    this.scale = this.o.max || this.o.floor;
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    this.resize();
  }
  resize() {
    const r = this.cv.parentElement.getBoundingClientRect();
    const d = window.devicePixelRatio || 1;
    this.w = r.width; this.h = r.height;
    this.cv.width = Math.max(1, Math.round(r.width * d));
    this.cv.height = Math.max(1, Math.round(r.height * d));
    this.ctx.setTransform(d, 0, 0, d, 0, 0);
  }
  draw(frac) {
    const { ctx, w, h, o } = this;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    const N = o.n, step = w / (N - 1);
    const series = o.series.map((s) => ({ ...s, data: H[s.key].slice(-(N + 1)) }));
    const len = series[0].data.length;

    if (!o.max) {
      let m = o.floor;
      for (const s of series) for (const v of s.data) if (v > m) m = v;
      const target = m * 1.2;
      this.scale += (target - this.scale) * (target > this.scale ? 0.3 : 0.05);
    }
    const max = o.max || this.scale;
    const baseY = o.mirror ? h / 2 : h - 1;
    const amp = o.mirror ? h / 2 - 3 : h - 6;
    const shift = (1 - frac) * step;

    // grid: horizontal guides + vertical lines every 15 s that scroll with the data
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(148,170,220,0.07)";
    ctx.beginPath();
    for (const r of o.mirror ? [0.5] : [0.25, 0.5, 0.75]) {
      const y = Math.round(h * r) + 0.5;
      ctx.moveTo(0, y); ctx.lineTo(w, y);
    }
    for (let i = 0; i < len; i++) {
      if ((Hcount - (len - 1 - i)) % 15 !== 0) continue;
      const x = Math.round(w - (len - 1 - i) * step + shift) + 0.5;
      if (x >= 0 && x <= w) { ctx.moveTo(x, 0); ctx.lineTo(x, h); }
    }
    ctx.stroke();

    for (const s of series) this.line(s, step, shift, baseY, amp, max, frac);

    if (o.fmt) {
      ctx.font = '500 11px "JetBrainsMono NF", "JetBrains Mono", monospace';
      ctx.fillStyle = "rgba(141,150,170,.75)";
      ctx.textBaseline = "top";
      ctx.fillText(o.fmt(max), 4, 3);
    }
  }
  line(s, step, shift, baseY, amp, max, frac) {
    const { ctx, w } = this;
    const d = s.data, len = d.length;
    if (len < 2) return;
    const dir = s.down ? 1 : -1;
    const Y = (v) => baseY + dir * clamp(v / max) * amp;
    const pts = d.map((v, i) => [w - (len - 1 - i) * step + shift, Y(v)]);
    const path = new Path2D();
    path.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
      const mx = (x0 + x1) / 2;
      path.bezierCurveTo(mx, y0, mx, y1, x1, y1);
    }
    const area = new Path2D(path);
    area.lineTo(pts[pts.length - 1][0], baseY);
    area.lineTo(pts[0][0], baseY);
    area.closePath();
    const g = ctx.createLinearGradient(0, baseY + dir * amp, 0, baseY);
    g.addColorStop(0, rgba(s.color, s.fillA ?? 0.34));
    g.addColorStop(1, rgba(s.color, 0));
    ctx.fillStyle = g;
    ctx.fill(area);
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.strokeStyle = rgba(s.color, 0.16); ctx.lineWidth = 6; ctx.stroke(path);
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.7; ctx.stroke(path);
    // glowing head at the right edge
    const v = d[len - 2] + (d[len - 1] - d[len - 2]) * frac;
    const y = Y(v);
    ctx.fillStyle = rgba(s.color, 0.25);
    ctx.beginPath(); ctx.arc(w - 4, y, 6, 0, 7); ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.beginPath(); ctx.arc(w - 4, y, 2.2, 0, 7); ctx.fill();
  }
}

/* ───────────── per-request throughput bars ───────────── */
class RunBars {
  constructor(canvas) {
    this.cv = canvas; this.ctx = canvas.getContext("2d");
    this.runs = []; this.live = null;
    new ResizeObserver(() => { this.resize(); this.draw(); }).observe(canvas.parentElement);
    this.resize();
  }
  resize() {
    const r = this.cv.parentElement.getBoundingClientRect();
    const d = window.devicePixelRatio || 1;
    this.w = r.width; this.h = r.height;
    this.cv.width = Math.max(1, Math.round(r.width * d));
    this.cv.height = Math.max(1, Math.round(r.height * d));
    this.ctx.setTransform(d, 0, 0, d, 0, 0);
  }
  set(runs, live) { this.runs = runs; this.live = live; this.draw(); }
  draw() {
    const { ctx, w, h } = this;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    const N = 24, gap = 4, axisW = 30;
    const vals = this.runs.slice(-(this.live != null ? N - 1 : N)).map((r) => r.gen_tps);
    if (this.live != null) vals.push(this.live);
    const peak = Math.max(5, ...vals);
    const nice = peak <= 10 ? 10 : peak <= 20 ? 20 : peak <= 40 ? 40 : peak <= 80 ? 80 : Math.ceil(peak / 50) * 50;
    const top = 8, bottom = h - 2, plotW = w - axisW;
    ctx.font = '500 10.5px "JetBrainsMono NF", "JetBrains Mono", monospace';
    ctx.textBaseline = "middle"; ctx.textAlign = "right";
    for (const f of [0, 0.5, 1]) {
      const y = Math.round(bottom - (bottom - top) * f) + 0.5;
      ctx.strokeStyle = "rgba(148,170,220,0.08)";
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW - 4, y); ctx.stroke();
      ctx.fillStyle = "rgba(141,150,170,.75)";
      ctx.fillText(String(Math.round(nice * f)), w - 2, y);
    }
    if (!vals.length) {
      ctx.textAlign = "center"; ctx.fillStyle = "rgba(141,150,170,.6)";
      ctx.fillText("tok/s per request appears here", plotW / 2, (top + bottom) / 2);
      return;
    }
    const bw = (plotW - 4 - gap * (N - 1)) / N;
    const x0 = plotW - 4 - vals.length * (bw + gap) + gap;
    vals.forEach((v, i) => {
      const bh = Math.max(2, (bottom - top) * clamp(v / nice));
      const x = x0 + i * (bw + gap), y = bottom - bh;
      const isLast = i === vals.length - 1;
      const col = isLast ? C.pink : C.violet;
      const g = ctx.createLinearGradient(0, y, 0, bottom);
      g.addColorStop(0, rgba(col, isLast ? 0.95 : 0.6));
      g.addColorStop(1, rgba(col, isLast ? 0.25 : 0.12));
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.roundRect(x, y, bw, bh, [3, 3, 1, 1]); ctx.fill();
      if (isLast) { ctx.fillStyle = "#fff"; ctx.fillRect(x, y, bw, 2); }
    });
  }
}

/* ───────────── static UI setup ───────────── */
const rings = {
  cpu: makeRing($("cpu-ring"), { label: "load", color: C.cyan, color2: C.violet }),
  gpu: makeRing($("gpu-ring"), { label: "gpu", color: C.violet, color2: C.pink }),
  vram: makeRing($("vram-ring"), { label: "vram", color: C.pink, color2: C.amber }),
  ka: makeRing($("ka-ring"), { color: C.green, color2: C.cyan, ticks: false }),
};

const charts = [
  new Chart($("ch-cpu"), { max: 100, series: [{ key: "cpu", color: C.cyan }] }),
  new Chart($("ch-mem"), { max: 100, series: [{ key: "mem", color: C.amber, fillA: 0.28 }] }),
  new Chart($("ch-gpu"), { max: 100, series: [{ key: "vram", color: C.pink, fillA: 0.12 }, { key: "gpu", color: C.violet }] }),
  new Chart($("ch-net"), { mirror: true, floor: 50e3, fmt: (m) => fmtRate(m).join(" "),
    series: [{ key: "tx", color: C.cyan }, { key: "rx", color: C.green, down: true }] }),
  new Chart($("ch-disk"), { mirror: true, floor: 1e6, fmt: (m) => fmtRate(m).join(" "),
    series: [{ key: "wr", color: C.amber }, { key: "rd", color: C.orange, down: true }] }),
];
const runBars = new RunBars($("ch-tps"));

let coreEls = [];
function buildCores(n) {
  const box = $("cores");
  box.innerHTML = "";
  coreEls = [];
  const pairs = Math.ceil(n / 2);
  box.style.gridTemplateColumns = `repeat(${pairs}, minmax(0,1fr))`;
  for (let p = 0; p < pairs; p++) {
    const pair = document.createElement("div");
    pair.className = "core-pair";
    pair.innerHTML = `<div class="core-bars"><div class="core"><i></i></div><div class="core"><i></i></div></div><span>C${p}</span>`;
    box.appendChild(pair);
    coreEls.push(...pair.querySelectorAll(".core i"));
  }
}

/* how many fixed-height rows fit in a list without showing a half-cut row */
/* rows that fit, measured from the rendered row height (works at any zoom/density) */
function fitRows(el, rowH) {
  const first = el.firstElementChild;
  const gap = parseFloat(getComputedStyle(el).rowGap) || 0;
  const h = first && first.offsetHeight ? first.offsetHeight + gap : rowH;
  return Math.max(1, Math.floor((el.clientHeight + gap) / h));
}

function tiles(el, items) {
  const html = items.map(([label, val, cls]) =>
    `<div class="tile"><span>${label}</span><b class="${cls || ""}">${val}</b></div>`).join("");
  if (el.innerHTML !== html) el.innerHTML = html;
}
function setHTML(el, html) { if (el.innerHTML !== html) el.innerHTML = html; }

/* ───────────── render ───────────── */
let INFO = {};
let S = {};
let kaMax = { key: "", max: 1 };

function render(s) {
  S = s;
  renderHeader(s);
  renderCPU(s);
  renderMem(s);
  renderProcs(s);
  renderAI(s);
  renderGPU(s);
  renderNet(s);
  renderDisk(s);
  renderDev(s);
}

function renderHeader(s) {
  $("uptime").textContent = s.uptime ? fmtUptime(s.uptime) : "—";
  const b = s.battery;
  const pw = $("power");
  pw.textContent = b ? (b.plugged ? `AC · ${b.percent}%` : `BAT ${b.percent}%`) : "AC";
  pw.classList.toggle("charging", !!b?.plugged);
  const vpn = s.ifaces?.vpn;
  $("vpn").hidden = !vpn;
  if (vpn) $("vpn").textContent = `VPN · ${vpn}`;

  const al = [];
  const ct = s.cpu?.temp, gt = s.gpu?.temp;
  if (ct >= 92) al.push(["crit", `CPU ${Math.round(ct)}°C`]); else if (ct >= 84) al.push(["", `CPU ${Math.round(ct)}°C`]);
  if (gt >= 87) al.push(["crit", `GPU ${Math.round(gt)}°C`]); else if (gt >= 80) al.push(["", `GPU ${Math.round(gt)}°C`]);
  if (s.mem) {
    const freePct = s.mem.available / s.mem.total * 100;
    if (freePct < 6) al.push(["crit", `RAM ${s.mem.percent}%`]); else if (freePct < 12) al.push(["", `RAM ${s.mem.percent}%`]);
  }
  if (s.gpu?.vram_pct >= 96) al.push(["", "VRAM full"]);
  for (const d of s.disks || []) if (d.percent >= 90) al.push(["", `${d.mount} ${Math.round(d.percent)}%`]);
  const errs = Object.keys(s.errors || {});
  if (errs.length) al.push(["", `${errs.length} sampler error${errs.length > 1 ? "s" : ""}`]);
  setHTML($("alerts"), al.map(([c, t]) => `<span class="alert ${c}">${esc(t)}</span>`).join(""));
  $("alerts").title = errs.map((k) => `${k}: ${s.errors[k]}`).join("\n");
}

function renderCPU(s) {
  const c = s.cpu;
  if (!c) return;
  rings.cpu.set(c.total / 100, `${Math.round(c.total)}<small>%</small>`);
  const t = c.temp;
  setHTML($("cpu-kv"), `
    <div><dt>Temp</dt><dd class="${t >= 92 ? "crit" : t >= 84 ? "warn" : ""}">${t != null ? Math.round(t) : "—"}<small>°C</small></dd></div>
    <div><dt>Clock</dt><dd>${c.freq ? (c.freq / 1000).toFixed(2) : "—"}<small>GHz</small></dd></div>
    <div><dt>Load 1m</dt><dd>${c.load[0].toFixed(2)}<small>/ ${INFO.threads}</small></dd></div>
    <div><dt>Ollama</dt><dd>${s.procs ? s.procs.ollama_cpu : "—"}<small>% cpu</small></dd></div>`);
  if (coreEls.length !== c.cores.length) buildCores(c.cores.length);
  c.cores.forEach((v, i) => {
    const el = coreEls[i];
    el.style.transform = `scaleY(${clamp(v / 100, 0.03, 1)})`;
    el.style.backgroundColor = heat(v);
    el.style.boxShadow = v > 60 ? `0 0 10px ${heat(v)}` : "none";
  });
}

function renderMem(s) {
  const m = s.mem;
  if (!m) return;
  $("mem-used").textContent = (m.used / GiB).toFixed(1);
  $("mem-total").textContent = `/ ${(m.total / GiB).toFixed(1)} GiB`;
  $("mem-avail").textContent = fmtBytes(m.available);
  $("mem-sub").textContent = `${m.percent}% in use`;
  const oll = Math.min(s.procs?.ollama_rss || 0, m.used);
  const segs = [
    [oll, C.pink, "Ollama"],
    [m.used - oll, C.amber, "Apps"],
    [m.cache, rgba(C.amber, 0.32), "Cache"],
  ];
  const bar = $("mem-bar");
  if (bar.children.length !== segs.length) bar.innerHTML = segs.map(() => "<i></i>").join("");
  segs.forEach(([v, col], i) => {
    const el = bar.children[i];
    el.style.width = `${(v / m.total * 100).toFixed(2)}%`;
    el.style.background = col;
    el.style.boxShadow = i === 0 && v > 0 ? `0 0 12px ${rgba(C.pink, 0.6)}` : "none";
  });
  setHTML($("mem-legend"), [...segs, [m.free, "rgba(255,255,255,.1)", "Free"]]
    .map(([v, col, name]) => `<span><i class="sw" style="background:${col}"></i>${name} <b>${fmtBytes(v)}</b></span>`).join(""));
  const swPct = m.swap_total ? m.swap_used / m.swap_total * 100 : 0;
  $("swap-fill").style.width = `${swPct}%`;
  $("swap-txt").textContent = m.swap_total ? `${fmtBytes(m.swap_used)} / ${fmtBytes(m.swap_total)}` : "none";
}

function renderProcs(s) {
  const p = s.procs;
  if (!p) return;
  $("proc-count").textContent = `${p.count} running · grouped by name`;
  const topCpu = Math.max(20, ...p.cpu.map((x) => x.cpu));
  const topMem = Math.max(2 * GiB, ...p.mem.map((x) => x.mem));
  const row = (x, val, w) => `<li class="${isRunner(x.name) ? "hl" : ""}" style="--w:${w}%">
      <span class="n">${esc(x.name)}${x.n > 1 ? `<em>×${x.n}</em>` : ""}</span><span class="v">${val}</span></li>`;
  setHTML($("pl-cpu"), p.cpu.slice(0, fitRows($("pl-cpu"), 30)).map((x) => row(x, `${x.cpu.toFixed(1)}%`, (x.cpu / topCpu * 100).toFixed(1))).join(""));
  setHTML($("pl-mem"), p.mem.slice(0, fitRows($("pl-mem"), 30)).map((x) => row(x, fmtBytes(x.mem), (x.mem / topMem * 100).toFixed(1))).join(""));
}

function aiPhase(s) {
  const o = s.ollama, l = s.llm;
  if (!o || !o.online) return "offline";
  if (l && ["loading", "prefill", "generating"].includes(l.phase)) return l.phase;
  return o.models?.length ? "ready" : "idle";
}

function renderAI(s) {
  const o = s.ollama || {}, l = s.llm || {}, procs = s.procs || {};
  const phase = aiPhase(s);
  $("ollama-ver").textContent = o.version ? `ollama v${o.version}` : "ollama";
  const pill = $("ai-phase");
  pill.dataset.phase = phase;
  pill.textContent = {
    offline: "offline", idle: "no model", ready: "ready", loading: "loading",
    prefill: `prefill ${Math.round((l.prefill || 0) * 100)}%`, generating: "generating",
  }[phase];
  $("p-ai").classList.toggle("live", phase === "generating" || phase === "prefill");

  const m = (o.models || [])[0];
  const ld = l.load;
  const nameEl = $("model-name");
  // /api/ps under-reports size on the llama-server engine, so trust the installed
  // model size and the runner's logged layer split when they are available.
  const tagSize = m ? ((o.tags || []).find((t) => t.name === m.name)?.size || 0) : 0;
  const total = m ? Math.max(m.size || 0, tagSize) : 0;
  const gpuFrac = m ? (ld?.layers ? ld.gpu_layers / ld.layers : (m.gpu_pct || 0) / 100) : 0;
  if (m) {
    nameEl.textContent = m.name;
    nameEl.classList.remove("none");
    const chips = [
      m.params && `<b>${esc(m.params)}</b> params`,
      m.quant && esc(m.quant),
      m.family && esc(m.family),
      m.ctx && `ctx <b>${m.ctx.toLocaleString()}</b>`,
      `size <b>${fmtBytes(total)}</b>`,
      ld?.ready_s != null && `loaded in <b>${ld.ready_s}s</b>`,
    ].filter(Boolean);
    setHTML($("model-chips"), chips.map((c) => `<span class="chip">${c}</span>`).join(""));
  } else {
    nameEl.textContent = phase === "offline" ? "Ollama offline" : phase === "loading" ? "Loading model…" : "No model loaded";
    nameEl.classList.add("none");
    const tags = o.tags || [];
    setHTML($("model-chips"), tags.length
      ? `<span class="chip">installed: <b>${tags.map((t) => esc(t.name)).join(", ")}</b></span><span class="chip">loads on first request</span>`
      : `<span class="chip">${phase === "offline" ? esc(o.error || "not reachable on :11434") : "no models installed"}</span>`);
  }

  // keep-alive countdown
  if (m && m.expires) {
    const left = m.expires - Date.now() / 1000;
    const key = `${m.name}|${Math.round(m.expires)}`;
    if (kaMax.key !== key) kaMax = { key, max: Math.max(left, 1) };
    if (left > 86400 * 30) rings.ka.set(1, "∞");
    else {
      const l2 = Math.max(0, left);
      rings.ka.set(l2 / kaMax.max, `${Math.floor(l2 / 60)}:${String(Math.floor(l2 % 60)).padStart(2, "0")}`);
    }
  } else rings.ka.set(0, "—");

  // placement
  const hint = $("pl-hint");
  if (m) {
    const g = gpuFrac * 100, cpuPct = Math.max(0, 100 - g);
    $("place-gpu").style.width = `${g}%`;
    $("place-cpu").style.width = `${cpuPct}%`;
    $("pl-gpu-txt").textContent = `≈${fmtBytes(total * gpuFrac)} · ${Math.round(g)}%`;
    $("pl-cpu-txt").textContent = `≈${fmtBytes(total * (1 - gpuFrac))} · ${Math.round(cpuPct)}%`;
    $("pl-layers").textContent = ld?.layers ? `${ld.gpu_layers}/${ld.layers} layers on GPU${ld.draft ? ` · draft ${ld.draft}` : ""}` : "";
    if (cpuPct >= 1) {
      hint.className = "hint";
      const vr = s.gpu?.vram_total ? Math.round(s.gpu.vram_total / 1024) : "?";
      hint.textContent = `⚠ ${Math.round(cpuPct)}% of the model runs on CPU, so speed is capped by RAM bandwidth. A smaller quant or lower num_ctx could fit it in ${vr} GiB VRAM.`;
    } else {
      hint.className = "hint ok";
      hint.textContent = "✓ Fully offloaded to GPU";
    }
  } else {
    $("place-gpu").style.width = "0%"; $("place-cpu").style.width = "0%";
    $("pl-gpu-txt").textContent = "—"; $("pl-cpu-txt").textContent = "—";
    $("pl-layers").textContent = ld?.layers ? `last load: ${ld.gpu_layers}/${ld.layers} layers on GPU` : "";
    hint.textContent = "";
  }

  // throughput
  const runs = l.runs || [];
  const last = runs[runs.length - 1];
  const live = phase === "generating";
  const lab = $("tps-label");
  lab.classList.toggle("live", live || phase === "prefill");
  if (live) {
    $("tps").textContent = (l.live_tps || 0).toFixed(1);
    lab.textContent = `live · ${l.n_gen} tokens`;
  } else {
    $("tps").textContent = last ? last.gen_tps.toFixed(1) : "0.0";
    lab.textContent = phase === "prefill" ? "reading prompt" : last ? `last run · ${hhmmss(last.t)}` : "no runs yet";
  }
  $("prefill").hidden = phase !== "prefill";
  if (phase === "prefill") {
    $("prefill-fill").style.width = `${(l.prefill || 0) * 100}%`;
    $("prefill-txt").textContent = `PREFILL ${l.prefill_tokens} tok · ${Math.round((l.prefill || 0) * 100)}%`;
  }
  runBars.set(runs, live ? l.live_tps : null);

  tiles($("ai-tiles"), [
    ["Prompt", last?.prompt_tps != null ? `${last.prompt_tps.toFixed(1)}<small>tok/s</small>` : "—"],
    ["Output", last ? `${last.gen_tokens}<small>tok</small>` : "—"],
    ["Draft accept", last?.draft_acc != null ? `${last.draft_acc}<small>%</small>` : "—"],
    ["Runner CPU", `${procs.ollama_cpu ?? 0}<small>%</small>`, procs.ollama_cpu > 70 ? "warn" : ""],
    ["Runner RAM", fmtBytes(procs.ollama_rss || 0)],
  ]);

  const reqs = (l.requests || []).slice().reverse().slice(0, fitRows($("feed"), 24));
  setHTML($("feed"), reqs.length ? reqs.map((r) => `<li class="${r.status >= 400 ? "err" : ""}">
      <span>${hhmmss(r.t)}</span>
      <span class="p">${esc(r.method)} ${esc(r.path)}</span>
      <span class="t">${r.gen_tps != null ? `${r.gen_tps.toFixed(1)} t/s` : ""}</span>
      <span class="d">${r.gen_tokens != null ? `${r.gen_tokens} tok` : r.status}</span>
      <span class="d">${fmtDur(r.dur)}</span></li>`).join("")
    : `<li><span class="empty">No API requests seen yet. Chat with a model and it shows up here.</span></li>`);
  if (runs.length) {
    const avg = runs.reduce((a, r) => a + r.gen_tps, 0) / runs.length;
    $("feed-count").textContent = `avg ${avg.toFixed(1)} tok/s over ${runs.length} run${runs.length > 1 ? "s" : ""}`;
  } else $("feed-count").textContent = "";

  // actions
  const bench = s.bench || {};
  const bb = $("btn-bench");
  bb.disabled = !!bench.running || phase === "offline" || !(o.tags || []).length;
  bb.classList.toggle("busy", !!bench.running);
  $("btn-unload").disabled = !m;
  const br = $("bench-res");
  if (bench.running) setHTML(br, `running on ${esc(bench.model || "model")}…`);
  else if (bench.error) setHTML(br, `<span style="color:var(--red)">bench failed: ${esc(bench.error)}</span>`);
  else if (bench.gen_tps != null) setHTML(br, `bench <b>${bench.gen_tps}</b> tok/s · prompt ${bench.prompt_tps} · load ${bench.load_s}s`);
  else setHTML(br, "");
  setHTML($("installed"), (o.tags || []).slice(0, 3)
    .map((t) => `<span class="chip">${esc(t.name)} <b>${fmtBytes(t.size)}</b></span>`).join(""));
}

const APP_COLORS = [C.violet, C.cyan, C.blue, C.amber, C.green, C.orange];
function renderGPU(s) {
  const g = s.gpu;
  if (!g) { $("gpu-name").textContent = s.errors?.gpu || "waiting for nvidia-smi…"; return; }
  $("gpu-name").textContent = g.name;
  $("gpu-pstate").textContent = g.pstate || "—";
  rings.gpu.set((g.util || 0) / 100, `${Math.round(g.util || 0)}<small>%</small>`);
  rings.vram.set((g.vram_pct || 0) / 100, `${(g.vram_used / 1024).toFixed(1)}<small>G</small>`);
  tiles($("gpu-tiles"), [
    ["Temp", `${g.temp ?? "—"}<small>°C</small>`, g.temp >= 87 ? "crit" : g.temp >= 80 ? "warn" : ""],
    ["Power", `${g.power != null ? g.power.toFixed(0) : "—"}<small>/ ${g.power_limit ?? "?"} W</small>`],
    ["Core", `${g.clock ?? "—"}<small>MHz</small>`],
    ["Memory", `${g.mem_clock ?? "—"}<small>MHz</small>`],
  ]);
  const tot = g.vram_total || 1;
  let ci = 0;
  const apps = (g.apps || []).map((a) => ({ ...a, color: isRunner(a.name) ? C.pink : APP_COLORS[ci++ % APP_COLORS.length] }));
  const known = apps.reduce((a, x) => a + x.mem, 0);
  const segs = [...apps, { name: "driver/other", mem: Math.max(0, (g.vram_used || 0) - known), color: "rgba(255,255,255,.22)" }];
  setHTML($("vram-bar"), segs.map((x) => `<i style="width:${(x.mem / tot * 100).toFixed(2)}%;background:${x.color}"></i>`).join(""));
  setHTML($("vram-legend"), segs.filter((x) => x.mem > 0).slice(0, 5)
    .map((x) => `<span><i class="sw" style="background:${x.color}"></i>${esc(x.name)} <b>${fmtBytes(x.mem * 1024 * 1024)}</b></span>`).join(""));
  $("vram-txt").textContent = `${(g.vram_used / 1024).toFixed(2)} / ${(tot / 1024).toFixed(1)} GiB`;
}

function renderNet(s) {
  if (!s.io) return;
  $("net-rx").innerHTML = rateHTML(s.io.rx);
  $("net-tx").innerHTML = rateHTML(s.io.tx);
  const ifs = s.ifaces?.list || [];
  $("net-ifaces").textContent = ifs.map((i) => i.name).join(" · ") || "—";
  const total = (b) => { const [v, u] = fmtRate(b); return `${v} ${u.replace("/s", "")}`; };
  setHTML($("net-foot"), ifs.map((i) => `<span>${esc(i.name)} <b>${esc(maskIP(i.ip))}</b>${i.speed ? ` · ${i.speed} Mb/s` : ""}</span>`).join("")
    + `<span>since boot ↓ <b>${total(s.io.rx_total)}</b> ↑ <b>${total(s.io.tx_total)}</b></span>`);
}

const ROLE = { system: "system", windows: "windows", usb: "usb", data: "data" };
const fmtTB = (b) => (b >= 1e12 ? `${(b / 1e12).toFixed(1)} TB` : `${(b / 1e9).toFixed(0)} GB`);
const fmtHours = (h) => (h >= 8760 ? `${(h / 8760).toFixed(1)} y` : h >= 1000 ? `${(h / 1000).toFixed(1)}k h` : `${h} h`);
const shortRate = (b) => { const [v, u] = fmtRate(b); return `${v}${u.replace("B/s", "").replace("/s", "") || "B"}`; };

function partLabel(p) {
  const size = fmtBytes(p.size, 0);
  if (p.mount) {
    const pct = p.fssize ? Math.round(p.used / p.fssize * 100) : null;
    return `${esc(p.mount)}<em>${pct != null ? `${pct}% · ${fmtBytes(p.used, 0)} / ${fmtBytes(p.fssize, 0)}` : size}</em>`;
  }
  if (p.encrypted) return `🔒 ${esc(p.fs)}<em>${size}</em>`;
  const name = p.label && !/partition$/i.test(p.label) ? p.label : (p.fs || "unformatted");
  return `${esc(name)}<em>${p.fs ? `${esc(p.fs.toUpperCase())} · ` : ""}${size} · not mounted</em>`;
}

function driveHealth(h) {
  if (!h || (h.wear == null && h.power_on_h == null)) return `<span>no SMART data (USB bridge)</span>`;
  const out = [];
  const problems = [];
  if (h.critical) problems.push("critical warning");
  if (h.media_errors) problems.push(`${h.media_errors} media errors`);
  if (h.spare != null && h.spare_thresh != null && h.spare <= h.spare_thresh) problems.push("spare low");
  if (problems.length) out.push(`<span class="bad">⚠ ${problems.join(" · ")}</span>`);
  else if (h.wear >= 80) out.push(`<span class="warn">⚠ worn</span>`);
  else out.push(`<span class="ok">✓ healthy</span>`);
  if (h.wear != null) out.push(`<span>wear <b>${h.wear}%</b></span>`);
  if (h.written != null) out.push(`<span>written <b>${fmtTB(h.written)}</b></span>`);
  if (h.power_on_h != null) out.push(`<span>on <b>${fmtHours(h.power_on_h)}</b></span>`);
  if (h.unsafe_shutdowns) out.push(`<span>unsafe off <b>${h.unsafe_shutdowns}</b></span>`);
  return out.join("");
}

function renderDisk(s) {
  if (!s.io) return;
  $("disk-rd").innerHTML = rateHTML(s.io.rd);
  $("disk-wr").innerHTML = rateHTML(s.io.wr);
  const st = s.storage;
  if (!st) return;
  const drives = st.drives || [];
  const total = drives.reduce((a, d) => a + d.size, 0);
  const root = drives.flatMap((d) => d.parts).find((p) => p.mount === "/");
  $("disk-sub").textContent = `${drives.length} drives · ${fmtTB(total)}${root?.fssize ? ` · ${fmtBytes(root.fssize - root.used, 0)} free on /` : ""}`;
  const io = s.disk_io || {}, temps = s.drive_temps || {};
  setHTML($("drives"), drives.map((d) => {
    const t = temps[d.name];
    const r = io[d.name] || { rd: 0, wr: 0 };
    const busy = r.rd + r.wr > 1024;
    const model = d.model.replace(new RegExp(`^${d.vendor || ""}\\s*`, "i"), "").split("-")[0];
    const name = d.vendor && d.vendor !== d.model ? `${esc(d.vendor)}<small>${esc(model === "Generic" ? "" : model)}</small>` : esc(d.model || d.name);
    const segs = d.parts.length ? d.parts : [{ size: d.size, fs: null, label: "unpartitioned" }];
    return `<div class="drive ${ROLE[d.role] || ""}">
      <div class="drv-head">
        <span class="drv-tag">${esc(d.bus || "DISK")}</span>
        <span class="drv-name">${name}<em>${fmtTB(d.size)} · ${esc(d.role)}</em></span>
        <span class="drv-io">${busy ? `<span class="r">↓<b>${shortRate(r.rd)}</b></span><span class="w">↑<b>${shortRate(r.wr)}</b></span>` : `<span class="idle">idle</span>`}</span>
        <span class="drv-temp ${t >= 70 ? "crit" : t >= 60 ? "warn" : ""}">${t != null ? `${Math.round(t)}°C` : ""}</span>
      </div>
      <div class="pmap">${segs.map((p) => {
        const pct = p.mount && p.fssize ? p.used / p.fssize * 100 : 0;
        const cls = p.mount ? `mounted${pct >= 90 ? " full" : ""}` : p.encrypted ? "locked" : "idle";
        const wide = p.size / d.size > 0.12;
        return `<i class="seg ${cls}" style="flex-grow:${p.size}">${p.mount ? `<b style="width:${pct.toFixed(1)}%"></b>` : ""}${wide ? `<span>${partLabel(p)}</span>` : ""}</i>`;
      }).join("")}</div>
      <div class="drv-health">${driveHealth(d.health)}</div>
    </div>`;
  }).join(""));
  const extra = [];
  const tags = s.ollama?.tags || [];
  if (tags.length) extra.push(`<span>Ollama models <b>${fmtBytes(tags.reduce((a, t) => a + t.size, 0))}</b></span>`);
  const dk = st.docker;
  const dsize = (s) => esc(String(s || "").replace(/\s*\(.*\)/, "").replace(/([\d.]+)\s*([kKMGT]?B)/, (_, n, u) => `${(+n).toFixed(1)} ${u}`));
  if (dk?.Images) {
    const vol = dk["Local Volumes"]?.size;
    extra.push(`<span>Docker <b>${dsize(dk.Images.size)}</b>${vol ? ` + vol ${dsize(vol)}` : ""} · ${dsize(dk.Images.reclaimable)} reclaimable</span>`);
  }
  setHTML($("disk-extra"), extra.join(""));
}

const DEV = new Set(["node", "bun", "deno", "python", "python3", "uvicorn", "gunicorn", "java", "ruby", "php", "go", "cargo", "dotnet", "npm", "pnpm", "vite", "next-server"]);
function renderDev(s) {
  const rank = (p) => (p.mine && (DEV.has(p.name) || p.project) ? 0 : ["ollama", "sysdeck"].includes(p.name) ? 1 : 2);
  const ports = (s.ports || []).slice().sort((a, b) => rank(a) - rank(b) || a.port - b.port).slice(0, fitRows($("ports"), 33));
  setHTML($("ports"), ports.length ? ports.map((p) => {
    const dev = p.mine && (DEV.has(p.name) || p.project);
    const lan = p.addr === "0.0.0.0" || p.addr === "::";
    return `<li class="${dev ? "" : "sys"}"><span class="port">:${p.port}</span>
      <span class="who">${esc(p.name || "system")}${p.project && !PRIVACY ? `<em>${esc(p.project)}</em>` : ""}</span>
      <span class="scope ${lan ? "lan" : ""}">${lan ? "LAN" : "local"}</span></li>`;
  }).join("") : `<li class="sys"><span class="who">nothing listening</span></li>`);
  const d = s.docker || {};
  $("docker-state").textContent = d.available ? `${(d.containers || []).length} running` : "daemon offline";
  setHTML($("containers"), (d.containers || []).length
    ? d.containers.map((c) => `<li><span class="nm">${esc(c.name)}<em>${esc(c.image)}</em></span><span class="st">${esc(c.status)}</span></li>`).join("")
    : `<li class="empty">${d.available ? "no containers running" : "docker not reachable"}</li>`);
}

/* ───────────── clock ───────────── */
function clock() {
  const d = new Date();
  $("time").textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  $("secs").textContent = String(d.getSeconds()).padStart(2, "0");
  $("date").textContent = d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}
clock();
setInterval(clock, 1000);

/* ───────────── actions ───────────── */
async function post(path, body) {
  try {
    await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Sysdeck": "1" }, body: JSON.stringify(body || {}) });
  } catch (e) { console.warn(path, e); }
}
$("btn-bench").addEventListener("click", () => {
  const m = (S.ollama?.models || [])[0]?.name || (S.ollama?.tags || [])[0]?.name;
  post("/api/bench", { model: m });
});
$("btn-unload").addEventListener("click", () => post("/api/unload"));

/* ───────────── stream ───────────── */
function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("init", (ev) => {
    const s = JSON.parse(ev.data);
    INFO = s.info || {};
    $("host").textContent = PRIVACY ? "privacy mode" : INFO.host || "";
    $("os").textContent = INFO.os || "";
    $("kernel").textContent = INFO.kernel || "";
    $("cpu-model").textContent = `${(INFO.cpu || "").replace(/ with Radeon.*$/, "")} · ${INFO.cores}C/${INFO.threads}T`;
    for (const k of KEYS) H[k] = (s.history?.[k] || []).slice(-300);
    Hcount = H.cpu.length;
    lastTick = performance.now();
    $("conn").hidden = true;
    render(s);
  });
  es.onmessage = (ev) => {
    const s = JSON.parse(ev.data);
    pushHistory(s);
    render(s);
    if (FPS === 0) drawCharts(1);
  };
  es.onerror = () => { $("conn").hidden = false; };
  es.onopen = () => { $("conn").hidden = true; };
}
connect();

/* ───────────── animation loop (≈30 fps, paused when hidden) ───────────── */
// ?fps=0 draws once per data tick (cheapest); higher values scroll charts smoothly.
const FPS = Math.max(0, Math.min(60, Number(new URLSearchParams(location.search).get("fps") ?? 10)));
let lastFrame = 0;
function drawCharts(frac) { for (const c of charts) c.draw(frac); }
if (FPS > 0) {
  const interval = 1000 / FPS;
  const frame = (now) => {
    if (!document.hidden && now - lastFrame >= interval - 2) {
      lastFrame = now;
      drawCharts(clamp((now - lastTick) / 1000));
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

/* the dashboard never scrolls; undo any scroll the engine applies when zoom changes */
const unscroll = () => { if (window.scrollX || window.scrollY) window.scrollTo(0, 0); };
window.addEventListener("resize", unscroll);
window.addEventListener("scroll", unscroll, { passive: true });
