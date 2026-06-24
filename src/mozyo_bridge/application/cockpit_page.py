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

Grouped scannability (Redmine #12377): the grouped Project Group -> Unit ->
Target view makes the project / lane / pane-role relationships readable at a
glance without changing the ``/api/units`` or ``/api/grouped-units`` payload
contract. It renders, from the data the grouped payload already carries:

- **project vs lane vs role separation** — a Project Group box (managed vs
  default), then each lane (Unit) as its own row with a distinct lane-identity
  column, then a per-role Target slot;
- **same-lane Codex / Claude grouping** — the two canonical roles render as a
  fixed role matrix on the one lane row, so a lane's Codex and Claude read as one
  group instead of two unrelated table rows;
- **missing / one-sided / stale clarity** — a one-sided lane shows the absent
  canonical role as a dashed "missing" slot, an empty / missing-lane group stays
  visible (never dropped) with a "no lane observed" row, and a stale /
  reload-required lane carries an attention background plus the existing
  freshness state — none of which read as current.

Class names for the role slots come from the front-end's whitelist
(``GROUPED_ROLES``) plus the payload's role *presence*, never a payload-supplied
string, so the DOM-only rendering keeps its no-injection property.
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
  /* Empty vs error must never render as the same blank surface (#12378): an
     empty cockpit (the daemon responded, nothing to show) reads as a neutral
     muted note, while a data-unavailable error (the daemon could not be reached)
     reads fail-closed red. Defined after .muted so the later equal-specificity
     rule wins when both classes are present. */
  .units-state { padding: 4px 8px; font-size: 12px; }
  .state-empty { color: #616161; }
  .state-error { color: #b71c1c; font-weight: 600; }
  /* Project Group box: a managed (configured) group reads with a left accent;
     a default / ungrouped bucket stays plain so the two are visually separable. */
  .group { margin: 8px 0; border: 1px solid #e0e0e0; border-radius: 4px;
           border-left: 3px solid #e0e0e0; }
  .group.managed { border-left-color: #1565c0; }
  .group.default { border-left-color: #bdbdbd; }
  .group-header { background: #f5f5f5; padding: 4px 8px; font-weight: 600;
                  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .group-header .group-title { flex: 1 1 auto; }
  .group-header .tag { font-weight: 400; font-size: 11px; color: #757575; }
  .group-header .stale { color: #b71c1c; }
  .group-header .reload { color: #ef6c00; }
  .group-summary { font-weight: 400; font-size: 11px; color: #757575; }
  .group-summary.attention { color: #ef6c00; font-weight: 600; }
  /* One lane (Unit) within a group: lane identity, state/freshness, role matrix. */
  .lane-row { padding: 3px 8px; border-top: 1px solid #f0f0f0; display: flex;
              align-items: center; gap: 10px; flex-wrap: wrap; }
  .lane-row.hidden-unit { opacity: 0.65; }
  .lane-row.lane-attention { background: #fff8e1; }
  .lane-ident { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap;
                min-width: 12ch; }
  .lane-id { font-weight: 600; }
  .lane-issue { color: #555; font-size: 12px; }
  .lane-state { font-size: 12px; }
  /* The Target layer: one slot per canonical role (codex, claude). A present role
     carries its action buttons; a missing role reads "missing" so a one-sided lane
     (only Codex or only Claude live) is obvious at a glance. */
  .role-matrix { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .role-slot { display: inline-flex; align-items: center; gap: 4px;
               border: 1px solid #e0e0e0; border-radius: 3px; padding: 1px 4px; }
  .role-slot .role-name { font-size: 11px; font-weight: 600; }
  .role-present { border-color: #2e7d32; }
  .role-present .role-name { color: #2e7d32; }
  .role-missing { border-style: dashed; border-color: #b71c1c; opacity: 0.85; }
  .role-missing .role-name { color: #b71c1c; }
  .role-missing-tag { font-size: 11px; color: #b71c1c; }
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
<p id="units-state" class="units-state" style="display:none"></p>
<p class="muted">three layers: state is OTel activity (active / idle /
unknown — never "dead"); tmux liveness is the row's presence itself;
redmine is read-only gate context (latest open issue), degrading to
unconfigured / unavailable without affecting the other layers.
Jump switches the attached tmux client (iTerm2 -CC focus is out of scope).</p>
<h3>grouped (Project Group &#8594; Unit &#8594; Target)</h3>
<div id="grouped-meta" class="muted">grouped: loading…</div>
<div id="grouped"></div>
<p class="muted">grouped read model (#12286 / #12377): Project Group headers
(managed vs default, with an active / reload / attention summary), each lane's
lane / issue identity, and a fixed Codex / Claude role matrix (the Target layer) so
a one-sided lane shows the absent role as "missing" and an empty / missing lane and
a stale row stay visible. Display only — group membership and freshness are a
projection, never routing authority; an action re-resolves its candidate Unit live
before acting. project_group_presentation is a desired display-placement request
(same_cockpit_column default), never a guaranteed window / tab.</p>
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
// #12378 empty vs error: an empty cockpit (the daemon responded but nothing is
// observed) must never look the same as a data-unavailable error (the daemon
// could not be reached). The two carry distinct text and a distinct state class.
const EMPTY_UNITS_TEXT = 'no agents observed — the cockpit is empty (the daemon responded)';
const ERROR_UNITS_TEXT = 'cockpit data unavailable — could not reach the daemon (retrying)';
const EMPTY_GROUPED_TEXT = 'no project groups observed — the cockpit is empty';
// Surface the flat unit table's empty / error / ok state on a dedicated line so
// an empty cockpit and an unreachable daemon never render as the same blank
// table (#12378). Diagnostic only — it moves no gate and authorizes no action.
function setUnitsState(mode, text) {
  const el = document.getElementById('units-state');
  if (mode === 'ok') {
    el.style.display = 'none';
    el.className = 'units-state';
    el.textContent = '';
    return;
  }
  el.style.display = 'block';
  el.className = 'units-state state-' + mode;  // state-empty | state-error
  el.textContent = text;
}
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
// The canonical role-pane vocabulary (cockpit_layout.ROLES = codex, claude),
// pinned in the front end so each lane renders a fixed slot per role. The class
// names a slot uses (role-present / role-missing) come from this whitelist + the
// payload's role *presence*, never from a payload-supplied string, so the (local
// but untrusted) payload can never inject a class. Codex (owner-facing gateway)
// is shown first, mirroring grouped_display.ROLE_DISPLAY_ORDER.
const GROUPED_ROLES = ["codex", "claude"];
// One Target-layer slot for a single role of a lane. A present role carries its
// jump / Finder action buttons (disabled when the row is not current); a missing
// canonical role reads "missing" so a one-sided lane is obvious at a glance. The
// server re-preflights every action regardless, so this is display only.
function roleSlot(unit, role, isPresent) {
  const slot = document.createElement('span');
  slot.className = isPresent ? 'role-slot role-present' : 'role-slot role-missing';
  const name = document.createElement('span');
  name.className = 'role-name';
  name.textContent = role;
  slot.appendChild(name);
  if (isPresent) {
    for (const [kind, label] of [['jump', 'jump'], ['reveal', 'Finder']]) {
      const button = document.createElement('button');
      button.textContent = label;
      button.disabled = !!unit.reload_required;
      button.addEventListener('click', () => actGrouped(kind, unit, role));
      slot.appendChild(button);
    }
  } else {
    const miss = document.createElement('span');
    miss.className = 'role-missing-tag';
    miss.textContent = 'missing';
    slot.appendChild(miss);
  }
  return slot;
}
// Render one lane (Unit) row: its lane identity (lane + issue label), its
// state / freshness, and the role matrix (a fixed codex / claude slot plus any
// other observed role). DOM APIs only; every label lands via textContent and the
// freshness / role class names are whitelisted, so the payload can never inject
// markup or a class. A degraded (reload_required) row reads as needing attention
// and its action buttons are disabled; the server re-preflights regardless.
function laneRow(unit, hidden) {
  const row = document.createElement('div');
  row.className = hidden ? 'lane-row hidden-unit' : 'lane-row';
  if (unit.reload_required) row.classList.add('lane-attention');
  const ident = document.createElement('div');
  ident.className = 'lane-ident';
  const laneId = document.createElement('span');
  laneId.className = 'lane-id';
  laneId.textContent = unit.lane_label || '-';
  ident.appendChild(laneId);
  if (unit.issue_label) {
    const issue = document.createElement('span');
    issue.className = 'lane-issue';
    issue.textContent = unit.issue_label;
    ident.appendChild(issue);
  }
  if (hidden) {
    const tag = document.createElement('span');
    tag.className = 'muted';
    tag.textContent = '(hidden)';
    ident.appendChild(tag);
  }
  row.appendChild(ident);
  const fresh = KNOWN_FRESHNESS.includes(unit.freshness) ? unit.freshness : 'unknown';
  const state = document.createElement('span');
  state.className = 'lane-state fresh-' + fresh;
  state.textContent = (unit.state_label || unit.status || 'unknown') +
    ' / ' + (unit.freshness_label || fresh);
  row.appendChild(state);
  // The role matrix: a fixed slot per canonical role so a one-sided lane shows a
  // "missing" slot for the absent role, then any other observed role as present.
  const present = new Set(unit.roles || []);
  const extras = (unit.roles || []).filter((r) => !GROUPED_ROLES.includes(r));
  const matrix = document.createElement('div');
  matrix.className = 'role-matrix';
  for (const role of GROUPED_ROLES) matrix.appendChild(roleSlot(unit, role, present.has(role)));
  for (const role of extras) matrix.appendChild(roleSlot(unit, role, true));
  row.appendChild(matrix);
  return row;
}
// The whole-projection summary line: placement + freshness, the lane / active /
// reload / attention roll-up (#12297 summary), and a reload hint. Counts only,
// no routing authority.
function groupedSummaryText(data) {
  const s = data.summary || {};
  let text = 'placement: ' + (data.project_group_presentation || 'unknown') +
    ' · ' + (data.freshness_label || 'unknown');
  if (typeof s.total === 'number') {
    text += ' · ' + s.total + ' lanes · ' + (s.active_lanes || 0) + ' active · ' +
      (s.reload_required || 0) + ' reload · ' + (s.attention || 0) + ' attention';
  }
  if (data.needs_attention) text += ' · reload recommended';
  return text;
}
// Render one Project Group section: a header (label + managed/source tag + the
// projection-only attention summary) and its lane rows. A managed (configured)
// group and a default / ungrouped bucket carry distinct classes so they read as
// separate; an empty group stays visible (never dropped) so a missing lane shows.
function groupSection(g) {
  const box = document.createElement('div');
  box.className = 'group ' + (g.managed ? 'managed' : 'default');
  const header = document.createElement('div');
  header.className = 'group-header';
  const title = document.createElement('span');
  title.className = 'group-title';
  title.textContent = g.header_label || '(ungrouped)';
  header.appendChild(title);
  const tag = document.createElement('span');
  tag.className = 'tag';
  let tagText = g.managed ? g.source : g.source + ' (unmanaged)';
  if (g.stale) tagText += ' · stale';
  if (g.reload_required) tagText += ' · reload';
  tag.textContent = tagText;
  if (g.stale) tag.classList.add('stale');
  else if (g.reload_required) tag.classList.add('reload');
  header.appendChild(tag);
  const summary = g.summary || {};
  const sum = document.createElement('span');
  sum.className = 'group-summary';
  sum.textContent = (summary.active_lanes || 0) + ' active / ' +
    (summary.reload_required || 0) + ' reload / ' + (summary.attention || 0) + ' attention';
  if (summary.needs_attention) sum.classList.add('attention');
  header.appendChild(sum);
  box.appendChild(header);
  for (const u of (g.units || [])) box.appendChild(laneRow(u, false));
  for (const u of (g.hidden_units || [])) box.appendChild(laneRow(u, true));
  if (!(g.units || []).length && !(g.hidden_units || []).length) {
    const empty = document.createElement('div');
    empty.className = 'lane-row muted';
    empty.textContent = 'no lane observed in this group';
    box.appendChild(empty);
  }
  return box;
}
function renderGrouped(data) {
  const meta = document.getElementById('grouped-meta');
  const container = document.getElementById('grouped');
  container.replaceChildren();
  if (!data || !Array.isArray(data.groups)) {
    // Malformed / missing payload is an error, not an empty cockpit (#12378).
    meta.className = 'muted state-error';
    meta.textContent = 'grouped: unavailable';
    return;
  }
  meta.className = 'muted';
  meta.textContent = groupedSummaryText(data);
  if (!data.groups.length) {
    // Zero groups is an empty projection (the daemon responded), shown as a
    // neutral empty note — distinct from the red "unavailable" error (#12378).
    const empty = document.createElement('div');
    empty.className = 'lane-row state-empty';
    empty.textContent = EMPTY_GROUPED_TEXT;
    container.appendChild(empty);
    return;
  }
  for (const g of data.groups) container.appendChild(groupSection(g));
}
async function refreshGrouped() {
  const meta = document.getElementById('grouped-meta');
  try {
    const res = await fetch('/api/grouped-units');
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      meta.className = 'muted state-error';
      meta.textContent = 'grouped: ' + (body.error || 'unavailable');
      document.getElementById('grouped').replaceChildren();
      return;
    }
    renderGrouped(await res.json());
  } catch (e) {
    // Daemon unreachable: surface a grouped error state distinct from empty,
    // then recover on the next poll.
    meta.className = 'muted state-error';
    meta.textContent = 'grouped: unavailable';
    document.getElementById('grouped').replaceChildren();
  }
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
    let rendered = 0;
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
      rendered += 1;
    }
    // Empty (the daemon responded, nothing observed) reads as a neutral note,
    // distinct from the fail-closed error state below (#12378).
    setUnitsState(rendered ? 'ok' : 'empty', EMPTY_UNITS_TEXT);
  } catch (e) {
    // The daemon is unreachable / returned unparseable data. Surface an explicit
    // error state — never the same blank surface as the empty state — and let the
    // next poll recover. The previous build swallowed this silently, so an
    // unreachable daemon looked identical to an empty cockpit.
    renderObservation(null);
    setUnitsState('error', ERROR_UNITS_TEXT);
  }
  // Transitions are a secondary panel; a failure here must not be read as a
  // units error. The units empty / error state above already reflects the
  // primary fetch.
  try {
    const tr = await (await fetch('/api/transitions')).json();
    const list = document.getElementById('transitions');
    list.replaceChildren();
    for (const t of (tr.transitions || [])) {
      const item = document.createElement('li');
      item.textContent = t.observed_at + ' ' + t.agent_kind + '@' +
        t.session + ': ' + t.previous_state + ' \\u2192 ' + t.state;
      list.appendChild(item);
    }
  } catch (e) { /* transitions are secondary; the units state already shows */ }
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
