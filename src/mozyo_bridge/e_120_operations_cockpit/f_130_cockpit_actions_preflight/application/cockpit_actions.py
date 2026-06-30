"""Served cockpit action endpoints + action-time live preflight bridge.

Split out of ``cockpit_ui`` (Redmine #12323) so the side-effecting actions and
their action-time live preflight no longer share a module with UI rendering
(:mod:`mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application.cockpit_page`) or the read-only served-API
payload assembly (:mod:`mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application.cockpit_payload`). This module
owns the action permission surface only: it re-resolves a pane / candidate Unit
against a FRESH runtime inventory at action time and fails closed on every
uncertainty, then performs the structured ``open`` / ``switch-client`` side
effect. It depends on no rendering and no served-payload code.

Owner decision #11639 journal #56164 constraints carried into the actions:

- **127.0.0.1 only** — the UI inherits the receiver's loopback-only bind gate.
- **No auto-foregrounding** (US constraint 5): every action runs only as the
  direct result of an explicit user click (the POST request); nothing in the
  daemon initiates focus changes on its own.
- **Structured commands only**: ``open`` and ``tmux`` are invoked with argument
  lists, never shell strings, so paths with spaces / Japanese segments cannot
  inject.
- **Stale-safe**: actions re-resolve the pane against a fresh runtime inventory;
  a vanished pane / session / tmux server fails with an explanatory error
  telling the operator to refresh, never with a blind command.
- **Jump v1 is ``switch-client``** on an attached tmux client (most recently
  active non-control-mode client preferred). Moving focus of an iTerm2 ``-CC``
  window is explicitly out of v1 scope.
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
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import run_tmux

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
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import run_tmux

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


# --- grouped cockpit Unit actions (Redmine #12265) ---------------------------
#
# A grouped cockpit UI (the Project Group -> Unit view the #12264 grouped read
# model projects) lets the operator act on a *Unit row*, which carries no pane /
# Target — only the Unit's public-safe identity (workspace_id / lane_id /
# host_id). The grouped read model is a display projection and NEVER a routing
# authority: its group membership, position, ``active`` flag and freshness are
# display state, not a permission (``unit-target-model.md`` "Target resolver は
# Project Group を authority として使わない"; ``runtime-observability-boundary.md``
# ``## Action-Time Live Preflight Boundary``).
#
# So a grouped action takes only the candidate Unit identity and re-resolves it
# to exactly one live pane against a FRESH inventory at action time, failing
# closed on every uncertainty, then delegates the side effect to the existing
# pane-centric ``reveal_in_finder`` / ``jump_to_unit`` — action permission stays
# with the established live-preflight surface, never with the displayed snapshot.

DEFAULT_LANE: str = "default"
DEFAULT_HOST: str = "local"


def candidate_unit_selector(unit_view) -> dict:
    """The identity selector a grouped action may carry from a read-model row.

    A :class:`~mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.grouped_read_model.UnitView` is display state.
    Only its public-safe *identity* (``workspace_id`` / ``lane_id`` / ``host_id``)
    may seed an action; the row's ``group_id`` / ``active`` / ``position`` /
    freshness are display facts, never routing authority. A degraded row — one
    that ``needs_reload`` (stale / unreadable / contradicted / identity_conflict /
    desired_unit_missing / partial / unknown) — yields no selector at all: it
    fails closed so the operator must reload and live-preflight first.

    This helper may consult the snapshot's status only to *refuse* (fail closed),
    never to *permit*: a fresh row contributes identity only, and the side
    effect's permission is still decided by the action-time live preflight in
    :func:`_resolve_unit_target`, not by this row.
    """
    if getattr(unit_view, "needs_reload", True):
        raise CockpitActionError(
            "the displayed unit row is not current (status="
            f"{getattr(unit_view, 'status', 'unknown')!r}); reload and "
            "live-preflight before acting on it."
        )
    return {
        "workspace_id": unit_view.workspace_id,
        "lane_id": unit_view.lane_id,
        "host_id": unit_view.host_id,
    }


def _resolve_unit_target(
    *,
    workspace_id: str,
    role: str,
    lane_id: str = DEFAULT_LANE,
    host_id: str = DEFAULT_HOST,
    home: Path | None = None,
) -> InventoryRecord:
    """Action-time live preflight for a grouped read-model *candidate* Unit.

    Maps a candidate Unit identity (the only thing a grouped read-model row may
    contribute) to exactly one live pane, ignoring the displayed projection
    entirely and re-querying a fresh inventory. The candidate's ``lane_id`` only
    *narrows* the live match set against the lane the fresh inventory reads from
    each pane's ``@mozyo_lane_id`` option (Redmine #12293) — it is an identity
    selector, never routing / approval / close authority, and the truth is always
    re-read live. Fails closed on every uncertainty so a grouped projection can
    only *name* a candidate, never authorize a side effect:

    - a missing / empty ``workspace_id`` or a non-agent ``role`` is rejected;
    - a non-local ``host_id`` is rejected — the cockpit inventory observes the
      local tmux server only, so a remote candidate cannot be faithfully resolved
      here and must use an explicit live target
      (``local-remote-cockpit-host-boundary.md`` / ``unit-target-model.md``);
    - a stale snapshot (tmux runtime unreadable) is rejected;
    - zero live panes matching the ``(workspace_id, lane_id, role)`` identity is
      rejected (the Unit / lane may have exited); and
    - more than one matching live pane is *ambiguous* and rejected — the lane
      discriminator did not faithfully separate them, so a contradicted / drifted
      projection never picks one silently.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import ROLE_CLAUDE, ROLE_CODEX

    if not isinstance(workspace_id, str) or not workspace_id:
        raise CockpitActionError(
            "grouped action requires a workspace_id from the candidate unit."
        )
    if role not in (ROLE_CLAUDE, ROLE_CODEX):
        raise CockpitActionError(
            "grouped action requires an agent role (claude/codex); got "
            f"{role!r}."
        )
    if host_id != DEFAULT_HOST:
        raise CockpitActionError(
            f"grouped action cannot resolve a non-local host ({host_id!r}); the "
            "cockpit inventory observes the local tmux server only. Use an "
            "explicit live target."
        )
    lane_id = (lane_id or "").strip() or DEFAULT_LANE
    snapshot = take_inventory(home=home)
    if snapshot.stale:
        raise CockpitActionError(
            "tmux runtime is unavailable (snapshot is stale); cannot act on "
            "panes right now. Check the tmux server and refresh."
        )
    matches = [
        record
        for record in snapshot.records
        if record.agent_kind == role
        and record.workspace is not None
        and record.workspace.workspace_id == workspace_id
        and ((record.lane_id or "").strip() or DEFAULT_LANE) == lane_id
    ]
    if not matches:
        raise CockpitActionError(
            f"no live {role} pane for workspace {workspace_id!r} / lane "
            f"{lane_id!r} is in the runtime inventory (the unit may have "
            "exited). Refresh the unit list."
        )
    if len(matches) > 1:
        candidates = ", ".join(sorted(record.pane_id for record in matches))
        raise CockpitActionError(
            f"grouped target is ambiguous: {len(matches)} live {role} panes "
            f"match workspace {workspace_id!r} / lane {lane_id!r} "
            f"({candidates}). Use an explicit pane target."
        )
    return matches[0]


