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


def attach_attention(payload: dict, *, observed_at: str) -> dict:
    """Enrich a units payload's panes with the additive ``attention`` field (#12007).

    A fourth, read-only projection layer over the inventory snapshot — after the
    tmux liveness (the row's presence + ``stale``), OTel ``activity``, and
    Redmine gate layers: the derived #11951 ``AttentionRecord`` so a cockpit
    frontend consumer can triage owner_waiting / review_waiting / blocked /
    stalled panes from the same data source as ``agents targets --json``, which
    already carries this field (#11952). Shares
    :func:`~mozyo_bridge.domain.attention.conservative_attention` with that
    surface so the two attention projections never drift.

    Additive and public-safe: it adds one ``attention`` key per pane, never
    removing or altering the ``pane_id`` identity or the tmux / OTel / Redmine
    layers; no durable attention source is wired yet, so on a live (runtime-
    readable) snapshot it never fabricates an owner/review signal — a cleanly-
    identified pane derives ``healthy`` / ``no_attention_source`` and an
    unreadable identity ``unknown``; and ``source_refs`` carry only the tmux pane
    id, so no path / secret leaks. Cockpit-layer only — like the Redmine join,
    the ``session list`` CLI payload stays attention-free.

    Stale fail-safe (Redmine #12007 review j#58888): when the snapshot is
    ``stale`` (tmux runtime unreadable, rows served from the cache), per-pane
    liveness cannot be honestly asserted, so attention degrades to ``unknown`` /
    ``source_unreadable`` for the whole payload rather than showing a cached row
    as ``healthy``. ``cockpit-attention-state.md`` (the ``unknown`` state and its
    verification note) and ``runtime-observability-boundary.md`` both require
    source-unreadable to derive ``unknown``, never ``healthy`` — a frontend
    consumer must not read a runtime-unreadable pane as healthy from the
    attention field even when the top-level ``stale`` flag is set.

    Limitation: this attention projection keys on ``workspace_id`` and does not
    consume the per-pane lane identity (the inventory now folds ``@mozyo_lane_id``
    into each record's ``lane_id`` for the grouped Unit projection, Redmine #12293,
    but the flat attention layer ignores it) and carries no per-pane
    role-ambiguity flag here (``agents targets`` carries ``ambiguous``);
    ``unit_id`` is opaque provenance, never a routing key.
    """
    from mozyo_bridge.domain.agent_discovery import (
        CONFIDENCE_NONE,
        ROLE_SOURCE_UNKNOWN,
    )
    from mozyo_bridge.domain.attention import (
        ROLE_CLAUDE,
        ROLE_CODEX,
        conservative_attention,
    )

    # A stale snapshot makes the runtime source unreadable for every pane, so no
    # row can derive `healthy` regardless of how strong its cached identity is.
    stale = bool(payload.get("stale"))
    for pane in payload.get("panes") or []:
        if not isinstance(pane, dict):
            continue
        role = pane.get("agent_kind") or ""
        workspace = pane.get("workspace")
        workspace_id = (
            (workspace.get("workspace_id") or "")
            if isinstance(workspace, dict)
            else ""
        )
        identity_readable = (
            not stale
            and role in (ROLE_CLAUDE, ROLE_CODEX)
            and pane.get("confidence") != CONFIDENCE_NONE
            and pane.get("role_source") != ROLE_SOURCE_UNKNOWN
        )
        record = conservative_attention(
            observed_at=observed_at,
            role=role,
            identity_readable=identity_readable,
            # The inventory payload carries no per-pane role-ambiguity flag; a
            # genuinely unreadable identity already degrades via
            # ``identity_readable`` above.
            contradictory=False,
            workspace_id=workspace_id,
            pane_id=pane.get("pane_id"),
        )
        pane["attention"] = record.as_payload()
    return payload


