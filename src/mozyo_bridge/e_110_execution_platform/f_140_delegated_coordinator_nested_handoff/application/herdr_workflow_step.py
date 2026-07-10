"""herdr-native `workflow step` resolution adapter (Redmine #13489).

Bridges the impure runtime inputs a herdr session carries — launch-time sender env, the
repo anchor, the lane metadata store, and the live ``herdr agent list`` inventory — into the
pure herdr classifier + resolver (:mod:`...domain.workflow_step_herdr`). It is the herdr
counterpart of the tmux wiring in :func:`...application.cli_workflow.cmd_workflow_step`
(``current_pane`` + tmux inventory -> :func:`...domain.workflow_step.resolve_workflow_step`),
and it produces the SAME replayable :class:`~...domain.workflow_step.WorkflowStepOutcome`
envelope so ``workflow step`` reads the same under either backend.

Increment 1 (Redmine #13489 j#74685 design_boundary) is **resolution-only**: it resolves the
current lane's herdr-native identity + role and, for a worker / gateway lane, verifies the
lane's Redmine issue anchor (from the lane metadata record) and — for a gateway — the
same-lane worker liveness **cardinality** before naming a role-appropriate next action. It
fails closed on an unattested identity, an unclassifiable lane (default-lane pair / unknown
provider), a missing / ambiguous / retired anchor, or a missing / duplicate / unaddressable
worker. It performs no sublane lifecycle mutation and no delivery — the policy-permitted
one-step auto-execution and the fail-closed destructive drain/retire boundary are increment 2.

Mid-review corrections landed here (j#74748 / j#74749 / j#74750): F1 removes the registry
``project_name`` project-scope heuristic and defers to the pure classifier's default-lane
fail-closed; F3 adds the lane-metadata anchor join; and the same-lane worker liveness returns
the 0 / 1 / 2+ cardinality (duplicate identity is ambiguity, not a target).
"""

from __future__ import annotations

import argparse
import os
from typing import Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_WORKER,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    OWNER_OPERATOR,
    PRIMITIVE_NONE,
    STATE_LANE_UNRESOLVED,
    WorkflowAnchor,
    WorkflowStepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    ANCHOR_AMBIGUOUS,
    ANCHOR_MISMATCH,
    ANCHOR_MISSING,
    ANCHOR_RETIRED,
    ANCHOR_VERIFIED,
    REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
    WORKER_ABSENT,
    WORKER_AMBIGUOUS,
    WORKER_LIVE,
    WORKER_LOCATOR_MISSING,
    WORKER_UNAVAILABLE,
    classify_herdr_workflow_lane,
    resolve_herdr_workflow_step,
)


def _anchor_workspace_id(repo_root) -> Optional[str]:
    """The sender's own workspace segment for the anchor↔env gate (mirrors herdr_send_entry).

    Under the #13377 shared-project-workspace model a lane agent runs in a linked worktree
    whose segment resolves to the MAIN checkout's workspace identity; a standalone / main
    checkout resolves to its registry workspace_id. Legacy ``wt_<hash>`` lane attestation
    (pre-#13377 lanes still live during the transition) is accepted exactly when the env
    carries the worktree's deterministically re-derived token, never an arbitrary env value.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
        herdr_workspace_segment,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        _norm,
        derive_lane_workspace_token,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
        MOZYO_WORKSPACE_ID_ENV,
    )

    anchor_ws = herdr_workspace_segment(repo_root) or None
    env_ws = _norm(os.environ.get(MOZYO_WORKSPACE_ID_ENV))
    if env_ws and env_ws != (anchor_ws or ""):
        try:
            legacy_token = derive_lane_workspace_token(str(repo_root))
        except (OSError, ValueError):
            legacy_token = ""
        if legacy_token and env_ws == legacy_token:
            anchor_ws = legacy_token
    return anchor_ws


def _load_workflow_store(args: argparse.Namespace):
    """The persisted workflow runtime store (``--store-path`` or the home default), or ``None``.

    The store is the durable workflow gate: ``workflow watch`` / ``runtime`` fold Redmine gate
    journal markers into its ``workflow_route_identities`` (issue per lane) and ``workflow_events``
    (``event_id = <issue>:<journal>``) tables. Returns ``None`` on any construction failure — the
    caller then fails **closed** (the herdr anchor gate never fail-opens on an unreadable store).
    """
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_resume import (
            _store_from_args,
        )

        return _store_from_args(args)
    except Exception:  # noqa: BLE001 - a store construction failure fails the anchor gate closed
        return None


def _journal_from_event_id(event_id: str) -> str:
    """The journal id from a durable event anchor (``<issue>:<journal>`` / ``redmine:<i>:<j>``)."""
    s = (event_id or "").strip()
    if ":" not in s:
        return ""
    return s.rsplit(":", 1)[1].strip()


def _lane_metadata_candidate_issues(repo_root, lane_id: str) -> tuple[set[str], bool]:
    """Candidate (display-only) issue ids for the lane + whether the records are all retired.

    The lane metadata store is **display metadata, never routing authority** (its own module
    contract): it is read here ONLY as a candidate/cross-check against the authoritative durable
    gate — a mismatch or an all-retired lane fails the anchor gate closed. Returns
    ``(active_issue_ids, all_retired)``; an empty set with ``all_retired=False`` means no record
    joins this lane at all (the durable gate then stands alone as the authority).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
        repo_scope_workspace_id,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        _norm,
        _norm_lane,
    )

    want_ws = _norm(repo_scope_workspace_id(repo_root))
    want_lane = _norm_lane(lane_id)
    if not want_ws:
        return set(), False
    try:
        from mozyo_bridge.core.state.lane_metadata import load_lane_records

        records = load_lane_records()
    except Exception:  # noqa: BLE001 - a display read never breaks the (store-authoritative) gate
        return set(), False

    active: set[str] = set()
    had_record = False
    had_active = False
    for record in records.values():
        if _norm(getattr(record, "repo_workspace_id", "")) != want_ws:
            continue
        if _norm_lane(getattr(record, "lane_id", "")) != want_lane:
            continue
        had_record = True
        if getattr(record, "retired", False):
            continue
        had_active = True
        issue = _norm(getattr(record, "issue_id", ""))
        if issue:
            active.add(issue)
    return active, (had_record and not had_active)


