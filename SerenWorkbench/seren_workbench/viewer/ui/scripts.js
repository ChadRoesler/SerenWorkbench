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
document.getElementById("proposals-kind")?.addEventListener("change", renderProposals);

function renderProposals() {
  const body = document.getElementById("proposals-body");
  body.className = "";
  if (PROPOSALS.length === 0) {
    body.innerHTML = `<div class="empty">nothing waiting — no tools have been
      proposed</div>`;
    return;
  }
  const kind = document.getElementById("proposals-kind")?.value ?? "";
  // A need staged by a project is shown under its parent when the parent is
  // in the list, so the family reads as one thing; on its own otherwise.
  const shownIds = new Set(PROPOSALS.map(p => p.id));
  const top = PROPOSALS.filter(p => !(p.parent && shownIds.has(p.parent)))
                       .filter(p => !kind || (p.kind || "tool") === kind);
  if (top.length === 0) {
    body.innerHTML = `<div class="empty">nothing of that kind here</div>`;
    return;
  }
  body.innerHTML = top.map(p => proposalCardHtml(p) +
    PROPOSALS.filter(c => c.parent === p.id).map(c => proposalCardHtml(c, true)).join("")
  ).join("");
  body.querySelectorAll("[data-plan]").forEach(b =>
    b.addEventListener("click", () => showPlan(b.dataset.plan)));
  body.querySelectorAll(".prop-head").forEach(h =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("expanded")));
  body.querySelectorAll("[data-approve]").forEach(b =>
    b.addEventListener("click", () => actOn(b.dataset.approve, "approve")));
  body.querySelectorAll("[data-reject]").forEach(b =>
    b.addEventListener("click", () => actOn(b.dataset.reject, "reject")));
  body.querySelectorAll("[data-detail]").forEach(b =>
    b.addEventListener("click", () => loadDetail(b.dataset.detail)));
}

function proposalCardHtml(p, nested = false) {
  const cls = { pending: "kind", approved: "ok", rejected: "err",
                superseded: "disabled" }[p.status] || "kind";
  const kind = p.kind || "tool";
  const kindBadge = kind !== "tool" ? `<span class="badge kind">${escapeHtml(kind)}</span>` : "";
  const family = p.for_project && !nested
    ? `<span class="badge disabled">for ${escapeHtml(p.for_project)}</span>` : "";
  const attempt = p.attempt > 1 ? `<span class="badge kind">attempt ${p.attempt}</span>` : "";
  const final = p.final ? `<span class="badge err">permanent</span>` : "";
  const critique = p.critique
    ? `<div class="note err"><span class="label">your critique${p.final ? " (final)" : ""}:</span>
         ${escapeHtml(p.critique)}</div>` : "";
  const approveLabel = kind === "project" ? "approve → agree to the plan and limits"
                     : kind === "revision" ? "approve → apply this change"
                     : "approve → install (disabled)";
  const controls = p.status === "pending" ? `
      <div class="prop-actions">
        <button class="btn-approve" data-approve="${escapeHtml(p.id)}">${approveLabel}</button>
        <button class="btn-reject" data-reject="${escapeHtml(p.id)}">reject with critique</button>
      </div>` : "";
  const detailLabel = kind === "project" || kind === "revision"
    ? "show the contract ▾" : "show what it would run ▾";
  const planBtn = (kind === "project" || kind === "revision")
    ? `<button data-plan="${escapeHtml(p.id)}">read the plan</button>` : "";
  const contract = kind === "project" ? projectSummaryHtml(p.project || {})
                 : kind === "revision" ? revisionSummaryHtml(p.revision || {}) : "";
  return `
    <div class="tool-card prop-card${nested ? " prop-nested" : ""}" id="card-${escapeHtml(p.id)}">
      <div class="card-head prop-head">
        <span class="caret">▸</span>
        <span class="name">${escapeHtml(p.title || (p.tool_names || []).join(", ") || "(unnamed)")}</span>
        <span class="badge ${cls}">${escapeHtml(p.status)}</span>
        ${kindBadge}${family}${attempt}${final}
        <span class="src">${escapeHtml(p.id)}</span>
      </div>
      <div class="card-body">
        <div class="desc"><span class="label">why:</span> ${escapeHtml(p.rationale || "")}</div>
        ${contract}
        ${critique}
        ${planBtn ? `<div class="prop-plan">${planBtn}</div>` : ""}
        <div class="prop-detail" id="detail-${escapeHtml(p.id)}">
          <button data-detail="${escapeHtml(p.id)}">${detailLabel}</button>
        </div>
        ${controls}
      </div>
    </div>`;
}