def attach_observation(payload: dict, snapshot, *, now) -> dict:
    """Attach the runtime observation freshness envelope to a units payload (#12225).

    A fifth, read-only projection layer over the inventory snapshot — after the
    tmux liveness, OTel ``activity``, Redmine gate, and ``attention`` layers: the
    #12224 runtime observation snapshot envelope (``observed_at`` / ``source`` /
    ``method`` / ``freshness`` / ``readability`` / ``strength`` / ``stale_reason``
    / ``display_state``) describing how fresh the *displayed* inventory snapshot
    itself is. The cockpit UI renders it as a "last refreshed / observed_at"
    freshness line plus a manual **Reload** affordance, so an operator sees the
    runtime view is a timestamped snapshot — not live truth — and can refresh it
    on demand (v1 = explicit reload, no background polling/push added here).

    The envelope is derived from the same inventory snapshot the rows are built
    from (``snapshot``), via the one mapping
    :func:`~mozyo_bridge.application.commands_runtime_observation.snapshot_from_inventory`
    the ``observe reload`` CLI uses, so the GUI and CLI never disagree about
    freshness.

    Boundary (``runtime-observability-boundary.md`` ``### Contract handoff to
    follow-up issues`` / ``### Freshness / fail-safe semantics``): this is
    diagnostic / display only. It never updates workflow truth, owner approval,
    review, routing, close, or completion (those stay with the Redmine durable
    record); it never authorizes a side-effecting action (those run their own
    action-time live preflight in :func:`_resolve_record`); and a stale /
    unreadable snapshot derives ``reload_required`` / ``unknown``, never
    ``healthy``. The visible "stale" label rides in ``freshness``, so the
    snapshot can still be shown without reading as current.

    Additive and public-safe: it adds one top-level ``observation`` key, never
    altering the panes or the tmux / OTel / Redmine / attention layers, and the
    envelope's ``source_refs`` carry only a tmux/cache tag plus the snapshot
    time, no path / secret. Cockpit-layer only, like the Redmine and attention
    joins — the ``session list`` CLI payload stays observation-free.
    """
    from mozyo_bridge.application.commands_runtime_observation import (
        snapshot_from_inventory,
    )

    snap = snapshot_from_inventory(snapshot, now=now)
    payload["observation"] = snap.as_payload()
    return payload


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

    A :class:`~mozyo_bridge.domain.grouped_read_model.UnitView` is display state.
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
    from mozyo_bridge.domain.attention import ROLE_CLAUDE, ROLE_CODEX

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


# --- served grouped cockpit read-model wiring (Redmine #12286) ----------------
#
# The predecessors (#12263 schema/resolver, #12264 read model, #12266 reload
# view, #12255 display view) are all pure object-to-object slices that never read
# the live runtime or the on-disk grouping config. #12286 is the *live wiring*:
# it builds the grouped display view a cockpit renders from (a) the repo-local
# desired grouping config loaded from `.mozyo-bridge/config.yaml` and (b) the
# live tmux inventory snapshot, so the Project Group -> Unit -> Target view is
# served from real data and its reload / freshness line matches the same snapshot
# the rows are built from.
#
# It stays a *display projection*: it resolves no handoff target and grants no
# authority. Acting on a grouped row still goes through the candidate-Unit
# selector + action-time live preflight (`grouped_reveal` / `grouped_jump`), so
# the served projection can only NAME a candidate, never authorize a side effect.