def _resolve_lane_anchor(args: argparse.Namespace, workspace_id: str, repo_root, lane_id: str) -> tuple[str, str]:
    """Verify the lane's Redmine ``issue+journal`` anchor from the durable workflow gate (F3).

    The **authoritative** source is the persisted workflow runtime store (fed from Redmine gate
    journal markers via ``workflow watch`` / ``runtime``), NOT the display-only lane metadata
    (mid-review j#74766/j#74767 R1). Resolution:

    - the store's ``workflow_route_identities`` joined on ``(workspace_id, lane_id)`` give the
      lane's issue: zero -> :data:`ANCHOR_MISSING`, two+ distinct -> :data:`ANCHOR_AMBIGUOUS`;
    - the store's ``workflow_events`` for that issue give the gate journal (the latest
      ``event_id = <issue>:<journal>``): no gate journal -> :data:`ANCHOR_MISSING`;
    - the lane metadata is consulted ONLY as a candidate cross-check: a display record whose
      issue disagrees with the store issue -> :data:`ANCHOR_MISMATCH`; an all-retired lane ->
      :data:`ANCHOR_RETIRED`.

    Store absent / unreadable / no route / no journal all fail **closed**
    (:data:`ANCHOR_MISSING`) — the herdr anchor gate never fail-opens. On success returns
    (:data:`ANCHOR_VERIFIED`, ``redmine:issue=<id>:journal=<id>``).
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        _norm,
        _norm_lane,
    )

    store = _load_workflow_store(args)
    if store is None or not store.path.exists():
        return ANCHOR_MISSING, ""
    try:
        routes = store.read_route_identities()
        events = store.read_events()
    except Exception:  # noqa: BLE001 - an unreadable store fails the anchor gate closed
        return ANCHOR_MISSING, ""

    want_ws = _norm(workspace_id)
    want_lane = _norm_lane(lane_id)
    issues = {
        _norm(getattr(r, "issue", ""))
        for r in routes
        if _norm(getattr(r, "workspace_id", "")) == want_ws
        and _norm_lane(getattr(r, "lane_id", "")) == want_lane
        and _norm(getattr(r, "issue", ""))
    }
    if not issues:
        return ANCHOR_MISSING, ""
    if len(issues) >= 2:
        return ANCHOR_AMBIGUOUS, ""
    issue = next(iter(issues))

    # The latest gate event for the issue supplies the journal (events are seq-ordered).
    journal = ""
    for event in events:
        if _norm(getattr(event, "issue", "")) != issue:
            continue
        candidate = _journal_from_event_id(getattr(event, "event_id", ""))
        if candidate:
            journal = candidate
    if not journal:
        return ANCHOR_MISSING, ""

    # Candidate cross-check against the display-only lane metadata (never the authority).
    candidate_issues, all_retired = _lane_metadata_candidate_issues(repo_root, lane_id)
    if candidate_issues and issue not in candidate_issues:
        return ANCHOR_MISMATCH, ""
    if all_retired:
        return ANCHOR_RETIRED, ""

    return ANCHOR_VERIFIED, WorkflowAnchor(issue=issue, journal=journal).pointer()


def _same_lane_worker_liveness(
    workspace_id: str, lane_id: str, *, env: Mapping[str, str]
) -> str:
    """The same-lane ``claude`` worker slot cardinality (mid-review j#74749 F2 / j#74750).

    Preserves 0 / 1 / 2+ and the usable-locator distinction from the live ``herdr agent list``
    inventory: :data:`WORKER_ABSENT` (0), :data:`WORKER_LIVE` (1 with a usable locator),
    :data:`WORKER_LOCATOR_MISSING` (1 without a locator), :data:`WORKER_AMBIGUOUS` (2+ =
    duplicate identity), :data:`WORKER_UNAVAILABLE` (the inventory could not be read). Pure over
    the decode of each row's mzb1 name — a duplicate is ambiguity, never a silently-picked target.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
        list_herdr_agent_rows,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
        WORKER_ROLE,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
        HerdrSessionStartError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        AGENT_KEY_NAME,
        _agent_locator,
        _norm_lane,
        decode_assigned_name,
    )

    try:
        rows = list_herdr_agent_rows(env)
    except HerdrSessionStartError:
        return WORKER_UNAVAILABLE
    want_lane = _norm_lane(lane_id)
    present = 0
    with_locator = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != workspace_id or identity.role != WORKER_ROLE:
            continue
        if _norm_lane(identity.lane_id) != want_lane:
            continue
        present += 1
        if _agent_locator(row):
            with_locator += 1
    if present == 0:
        return WORKER_ABSENT
    if present >= 2:
        return WORKER_AMBIGUOUS
    return WORKER_LIVE if with_locator == 1 else WORKER_LOCATOR_MISSING


