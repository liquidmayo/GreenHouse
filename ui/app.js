/* GreenHouse Monitor dashboard — polls /api/state and renders machines/components. */

const POLL_MS = 5000;
const SEV = { ok: 0, unknown: 0, warn: 1, crit: 2 };

let failures = 0;

function fmtAge(s) {
  if (s == null) return "—";
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  return (s / 3600).toFixed(1) + "h";
}

function fmtTs(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour12: false });
}

function metricChip(key, val) {
  let label = key, shown = val;
  if (key.endsWith("_age_s") || key.endsWith("_silence_s")) {
    label = key.replace(/_s$/, "");
    shown = fmtAge(val);
  } else if (key.endsWith("_ms")) {
    shown = val + "ms";
  } else if (key.endsWith("_mb")) {
    shown = val + " MB";
  } else if (key.endsWith("_gb")) {
    shown = val + " GB";
  } else if (key.endsWith("_pct")) {
    shown = val + "%";
  }
  label = label.replace(/_/g, " ");
  return `<span class="chip">${esc(label)} <b>${esc(String(shown))}</b></span>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function featuredLabel(key) {
  if (key.toLowerCase().includes("listener")) return "LISTENERS";
  if (key.toLowerCase().includes("viewer")) return "VIEWERS";
  return key.replace(/_/g, " ").toUpperCase();
}

function renderCard(comp) {
  const st = comp.status || "unknown";
  const featuredKeys = [...(comp.featured || []), ...(comp.featured_card || [])];
  let featuredRow = "";
  if (featuredKeys.length) {
    featuredRow = `<div class="card-featured">` + featuredKeys.map(k => {
      const v = (comp.metrics || {})[k];
      const missing = v === undefined || v === null;
      return `<div><div class="cf-value ${missing ? "missing" : ""}">${missing ? "—" : esc(String(v))}</div>
              <div class="cf-label">${esc(featuredLabel(k))}</div></div>`;
    }).join("") + `</div>`;
  }
  const chips = Object.entries(comp.metrics || {})
    .filter(([k]) => !featuredKeys.includes(k))
    .slice(0, 8)
    .map(([k, v]) => metricChip(k, v))
    .join("");
  const strip = (comp.strip || [])
    .map(t => `<span class="tick ${t.s}" title="${fmtTs(t.ts)} ${t.s}"></span>`)
    .join("");
  return `
    <div class="card ${st}">
      <div class="card-head">
        <span class="dot ${st}"></span>
        <span class="card-title">${esc(comp.label)}</span>
        <span class="card-status ${st}">${st.toUpperCase()}</span>
      </div>
      <div class="card-summary">${esc(comp.summary || "")}</div>
      ${featuredRow}
      <div class="chips">${chips}</div>
      <div class="strip">${strip}</div>
    </div>`;
}

function sysStatClass(pct, warnAt, critAt) {
  if (pct >= (critAt || 95)) return "crit";
  if (pct >= (warnAt || 85)) return "warn";
  return "";
}

function renderMachine(name, m) {
  const sys = m.system || {};
  const disks = (sys.disks || []).map(d => {
    if (d.error) return `<span class="sysstat warn">${esc(d.mount)} <b>?</b></span>`;
    return `<span class="sysstat ${sysStatClass(d.used_pct)}">${esc(d.mount)} <b>${d.used_pct}%</b> (${d.free_gb} GB free)</span>`;
  }).join("");
  const cards = (m.components || [])
    .filter(c => !c.hero_only)
    .map(renderCard).join("");
  return `
    <section class="machine">
      <div class="machine-head">
        <span class="machine-name ${m.offline ? "offline" : ""}">${esc(name)}</span>
        <span class="machine-meta">${m.offline ? "OFFLINE — last seen " + fmtAge(m.agent_age_s) + " ago" : "updated " + fmtAge(m.agent_age_s) + " ago"} · agent v${esc(m.agent_version || "?")}</span>
        <div class="sysbar">
          ${sys.cpu_pct != null ? `<span class="sysstat ${sysStatClass(sys.cpu_pct, 92)}">CPU <b>${sys.cpu_pct}%</b></span>` : ""}
          ${sys.mem_pct != null ? `<span class="sysstat ${sysStatClass(sys.mem_pct, 92)}">MEM <b>${sys.mem_pct}%</b></span>` : ""}
          ${disks}
        </div>
      </div>
      <div class="cards">${cards}</div>
    </section>`;
}

function renderBanner(machines) {
  const banner = document.getElementById("banner");
  const dot = document.getElementById("live-dot");
  const problems = [];
  let worst = "ok";
  for (const [name, m] of Object.entries(machines)) {
    if (m.offline) {
      problems.push(`${name}: agent offline`);
      if (SEV.crit > SEV[worst]) worst = "crit";
    }
    for (const c of m.components || []) {
      if (c.status === "crit" || c.status === "warn") {
        problems.push(`${name} / ${c.label}${c.summary ? " — " + c.summary : ""}`);
        if (SEV[c.status] > SEV[worst]) worst = c.status;
      }
    }
    if ((m.system || {}).status === "warn" || (m.system || {}).status === "crit") {
      problems.push(`${name}: ${(m.system.notes || []).join(", ")}`);
      if (SEV[m.system.status] > SEV[worst]) worst = m.system.status;
    }
  }
  dot.className = "brand-dot " + (worst === "ok" ? "" : worst);
  banner.classList.remove("hidden", "ok", "warn", "crit");
  if (problems.length === 0) {
    banner.classList.add("ok");
    banner.textContent = "ALL SYSTEMS NOMINAL";
  } else {
    banner.classList.add(worst);
    banner.textContent = (worst === "crit" ? "⬤ CRITICAL — " : "⬤ ATTENTION — ") +
      problems.slice(0, 4).join("  |  ") +
      (problems.length > 4 ? `  (+${problems.length - 4} more)` : "");
  }
}

let uiCfg = {};

function renderHero(machines) {
  const hero = document.getElementById("hero");
  const merges = (uiCfg.hero_merge || []);
  const mergedKeys = new Set(merges.map(mg => mg.metric));
  const mergedTiles = new Map();  // metric -> {label, total, parts:[{src, v}]}
  const tiles = [];

  for (const [name, m] of Object.entries(machines)) {
    for (const c of m.components || []) {
      for (const key of c.featured || []) {
        const v = (c.metrics || {})[key];
        const missing = v === undefined || v === null;
        if (mergedKeys.has(key)) {
          const mg = merges.find(x => x.metric === key);
          if (!mergedTiles.has(key)) {
            mergedTiles.set(key, {label: mg.label || featuredLabel(key), total: 0, any: false, parts: []});
          }
          const t = mergedTiles.get(key);
          if (!missing) { t.total += Number(v); t.any = true; }
          t.parts.push({src: name === Object.keys(machines)[0] ? "local" : name.toLowerCase(), v: missing ? "—" : v});
          continue;
        }
        tiles.push({
          html: `
          <div class="tile ${missing ? "missing" : ""}" data-machine="${esc(name)}" data-component="${esc(c.id)}" data-metric="${esc(key)}" data-title="${esc(c.label.toUpperCase() + " " + featuredLabel(key))}" title="Click for history">
            <div class="tile-value">${missing ? "—" : esc(String(v))}</div>
            <div class="tile-label">${esc(c.label.toUpperCase())} ${esc(featuredLabel(key))}</div>
            ${missing ? `<div class="tile-note">awaiting credentials in monitors.yml</div>` : ""}
          </div>`,
          order: 1,
        });
      }
    }
  }
  // merged tiles go first — they're the headline aggregates
  const mergedHtml = [];
  for (const [key, t] of mergedTiles) {
    const breakdown = t.parts.length > 1
      ? `<div class="tile-note">${t.parts.map(p => `${esc(String(p.v))} ${esc(p.src)}`).join(" + ")}</div>` : "";
    mergedHtml.push(`
      <div class="tile ${t.any ? "" : "missing"}" data-merged="${esc(key)}" data-title="${esc(t.label)}" title="Click for history">
        <div class="tile-value">${t.any ? esc(String(t.total)) : "—"}</div>
        <div class="tile-label">${esc(t.label)}</div>
        ${breakdown}
      </div>`);
  }
  const all = [...mergedHtml, ...tiles.map(t => t.html)];
  if (all.length === 0) {
    hero.classList.add("hidden");
  } else {
    hero.classList.remove("hidden");
    hero.innerHTML = all.join("");
  }
}

// ---------- history modal ----------
let trendCtx = null;   // {title, params, hours}

async function openTrend(el) {
  const title = el.dataset.title;
  const params = el.dataset.merged
    ? {metric: el.dataset.merged, merged: 1}
    : {machine: el.dataset.machine, component: el.dataset.component, metric: el.dataset.metric};
  trendCtx = {title, params, hours: 24};
  document.getElementById("trend-modal").classList.remove("hidden");
  document.getElementById("trend-title").textContent = title;
  await loadTrend();
}

async function loadTrend() {
  if (!trendCtx) return;
  const q = new URLSearchParams({...trendCtx.params, hours: trendCtx.hours});
  document.querySelectorAll(".trend-range button").forEach(b =>
    b.classList.toggle("active", Number(b.dataset.h) === trendCtx.hours));
  const el = document.getElementById("trend-stats");
  el.textContent = "loading…";
  try {
    const rows = await (await fetch("/api/trend?" + q)).json();
    drawTrend(rows);
    if (rows.length) {
      const avgs = rows.map(r => r.avg), peaks = rows.map(r => r.peak);
      const overall = avgs.reduce((a, b) => a + b, 0) / avgs.length;
      el.textContent = `peak ${Math.max(...peaks)} · avg ${overall.toFixed(1)} · low ${Math.min(...avgs)} · ${rows.length} samples (5-min buckets)`;
    } else {
      el.textContent = "no history yet for this range — data accumulates from now on";
    }
  } catch (e) {
    el.textContent = "failed to load history";
  }
}

function drawTrend(rows) {
  const canvas = document.getElementById("trend-canvas");
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  if (!rows.length) return;
  const padL = 44, padR = 12, padT = 12, padB = 26;
  const xs = rows.map(r => r.ts), ys = rows.map(r => r.avg), ps = rows.map(r => r.peak);
  const x0 = Math.min(...xs), x1 = Math.max(...xs) || x0 + 1;
  const yMax = Math.max(1, ...ps) * 1.1, yMin = 0;
  const X = t => padL + (t - x0) / Math.max(1, x1 - x0) * (W - padL - padR);
  const Y = v => H - padB - (v - yMin) / (yMax - yMin) * (H - padT - padB);
  // grid + y labels
  ctx.strokeStyle = "rgba(34,197,94,0.12)"; ctx.fillStyle = "#5f7f66";
  ctx.font = "11px Consolas, monospace"; ctx.textAlign = "right"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = yMin + (yMax - yMin) * i / 4, y = Y(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillText(Math.round(v), padL - 6, y + 4);
  }
  // x labels
  ctx.textAlign = "center";
  const span = x1 - x0, ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const t = x0 + span * i / ticks, d = new Date(t * 1000);
    const lbl = span > 2 * 86400 ? d.toLocaleDateString([], {month: "numeric", day: "numeric"})
                                 : d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false});
    ctx.fillText(lbl, X(t), H - 8);
  }
  // peak band
  ctx.beginPath();
  rows.forEach((r, i) => { const x = X(r.ts), y = Y(r.peak); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = "rgba(34,197,94,0.35)"; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);
  // avg fill + line
  ctx.beginPath();
  rows.forEach((r, i) => { const x = X(r.ts), y = Y(r.avg); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  const last = rows[rows.length - 1];
  ctx.lineTo(X(last.ts), Y(0)); ctx.lineTo(X(rows[0].ts), Y(0)); ctx.closePath();
  const grad = ctx.createLinearGradient(0, padT, 0, H - padB);
  grad.addColorStop(0, "rgba(34,197,94,0.35)"); grad.addColorStop(1, "rgba(34,197,94,0.02)");
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  rows.forEach((r, i) => { const x = X(r.ts), y = Y(r.avg); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = "#22c55e"; ctx.lineWidth = 2; ctx.shadowColor = "rgba(34,197,94,0.8)"; ctx.shadowBlur = 8;
  ctx.stroke(); ctx.shadowBlur = 0;
}

function closeTrend() {
  trendCtx = null;
  document.getElementById("trend-modal").classList.add("hidden");
}

document.addEventListener("click", e => {
  const tile = e.target.closest("#hero .tile");
  if (tile) { openTrend(tile); return; }
  if (e.target.closest("#trend-close") || e.target.id === "trend-modal") { closeTrend(); return; }
  const rb = e.target.closest(".trend-range button");
  if (rb && trendCtx) { trendCtx.hours = Number(rb.dataset.h); loadTrend(); }
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeTrend(); });

function renderCalls(calls) {
  const rate = document.getElementById("calls-rate");
  const el = document.getElementById("calls");
  if (!calls || !calls.recent || calls.recent.length === 0) {
    rate.textContent = "";
    el.innerHTML = `<div class="events-empty">No calls received — configure the SDRTrunk webhook stream to feed this panel.</div>`;
    return;
  }
  rate.textContent = `· ${calls.last_min}/min · ${calls.last_hour}/hr · last ${fmtAge(calls.last_call_age_s)} ago`;
  el.innerHTML = calls.recent.map(c => `
    <div class="event-row">
      <span class="event-ts">${fmtTs(c.ts)}</span>
      <span class="call-sys">${esc(c.system || "?")}</span>
      <span class="call-tg">${esc(c.talkgroup || "?")}</span>
      <span class="event-msg">${esc(c.radio || "")}</span>
      <span class="call-dur">${c.duration_s != null ? Number(c.duration_s).toFixed(1) + "s" : ""}</span>
    </div>`).join("");
}

function renderEvents(events) {
  const el = document.getElementById("events");
  if (!events || events.length === 0) {
    el.innerHTML = `<div class="events-empty">No recent events.</div>`;
    return;
  }
  el.innerHTML = events.map(e => `
    <div class="event-row">
      <span class="event-ts">${fmtTs(e.ts)}</span>
      <span class="event-src">${esc(e.machine)} / ${esc(e.component_label || e.component)}</span>
      <span class="event-label ${esc(e.level)}">${esc(e.label)}</span>
      <span class="event-msg">${esc(e.message || "")}</span>
    </div>`).join("");
}

async function refresh() {
  const conn = document.getElementById("conn-state");
  try {
    const resp = await fetch("/api/state");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    failures = 0;
    conn.textContent = "LIVE";
    conn.className = "conn-state live";

    if (data.brand) {
      document.getElementById("brand-title").textContent = data.brand;
      document.getElementById("brand-footer").textContent = data.brand + " — INFRASTRUCTURE MONITOR";
      document.title = data.brand;
    }
    uiCfg = data.ui || {};
    const machines = data.machines || {};
    const container = document.getElementById("machines");
    const names = Object.keys(machines).sort();
    if (names.length > 0) {
      container.innerHTML = names.map(n => renderMachine(n, machines[n])).join("");
    }
    renderBanner(machines);
    renderHero(machines);
    renderCalls(data.calls);
    renderEvents(data.events);
  } catch (err) {
    failures++;
    if (failures >= 2) {
      conn.textContent = "CONNECTION LOST";
      conn.className = "conn-state lost";
      document.getElementById("live-dot").className = "brand-dot crit";
    }
  }
}

function tickClock() {
  document.getElementById("clock").textContent =
    new Date().toLocaleString([], { hour12: false });
}

setInterval(refresh, POLL_MS);
setInterval(tickClock, 1000);
tickClock();
refresh();
