"use strict";
/* global api, showTab, escapeHtml — provided by the SerenMeninges shell.
 *
 * Contract (see VIEWER-CUTOVER-PLAYBOOK.md):
 *   api(path, opts)  — fetch that auto-attaches the saved bearer token and
 *                      returns PARSED JSON (not a Response). Throws on
 *                      network/parse failure; error bodies come back as data.
 *   escapeHtml(s)    — HTML-escape a string.
 *   showTab(id)      — shell tab helper (we do our own panel toggling below,
 *                      which is fine; we just never redefine the helpers).
 */

// ── Tabs ───────────────────────────────────────────────────────────
// The shell owns show/hide via showTab() (toggles the active .tabbar .tab and
// the .view whose id === the tab) and auto-activates the first tab on load.
// We wire the clicks (the shell doesn't) and lazy-load each view on first open.
const _loaded = {};
function lazyLoad(tab) {
  if (!tab || _loaded[tab]) return;
  _loaded[tab] = true;
  REFRESH[tab]?.();
}
document.querySelectorAll(".tabbar .tab, .tab-link").forEach(el =>
  el.addEventListener("click", e => {
    e.preventDefault();
    const tab = el.dataset.tab;
    if (!tab) return;
    showTab(tab);        // shell: toggles the active .tabbar .tab + .view by id
    lazyLoad(tab);
  })
);

// ── Refresh buttons ───────────────────────────────────────────────
document.querySelectorAll("[data-refresh]").forEach(b =>
  b.addEventListener("click", () => REFRESH[b.dataset.refresh]?.()));
document.getElementById("refresh-all")?.addEventListener("click", () =>
  Object.values(REFRESH).forEach(fn => fn()));

// ── Shared helpers ────────────────────────────────────────────────
function setLoading(el) { el.className = "loading"; el.textContent = "loading…"; }
function showError(el, e) {
  el.className = "";
  el.innerHTML =
    `<div class="note err">failed to load: ${escapeHtml(e.message || String(e))}</div>`;
}

// ── Header summary ────────────────────────────────────────────────
async function refreshSummary() {
  const pill = document.getElementById("status-pill");
  try {
    const data = await api("/");
    if (pill) {
      pill.className = "status-pill ok";
      pill.textContent = `v${data.version} · ${data.tools_count} tools`;
    }
  } catch (e) {
    if (pill) { pill.className = "status-pill err"; pill.textContent = "disconnected"; }
  }
}

// ── Tools panel ──────────────────────────────────────────────────
let TOOLS_DATA = [];
async function refreshTools() {
  const body = document.getElementById("tools-body");
  setLoading(body);
  try {
    const data = await api("/tools");
    TOOLS_DATA = data.tools || [];
    document.getElementById("tools-info").textContent =
      `${data.count ?? TOOLS_DATA.length} tools registered`;
    renderTools(document.getElementById("tools-filter").value);
  } catch (e) { showError(body, e); }
}
document.getElementById("tools-filter")?.addEventListener("input",
  e => renderTools(e.target.value));

function renderTools(filter) {
  const body = document.getElementById("tools-body");
  const f = (filter || "").trim().toLowerCase();
  const filtered = !f ? TOOLS_DATA : TOOLS_DATA.filter(t =>
    t.name.toLowerCase().includes(f) ||
    (t.description || "").toLowerCase().includes(f));
  body.className = "";
  if (filtered.length === 0) {
    body.innerHTML = `<div class="empty">${
      TOOLS_DATA.length ? `no tools match "${escapeHtml(f)}"`
                        : "no tools registered"}</div>`;
    return;
  }
  body.innerHTML = filtered.map(toolCardHtml).join("");
  body.querySelectorAll(".card-head").forEach(h =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("expanded")));
}