// What a project asks for, at a glance - the limits are the review.
function projectSummaryHtml(pr) {
  const dl = (pr.downloads || []).length
    ? `<ul class="grant-list">${(pr.downloads || []).map(d =>
        `<li><code>${escapeHtml(d.url)}</code> <span class="ptype">${escapeHtml(d.purpose || "")}</span></li>`).join("")}</ul>`
    : `<span class="ptype">fetches nothing</span>`;
  const needs = (pr.needs || []).length
    ? (pr.needs || []).map(n => {
        const c = PROPOSALS.find(x => x.id === n.proposal);
        const st = c ? c.status : "?";
        const cls = { pending: "kind", approved: "ok", rejected: "err" }[st] || "disabled";
        return `<li><code>${escapeHtml(n.name || n.proposal)}</code>
          <span class="badge kind">${escapeHtml(n.kind || "")}</span>
          <span class="badge ${cls}">${escapeHtml(st)}</span></li>`; }).join("")
    : "";
  return `<div class="grants">
    <div><span class="label">workspace:</span> ${pr.workspace ? `<code>${escapeHtml(pr.workspace)}</code>` : `<span class="ptype">none</span>`}</div>
    <div><span class="label">may fetch from:</span> ${dl}</div>
    <div><span class="label">needs:</span> ${needs ? `<ul class="grant-list">${needs}</ul>` : `<span class="ptype">nothing beyond what exists</span>`}</div>
  </div>`;
}

function revisionSummaryHtml(rv) {
  const ch = rv.changes || {};
  const rows = [];
  if (ch.workspace) rows.push(`workspace: <code>${escapeHtml(ch.workspace.from || "none")}</code> → <code>${escapeHtml(ch.workspace.to || "none")}</code>`);
  if (ch.title) rows.push(`title: ${escapeHtml(ch.title.from || "")} → ${escapeHtml(ch.title.to || "")}`);
  (ch.downloads_added || []).forEach(d => rows.push(`<span class="badge ok">+ site</span> <code>${escapeHtml(d.url)}</code> <span class="ptype">${escapeHtml(d.purpose || "")}</span>`));
  (ch.downloads_removed || []).forEach(d => rows.push(`<span class="badge err">− site</span> <code>${escapeHtml(d.url)}</code>`));
  (ch.needs_added || []).forEach(n => rows.push(`<span class="badge ok">+ need</span> <code>${escapeHtml(n.name || n.proposal)}</code> <span class="badge kind">${escapeHtml(n.kind || "")}</span>`));
  (ch.needs_removed || []).forEach(n => rows.push(`<span class="badge err">− need</span> <code>${escapeHtml(n.name || n.proposal)}</code>`));
  if (ch.plan_changed) rows.push(`plan rewritten`);
  if (ch.testing_changed) rows.push(`testing notes rewritten`);
  return `<div class="grants">
    <div><span class="label">against:</span> <code>${escapeHtml(rv.project || "")}</code></div>
    <div><span class="label">changes:</span> ${rows.length
      ? `<ul class="grant-list">${rows.map(r => `<li>${r}</li>`).join("")}</ul>`
      : `<span class="ptype">nothing - a revision that changes nothing</span>`}</div>
  </div>`;
}

