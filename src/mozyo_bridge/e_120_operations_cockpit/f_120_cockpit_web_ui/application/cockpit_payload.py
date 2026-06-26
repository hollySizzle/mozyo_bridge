"""Served cockpit API payload assembly + reload / freshness presentation.

Split out of ``cockpit_ui`` (Redmine #12323) so the read-only served-API payload
projections no longer share a module with UI rendering
(:mod:`mozyo_bridge.application.cockpit_page`) or the side-effecting action /
preflight bridge (:mod:`mozyo_bridge.application.cockpit_actions`). This module
owns only the data the cockpit endpoints serve: the flat units payload and its
additive join layers (attention, runtime-observation freshness), and the grouped
read-model display payload.

Every projection here is read-only and public-safe: it never moves a workflow
gate, never authorizes a side effect (those re-preflight live in
:mod:`mozyo_bridge.application.cockpit_actions`), and a stale / unreadable
snapshot degrades to a fail-closed display state rather than reading as current.
The grouped rows carry identity + role presence only — never a pane / target —
so acting on one still re-resolves its candidate Unit live.
"""

from __future__ import annotations

from pathlib import Path

from mozyo_bridge.application.cockpit_actions import DEFAULT_HOST, DEFAULT_LANE
from mozyo_bridge.session_inventory import take_inventory


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
    :func:`~mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention.conservative_attention` with that
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
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
        CONFIDENCE_NONE,
        ROLE_SOURCE_UNKNOWN,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import (
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
    action-time live preflight in
    :func:`~mozyo_bridge.application.cockpit_actions._resolve_record`); and a
    stale / unreadable snapshot derives ``reload_required`` / ``unknown``, never
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
# selector + action-time live preflight (`grouped_reveal` / `grouped_jump` in
# cockpit_actions), so the served projection can only NAME a candidate, never
# authorize a side effect.


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
    operator must use an explicit pane target.
    :func:`~mozyo_bridge.application.cockpit_actions._resolve_unit_target` still
    fails closed on the same per-lane ambiguity, so this is defense in depth, not
    the only guard. The lane identity is a display / split fact only; it never
    becomes routing, approval, or close authority.

    Pure aggregation: it carries identity + role *presence* only, never a
    pane id / target, so the result is display state, not a routing endpoint.
    """
    from dataclasses import replace

    from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import ROLE_CLAUDE, ROLE_CODEX
    from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.grouped_read_model import ObservedUnit
    from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (
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
    from mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.domain.grouped_display import build_grouped_display_view
    from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.grouped_read_model import build_grouped_read_model

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