def grouped_reveal(
    *,
    workspace_id: str,
    role: str,
    lane_id: str = DEFAULT_LANE,
    host_id: str = DEFAULT_HOST,
    home: Path | None = None,
) -> dict:
    """Reveal a grouped *candidate* Unit's repo root, resolved live at action time.

    The candidate identity is re-resolved to a single live pane via
    :func:`_resolve_unit_target` (fail-closed), then the side effect runs through
    the existing pane-centric :func:`reveal_in_finder` — so action permission
    stays with the established live-preflight surface, never with the grouped
    read model.
    """
    record = _resolve_unit_target(
        workspace_id=workspace_id,
        role=role,
        lane_id=lane_id,
        host_id=host_id,
        home=home,
    )
    result = reveal_in_finder(record.pane_id, home=home)
    result["workspace_id"] = workspace_id
    result["role"] = role
    return result


def grouped_jump(
    *,
    workspace_id: str,
    role: str,
    lane_id: str = DEFAULT_LANE,
    host_id: str = DEFAULT_HOST,
    home: Path | None = None,
) -> dict:
    """Jump to a grouped *candidate* Unit's window, resolved live at action time.

    Like :func:`grouped_reveal`: the candidate identity is re-resolved to a
    single live pane via :func:`_resolve_unit_target` (fail-closed), then the
    side effect runs through the existing pane-centric :func:`jump_to_unit`.
    """
    record = _resolve_unit_target(
        workspace_id=workspace_id,
        role=role,
        lane_id=lane_id,
        host_id=host_id,
        home=home,
    )
    result = jump_to_unit(record.pane_id, home=home)
    result["workspace_id"] = workspace_id
    result["role"] = role
    return result


def grouped_action_preview(
    *,
    workspace_id: str,
    role: str,
    lane_id: str = DEFAULT_LANE,
    host_id: str = DEFAULT_HOST,
    home: Path | None = None,
) -> dict:
    """Non-mutating command preview for a grouped *candidate* Unit (Redmine #12296).

    Runs the **same** action-time live preflight as :func:`grouped_reveal` /
    :func:`grouped_jump` (:func:`_resolve_unit_target`) against a fresh inventory,
    but performs **no** side effect: it only reports whether the candidate identity
    would currently resolve to exactly one live pane. So the served grouped cockpit
    can show, on the Unit detail screen, whether the safe actions look available
    *right now* — including the cases a pure display projection cannot see, because
    this re-queries the live runtime: a stale snapshot, a vanished pane, or an
    **ambiguous** target (more than one live pane for the identity) each report
    ``available: False`` with the live preflight's own reason.

    This is a preview, never an authorization: ``available: True`` does not act and
    does not reserve the pane — executing the action still re-resolves the identity
    through the real preflight. The result carries only the candidate identity and
    a public-safe reason string (no repo path / credential / prompt body); the
    preflight's ambiguity reason may name pane ids, which the local inventory
    already exposes.
    """
    try:
        _resolve_unit_target(
            workspace_id=workspace_id,
            role=role,
            lane_id=lane_id,
            host_id=host_id,
            home=home,
        )
    except CockpitActionError as exc:
        return {
            "action": "preview",
            "workspace_id": workspace_id,
            "role": role,
            "lane_id": lane_id,
            "host_id": host_id,
            "available": False,
            "live_preflight_required": True,
            "reason": str(exc),
        }
    return {
        "action": "preview",
        "workspace_id": workspace_id,
        "role": role,
        "lane_id": lane_id,
        "host_id": host_id,
        "available": True,
        "live_preflight_required": True,
        "actions": ["reveal", "jump"],
    }
