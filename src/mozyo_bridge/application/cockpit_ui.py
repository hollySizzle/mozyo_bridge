"""Cockpit Web UI served by the mozyo-bridge daemon (Redmine #11679/#11680).

Owner decision #11639 journal #56164: the cockpit is a localhost Web UI
served by the same daemon process that already receives OTLP — the iTerm2
Toolbelt webview is the default host but any browser shows the identical
UI, so the GUI investment is terminal-independent. Constraints carried
into this module:

- **127.0.0.1 only** — the UI inherits the receiver's loopback-only bind
  gate; workspace names and paths of confidential departments appear here,
  so there is no remote exposure (authenticated remote access is a future
  separate issue).
- **No auto-foregrounding** (US constraint 5): every action below runs
  only as the direct result of an explicit user click (the POST request);
  nothing in the daemon initiates focus changes on its own.
- **Structured commands only**: `open` and `tmux` are invoked with
  argument lists, never shell strings, so paths with spaces / Japanese
  segments cannot inject.
- **Stale-safe**: actions re-resolve the pane against a fresh runtime
  inventory; a vanished pane / session / tmux server fails with an
  explanatory JSON error telling the operator to refresh, never with a
  blind command.
- **Jump v1 is `switch-client`** on an attached tmux client (most
  recently active non-control-mode client preferred). Moving focus of an
  iTerm2 `-CC` window is explicitly out of v1 scope.
- No prompts / secrets / personal data beyond what the inventory already
  exposes locally; nothing is written to Redmine.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mozyo_bridge.session_inventory import InventoryRecord, take_inventory


class CockpitActionError(RuntimeError):
    """User-facing action failure (stale target, no client, bad input)."""


def units_payload(*, home: Path | None = None) -> dict:
    """The unit list the UI renders: the inventory snapshot payload.

    Carries all three available layers per unit: tmux runtime presence
    (the snapshot itself + ``stale``), OTel ``activity``, and — in phase 4
    — the Redmine gate context will join here.
    """
    return take_inventory(home=home).as_payload()


def _resolve_record(
    pane_id: str, *, home: Path | None = None
) -> InventoryRecord:
    """Re-resolve a pane against the live runtime before acting on it."""
    if not isinstance(pane_id, str) or not pane_id.startswith("%"):
        raise CockpitActionError(f"invalid pane id {pane_id!r}")
    snapshot = take_inventory(home=home)
    if snapshot.stale:
        raise CockpitActionError(
            "tmux runtime is unavailable (snapshot is stale); cannot act on "
            "panes right now. Check the tmux server and refresh."
        )
    for record in snapshot.records:
        if record.pane_id == pane_id:
            return record
    raise CockpitActionError(
        f"pane {pane_id} is no longer in the runtime inventory (it may "
        "have exited). Refresh the unit list."
    )


def reveal_in_finder(pane_id: str, *, home: Path | None = None) -> dict:
    """Open the unit's repo root in the OS file manager (macOS ``open``)."""
    record = _resolve_record(pane_id, home=home)
    root = record.repo_root or record.cwd
    if not root or not Path(root).is_dir():
        raise CockpitActionError(
            f"pane {pane_id} has no existing directory to reveal "
            f"(repo_root={record.repo_root!r}, cwd={record.cwd!r})."
        )
    if sys.platform != "darwin":
        raise CockpitActionError(
            "Reveal in Finder is macOS-only (`open`); this host is "
            f"{sys.platform}."
        )
    result = subprocess.run(
        ["open", root], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise CockpitActionError(
            f"open failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return {"action": "reveal", "pane_id": pane_id, "path": root}


def _pick_attached_client() -> str:
    """The most recently active attached tmux client, non-control preferred.

    Jump v1 targets a regular attach client (`switch-client`); an iTerm2
    control-mode (`-CC`) client may technically accept switch-client but
    window focus there is iTerm2's domain and out of v1 scope, so control
    clients are only used when no regular client exists.
    """
    from mozyo_bridge.infrastructure.tmux_client import run_tmux

    result = run_tmux(
        "list-clients",
        "-F",
        "#{client_activity}\t#{client_control_mode}\t#{client_name}",
        check=False,
    )
    if result.returncode != 0:
        raise CockpitActionError(
            "tmux list-clients failed; is the tmux server running?"
        )
    regular: list[tuple[int, str]] = []
    control: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[2]:
            continue
        try:
            activity = int(parts[0])
        except ValueError:
            activity = 0
        (control if parts[1] == "1" else regular).append((activity, parts[2]))
    pool = regular or control
    if not pool:
        raise CockpitActionError(
            "no attached tmux client to switch; attach a terminal to the "
            "tmux server first (jump v1 uses `switch-client`)."
        )
    return max(pool)[1]


def jump_to_unit(pane_id: str, *, home: Path | None = None) -> dict:
    """Switch the attached tmux client to the unit's window (jump v1)."""
    from mozyo_bridge.infrastructure.tmux_client import run_tmux

    record = _resolve_record(pane_id, home=home)
    client = _pick_attached_client()
    target = f"{record.session}:{record.window_index}"
    result = run_tmux(
        "switch-client", "-c", client, "-t", target, check=False
    )
    if result.returncode != 0:
        raise CockpitActionError(
            f"switch-client failed for {target}: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown'}. "
            "The session may have closed; refresh the unit list."
        )
    return {
        "action": "jump",
        "pane_id": pane_id,
        "client": client,
        "target": target,
    }


# The page is a single self-contained document: no external assets, no
# CDN, nothing fetched off-host — consistent with the loopback-only and
# no-exfiltration posture. Kept intentionally small; it is an indicator
# surface, not an app platform.
#
# Two safety properties are load-bearing (review #56197):
#
# - Rendering uses DOM APIs (`textContent` / `createElement`) only —
#   never `innerHTML` — so workspace / session / path strings, which are
#   operator- or checkout-controlled local input, cannot inject HTML/JS
#   into the UI origin.
# - Every action request carries the per-process cockpit token (injected
#   into the ``__COCKPIT_TOKEN__`` placeholder when the page is served)
#   in a custom header, which the action endpoints require. A custom
#   header also forces a CORS preflight, so a cross-site simple request
#   can never express action intent.
INDEX_HTML_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>mozyo cockpit</title>
<style>
  body { font: 13px/1.5 -apple-system, sans-serif; margin: 1rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 2px 8px; border-bottom: 1px solid #ddd; }
  .active  { color: #2e7d32; font-weight: 600; }
  .idle    { color: #ef6c00; font-weight: 600; }
  .unknown { color: #757575; }
  .stale-banner { background: #fff3e0; padding: 4px 8px; display: none; }
  button { font-size: 12px; }
  #transitions li { color: #555; }
  .muted { color: #999; font-size: 11px; }
</style>
</head>
<body>
<h2>mozyo cockpit</h2>
<div id="stale" class="stale-banner">tmux runtime unavailable — showing the
last cached snapshot; activity may be outdated and actions are disabled.</div>
<table id="units"><thead><tr>
<th>state</th><th>agent</th><th>session</th><th>workspace</th><th>actions</th>
</tr></thead><tbody></tbody></table>
<p class="muted">state is OTel activity (active / idle / unknown) — unknown
or idle never means dead; tmux liveness is the row's presence itself.
Jump switches the attached tmux client (iTerm2 -CC focus is out of scope).</p>
<h3>recent transitions</h3>
<ul id="transitions"></ul>
<script>
const COCKPIT_TOKEN = "__COCKPIT_TOKEN__";
const KNOWN_STATES = ["active", "idle", "unknown"];
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
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