function toolCardHtml(t) {
  const badge = `<span class="badge ${t.type}">${escapeHtml(t.type)}</span>`;
  const enabledBadge = t.enabled
    ? `<span class="badge ok">enabled</span>`
    : `<span class="badge disabled">disabled</span>`;
  const params = t.parameters || [];
  const paramsHtml = params.length === 0
    ? `<div class="no-params">no parameters</div>`
    : `<table class="params">
         <thead><tr><th>name</th><th>type</th><th>required</th><th>default</th><th>description</th></tr></thead>
         <tbody>${params.map(p => `
           <tr>
             <td class="pname">${escapeHtml(p.name)}</td>
             <td class="ptype">${escapeHtml(p.type || "")}</td>
             <td class="preq">${p.required ? "yes" : "—"}</td>
             <td class="pdef">${p.default == null ? "—" : escapeHtml(String(p.default))}</td>
             <td class="pdesc">${escapeHtml(p.description || "")}</td>
           </tr>`).join("")}</tbody>
       </table>`;
  return `
    <div class="tool-card">
      <div class="card-head">
        <span class="caret">▸</span>
        <span class="name">${escapeHtml(t.name)}</span>
        ${badge} ${enabledBadge}
        <span class="src">${escapeHtml(t.source || "")}</span>
      </div>
      <div class="card-body">
        <div class="desc">${escapeHtml(t.description || "(no description)")}</div>
        ${paramsHtml}
      </div>
    </div>`;
}

// ── Tool State panel ─────────────────────────────────────────────
let STATE_DATA = { tools: [] };
async function refreshState() {
  const body = document.getElementById("state-body");
  setLoading(body);
  try {
    STATE_DATA = await api("/tools/state");
    const total = STATE_DATA.tools.length;
    const disabled = STATE_DATA.tools.filter(t => !t.enabled).length;
    document.getElementById("state-info").textContent =
      `${total} tools · ${disabled} disabled`;
    renderState(document.getElementById("state-filter").value);
  } catch (e) { showError(body, e); }
}
document.getElementById("state-filter")?.addEventListener("input",
  e => renderState(e.target.value));

function renderState(filter) {
  const body = document.getElementById("state-body");
  const f = (filter || "").trim().toLowerCase();
  const filtered = !f ? STATE_DATA.tools : STATE_DATA.tools.filter(t =>
    t.name.toLowerCase().includes(f));
  body.className = "";
  if (filtered.length === 0) {
    body.innerHTML = `<div class="empty">${
      STATE_DATA.tools.length ? `no tools match "${escapeHtml(f)}"`
                              : "no tools registered"}</div>`;
    return;
  }
  body.innerHTML = filtered.map(stateCardHtml).join("");
  body.querySelectorAll(".state-head").forEach(h =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("expanded")));
  body.querySelectorAll(".toggle-switch input").forEach(input =>
    input.addEventListener("change", handleToggle));
}

function stateCardHtml(t) {
  const badge = t.enabled
    ? `<span class="badge ok">enabled</span>`
    : `<span class="badge disabled">disabled</span>`;
  const actions = t.actions || [];
  const actionsHtml = actions.length === 0
    ? `<div class="no-params">no sub-actions</div>`
    : `<div>${actions.map(a => `
          <div class="toggle-row">
            <label class="toggle-switch">
              <input type="checkbox" ${a.enabled ? "checked" : ""}
                     data-tool="${escapeHtml(t.name)}"
                     data-action="${escapeHtml(a.name)}">
              <span class="toggle-slider"></span>
            </label>
            <span class="toggle-label">${escapeHtml(a.name)}</span>
            <span class="toggle-desc">${escapeHtml(a.description || "")}</span>
          </div>`).join("")}</div>`;
  return `
    <div class="state-card">
      <div class="state-head">
        <span class="state-caret">▸</span>
        <span class="state-name">${escapeHtml(t.name)}</span>
        ${badge}
      </div>
      <div class="state-body">
        <div class="toggle-row toggle-row-primary">
          <label class="toggle-switch">
            <input type="checkbox" ${t.enabled ? "checked" : ""}
                   data-tool="${escapeHtml(t.name)}" data-action="">
            <span class="toggle-slider"></span>
          </label>
          <span class="toggle-label strong">enable/disable tool</span>
          <span class="toggle-desc">${escapeHtml(t.description || "")}</span>
        </div>
        ${actionsHtml}
      </div>
    </div>`;
}

async function handleToggle(e) {
  const input = e.target;
  const tool = input.dataset.tool;
  const action = input.dataset.action || "";
  const enabled = input.checked;
  try {
    const data = await api("/tools/state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool, action: action || undefined, enabled }),
    });
    if (data && data.ok === false) {
      input.checked = !enabled;  // server refused — revert
      console.error("toggle failed:", data.error);
    }
  } catch (err) {
    input.checked = !enabled;    // network/HTTP error — revert
    console.error("toggle error:", err);
  }
}

