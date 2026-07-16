"""Glance authority / execution-surface / reconcile projection (Redmine #13758).

The central-query projection the event-driven reconciler owes so a coordinator sees the
active actor / provider / authority and the reconcile progress **without pane inspection**
(owner intent j#78309 authority visibility, j#78321 execution-surface provenance, j#78002
reconcile projection). Pure fixed-token facts joined onto the existing
:class:`...workflow_glance.WorkflowGlanceRow` — **no pane text stored**, every field a
bounded, fail-closed token consistent with the row's existing anomaly / runtime
vocabularies.

Three fact groups (each a frozen sub-record with a fail-closed ``validated()`` and an
``as_payload()``):

- :class:`ReconcileFacts` (j#78002) — the derived self-heal-ladder projection:
  ``expected_gate / expected_owner / reconcile_attempt / deadline / last_disposition /
  escalated``. Projected from the ``reconcile_state`` component row.
- :class:`AuthorityFacts` (j#78309) — the active execution role / provider + the authority
  transition: ``active_execution_role / active_provider / authority_anchor /
  authority_generation / superseded_authority_generation / transition_reason /
  concurrent_actor_count / worktree_mutation_attribution``.
- :class:`ExecutionSurfaceFacts` (j#78321) — the execution-surface provenance:
  ``execution_surface / managed_lane_identity_verified / lane_lifecycle_revision /
  gateway_dispatch_state / worker_dispatch_state / productive_capacity_eligible``.

The producer (glance snapshot source) fills these from the reconcile-state store, the
role-authority / provider machinery, and the lane lifecycle + sublane views; a missing /
unreadable source degrades every field to its fail-closed ``unknown`` / blank token (never a
fabricated ``active`` / ``verified``).
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Execution-surface provenance vocabulary (j#78321). Only ``managed_sublane`` with a
# verified lane identity may be reported as a sublane; an internal task agent is a distinct
# surface; ``unknown`` is the fail-closed catch-all (never narrated as an active lane).
# ---------------------------------------------------------------------------
EXECUTION_SURFACE_MANAGED_SUBLANE = "managed_sublane"
EXECUTION_SURFACE_COORDINATOR_LOCAL = "coordinator_local"
EXECUTION_SURFACE_INTERNAL_TASK_AGENT = "internal_task_agent"
EXECUTION_SURFACE_UNKNOWN = "unknown"

EXECUTION_SURFACES = frozenset(
    {
        EXECUTION_SURFACE_MANAGED_SUBLANE,
        EXECUTION_SURFACE_COORDINATOR_LOCAL,
        EXECUTION_SURFACE_INTERNAL_TASK_AGENT,
        EXECUTION_SURFACE_UNKNOWN,
    }
)

# Gateway / worker dispatch-state vocabulary (derived from the sublane view + lifecycle).
DISPATCH_STATE_DISPATCHED = "dispatched"
DISPATCH_STATE_NOT_DISPATCHED = "not_dispatched"
DISPATCH_STATE_DETACHED = "detached"
DISPATCH_STATE_UNKNOWN = "unknown"

DISPATCH_STATES = frozenset(
    {
        DISPATCH_STATE_DISPATCHED,
        DISPATCH_STATE_NOT_DISPATCHED,
        DISPATCH_STATE_DETACHED,
        DISPATCH_STATE_UNKNOWN,
    }
)

#: Fail-closed catch-all for a bounded string token that is missing / out-of-vocabulary.
UNKNOWN_TOKEN = "unknown"


def _norm(value: object) -> str:
    return str(value or "").strip()


def _enum(value: object, allowed: frozenset, fallback: str) -> str:
    """Fail-closed enum: return ``value`` iff it is in ``allowed``, else ``fallback``."""
    token = _norm(value)
    return token if token in allowed else fallback


def _int(value: object) -> int:
    """A non-negative int; a bool / float / non-numeric folds to 0 (fail-closed count)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 0 else 0


@dataclass(frozen=True)
class ReconcileFacts:
    """The derived reconcile-ladder projection (j#78002). All fail-closed tokens."""

    expected_gate: str = ""
    expected_owner: str = ""
    reconcile_attempt: int = 0
    deadline: str = ""
    last_disposition: str = ""
    escalated: bool = False

    def validated(self) -> "ReconcileFacts":
        return ReconcileFacts(
            expected_gate=_norm(self.expected_gate),
            expected_owner=_norm(self.expected_owner),
            reconcile_attempt=_int(self.reconcile_attempt),
            deadline=_norm(self.deadline),
            last_disposition=_norm(self.last_disposition),
            escalated=bool(self.escalated),
        )

    def as_payload(self) -> dict[str, object]:
        v = self.validated()
        return {
            "expected_gate": v.expected_gate,
            "expected_owner": v.expected_owner,
            "reconcile_attempt": v.reconcile_attempt,
            "deadline": v.deadline,
            "last_disposition": v.last_disposition,
            "escalated": v.escalated,
        }


