"""Served cockpit HTML / static page rendering (Redmine #12323 split).

The single self-contained document the daemon serves at ``/`` for the cockpit
Web UI. Split out of ``cockpit_ui`` (#12323) so UI / rendering changes no longer
share a module with served-API payload assembly or the action-time preflight
bridge: this module owns only the page markup + front-end script, the payload
contract lives in :mod:`mozyo_bridge.application.cockpit_payload`, and the
side-effecting actions live in :mod:`mozyo_bridge.application.cockpit_actions`.

The page is a single self-contained document: no external assets, no
CDN, nothing fetched off-host — consistent with the loopback-only and
no-exfiltration posture. Kept intentionally small; it is an indicator
surface, not an app platform.

Two safety properties are load-bearing (review #56197):

- Rendering uses DOM APIs (``textContent`` / ``createElement``) only —
  never ``innerHTML`` — so workspace / session / path strings, which are
  operator- or checkout-controlled local input, cannot inject HTML/JS
  into the UI origin.
- Every action request carries the per-process cockpit token (injected
  into the ``__COCKPIT_TOKEN__`` placeholder when the page is served)
  in a custom header, which the action endpoints require. A custom
  header also forces a CORS preflight, so a cross-site simple request
  can never express action intent.

Visual-fit posture (Redmine #12298): the page must stay readable when an
operator opens it in a narrow (mobile-ish) browser viewport, not only the
desktop iTerm2 webview. Two CSS properties are load-bearing for that and are
pinned by the served-cockpit browser smoke (``test_cockpit_page``):

- the responsive ``<meta name="viewport">`` so a phone browser lays the page
  out at device width instead of an emulated 980px desktop canvas; and
- overflow containment — the unit table scrolls horizontally inside its own
  wrapper, long workspace / session / path strings wrap instead of forcing the
  body wider than the viewport, and the controls row wraps — so freshness /
  unavailable text and unit rows never overlap or overflow off-screen. This is
  a fit affordance, not a layout policy: no marketing chrome, no private
  operator layout baked into the OSS default.
"""

from __future__ import annotations