// ── Config panel ─────────────────────────────────────────────────
async function refreshConfig() {
  const body = document.getElementById("config-body");
  setLoading(body);
  try {
    const data = await api("/config");
    const overrides = data.tool_overrides || {};
    const n = Object.keys(overrides).length;
    document.getElementById("config-info").textContent =
      n ? `${n} tool override(s)` : "call-site defaults — no overrides";

    let html = "";
    html += configSection("server", data.server);
    html += configSection("tls", data.tls);
    html += configSection("dashboard", data.dashboard);
    if (n) {
      for (const [tool, kv] of Object.entries(overrides)) {
        html += configSection("override · " + tool, kv);
      }
    } else {
      html += `<div class="note"><span class="label">tool overrides:</span>
        none set — every tool is running on its Nano-floor call-site default.</div>`;
    }
    body.className = "";
    body.innerHTML = html;
  } catch (e) { showError(body, e); }
}

function configSection(title, obj) {
  obj = obj || {};
  const rows = Object.entries(obj).map(([k, v]) =>
    `<tr><td class="k">${escapeHtml(k)}</td>
         <td class="v">${escapeHtml(maskSecret(k, v))}</td></tr>`).join("");
  return `<div class="config-section">
    <h4>${escapeHtml(title)}</h4>
    <table class="kv-table">${rows ||
      `<tr><td class="k">—</td><td class="v">—</td></tr>`}</table>
  </div>`;
}

function maskSecret(key, value) {
  // Defence-in-depth: never render a token/secret even if the endpoint sends it.
  if (value == null) return "—";
  if (/token|secret|password|bearer/i.test(key) && String(value).length > 0)
    return "•••••••• (set)";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// ── Logs panel ───────────────────────────────────────────────────
let LOGS_DATA = [];
async function refreshLogs() {
  const body = document.getElementById("logs-body");
  setLoading(body);
  try {
    const data = await api("/logs?limit=200");
    LOGS_DATA = data.entries || [];
    document.getElementById("logs-info").textContent =
      `${LOGS_DATA.length} shown · ${data.count || 0} total`;
    renderLogs(document.getElementById("logs-filter").value);
  } catch (e) { showError(body, e); }
}
document.getElementById("logs-filter")?.addEventListener("input",
  e => renderLogs(e.target.value));

function renderLogs(filter) {
  const body = document.getElementById("logs-body");
  const f = (filter || "").trim().toLowerCase();
  const rows = f ? LOGS_DATA.filter(e => (e.tool || "").toLowerCase().includes(f))
                 : LOGS_DATA;
  body.className = "";
  if (rows.length === 0) {
    body.innerHTML = `<div class="empty">${
      LOGS_DATA.length ? `no entries match "${escapeHtml(f)}"`
                       : "no tool invocations recorded yet — fire one and refresh"}</div>`;
    return;
  }
  body.innerHTML = `
    <table class="logs">
      <thead><tr>
        <th>time (utc)</th><th>tool</th><th>kind</th><th>source</th>
        <th class="num">duration</th><th>status</th>
      </tr></thead>
      <tbody>${rows.map(logRowHtml).join("")}</tbody>
    </table>`;
}

function logRowHtml(e) {
  // AuditEntry.timestamp is unix SECONDS; JS Date wants ms.
  const ms = (e.timestamp || 0) * 1000;
  const ts = ms ? new Date(ms).toISOString().replace("T", " ").substring(0, 19) : "—";
  const status = e.success
    ? `<span class="badge ok">ok</span>`
    : `<span class="badge err">err</span>`;
  // Fields are snake_case off the AuditEntry dataclass.
  const errRow = e.success ? "" :
    `<tr class="err-detail"><td colspan="6">${
      escapeHtml(e.error_message || "(no error message)")}</td></tr>`;
  return `
    <tr class="${e.success ? "" : "has-err"}">
      <td class="ts">${escapeHtml(ts)}</td>
      <td class="tn">${escapeHtml(e.tool || "")}</td>
      <td><span class="badge kind">${escapeHtml(e.kind || "?")}</span></td>
      <td class="src-cell">${escapeHtml(e.source_file || "")}</td>
      <td class="dur num">${e.duration_ms || 0} ms</td>
      <td class="st">${status}</td>
    </tr>${errRow}`;
}

// ── Init ─────────────────────────────────────────────────────────
const REFRESH = {
  tools: refreshTools,
  config: refreshConfig,
  state: refreshState,
  logs: refreshLogs,
};

refreshSummary();
// the shell activates the first .view on DOMContentLoaded; load its data too
lazyLoad(document.querySelector(".tabbar .tab")?.dataset?.tab);