def observed_units_from_inventory(snapshot, *, observation):
    """Aggregate a live inventory snapshot into grouped read-model ``ObservedUnit``s.

    The flat inventory is pane-centric (one row per agent pane); the grouped read
    model is Unit-centric (one row per ``workspace_id`` / lane / host, carrying the
    set of agent *roles* that have a live pane). This maps the former to the
    latter:

    - only agent panes (``claude`` / ``codex``) with a resolved ``workspace_id``
      become Units; a pane with no workspace identity cannot form a routable /
      groupable Unit and is skipped (it still shows in the flat table);
    - panes are aggregated by ``(workspace_id, lane_id)`` into one Unit per lane
      whose ``roles`` is the observed role set and whose ``repo_label`` is the
      workspace's public-safe display label (project name / canonical session).
      ``lane_id`` is the pane's checkout-local lane identity (Redmine #12293): the
      ``@mozyo_lane_id`` pane option the inventory now folds (the
      :data:`~mozyo_bridge.session_inventory.DEFAULT_LANE` for a normal ``mozyo``
      pane with no lane option). So one repo running several lanes / worktrees with
      distinct lane ids splits into **distinct** Units — a faithful
      ``Unit = workspace + lane + role set`` — instead of collapsing into one row;
    - ``host_id`` is ``local`` because the cockpit inventory observes the local
      tmux server only; a remote host cannot be fabricated here;
    - ``active`` is the observed liveness fact: a Unit has a live Target only when
      the runtime is actually readable, so a **stale** snapshot yields
      ``active=False`` (the fail-safe posture — no live Target is asserted from a
      cache) and the per-Unit ``observation`` envelope carries the staleness;
    - every Unit shares the whole-projection ``observation`` envelope so the
      grouped reload / freshness line never contradicts the per-row freshness
      (both derive from the one snapshot) — **except** a lane-ambiguous Unit (see
      below), whose envelope carries a visible contradiction.

    Lane-ambiguous fail-closed fallback (Redmine #12286 review j#61995, preserved
    by #12293). A faithful Unit is one ``(workspace_id, lane_id, host_id)`` with at
    most one live pane per role. Reading ``@mozyo_lane_id`` makes the common
    multi-lane case faithful (distinct lanes → distinct Units), but the lane
    discriminator can still be **unreadable**: several panes that carry no lane
    option (or the same lane id) collapse onto the same ``(workspace_id, lane_id)``
    bucket and produce *more than one* live pane for a role. Without a faithful
    discriminator we cannot split them, so collapsing into one healthy actionable
    Unit would serve enabled action buttons whose candidate then resolves
    *ambiguous* at action time. Instead the Unit is degraded to a **visible
    contradicted** row (``live_runtime_conflict``): it is shown but reads
    ``needs_reload`` / unactionable, so its action affordances are disabled and the
    operator must use an explicit pane target. ``_resolve_unit_target`` still fails
    closed on the same per-lane ambiguity, so this is defense in depth, not the
    only guard. The lane identity is a display / split fact only; it never becomes
    routing, approval, or close authority.

    Pure aggregation: it carries identity + role *presence* only, never a
    pane id / target, so the result is display state, not a routing endpoint.
    """
    from dataclasses import replace

    from mozyo_bridge.domain.attention import ROLE_CLAUDE, ROLE_CODEX
    from mozyo_bridge.domain.grouped_read_model import ObservedUnit
    from mozyo_bridge.domain.runtime_observation import (
        CONTRADICTION_LIVE_RUNTIME_CONFLICT,
        DISPLAY_STATE_RELOAD_REQUIRED,
    )

    live = not snapshot.stale
    # Aggregate by (workspace_id, lane_id): one Unit per lane within a workspace,
    # so distinct lanes of one repo stay distinct Units (faithful split) and only a
    # genuinely unreadable lane discriminator collapses panes onto one bucket.
    by_unit: dict[tuple[str, str], dict] = {}
    for record in snapshot.records:
        if record.agent_kind not in (ROLE_CLAUDE, ROLE_CODEX):
            continue
        workspace = record.workspace
        workspace_id = workspace.workspace_id if workspace is not None else None
        if not workspace_id:
            continue
        lane_id = (record.lane_id or "").strip() or DEFAULT_LANE
        key = (workspace_id, lane_id)
        entry = by_unit.setdefault(
            key, {"role_counts": {}, "label": None}
        )
        entry["role_counts"][record.agent_kind] = (
            entry["role_counts"].get(record.agent_kind, 0) + 1
        )
        if entry["label"] is None and workspace is not None:
            entry["label"] = (
                workspace.project_name
                or workspace.canonical_session
                or workspace_id
            )
    units: list = []
    for workspace_id, lane_id in sorted(by_unit):
        entry = by_unit[(workspace_id, lane_id)]
        role_counts = entry["role_counts"]
        roles = tuple(
            role for role in (ROLE_CODEX, ROLE_CLAUDE) if role in role_counts
        )
        # >1 live pane for any role under the same (workspace, lane, host) means
        # the lane discriminator did not faithfully separate them; degrade to a
        # visible contradicted row rather than a healthy actionable one.
        ambiguous = any(count > 1 for count in role_counts.values())
        unit_observation = observation
        if ambiguous:
            unit_observation = replace(
                observation,
                contradiction=CONTRADICTION_LIVE_RUNTIME_CONFLICT,
                display_state=DISPLAY_STATE_RELOAD_REQUIRED,
            )
        units.append(
            ObservedUnit(
                workspace_id=workspace_id,
                lane_id=lane_id,
                host_id=DEFAULT_HOST,
                repo_label=entry["label"],
                active=live,
                roles=roles,
                observation=unit_observation,
            )
        )
    return units