INDEX_HTML_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mozyo cockpit</title>
<style>
  body { font: 13px/1.5 -apple-system, sans-serif; margin: 1rem; }
  #units-wrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 2px 8px; border-bottom: 1px solid #ddd; }
  td { overflow-wrap: anywhere; word-break: break-word; }
  .active  { color: #2e7d32; font-weight: 600; }
  .idle    { color: #ef6c00; font-weight: 600; }
  .unknown { color: #757575; }
  .rm-available    { color: #1565c0; }
  .rm-unconfigured { color: #9e9e9e; }
  .rm-unavailable  { color: #b71c1c; }
  .stale-banner { background: #fff3e0; padding: 4px 8px; display: none; }
  button { font-size: 12px; white-space: nowrap; }
  #transitions { padding-left: 1.2rem; }
  #transitions li { color: #555; overflow-wrap: anywhere; }
  .muted { color: #999; font-size: 11px; }
  #controls { margin: 4px 0; display: flex; align-items: center;
              gap: 8px; flex-wrap: wrap; }
  .obs-healthy { color: #2e7d32; }
  .obs-reload_required { color: #ef6c00; font-weight: 600; }
  .obs-unknown { color: #b71c1c; font-weight: 600; }
  .group { margin: 8px 0; border: 1px solid #e0e0e0; border-radius: 4px; }
  .group-header { background: #f5f5f5; padding: 4px 8px; font-weight: 600; }
  .group-header .tag { font-weight: 400; font-size: 11px; color: #757575; margin-left: 6px; }
  .group-header .stale { color: #b71c1c; }
  .group-header .reload { color: #ef6c00; }
  .unit-row { padding: 3px 8px; border-top: 1px solid #f0f0f0; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .unit-row.hidden-unit { opacity: 0.65; }
  .fresh-fresh { color: #2e7d32; }
  .fresh-stale, .fresh-expired { color: #ef6c00; font-weight: 600; }
  .fresh-unknown { color: #b71c1c; font-weight: 600; }
</style>
</head>
<body>
<h2>mozyo cockpit</h2>
<div id="controls">
<button id="reload" type="button">Reload</button>
<span id="observation" class="muted">observation: loading…</span>
</div>
<div id="stale" class="stale-banner">tmux runtime unavailable — showing the
last cached snapshot; activity may be outdated and actions are disabled.</div>
<div id="units-wrap">
<table id="units"><thead><tr>
<th>state</th><th>agent</th><th>session</th><th>workspace</th>
<th>redmine</th><th>actions</th>
</tr></thead><tbody></tbody></table>
</div>
<p class="muted">three layers: state is OTel activity (active / idle /
unknown — never "dead"); tmux liveness is the row's presence itself;
redmine is read-only gate context (latest open issue), degrading to
unconfigured / unavailable without affecting the other layers.
Jump switches the attached tmux client (iTerm2 -CC focus is out of scope).</p>
<h3>grouped (Project Group &#8594; Unit &#8594; Target)</h3>
<div id="grouped-meta" class="muted">grouped: loading…</div>
<div id="grouped"></div>
<p class="muted">grouped read model (#12286): Project Group headers, each Unit's
lane / issue and its Codex / Claude role panes (the Target layer). Display only —
group membership and freshness are a projection, never routing authority; an
action re-resolves its candidate Unit live before acting. project_group_presentation
is a desired display-placement request (same_cockpit_column default), never a
guaranteed window / tab.</p>
<h3>recent transitions</h3>
<ul id="transitions"></ul>
<script>
const COCKPIT_TOKEN = "__COCKPIT_TOKEN__";
const KNOWN_STATES = ["active", "idle", "unknown"];
const KNOWN_RM_STATES = ["available", "unconfigured", "unavailable"];
// #12225: the runtime observation snapshot's fail-closed display states. A
// stale / unreadable snapshot derives reload_required / unknown, never healthy
// (runtime-observability-boundary.md). The class is whitelisted from this list,
// so the (local but untrusted) payload can never inject a class name.
const KNOWN_DISPLAY_STATES = ["healthy", "reload_required", "unknown"];
function renderObservation(obs) {
  // The runtime view is a timestamped snapshot, not live truth. Surface its
  // observed_at / freshness / display_state so a stale or unreadable snapshot
  // reads as such instead of as current. Diagnostic only — it authorizes no
  // action (those re-preflight live) and moves no Redmine gate.
  const el = document.getElementById('observation');
  if (!obs) { el.className = 'muted'; el.textContent = 'observation: unavailable'; return; }
  const ds = KNOWN_DISPLAY_STATES.includes(obs.display_state)
    ? obs.display_state : 'unknown';
  let text = 'observed_at ' + (obs.observed_at || '-') +
    ' · freshness ' + (obs.freshness || 'unknown') +
    ' · ' + ds;
  if (obs.stale_reason) text += ' (' + obs.stale_reason + ')';
  el.className = 'obs-' + ds;
  el.textContent = text;
}
function redmineText(rm) {
  if (!rm || !KNOWN_RM_STATES.includes(rm.state)) return "unknown";
  if (rm.state !== "available") return rm.state;
  const latest = rm.latest_issue;
  if (!latest || !latest.id) return "available (no open issues)";
  let text = "#" + latest.id;
  if (latest.status) text += " " + latest.status;
  if (typeof rm.open_total === "number") text += " (" + rm.open_total + " open)";
  return text;
}
function redmineClass(rm) {
  const state = rm && rm.state;
  return KNOWN_RM_STATES.includes(state) ? ("rm-" + state) : "unknown";
}
async function act(kind, pane) {
  const res = await fetch('/api/actions/' + kind, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Mozyo-Cockpit-Token': COCKPIT_TOKEN
    },
    body: JSON.stringify({pane_id: pane})
  });
  const body = await res.json();
  if (!res.ok) alert(body.error || 'action failed');
}
// #12286 grouped action: the request carries only the candidate Unit identity
// (workspace_id / role / lane_id / host_id) the displayed row exposes — never a
// pane id. The server re-resolves it live and fails closed; this is the same
// explicit-click + token-gated path as `act`.
const KNOWN_FRESHNESS = ["fresh", "stale", "expired", "unknown"];
async function actGrouped(kind, unit, role) {
  const res = await fetch('/api/actions/grouped-' + kind, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Mozyo-Cockpit-Token': COCKPIT_TOKEN
    },
    body: JSON.stringify({
      workspace_id: unit.workspace_id,
      role: role,
      lane_id: unit.lane_id,
      host_id: unit.host_id
    })
  });
  const body = await res.json();
  if (!res.ok) alert(body.error || 'action failed');
}
// Render the grouped read model (Project Group -> Unit -> Target). DOM APIs only;
// every label lands via textContent, and the freshness/display-state class names
// are whitelisted, so the (local but untrusted) payload can never inject markup
// or a class. The grouped view is display only: a degraded (reload_required) row
// disables its action buttons, and the server re-preflights regardless.
function unitRow(unit, hidden) {
  const row = document.createElement('div');
  row.className = hidden ? 'unit-row hidden-unit' : 'unit-row';
  const lane = document.createElement('span');
  lane.textContent = (unit.lane_label || '-') +
    (unit.issue_label ? ' · ' + unit.issue_label : '');
  row.appendChild(lane);
  const fresh = KNOWN_FRESHNESS.includes(unit.freshness) ? unit.freshness : 'unknown';
  const state = document.createElement('span');
  state.className = 'fresh-' + fresh;
  state.textContent = (unit.state_label || unit.status || 'unknown') +
    ' / ' + (unit.freshness_label || fresh);
  row.appendChild(state);
  if (hidden) {
    const tag = document.createElement('span');
    tag.className = 'muted';
    tag.textContent = '(hidden)';
    row.appendChild(tag);
  }
  // The Target layer: one action affordance per observed role pane. Disabled
  // when the row is not current (reload_required) — the candidate selector would
  // fail closed anyway.
  for (const role of (unit.roles || [])) {
    for (const [kind, label] of [['jump', 'jump'], ['reveal', 'Finder']]) {
      const button = document.createElement('button');
      button.textContent = role + ':' + label;
      button.disabled = !!unit.reload_required;
      button.addEventListener('click', () => actGrouped(kind, unit, role));
      row.appendChild(button);
    }
  }
  if (!(unit.roles || []).length) {
    const none = document.createElement('span');
    none.className = 'muted';
    none.textContent = 'no live role pane';
    row.appendChild(none);
  }
  return row;
}
function renderGrouped(data) {
  const meta = document.getElementById('grouped-meta');
  const container = document.getElementById('grouped');
  container.replaceChildren();
  if (!data || !Array.isArray(data.groups)) {
    meta.textContent = 'grouped: unavailable';
    return;
  }
  meta.textContent = 'placement: ' + (data.project_group_presentation || 'unknown') +
    ' · ' + (data.freshness_label || 'unknown') +
    (data.needs_attention ? ' · reload recommended' : '');
  for (const g of data.groups) {
    const box = document.createElement('div');
    box.className = 'group';
    const header = document.createElement('div');
    header.className = 'group-header';
    const title = document.createElement('span');
    title.textContent = g.header_label || '(ungrouped)';
    header.appendChild(title);
    const tag = document.createElement('span');
    tag.className = 'tag';
    let tagText = g.source;
    if (g.stale) tagText += ' · stale';
    if (g.reload_required) tagText += ' · reload';
    tag.textContent = tagText;
    if (g.stale) tag.classList.add('stale');
    else if (g.reload_required) tag.classList.add('reload');
    header.appendChild(tag);
    box.appendChild(header);
    for (const u of (g.units || [])) box.appendChild(unitRow(u, false));
    for (const u of (g.hidden_units || [])) box.appendChild(unitRow(u, true));
    container.appendChild(box);
  }
}
async function refreshGrouped() {
  try {
    const res = await fetch('/api/grouped-units');
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      document.getElementById('grouped-meta').textContent =
        'grouped: ' + (body.error || 'unavailable');
      document.getElementById('grouped').replaceChildren();
      return;
    }
    renderGrouped(await res.json());
  } catch (e) { /* daemon restarting; next poll recovers */ }
}
// DOM construction only: every payload string lands via textContent, so
// workspace / session names with HTML metacharacters render as text.
function cell(row, text, cls) {
  const el = document.createElement('td');
  if (cls) el.className = cls;
  el.textContent = text;
  row.appendChild(el);
}
async function refresh() {
  try {
    const res = await fetch('/api/units');
    const data = await res.json();
    document.getElementById('stale').style.display =
      data.stale ? 'block' : 'none';
    renderObservation(data.observation);
    const tbody = document.querySelector('#units tbody');
    tbody.replaceChildren();
    for (const p of (data.panes || [])) {
      if (p.agent_kind === 'unknown') continue;
      const row = document.createElement('tr');
      const raw = (p.activity && p.activity.state) || 'unknown';
      const st = KNOWN_STATES.includes(raw) ? raw : 'unknown';
      const ws = (p.workspace && (p.workspace.project_name ||
                  p.workspace.canonical_session)) || '-';
      cell(row, st, st);
      cell(row, p.agent_kind);
      cell(row, p.session);
      cell(row, ws);
      cell(row, redmineText(p.redmine), redmineClass(p.redmine));
      const actions = document.createElement('td');
      for (const [kind, label] of [['jump', 'jump'], ['reveal', 'Finder']]) {
        const button = document.createElement('button');
        button.textContent = label;
        button.disabled = !!data.stale;
        button.addEventListener('click', () => act(kind, p.pane_id));
        actions.appendChild(button);
      }
      row.appendChild(actions);
      tbody.appendChild(row);
    }
    const tr = await (await fetch('/api/transitions')).json();
    const list = document.getElementById('transitions');
    list.replaceChildren();
    for (const t of (tr.transitions || [])) {
      const item = document.createElement('li');
      item.textContent = t.observed_at + ' ' + t.agent_kind + '@' +
        t.session + ': ' + t.previous_state + ' \\u2192 ' + t.state;
      list.appendChild(item);
    }
  } catch (e) { /* daemon restarting; next poll recovers */ }
  // The grouped read model is served from its own endpoint; refresh it on the
  // same cadence so its freshness line tracks the flat view.
  refreshGrouped();
}
// Explicit operator reload (v1 freshness model = explicit reload + action-time
// live preflight): re-fetch the snapshot on demand. Refreshing the display moves
// no workflow gate and authorizes no action.
document.getElementById('reload').addEventListener('click', refresh);
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