@dataclass(frozen=True)
class AuthorityFacts:
    """The active execution role / provider + authority transition projection (j#78309)."""

    active_execution_role: str = UNKNOWN_TOKEN
    active_provider: str = UNKNOWN_TOKEN
    authority_anchor: str = ""
    authority_generation: str = ""
    superseded_authority_generation: str = ""
    transition_reason: str = ""
    concurrent_actor_count: int = 0
    worktree_mutation_attribution: str = ""

    def validated(self) -> "AuthorityFacts":
        return AuthorityFacts(
            active_execution_role=_norm(self.active_execution_role) or UNKNOWN_TOKEN,
            active_provider=_norm(self.active_provider) or UNKNOWN_TOKEN,
            authority_anchor=_norm(self.authority_anchor),
            authority_generation=_norm(self.authority_generation),
            superseded_authority_generation=_norm(self.superseded_authority_generation),
            transition_reason=_norm(self.transition_reason),
            concurrent_actor_count=_int(self.concurrent_actor_count),
            worktree_mutation_attribution=_norm(self.worktree_mutation_attribution),
        )

    def as_payload(self) -> dict[str, object]:
        v = self.validated()
        return {
            "active_execution_role": v.active_execution_role,
            "active_provider": v.active_provider,
            "authority_anchor": v.authority_anchor,
            "authority_generation": v.authority_generation,
            "superseded_authority_generation": v.superseded_authority_generation,
            "transition_reason": v.transition_reason,
            "concurrent_actor_count": v.concurrent_actor_count,
            "worktree_mutation_attribution": v.worktree_mutation_attribution,
        }


@dataclass(frozen=True)
class ExecutionSurfaceFacts:
    """The execution-surface provenance projection (j#78321). All fail-closed tokens."""

    execution_surface: str = EXECUTION_SURFACE_UNKNOWN
    managed_lane_identity_verified: bool = False
    lane_lifecycle_revision: str = ""
    gateway_dispatch_state: str = DISPATCH_STATE_UNKNOWN
    worker_dispatch_state: str = DISPATCH_STATE_UNKNOWN
    productive_capacity_eligible: bool = False

    def validated(self) -> "ExecutionSurfaceFacts":
        surface = _enum(
            self.execution_surface, EXECUTION_SURFACES, EXECUTION_SURFACE_UNKNOWN
        )
        verified = bool(self.managed_lane_identity_verified)
        # Only a managed_sublane may carry a verified identity (j#78321: an internal task
        # agent never satisfies a sublane request); a verified flag on any other surface is a
        # provenance contradiction -> fail closed to unverified.
        if surface != EXECUTION_SURFACE_MANAGED_SUBLANE:
            verified = False
        # Capacity eligibility is fail-closed to a verified managed sublane (Redmine #13758
        # review F5): an internal task agent / coordinator-local / unknown-provenance surface,
        # or a managed sublane whose lane identity is not verified, NEVER satisfies a capacity
        # slot, so an incoming ``True`` on any of those is a provenance contradiction and is
        # dropped. Only ``managed_sublane`` with a verified identity may carry the incoming flag.
        eligible = bool(self.productive_capacity_eligible)
        if not (surface == EXECUTION_SURFACE_MANAGED_SUBLANE and verified):
            eligible = False
        return ExecutionSurfaceFacts(
            execution_surface=surface,
            managed_lane_identity_verified=verified,
            lane_lifecycle_revision=_norm(self.lane_lifecycle_revision),
            gateway_dispatch_state=_enum(
                self.gateway_dispatch_state, DISPATCH_STATES, DISPATCH_STATE_UNKNOWN
            ),
            worker_dispatch_state=_enum(
                self.worker_dispatch_state, DISPATCH_STATES, DISPATCH_STATE_UNKNOWN
            ),
            productive_capacity_eligible=eligible,
        )

    def as_payload(self) -> dict[str, object]:
        v = self.validated()
        return {
            "execution_surface": v.execution_surface,
            "managed_lane_identity_verified": v.managed_lane_identity_verified,
            "lane_lifecycle_revision": v.lane_lifecycle_revision,
            "gateway_dispatch_state": v.gateway_dispatch_state,
            "worker_dispatch_state": v.worker_dispatch_state,
            "productive_capacity_eligible": v.productive_capacity_eligible,
        }


#: Binding kinds (mirrors lane_lifecycle #13810; literal so this domain stays decoupled).
_BINDING_PROJECT_GATEWAY = "project_gateway"
_ROLE_PROJECT_GATEWAY = "project_gateway"
_ROLE_IMPLEMENTATION_WORKER = "implementation_worker"