def grouped_units_payload(
    *,
    home: Path | None = None,
    now=None,
    repo_root: Path | None = None,
) -> dict:
    """Build the served grouped cockpit display payload from live data (#12286).

    Composes the desired grouping config (loaded from the repo-local
    ``.mozyo-bridge/config.yaml`` — a missing / empty config is the
    behavior-preserving default) with the live tmux inventory snapshot, into the
    #12255 grouped display view a cockpit renders: Project Group headers, their
    Unit rows (lane / issue labels + Codex / Claude role presence), and the
    whole-view freshness / reload line plus the desired
    ``project_group_presentation`` display-placement mode.

    Boundary: this is a display projection. The freshness envelope is derived from
    the same ``snapshot`` the rows are built from (via ``snapshot_from_inventory``,
    the one mapping the ``observe reload`` CLI and ``attach_observation`` share),
    so the served reload / freshness display never contradicts the projection
    snapshot. No row carries a pane / target; acting on a grouped row re-resolves
    its candidate identity live through ``grouped_reveal`` / ``grouped_jump``.
    """
    from datetime import datetime, timezone

    from mozyo_bridge.application.commands_runtime_observation import (
        snapshot_from_inventory,
    )
    from mozyo_bridge.application.repo_local_config_loader import (
        load_repo_local_config,
    )
    from mozyo_bridge.domain.grouped_display import build_grouped_display_view
    from mozyo_bridge.domain.grouped_read_model import build_grouped_read_model

    if now is None:
        now = datetime.now(timezone.utc)
    config = load_repo_local_config(repo_root).presentation.grouping
    snapshot = take_inventory(home=home)
    observation = snapshot_from_inventory(snapshot, now=now)
    observed_units = observed_units_from_inventory(
        snapshot, observation=observation
    )
    model = build_grouped_read_model(
        config, observed_units, observation=observation
    )
    return build_grouped_display_view(model).as_payload()


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
  .rm-available    { color: #1565c0; }
  .rm-unconfigured { color: #9e9e9e; }
  .rm-unavailable  { color: #b71c1c; }
  .stale-banner { background: #fff3e0; padding: 4px 8px; display: none; }
  button { font-size: 12px; }
  #transitions li { color: #555; }
  .muted { color: #999; font-size: 11px; }
  #controls { margin: 4px 0; display: flex; align-items: center; gap: 8px; }
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
<table id="units"><thead><tr>
<th>state</th><th>agent</th><th>session</th><th>workspace</th>
<th>redmine</th><th>actions</th>
</tr></thead><tbody></tbody></table>
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
