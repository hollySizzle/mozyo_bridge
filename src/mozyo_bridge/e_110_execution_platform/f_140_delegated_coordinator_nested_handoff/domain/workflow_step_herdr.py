"""herdr-native lane classification + step resolution for `workflow step` (Redmine #13489).

`mozyo-bridge workflow step` resolves the current lane role from the discovered self
candidate under the tmux backend (:func:`...workflow_step.classify_workflow_lane`, a tmux
``%pane`` matched against the tmux inventory). A **pure herdr session** has no ``TMUX_PANE``
and the pane is not in the tmux inventory, so that path folds to ``self_lane_unresolved`` and
the #13446 preflight replaced the dead end with a fail-closed ``herdr_self_lane_unresolved``.

This module is the herdr-native counterpart that #13489 puts in that preflight's place: it
classifies the current lane role from the **herdr-native identity** (launch-time sender env
``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID``) and maps that role onto the SAME replayable
:class:`~...workflow_step.WorkflowStepOutcome` contract. It grows **no divergent identity
model** (design principle 4): the lane-role vocabulary is exactly the tmux state machine's
roles, derived here from the documented herdr shared-project-workspace model (spec
``vibes/docs/specs/herdr-native-identity.md`` §1, and the ``sublane list`` fold in
:mod:`...application.sublane_herdr_projection`).

Role authority (mid-review j#74748 F1 / j#74749 F1 / consolidation j#74750): the herdr mzb1
``role`` field is a runtime **provider** token (``claude`` / ``codex``), NOT a workflow
authority, and a default-lane pair (the coordinator's Codex + its Main-unit assistant Claude)
carries no step-time durable role authority to tell a ``project_gateway`` from a
``grandparent_coordinator`` — nor is a default-lane Claude an implementation worker. So this
module classifies only the **non-default lane slots** it can attribute a lane-local class to
(``codex`` -> the sublane gateway ``delegated_coordinator``; ``claude`` -> the
``implementation_worker``) and **fails closed on the default lane**
(``ambiguous_default_coordinator_role``) rather than promote provider/placement to a role
authority. The earlier registry-``project_name`` project-scope heuristic (display metadata
defaulted to the directory name) is removed — it was never a role authority.

Anchor gate (mid-review j#74748 F3): a worker / gateway lane only reaches ``ready`` / ``no_op``
with a **verified** Redmine issue anchor (resolved out of band from the lane metadata record
and passed in as ``anchor_status`` / ``anchor_pointer``); a missing / ambiguous / retired
anchor fails closed. Same-lane worker liveness is a **cardinality** (mid-review j#74749 F2 /
consolidation j#74750): a duplicate ``(workspace, lane, claude)`` slot is ambiguity, not a
dispatch target.

**Scope (increment 1, Redmine #13489 j#74685 design_boundary).** This is *resolution-only*:
it names the role-appropriate next action / owner / herdr surface and performs **no** sublane
lifecycle mutation and **no** delivery (``primitive=none`` throughout). The policy-permitted
one-step auto-execution and the fail-closed destructive drain/retire boundary are increment
2, gated behind the mandatory task-level design mid-review. Everything here is pure: value
objects + total functions over plain strings, no subprocess / env / registry / inventory read
(the application adapter supplies the sender identity, the worker liveness, and the anchor).
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_WORKER,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    EXECUTION_NO_OP,
    OWNER_CHILD,
    OWNER_GRANDCHILD,
    OWNER_OPERATOR,
    PRIMITIVE_NONE,
    STATE_CHILD_WORKER_DISPATCH,
    STATE_GRANDCHILD_REDMINE_WORK,
    STATE_LANE_UNRESOLVED,
    WorkflowLane,
    WorkflowStepOutcome,
)

# The herdr provider tokens (mzb1 "role" field = runtime provider, not workflow role).
# Kept literal here to avoid importing the terminal-runtime domain into this execution-
# platform module; they mirror `herdr_target_resolution.PROVIDER_CLAUDE / PROVIDER_CODEX`.
HERDR_PROVIDER_CLAUDE = "claude"
HERDR_PROVIDER_CODEX = "codex"

# The normalized stand-in for an unset lane (mirrors `herdr_identity.DEFAULT_LANE`); a
# herdr coordinator pair sits in this lane, every sublane slot in a non-default lane.
HERDR_DEFAULT_LANE = "default"

# ---------------------------------------------------------------------------
# Same-lane worker liveness cardinality (mid-review j#74749 F2 / j#74750): 0 / 1 / 2+ and the
# usable-locator distinction are preserved so a duplicate identity is ambiguity, not a target.
# ---------------------------------------------------------------------------
WORKER_LIVE = "live"  # exactly one same-lane worker slot with a usable locator
WORKER_ABSENT = "absent"  # no same-lane worker slot
WORKER_AMBIGUOUS = "ambiguous"  # 2+ same-lane worker slots (duplicate identity)
WORKER_LOCATOR_MISSING = "locator_missing"  # one slot but no usable locator
WORKER_UNAVAILABLE = "unavailable"  # the live inventory could not be read

# ---------------------------------------------------------------------------
# Redmine issue-anchor verification status (mid-review j#74748 F3): a worker / gateway lane is
# only ``ready`` with a verified anchor; missing / ambiguous / retired fails closed.
# ---------------------------------------------------------------------------
ANCHOR_VERIFIED = "verified"
ANCHOR_MISSING = "missing"
ANCHOR_AMBIGUOUS = "ambiguous"
ANCHOR_RETIRED = "retired"
#: The candidate issue could not be verified against the source-of-truth Redmine gate (the
#: live journal read was unconfigured / failed / found no structured gate marker, R1/F3a).
ANCHOR_UNVERIFIED = "unverified"
#: A caller-supplied advisory store asserts a *different* (issue, journal, gate) for this same
#: lane than the source-of-truth Redmine verification produced (drift / forgery, F3c).
ANCHOR_STORE_MISMATCH = "store_mismatch"


# ---------------------------------------------------------------------------
# herdr-native reason vocabulary (machine-readable; kept literal regardless of UI language).
# ---------------------------------------------------------------------------

#: The worker lane's own step: read its verified Redmine anchor and implement (no dispatch).
REASON_HERDR_WORKER_STEP_READY = "herdr_worker_step_ready"
#: The gateway lane's step: verified anchor + a single live same-lane worker -> dispatch / monitor.
REASON_HERDR_WORKER_DISPATCH_READY = "herdr_worker_dispatch_ready"
#: The gateway lane's step is blocked: no live same-lane worker slot to dispatch to.
REASON_HERDR_WORKER_SLOT_MISSING = "herdr_worker_slot_missing"
#: The gateway lane's step is blocked: 2+ same-lane worker slots (ambiguous identity, j#74749 F2).
REASON_HERDR_WORKER_AMBIGUOUS = "herdr_worker_ambiguous"
#: The gateway lane's step is blocked: the single same-lane worker slot has no usable locator.
REASON_HERDR_WORKER_LOCATOR_MISSING = "herdr_worker_locator_missing"
#: A worker / gateway lane has no verified Redmine issue+journal anchor (missing / retired, F3).
REASON_HERDR_ANCHOR_UNRESOLVED = "herdr_anchor_unresolved"
#: A worker / gateway lane resolves to more than one distinct Redmine issue anchor (drift).
REASON_HERDR_ANCHOR_AMBIGUOUS = "herdr_anchor_ambiguous"
#: The candidate issue could not be verified against the source-of-truth Redmine gate (F3a).
REASON_HERDR_ANCHOR_UNVERIFIED = "herdr_anchor_unverified"
#: A caller-supplied advisory store contradicts the source-of-truth anchor for this lane (F3c).
REASON_HERDR_ANCHOR_STORE_MISMATCH = "herdr_anchor_store_mismatch"
#: A default-lane Codex/Claude pair carries no step-time durable role authority (j#74748 F1).
REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED = "ambiguous_default_coordinator_role"
#: The current herdr lane's provider is not a known runtime provider (claude / codex).
REASON_HERDR_LANE_ROLE_UNRESOLVED = "herdr_lane_role_unresolved"
#: The herdr-native sender identity itself could not be resolved (adapter maps to this).
REASON_HERDR_SENDER_IDENTITY_UNRESOLVED = "herdr_sender_identity_unresolved"

HERDR_STEP_REASONS = frozenset(
    {
        REASON_HERDR_WORKER_STEP_READY,
        REASON_HERDR_WORKER_DISPATCH_READY,
        REASON_HERDR_WORKER_SLOT_MISSING,
        REASON_HERDR_WORKER_AMBIGUOUS,
        REASON_HERDR_WORKER_LOCATOR_MISSING,
        REASON_HERDR_ANCHOR_UNRESOLVED,
        REASON_HERDR_ANCHOR_AMBIGUOUS,
        REASON_HERDR_ANCHOR_UNVERIFIED,
        REASON_HERDR_ANCHOR_STORE_MISMATCH,
        REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED,
        REASON_HERDR_LANE_ROLE_UNRESOLVED,
        REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
    }
)

# Fixed detail prefixes so :func:`resolve_herdr_workflow_step` can tell a default-lane block
# (needs durable role authority) apart from an unknown-provider block, without a second field.
_DEFAULT_COORDINATOR_PREFIX = "herdr default-lane coordinator pair:"
_UNKNOWN_PROVIDER_PREFIX = "herdr unknown provider:"


def classify_herdr_workflow_lane(
    *,
    provider: str,
    lane_id: str,
    repo_root: str,
    locator: str = "",
) -> WorkflowLane:
    """Classify the current herdr lane's workflow role from its herdr-native identity (pure).

    Only the **non-default lane slots** are attributed a lane-local class (mid-review j#74748
    F1 / j#74750): a ``claude`` slot is the ``implementation_worker`` (grandchild), a ``codex``
    slot is the sublane gateway ``delegated_coordinator`` (child). The **default lane** — the
    coordinator's Codex + its Main-unit assistant Claude — carries no step-time durable role
    authority to tell a project gateway from a department-root coordinator (nor is its Claude
    an implementation worker), so it fails closed rather than promote provider / placement to
    a role authority. An unknown provider fails closed too.

    ``locator`` is the herdr transient locator (``agent list`` ``pane_id``) carried into the
    outcome's ``self_pane`` for diagnostics only — never a route authority (spec §2).
    """
    provider = (provider or "").strip()
    lane = (lane_id or "").strip() or HERDR_DEFAULT_LANE
    root = (repo_root or "").strip()

    def _blocked(detail: str) -> WorkflowLane:
        return WorkflowLane(
            self_pane=locator,
            caller_role=None,
            repo_root=root,
            project_scope="",
            provider_safe=False,
            detail=detail,
        )

    if provider not in (HERDR_PROVIDER_CLAUDE, HERDR_PROVIDER_CODEX):
        return _blocked(
            f"{_UNKNOWN_PROVIDER_PREFIX} sender provider {provider!r} is not a known runtime "
            f"provider ({HERDR_PROVIDER_CLAUDE!r} / {HERDR_PROVIDER_CODEX!r}); cannot classify "
            "the workflow lane role — fail closed rather than route on a guessed role"
        )

    if lane == HERDR_DEFAULT_LANE:
        return _blocked(
            f"{_DEFAULT_COORDINATOR_PREFIX} the default lane is the coordinator pair "
            f"(provider={provider!r}); workflow step carries no durable role authority to tell "
            "a project gateway from a department-root coordinator (and a default-lane Claude is "
            "the coordinator's assistant, not an implementation worker). Fail closed rather "
            "than promote provider/placement to a role authority"
        )

    if provider == HERDR_PROVIDER_CLAUDE:
        return WorkflowLane(
            self_pane=locator,
            caller_role=ROLE_IMPLEMENTATION_WORKER,
            repo_root=root,
            project_scope="",
            provider_safe=True,
            detail=f"herdr implementation worker lane (grandchild), lane={lane!r}",
        )
    return WorkflowLane(
        self_pane=locator,
        caller_role=ROLE_DELEGATED_COORDINATOR,
        repo_root=root,
        project_scope="",
        provider_safe=True,
        detail=f"herdr sublane gateway lane (child), lane={lane!r}",
    )


def _blocked_lane_outcome(lane: WorkflowLane) -> WorkflowStepOutcome:
    """Fail-closed outcome for an unclassifiable lane (default-lane pair / unknown provider)."""
    if lane.detail.startswith(_DEFAULT_COORDINATOR_PREFIX):
        reason = REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED
        next_action = (
            "default-lane coordinator pair: workflow step has no durable role authority to "
            "resolve this lane's coordinator role herdr-natively yet. Resolve the coordinator "
            "action from the durable Redmine record + `workflow admission` (herdr-native "
            "coordinator orchestration inside workflow step is increment 2)."
        )
    else:
        reason = REASON_HERDR_LANE_ROLE_UNRESOLVED
        next_action = (
            "resolve the current herdr lane identity before stepping: the launch-time sender "
            "env (MOZYO_AGENT_ROLE) must name a known provider (claude / codex). Run from "
            "inside an attested herdr lane agent."
        )
    return WorkflowStepOutcome(
        state=STATE_LANE_UNRESOLVED,
        next_action=next_action,
        execution=EXECUTION_BLOCKED,
        reason=reason,
        next_owner=OWNER_OPERATOR,
        primitive=PRIMITIVE_NONE,
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
        durable_anchor="none",
        detail=lane.detail,
    )


def _anchor_blocked(
    lane: WorkflowLane, state: str, anchor_status: Optional[str], next_owner: str
) -> WorkflowStepOutcome:
    """Fail-closed outcome when the lane's Redmine issue+journal anchor is not verified (F3)."""
    reason = {
        ANCHOR_AMBIGUOUS: REASON_HERDR_ANCHOR_AMBIGUOUS,
        ANCHOR_UNVERIFIED: REASON_HERDR_ANCHOR_UNVERIFIED,
        ANCHOR_STORE_MISMATCH: REASON_HERDR_ANCHOR_STORE_MISMATCH,
    }.get(anchor_status or "", REASON_HERDR_ANCHOR_UNRESOLVED)
    detail = {
        ANCHOR_AMBIGUOUS: (
            "the lane has more than one candidate record (duplicate / stale-retired "
            "coexistence); workflow step will not guess the anchor"
        ),
        ANCHOR_UNVERIFIED: (
            "the candidate issue could not be verified against the source-of-truth Redmine "
            "gate (live journal read unconfigured / failed / no structured gate marker)"
        ),
        ANCHOR_STORE_MISMATCH: (
            "a caller-supplied advisory store asserts a different anchor for this lane than "
            "the source-of-truth Redmine verification; fail closed rather than trust the store"
        ),
        ANCHOR_RETIRED: "the lane's only candidate record is retired (tombstone / stale)",
        ANCHOR_MISSING: "no candidate record joins this lane to a Redmine issue",
    }.get(anchor_status or "", "the lane's Redmine issue+journal anchor could not be verified")
    return WorkflowStepOutcome(
        state=state,
        next_action=(
            "resolve and verify this lane's Redmine issue anchor before stepping: "
            + detail
            + ". workflow step does not implement / dispatch against an unverified anchor."
        ),
        execution=EXECUTION_BLOCKED,
        reason=reason,
        next_owner=next_owner,
        primitive=PRIMITIVE_NONE,
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
        durable_anchor="none",
        detail=lane.detail + "; " + detail,
    )


def resolve_herdr_workflow_step(
    lane: WorkflowLane,
    *,
    worker_liveness: Optional[str] = None,
    anchor_status: Optional[str] = None,
    anchor_pointer: str = "",
    lane_label: str = "",
) -> WorkflowStepOutcome:
    """Map a classified herdr lane onto a resolution-only :class:`WorkflowStepOutcome` (pure).

    Increment 1 (Redmine #13489 j#74685 design_boundary): resolution-only — it names the
    role-appropriate next action / owner / herdr surface and performs no sublane mutation and
    no delivery (``primitive=none`` throughout).

    - unclassifiable lane (default-lane pair / unknown provider) -> ``blocked``
      (:data:`REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED` / :data:`REASON_HERDR_LANE_ROLE_UNRESOLVED`).
    - ``implementation_worker`` -> with a verified anchor, ``no_op`` (``herdr_worker_step_ready``,
      ``durable_anchor`` set): read the verified Redmine anchor and implement. Without a verified
      anchor -> fail closed (:func:`_anchor_blocked`).
    - ``delegated_coordinator`` (sublane gateway) -> requires a verified anchor AND a single live
      same-lane worker: ``no_op`` (``herdr_worker_dispatch_ready``, ``durable_anchor`` set) naming
      ``sublane dispatch-worker``. A missing anchor / a missing / duplicate / unaddressable worker
      each fails closed with its fixed reason (:data:`WORKER_ABSENT` ->
      ``herdr_worker_slot_missing``, :data:`WORKER_AMBIGUOUS` -> ``herdr_worker_ambiguous``,
      :data:`WORKER_LOCATOR_MISSING` -> ``herdr_worker_locator_missing``).
    """
    if lane.caller_role is None or not lane.provider_safe:
        return _blocked_lane_outcome(lane)

    base = dict(
        caller_role=lane.caller_role or "",
        self_pane=lane.self_pane,
        repo_root=lane.repo_root,
        project_scope=lane.project_scope,
    )

    if lane.caller_role == ROLE_IMPLEMENTATION_WORKER:
        if anchor_status != ANCHOR_VERIFIED:
            return _anchor_blocked(
                lane, STATE_GRANDCHILD_REDMINE_WORK, anchor_status, OWNER_CHILD
            )
        return WorkflowStepOutcome(
            state=STATE_GRANDCHILD_REDMINE_WORK,
            next_action=(
                f"read this worker lane's verified Redmine anchor ({anchor_pointer}) and its "
                "required docs, then implement / verify and record implementation_done / "
                "review_request on the durable record. This is the worker's own action — "
                "workflow step performs no dispatch here."
            ),
            execution=EXECUTION_NO_OP,
            reason=REASON_HERDR_WORKER_STEP_READY,
            next_owner=OWNER_GRANDCHILD,
            primitive=PRIMITIVE_NONE,
            durable_anchor=anchor_pointer or "none",
            detail=lane.detail,
            **base,
        )

    # delegated_coordinator (sublane gateway): anchor gate first, then worker cardinality.
    if anchor_status != ANCHOR_VERIFIED:
        return _anchor_blocked(lane, STATE_CHILD_WORKER_DISPATCH, anchor_status, OWNER_CHILD)

    if worker_liveness == WORKER_LIVE:
        # Increment 2 (coordinator disposition j#74855) authorizes a one-step worker dispatch,
        # but the dispatch-vs-monitor decision must come from the verified Redmine gate + the
        # worker runtime state + a duplicate fence — NOT from mere worker liveness (independent
        # review j#74912 / audit j#74909). Pending the coordinator's confirmation of the exact
        # dispatch-authorizing gate/state, runtime authority, and idempotency source (design
        # consultation), this leg is **monitor / no_op** — it never auto-dispatches. Explicit
        # dispatch stays the existing `sublane dispatch-worker --execute` primitive.
        return WorkflowStepOutcome(
            state=STATE_CHILD_WORKER_DISPATCH,
            next_action=(
                "this sublane gateway has a verified Redmine anchor "
                f"({anchor_pointer}) and a single live same-lane worker. Monitor the durable "
                "record; dispatch (or re-dispatch) the worker with `sublane dispatch-worker "
                "--execute` only when the Redmine gate + worker runtime state warrant it. The "
                "one-step auto-dispatch gate is pending coordinator confirmation."
            ),
            execution=EXECUTION_NO_OP,
            reason=REASON_HERDR_WORKER_DISPATCH_READY,
            next_owner=OWNER_CHILD,
            primitive=PRIMITIVE_NONE,
            durable_anchor=anchor_pointer or "none",
            detail=lane.detail,
            **base,
        )

    if worker_liveness == WORKER_AMBIGUOUS:
        reason = REASON_HERDR_WORKER_AMBIGUOUS
        extra = "2+ live same-lane worker slots (duplicate identity); workflow step will not guess"
    elif worker_liveness == WORKER_LOCATOR_MISSING:
        reason = REASON_HERDR_WORKER_LOCATOR_MISSING
        extra = "the single same-lane worker slot has no usable locator to address"
    else:  # WORKER_ABSENT / WORKER_UNAVAILABLE / None
        reason = REASON_HERDR_WORKER_SLOT_MISSING
        extra = (
            "the same-lane worker inventory is unavailable"
            if worker_liveness in (WORKER_UNAVAILABLE, None)
            else "no live same-lane worker slot to dispatch to"
        )
    return WorkflowStepOutcome(
        state=STATE_CHILD_WORKER_DISPATCH,
        next_action=(
            "this sublane gateway cannot resolve a single same-lane worker to dispatch to: "
            + extra
            + ". Resolve the worker slot (launch / disambiguate), then step again. workflow "
            "step never launches or guesses the worker itself."
        ),
        execution=EXECUTION_BLOCKED,
        reason=reason,
        next_owner=OWNER_CHILD,
        primitive=PRIMITIVE_NONE,
        durable_anchor=anchor_pointer or "none",
        detail=lane.detail + "; " + extra,
        **base,
    )


__all__ = (
    "HERDR_PROVIDER_CLAUDE",
    "HERDR_PROVIDER_CODEX",
    "HERDR_DEFAULT_LANE",
    "WORKER_LIVE",
    "WORKER_ABSENT",
    "WORKER_AMBIGUOUS",
    "WORKER_LOCATOR_MISSING",
    "WORKER_UNAVAILABLE",
    "ANCHOR_VERIFIED",
    "ANCHOR_MISSING",
    "ANCHOR_AMBIGUOUS",
    "ANCHOR_RETIRED",
    "ANCHOR_UNVERIFIED",
    "ANCHOR_STORE_MISMATCH",
    "REASON_HERDR_WORKER_STEP_READY",
    "REASON_HERDR_WORKER_DISPATCH_READY",
    "REASON_HERDR_WORKER_SLOT_MISSING",
    "REASON_HERDR_WORKER_AMBIGUOUS",
    "REASON_HERDR_WORKER_LOCATOR_MISSING",
    "REASON_HERDR_ANCHOR_UNRESOLVED",
    "REASON_HERDR_ANCHOR_AMBIGUOUS",
    "REASON_HERDR_ANCHOR_UNVERIFIED",
    "REASON_HERDR_ANCHOR_STORE_MISMATCH",
    "REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED",
    "REASON_HERDR_LANE_ROLE_UNRESOLVED",
    "REASON_HERDR_SENDER_IDENTITY_UNRESOLVED",
    "HERDR_STEP_REASONS",
    "classify_herdr_workflow_lane",
    "resolve_herdr_workflow_step",
)