def resolve_herdr_step_outcome(args: argparse.Namespace) -> WorkflowStepOutcome:
    """Resolve the herdr-native ``workflow step`` outcome for the current lane (Redmine #13489).

    Resolves the sender identity from launch env + the repo anchor (fail-closed on an
    unattested identity), classifies the workflow lane role, verifies the lane's Redmine issue
    anchor (worker / gateway), and — for a sublane gateway lane — reads the live inventory for
    its same-lane worker cardinality, then delegates to the pure resolver. Never mutates a lane
    or delivers anything (increment 1 is resolution-only).
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
        resolve_sender_identity,
    )

    repo_root = repo_root_from_args(args)
    anchor_ws = _anchor_workspace_id(repo_root)
    sender_res = resolve_sender_identity(os.environ, anchor_workspace_id=anchor_ws)
    if not sender_res.ok or sender_res.identity is None:
        # An unattested herdr identity is not a workflow-step origin. Name the herdr-native
        # cause and the one sanctioned lane-dispatch route (mirrors herdr_send_entry).
        return WorkflowStepOutcome(
            state=STATE_LANE_UNRESOLVED,
            next_action=(
                "resolve the herdr lane identity before stepping: this shell carries no "
                "attested launch-time lane-sender identity (MOZYO_WORKSPACE_ID / "
                "MOZYO_AGENT_ROLE). Run workflow step from inside an attested herdr lane "
                "agent, or dispatch lanes through the coordinator (coordinator -> "
                "target-lane Codex gateway -> same-lane Claude worker). See "
                "vibes/docs/specs/herdr-native-identity.md."
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
            next_owner=OWNER_OPERATOR,
            primitive=PRIMITIVE_NONE,
            repo_root=str(repo_root),
            durable_anchor="none",
            detail=f"sender identity unresolved ({sender_res.reason}): {sender_res.detail}",
        )

    sender = sender_res.identity
    lane = classify_herdr_workflow_lane(
        provider=sender.role,
        lane_id=sender.lane_id,
        repo_root=str(repo_root),
    )

    # A worker / gateway lane is anchor-gated (j#74748 F3); default-lane / unknown provider
    # fails closed in the pure resolver without any store / inventory read.
    anchor_status: Optional[str] = None
    anchor_pointer = ""
    worker_liveness: Optional[str] = None
    if lane.caller_role in (ROLE_IMPLEMENTATION_WORKER, ROLE_DELEGATED_COORDINATOR):
        anchor_status, anchor_pointer = _resolve_lane_anchor(
            args, sender.workspace_id, repo_root, sender.lane_id
        )
    if lane.caller_role == ROLE_DELEGATED_COORDINATOR and anchor_status == ANCHOR_VERIFIED:
        # Only read the live inventory when the gateway lane actually reaches the worker gate.
        worker_liveness = _same_lane_worker_liveness(
            sender.workspace_id, sender.lane_id, env=os.environ
        )

    return resolve_herdr_workflow_step(
        lane,
        worker_liveness=worker_liveness,
        anchor_status=anchor_status,
        anchor_pointer=anchor_pointer,
    )


__all__ = ("resolve_herdr_step_outcome",)
