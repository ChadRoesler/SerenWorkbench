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
    // A pip on the tab when something is waiting on you. Someone asked for a
    // capability; that shouldn't need you to go looking for it.
    const pip = document.getElementById("proposals-pip");
    const n = data.pending_proposals || 0;
    if (pip) { pip.hidden = n === 0; pip.textContent = String(n); }
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

// ── Toolbox grouping (shared by Tools and Tool State) ─────────────
// Builtins list as `Toolbox > tool`. Custom tools get one extra level —
// `Custom Toolboxes > box > tool` — so a deployment with forty manifest
// tools stays navigable instead of being one long scroll.
//
// Open/closed is remembered per tab+box. Re-rendering after a toggle
// otherwise slams every group shut under whoever was reading it.
const OPEN = new Set();
function boxKey(tab, box) { return `${tab}::${box}`; }

function groupByToolbox(list) {
  const builtin = new Map(), custom = new Map();
  for (const t of list) {
    const target = t.type === "dynamic" ? custom : builtin;
    const box = t.toolbox || "Other";
    if (!target.has(box)) target.set(box, []);
    target.get(box).push(t);
  }
  const sortBox = m => new Map([...m.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    .map(([k, v]) => [k, v.sort((x, y) =>
      (x.display_name || x.name).localeCompare(y.display_name || y.name))]));
  return { builtin: sortBox(builtin), custom: sortBox(custom) };
}

function matches(t, f) {
  if (!f) return true;
  return (t.name || "").toLowerCase().includes(f)
      || (t.display_name || "").toLowerCase().includes(f)
      || (t.description || "").toLowerCase().includes(f)
      || (t.toolbox || "").toLowerCase().includes(f);
}

/** One collapsible group. `forced` opens it regardless (used while filtering,
 *  because a filter that hides its own matches inside closed groups is worse
 *  than no filter). */
function groupHtml(tab, box, label, countLabel, inner, badges, forced) {
  const open = forced || OPEN.has(boxKey(tab, box));
  return `
    <div class="tbox ${open ? "expanded" : ""}" data-box="${escapeHtml(boxKey(tab, box))}">
      <div class="tbox-head">
        <span class="caret">▸</span>
        <span class="tbox-name">${escapeHtml(label)}</span>
        ${badges || ""}
        <span class="tbox-count">${escapeHtml(countLabel)}</span>
      </div>
      <div class="tbox-body">${inner}</div>
    </div>`;
}

function wireGroups(root) {
  root.querySelectorAll(".tbox-head").forEach(h =>
    h.addEventListener("click", e => {
      e.stopPropagation();
      const g = h.parentElement;
      g.classList.toggle("expanded");
      const k = g.dataset.box;
      g.classList.contains("expanded") ? OPEN.add(k) : OPEN.delete(k);
    }));
}

function renderTools(filter) {
  const body = document.getElementById("tools-body");
  const f = (filter || "").trim().toLowerCase();
  const filtered = TOOLS_DATA.filter(t => matches(t, f));
  body.className = "";
  if (filtered.length === 0) {
    body.innerHTML = `<div class="empty">${
      TOOLS_DATA.length ? `no tools match "${escapeHtml(f)}"`
                        : "no tools registered"}</div>`;
    return;
  }
  const { builtin, custom } = groupByToolbox(filtered);
  let html = "";
  for (const [box, tools] of builtin) {
    html += groupHtml("tools", box, `${box} Toolbox`,
      `${tools.length} tool${tools.length === 1 ? "" : "s"}`,
      tools.map(t => toolCardHtml(t, false)).join(""),
      `<span class="badge builtin">built in</span>`, !!f);
  }
  if (custom.size) {
    let inner = "";
    for (const [box, tools] of custom) {
      inner += groupHtml("tools-custom", box, box,
        `${tools.length} tool${tools.length === 1 ? "" : "s"}`,
        tools.map(t => toolCardHtml(t, false)).join(""), "", !!f);
    }
    const total = [...custom.values()].reduce((n, v) => n + v.length, 0);
    html += groupHtml("tools", "__custom__", "Custom Toolboxes",
      `${custom.size} box${custom.size === 1 ? "" : "es"} · ${total} tool${total === 1 ? "" : "s"}`,
      inner, "", !!f);
  }
  body.innerHTML = html;
  wireGroups(body);
  body.querySelectorAll(".card-head").forEach(h =>
    h.addEventListener("click", e => {
      e.stopPropagation();
      h.parentElement.classList.toggle("expanded");
    }));
}

function paramsTableHtml(params) {
  if (!params || params.length === 0)
    return `<div class="no-params">no parameters</div>`;
  return `<table class="params">
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
}

function toolCardHtml(t) {
  // The HEAD carries the human name; the raw callable name lives inside,
  // next to its source file. Someone auditing needs the identifier, but
  // they shouldn't have to read snake_case to find the tool first.
  const enabledBadge = t.enabled
    ? `<span class="badge ok">enabled</span>`
    : `<span class="badge disabled">disabled</span>`;
  const typeBadge = `<span class="badge ${t.type}">${escapeHtml(t.type)}</span>`;
  return `
    <div class="tool-card">
      <div class="card-head">
        <span class="caret">▸</span>
        <span class="name">${escapeHtml(t.display_name || t.name)}</span>
        ${enabledBadge} ${typeBadge}
      </div>
      <div class="card-body">
        <div class="ident">
          <code class="raw-name">${escapeHtml(t.name)}</code>
          <span class="src">${escapeHtml(t.source || "")}</span>
        </div>
        <div class="desc">${escapeHtml(t.description || "(no description)")}</div>
        ${paramsTableHtml(t.parameters)}
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
  const filtered = (STATE_DATA.tools || []).filter(t => matches(t, f));
  body.className = "";
  if (filtered.length === 0) {
    body.innerHTML = `<div class="empty">${
      (STATE_DATA.tools || []).length ? `no tools match "${escapeHtml(f)}"`
                                      : "no tools registered"}</div>`;
    return;
  }
  // Same hierarchy as the Tools tab, on purpose — the toggle for a tool
  // should be exactly where you just saw the tool.
  const { builtin, custom } = groupByToolbox(filtered);
  const off = list => list.filter(t => !t.enabled).length;
  const tag = list => {
    const n = off(list);
    return n ? `<span class="badge disabled">${n} off</span>` : "";
  };
  let html = "";
  for (const [box, tools] of builtin) {
    html += groupHtml("state", box, `${box} Toolbox`,
      `${tools.length} tool${tools.length === 1 ? "" : "s"}`,
      tools.map(stateCardHtml).join(""),
      `<span class="badge builtin">built in</span>${tag(tools)}`, !!f);
  }
  if (custom.size) {
    let inner = "";
    for (const [box, tools] of custom) {
      inner += groupHtml("state-custom", box, box,
        `${tools.length} tool${tools.length === 1 ? "" : "s"}`,
        tools.map(stateCardHtml).join(""), tag(tools), !!f);
    }
    const all = [...custom.values()].flat();
    html += groupHtml("state", "__custom__", "Custom Toolboxes",
      `${custom.size} box${custom.size === 1 ? "" : "es"} · ${all.length} tool${all.length === 1 ? "" : "s"}`,
      inner, tag(all), !!f);
  }
  body.innerHTML = html;
  wireGroups(body);
  body.querySelectorAll(".state-head").forEach(h =>
    h.addEventListener("click", e => {
      e.stopPropagation();
      h.parentElement.classList.toggle("expanded");
    }));
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
        <span class="state-name">${escapeHtml(t.display_name || t.name)}</span>
        ${badge}
      </div>
      <div class="state-body">
        <div class="ident">
          <code class="raw-name">${escapeHtml(t.name)}</code>
          <span class="src">${escapeHtml(t.source || "")}</span>
        </div>
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

// ── Proposals panel ──────────────────────────────────────────────
// The approvals queue. Two gates: approve INSTALLS (disabled), then the
// operator enables on the Tool State tab. Reject demands a critique,
// because a bare refusal gives the proposer nothing to revise against.
let PROPOSALS = [];
async function refreshProposals() {
  const body = document.getElementById("proposals-body");
  if (!body) return;
  setLoading(body);
  const status = document.getElementById("proposals-status")?.value ?? "pending";
  try {
    const data = await api("/proposals" + (status ? `?status=${encodeURIComponent(status)}` : ""));
    PROPOSALS = data.proposals || [];
    document.getElementById("proposals-info").textContent =
      `${PROPOSALS.length} shown · ${data.pending ?? 0} pending`;
    renderProposals();
  } catch (e) {
    // 503 = proposals disabled by config. That's a state, not a failure.
    if (/503/.test(e.message || "")) {
      body.className = "";
      body.innerHTML = `<div class="empty">tool proposals are disabled
        (dashboard.proposals_enabled)</div>`;
      return;
    }
    showError(body, e);
  }
}
document.getElementById("proposals-status")?.addEventListener("change", refreshProposals);

function renderProposals() {
  const body = document.getElementById("proposals-body");
  body.className = "";
  if (PROPOSALS.length === 0) {
    body.innerHTML = `<div class="empty">nothing waiting — no tools have been
      proposed</div>`;
    return;
  }
  body.innerHTML = PROPOSALS.map(proposalCardHtml).join("");
  body.querySelectorAll(".prop-head").forEach(h =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("expanded")));
  body.querySelectorAll("[data-approve]").forEach(b =>
    b.addEventListener("click", () => actOn(b.dataset.approve, "approve")));
  body.querySelectorAll("[data-reject]").forEach(b =>
    b.addEventListener("click", () => actOn(b.dataset.reject, "reject")));
  body.querySelectorAll("[data-detail]").forEach(b =>
    b.addEventListener("click", () => loadDetail(b.dataset.detail)));
}

function proposalCardHtml(p) {
  const cls = { pending: "kind", approved: "ok", rejected: "err",
                superseded: "disabled" }[p.status] || "kind";
  const attempt = p.attempt > 1 ? `<span class="badge kind">attempt ${p.attempt}</span>` : "";
  const critique = p.critique
    ? `<div class="note err"><span class="label">your critique:</span>
         ${escapeHtml(p.critique)}</div>` : "";
  const controls = p.status === "pending" ? `
      <div class="prop-actions">
        <button class="btn-approve" data-approve="${escapeHtml(p.id)}">approve → install (disabled)</button>
        <button class="btn-reject" data-reject="${escapeHtml(p.id)}">reject with critique</button>
      </div>` : "";
  return `
    <div class="tool-card prop-card" id="card-${escapeHtml(p.id)}">
      <div class="card-head prop-head">
        <span class="caret">▸</span>
        <span class="name">${escapeHtml((p.tool_names || []).join(", ") || "(unnamed)")}</span>
        <span class="badge ${cls}">${escapeHtml(p.status)}</span>
        ${attempt}
        <span class="src">${escapeHtml(p.id)}</span>
      </div>
      <div class="card-body">
        <div class="desc"><span class="label">why:</span> ${escapeHtml(p.rationale || "")}</div>
        ${critique}
        <div class="prop-detail" id="detail-${escapeHtml(p.id)}">
          <button data-detail="${escapeHtml(p.id)}">show what it would run ▾</button>
        </div>
        ${controls}
      </div>
    </div>`;
}

async function loadDetail(pid) {
  const host = document.getElementById(`detail-${pid}`);
  host.innerHTML = `<div class="loading">loading…</div>`;
  try {
    const p = await api(`/proposals/${encodeURIComponent(pid)}`);
    const effects = (p.effects || []).map(e => {
      const what = e.kind === "process"
        ? `<div class="eff-runs"><span class="label">runs:</span>
             <code>${escapeHtml((e.runs || []).join(" "))}</code></div>`
        : `<div class="eff-runs"><span class="label">calls:</span>
             <code>${escapeHtml(e.calls || "")}</code></div>`;
      const loud = e.executes_a_binary
        ? `<div class="note err">${escapeHtml(e.review_note || "")}</div>` : "";
      const params = (e.parameters || []).map(x =>
        `<li><code>${escapeHtml(x.name)}</code> <span class="ptype">${escapeHtml(x.type)}</span>
           ${x.constrained ? `<span class="badge ok">constrained</span>`
                           : `<span class="badge disabled">unconstrained</span>`}</li>`).join("");
      return `<div class="effect">
        <div class="eff-head"><strong>${escapeHtml(e.tool)}</strong>
          <span class="badge kind">${escapeHtml(e.kind)}</span></div>
        ${what}${loud}
        ${params ? `<ul class="eff-params">${params}</ul>`
                 : `<div class="no-params">no parameters</div>`}
      </div>`;
    }).join("");
    host.innerHTML = `${effects}
      <details class="manifest"><summary>full manifest</summary>
        <pre>${escapeHtml(p.manifest || "")}</pre></details>`;
  } catch (e) { showError(host, e); }
}

async function actOn(pid, action) {
  let bodyJson;
  if (action === "reject") {
    const critique = window.prompt(
      "Why are you rejecting this? The proposer reads this and revises against it.");
    if (critique === null) return;              // cancelled
    if (!critique.trim()) { window.alert("A critique is required."); return; }
    bodyJson = JSON.stringify({ critique });
  }
  try {
    const data = await api(`/proposals/${encodeURIComponent(pid)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: bodyJson,
    });
    if (data && data.ok === false) { window.alert(data.error || "refused"); }
    else if (action === "approve" && data?.next_step) { window.alert(data.next_step); }
  } catch (e) {
    window.alert("failed: " + (e.message || e));
  }
  // An approval changes the tool list and the state list too.
  refreshProposals(); refreshSummary();
  if (_loaded.tools) refreshTools();
  if (_loaded.state) refreshState();
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
  proposals: refreshProposals,
  config: refreshConfig,
  state: refreshState,
  logs: refreshLogs,
};

refreshSummary();
// the shell activates the first .view on DOMContentLoaded; load its data too
lazyLoad(document.querySelector(".tabbar .tab")?.dataset?.tab);