// ── A small, safe Markdown renderer ──────────────────────────────────
// Everything is HTML-escaped FIRST, then a handful of block and inline
// forms are recognised on the escaped text. No raw HTML, no javascript:
// links. It is enough to read a plan; it is not a Markdown engine.
function renderMarkdown(md) {
  const esc = escapeHtml(md || "");
  const lines = esc.split(/\r?\n/);
  const out = [];
  let i = 0;
  const inline = s => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" rel="noopener noreferrer" target="_blank">$1</a>');
  while (i < lines.length) {
    const ln = lines[i];
    if (/^```/.test(ln)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.push(`<pre><code>${buf.join("\n")}</code></pre>`); continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(ln);
    if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
    if (/^\s*([-*+]|\d+\.)\s+/.test(ln)) {
      const ordered = /^\s*\d+\./.test(ln); const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i]))
        items.push(`<li>${inline(lines[i++].replace(/^\s*([-*+]|\d+\.)\s+/, ""))}</li>`);
      out.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`); continue;
    }
    if (/^\s*&gt;\s?/.test(ln)) {
      const buf = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) buf.push(lines[i++].replace(/^\s*&gt;\s?/, ""));
      out.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`); continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(ln)) { out.push("<hr>"); i++; continue; }
    if (!ln.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*([-*+]|\d+\.)\s|\s*&gt;)/.test(lines[i]))
      buf.push(lines[i++]);
    out.push(`<p>${inline(buf.join(" "))}</p>`);
  }
  return out.join("\n");
}

let PLAN_TEXTS = { plan: "", testing: "", contract: "" };
async function showPlan(pid) {
  const dlg = document.getElementById("plan-dialog");
  const body = document.getElementById("plan-body");
  body.innerHTML = `<div class="loading">loading…</div>`;
  document.getElementById("plan-dialog-sub").textContent = pid;
  const close = () => dlg.close();
  dlg.querySelectorAll("[data-plan-close]").forEach(b => b.onclick = close);
  const show = part => {
    dlg.querySelectorAll("[data-plan-part]").forEach(b => b.classList.toggle("on", b.dataset.planPart === part));
    body.innerHTML = part === "contract"
      ? `<pre>${escapeHtml(PLAN_TEXTS.contract || "")}</pre>`
      : (PLAN_TEXTS[part] ? `<div class="md">${renderMarkdown(PLAN_TEXTS[part])}</div>`
                          : `<div class="empty">nothing written here</div>`);
  };
  dlg.querySelectorAll("[data-plan-part]").forEach(b => b.onclick = () => show(b.dataset.planPart));
  dlg.showModal();
  try {
    const p = await api(`/proposals/${encodeURIComponent(pid)}`);
    PLAN_TEXTS = { plan: p.texts?.plan || "", testing: p.texts?.testing || "", contract: p.manifest || "" };
    document.getElementById("plan-dialog-title").textContent = p.title || "Plan";
    show("plan");
  } catch (e) { showError(body, e); }
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
        : (e.kind === "project" || e.kind === "revision")
        ? `<div class="eff-runs"><span class="label">agrees to:</span>
             workspace <code>${escapeHtml(e.workspace || "none")}</code>,
             ${(e.downloads || []).length} download site(s), ${(e.needs || []).length} need(s)</div>`
        : `<div class="eff-runs"><span class="label">calls:</span>
             <code>${escapeHtml(e.calls || "")}</code></div>`;
      const loud = (e.executes_a_binary || e.sends_credential || e.kind === "plugin"
                    || e.kind === "project" || e.kind === "revision")
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

// The reject form. Resolves to {critique, final} on submit, null on cancel.
// A <dialog> rather than window.prompt: a critique is prose the proposer
// revises against, and a one-line prompt invites a one-word "no".
function askRejection(pid) {
  const dlg = document.getElementById("reject-dialog");
  const form = document.getElementById("reject-form");
  const text = document.getElementById("reject-critique");
  const finalBox = document.getElementById("reject-final");
  const p = PROPOSALS.find(x => x.id === pid);
  document.getElementById("reject-dialog-sub").textContent =
    `${p?.title || (p?.tool_names || []).join(", ") || pid} · ${pid}`;
  text.value = ""; finalBox.checked = false;
  return new Promise(resolve => {
    const done = v => { cleanup(); dlg.close(); resolve(v); };
    const onSubmit = e => {
      e.preventDefault();
      const critique = text.value.trim();
      if (!critique) { text.focus(); return; }   // `required` also blocks this
      done({ critique, final: finalBox.checked });
    };
    const onCancel = () => done(null);
    const cancels = dlg.querySelectorAll("[data-reject-cancel]");
    const cleanup = () => {
      form.removeEventListener("submit", onSubmit);
      dlg.removeEventListener("cancel", onCancel);
      cancels.forEach(b => b.removeEventListener("click", onCancel));
    };
    form.addEventListener("submit", onSubmit);
    dlg.addEventListener("cancel", onCancel);     // Esc
    cancels.forEach(b => b.addEventListener("click", onCancel));
    dlg.showModal();
    text.focus();
  });
}

async function actOn(pid, action) {
  let bodyJson;
  if (action === "reject") {
    const answer = await askRejection(pid);
    if (answer === null) return;                // cancelled
    bodyJson = JSON.stringify(answer);          // {critique, final}
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
  // An approval changes the tool list and the state list too - and a
  // project or revision approval lands in the cabinet.
  refreshProposals(); refreshSummary();
  if (_loaded.projects) refreshProjects();
  if (_loaded.tools) refreshTools();
  if (_loaded.state) refreshState();
}

// ── Projects panel ───────────────────────────────────────────────
// Life after approval. The queue says what was agreed; this says what has
// happened inside it since. Journal entries are the model's own words.
let PROJECTS = [];
async function refreshProjects() {
  const body = document.getElementById("projects-body");
  if (!body) return;
  setLoading(body);
  try {
    const data = await api("/projects");
    PROJECTS = data.projects || [];
    const asking = PROJECTS.filter(p => p.state?.done_requested).length;
    document.getElementById("projects-info").textContent =
      `${PROJECTS.length} agreed · ${PROJECTS.filter(p => p.state?.status === "in_progress").length} in progress` +
      (asking ? ` · ${asking} asking for done` : "");
    const pip = document.getElementById("projects-pip");
    if (pip) { pip.textContent = String(asking); pip.hidden = asking === 0; }
    renderProjects();
  } catch (e) {
    if (/503/.test(e.message || "")) {
      body.className = "";
      body.innerHTML = `<div class="empty">projects live behind tool proposals, which are disabled
        (dashboard.proposals_enabled)</div>`;
      return;
    }
    showError(body, e);
  }
}

function renderProjects() {
  const body = document.getElementById("projects-body");
  body.className = "";
  if (PROJECTS.length === 0) {
    body.innerHTML = `<div class="empty">nothing agreed yet — approve a project in the queue
      and it lands here</div>`;
    return;
  }
  body.innerHTML = PROJECTS.map(projectCardHtml).join("");
  body.querySelectorAll(".proj-head").forEach(h =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("expanded")));
  body.querySelectorAll("[data-proj-plan]").forEach(b =>
    b.addEventListener("click", () => showProjectPlan(b.dataset.projPlan)));
  body.querySelectorAll("[data-proj-journal]").forEach(b =>
    b.addEventListener("click", () => loadJournal(b.dataset.projJournal)));
  body.querySelectorAll("[data-proj-done]").forEach(b =>
    b.addEventListener("click", () => projectAct(b.dataset.projDone, "done")));
  body.querySelectorAll("[data-proj-status]").forEach(b =>
    b.addEventListener("click", () => projectAct(b.dataset.projStatus, b.dataset.to)));
}

const STATUS_CLS = { agreed: "disabled", in_progress: "kind", paused: "disabled", done: "ok" };
function fmtWhen(ts) {
  if (!ts) return "";
  try { return new Date(ts * 1000).toLocaleString(); } catch { return ""; }
}

function projectCardHtml(p) {
  const st = p.state?.status || "agreed";
  const asking = p.state?.done_requested
    ? `<span class="badge warn">asks: done</span>` : "";
  const last = p.last_entry
    ? `<div class="desc"><span class="label">last entry:</span>
         <span class="ptype">${escapeHtml(fmtWhen(p.last_entry.at))}</span>
         ${escapeHtml(p.last_entry.text.length > 240 ? p.last_entry.text.slice(0, 240) + "…" : p.last_entry.text)}</div>`
    : `<div class="desc"><span class="ptype">no journal entries yet</span></div>`;
  const files = (p.files_touched || []).length
    ? `<div><span class="label">files touched:</span>
         <ul class="grant-list">${p.files_touched.map(f => `<li><code>${escapeHtml(f)}</code></li>`).join("")}</ul></div>`
    : "";
  const dl = (p.downloads || []).length
    ? `<ul class="grant-list">${p.downloads.map(d =>
        `<li><code>${escapeHtml(d.url)}</code> <span class="ptype">${escapeHtml(d.purpose || "")}</span></li>`).join("")}</ul>`
    : `<span class="ptype">fetches nothing</span>`;
  const needs = (p.needs || []).length
    ? `<ul class="grant-list">${p.needs.map(n => {
        const cls = { pending: "kind", approved: "ok", rejected: "err" }[n.status] || "disabled";
        return `<li><code>${escapeHtml(n.name || n.proposal)}</code>
          <span class="badge kind">${escapeHtml(n.kind || "")}</span>
          <span class="badge ${cls}">${escapeHtml(n.status || "")}</span></li>`; }).join("")}</ul>`
    : `<span class="ptype">none</span>`;
  const actions = [];
  if (p.state?.done_requested || st !== "done")
    actions.push(`<button class="btn-approve" data-proj-done="${escapeHtml(p.name)}">confirm done</button>`);
  if (st === "done")
    actions.push(`<button data-proj-status="${escapeHtml(p.name)}" data-to="in_progress">reopen by hand</button>`);
  if (st === "in_progress")
    actions.push(`<button data-proj-status="${escapeHtml(p.name)}" data-to="paused">pause</button>`);
  if (st === "paused")
    actions.push(`<button data-proj-status="${escapeHtml(p.name)}" data-to="in_progress">resume</button>`);
  return `
    <div class="tool-card proj-card" id="proj-${escapeHtml(p.name)}">
      <div class="card-head proj-head">
        <span class="caret">▸</span>
        <span class="name">${escapeHtml(p.title || p.name)}</span>
        <span class="badge ${STATUS_CLS[st] || "kind"}">${escapeHtml(st.replace("_", " "))}</span>
        ${asking}
        <span class="src">${escapeHtml(p.name)} · ${p.journal_count || 0} entries</span>
      </div>
      <div class="card-body">
        ${last}
        <div class="grants">
          <div><span class="label">workspace:</span> ${p.workspace ? `<code>${escapeHtml(p.workspace)}</code>` : `<span class="ptype">none</span>`}</div>
          <div><span class="label">may fetch from:</span> ${dl}</div>
          <div><span class="label">needs:</span> ${needs}</div>
          ${files}
        </div>
        <div class="prop-plan">
          <button data-proj-plan="${escapeHtml(p.name)}">read the plan</button>
          <button data-proj-journal="${escapeHtml(p.name)}">open the journal ▾</button>
        </div>
        <div class="journal" id="journal-${escapeHtml(p.name)}"></div>
        <div class="prop-actions">${actions.join("")}</div>
      </div>
    </div>`;
}

async function loadJournal(name) {
  const host = document.getElementById(`journal-${name}`);
  host.innerHTML = `<div class="loading">loading…</div>`;
  try {
    const p = await api(`/projects/${encodeURIComponent(name)}`);
    const entries = (p.journal || []).slice().reverse();
    host.innerHTML = entries.length
      ? entries.map(e => `
        <div class="entry ${escapeHtml(e.by || "model")}">
          <div class="entry-head">
            <span class="ptype">#${e.id} · ${escapeHtml(fmtWhen(e.at))} · ${escapeHtml(e.by || "model")}</span>
            ${e.status ? `<span class="badge ${STATUS_CLS[e.status] || "kind"}">${escapeHtml(String(e.status).replace("_", " "))}</span>` : ""}
            ${e.asks ? `<span class="badge warn">asks: ${escapeHtml(e.asks)}</span>` : ""}
            ${e.commit ? `<code title="commit">${escapeHtml(e.commit.slice(0, 10))}</code>` : ""}
          </div>
          <div class="md">${renderMarkdown(e.text || "")}</div>
          ${(e.files || []).length ? `<div class="entry-files">${e.files.map(f => `<code>${escapeHtml(f)}</code>`).join(" ")}</div>` : ""}
        </div>`).join("")
      : `<div class="empty">the journal is empty</div>`;
  } catch (e) { showError(host, e); }
}

async function showProjectPlan(name) {
  const dlg = document.getElementById("plan-dialog");
  const body = document.getElementById("plan-body");
  body.innerHTML = `<div class="loading">loading…</div>`;
  document.getElementById("plan-dialog-sub").textContent = name;
  dlg.querySelectorAll("[data-plan-close]").forEach(b => b.onclick = () => dlg.close());
  const show = part => {
    dlg.querySelectorAll("[data-plan-part]").forEach(b => b.classList.toggle("on", b.dataset.planPart === part));
    body.innerHTML = part === "contract"
      ? `<pre>${escapeHtml(PLAN_TEXTS.contract || "")}</pre>`
      : (PLAN_TEXTS[part] ? `<div class="md">${renderMarkdown(PLAN_TEXTS[part])}</div>`
                          : `<div class="empty">nothing written here</div>`);
  };
  dlg.querySelectorAll("[data-plan-part]").forEach(b => b.onclick = () => show(b.dataset.planPart));
  dlg.showModal();
  try {
    const p = await api(`/projects/${encodeURIComponent(name)}`);
    const c = p.contract || {};
    PLAN_TEXTS = { plan: p.plan || "", testing: p.testing || "",
                   contract: JSON.stringify(c, null, 2) };
    document.getElementById("plan-dialog-title").textContent = c.title || name;
    show("plan");
  } catch (e) { showError(body, e); }
}

async function projectAct(name, what) {
  const note = window.prompt(what === "done"
    ? "Confirm done: the plan's 'what done looks like' was met. A note is optional."
    : `Set ${what.replace("_", " ")}. A note is optional.`, "");
  if (note === null) return;
  try {
    const path = what === "done" ? "done" : "status";
    const data = await api(`/projects/${encodeURIComponent(name)}/${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(what === "done" ? { note } : { status: what, note }),
    });
    if (data && data.ok === false) window.alert(data.error || "refused");
  } catch (e) { window.alert("failed: " + (e.message || e)); }
  refreshProjects();
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
  projects: refreshProjects,
  config: refreshConfig,
  state: refreshState,
  logs: refreshLogs,
};

refreshSummary();
// the shell activates the first .view on DOMContentLoaded; load its data too
lazyLoad(document.querySelector(".tabbar .tab")?.dataset?.tab);