def facts_from_lifecycle_record(record) -> "tuple[AuthorityFacts, ExecutionSurfaceFacts]":
    """Project a ``lane_lifecycle`` record onto ``(AuthorityFacts, ExecutionSurfaceFacts)``. (pure)

    ONLY the DURABLE provenance the lifecycle record actually carries (Redmine #13758 review
    R3-F3): the authority anchor (``decision_issue_id:decision_journal``) and generation
    (``lane_generation``), the execution surface (an enumerated active lifecycle-managed lane
    is ``managed_sublane``), the lane identity verification (a complete binding — non-empty
    worktree + declared slots), and the lifecycle revision. ``record`` is a duck-typed
    ``LaneLifecycleRecord``; ``None`` -> fail-closed empty.

    The LIVE-ACTOR facts are NEVER promoted from ownership metadata (review R3-F3): ``binding_kind``
    is which unit the lane is *bound to* (issue / project_gateway), NOT who is *currently
    executing*. So ``active_execution_role`` / ``active_provider`` (who is running now),
    ``concurrent_actor_count`` (the live actor count), ``productive_capacity_eligible`` (which
    needs the live dispatch/liveness state), the dispatch states, and the authority-transition
    history all stay at their fail-closed ``unknown`` / ``0`` / ``false`` / blank tokens — a
    gateway reviewing, an absent worker, multiple actors, or a stale declaration must never
    read as ``implementation_worker`` / ``claude`` / one-actor / capacity=true. They connect at
    the installed-artifact live surface (#13492).
    """
    if record is None:
        return AuthorityFacts(), ExecutionSurfaceFacts()
    decision_issue = str(getattr(record, "decision_issue_id", "") or "").strip()
    decision_journal = str(getattr(record, "decision_journal", "") or "").strip()
    anchor = (
        f"{decision_issue}:{decision_journal}"
        if decision_issue and decision_journal
        else ""
    )
    worktree = str(getattr(record, "worktree_identity", "") or "").strip()
    slots = str(getattr(record, "declared_slots", "") or "").strip()
    verified = bool(worktree) and bool(slots)
    authority = AuthorityFacts(
        # active_execution_role / active_provider / concurrent_actor_count are LIVE facts —
        # left fail-closed unknown/0 (never derived from binding_kind ownership). #13492.
        authority_anchor=anchor,
        authority_generation=str(getattr(record, "lane_generation", "") or "").strip(),
    ).validated()
    execution = ExecutionSurfaceFacts(
        execution_surface=EXECUTION_SURFACE_MANAGED_SUBLANE,
        managed_lane_identity_verified=verified,
        lane_lifecycle_revision=str(getattr(record, "revision", "") or "").strip(),
        # productive_capacity_eligible needs the live dispatch / liveness state (#13492); a
        # complete binding alone does not prove available capacity -> fail-closed false.
    ).validated()
    return authority, execution


def reconcile_facts_from_record(record) -> ReconcileFacts:
    """Project a ``reconcile_state`` component row onto :class:`ReconcileFacts`. (pure)

    ``record`` is a ``mozyo_bridge.core.state.reconcile_state.ReconcileStateRecord`` (duck
    typed to keep this domain free of a core-state import). ``None`` -> the fail-closed empty
    facts (no reconcile row = no projection, never a fabricated attempt count).
    """
    if record is None:
        return ReconcileFacts()
    return ReconcileFacts(
        expected_gate=getattr(record, "expected_gate", ""),
        expected_owner=getattr(record, "expected_next_owner", ""),
        reconcile_attempt=getattr(record, "reconcile_failure_count", 0),
        deadline=getattr(record, "deadline", ""),
        last_disposition=getattr(record, "last_disposition", ""),
        escalated=getattr(record, "escalated", False),
    ).validated()


__all__ = (
    "EXECUTION_SURFACE_MANAGED_SUBLANE",
    "EXECUTION_SURFACE_COORDINATOR_LOCAL",
    "EXECUTION_SURFACE_INTERNAL_TASK_AGENT",
    "EXECUTION_SURFACE_UNKNOWN",
    "EXECUTION_SURFACES",
    "DISPATCH_STATE_DISPATCHED",
    "DISPATCH_STATE_NOT_DISPATCHED",
    "DISPATCH_STATE_DETACHED",
    "DISPATCH_STATE_UNKNOWN",
    "DISPATCH_STATES",
    "UNKNOWN_TOKEN",
    "ReconcileFacts",
    "AuthorityFacts",
    "ExecutionSurfaceFacts",
    "reconcile_facts_from_record",
    "facts_from_lifecycle_record",
)
