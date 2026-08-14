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
  const cards = (m.components || []).map(renderCard).join("");
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

function renderHero(machines) {
  const hero = document.getElementById("hero");
  const tiles = [];
  for (const [name, m] of Object.entries(machines)) {
    for (const c of m.components || []) {
      for (const key of c.featured || []) {
        const v = (c.metrics || {})[key];
        const missing = v === undefined || v === null;
        tiles.push(`
          <div class="tile ${missing ? "missing" : ""}">
            <div class="tile-value">${missing ? "—" : esc(String(v))}</div>
            <div class="tile-label">${esc(c.label.toUpperCase())} ${esc(featuredLabel(key))}</div>
            ${missing ? `<div class="tile-note">awaiting credentials in monitors.yml</div>` : ""}
          </div>`);
      }
    }
  }
  if (tiles.length === 0) {
    hero.classList.add("hidden");
  } else {
    hero.classList.remove("hidden");
    hero.innerHTML = tiles.join("");
  }
}

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
