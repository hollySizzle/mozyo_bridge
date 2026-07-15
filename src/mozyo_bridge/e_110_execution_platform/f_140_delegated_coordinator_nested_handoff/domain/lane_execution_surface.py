"""Lane execution-surface taxonomy and the verified sublane projection (Redmine #13756).

A fill decision is only as honest as the thing it counts. The incident that forced
this module (#13756 j#78320): a coordinator hit a real dispatch blocker, substituted
internal parallel task agents, and then *described them as lanes*. Nothing in the
planning surface could contradict it — ``lane`` was a free-form label, so a task agent,
a bare worktree, a resident-but-undispatched process pair and a real managed sublane all
rendered as the same narrative "lane".

This module makes the execution surface a **closed, machine-verifiable** term:

- :data:`SURFACE_MANAGED_SUBLANE` is the only surface that can be a sublane, and a lane
  only *earns* it by presenting :class:`LaneProvenance` that verifies — durable lane
  metadata / lifecycle identity (workspace, lane label, lifecycle revision, durable
  anchor) plus a dispatch ACK state. An unverifiable claim degrades to
  :data:`SURFACE_UNKNOWN`; it never silently passes as a sublane.
- an internal task agent (:data:`SURFACE_INTERNAL_TASK_AGENT`) can never satisfy it. It
  is counted, separately, and **never consumes or fills sublane capacity** — so it can
  neither be used to claim the pipeline is full nor to claim it is productive.
- :func:`project_capacity` renders the four distinct counts the coordinator narrates
  from (:class:`CapacityProjection`). Narrative lane counts are meant to be rendered
  from this projection only, never from a free-form label.

Fail-closed posture (#13756 j#78320 item 5): an unrecognized / free-form surface token,
or a ``managed_sublane`` claim whose provenance does not verify, resolves to
:data:`SURFACE_UNKNOWN`. :func:`is_verified_managed_sublane` is false for it, and the
actionability policy (:mod:`...domain.lane_actionability`) refuses to honour any
non-blocking claim from a lane that is not a verified managed sublane.

Scope boundary: this module discovers nothing. Every :class:`LaneProvenance` field is
supplied by the caller from the durable record; nothing here reads a lifecycle store,
an inventory, or a pane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Execution surface vocabulary (machine-readable; literal regardless of UI language).
#
# `sublane` is a closed product term (#13756 j#78320 item 1): durable lane metadata /
# lifecycle identity plus a high-level runtime projection. The other surfaces exist so a
# coordinator can *name* what it actually ran instead of calling everything a lane.
# ---------------------------------------------------------------------------

SURFACE_MANAGED_SUBLANE = "managed_sublane"
SURFACE_INTERNAL_TASK_AGENT = "internal_task_agent"
SURFACE_COORDINATOR_LOCAL = "coordinator_local"
SURFACE_DETACHED_WORKTREE = "detached_worktree"
# The legacy / advisory input that predates the taxonomy: the caller made no surface
# claim at all. It is NOT a sublane (it is never counted in the verified projection) but
# it keeps the pre-#13756 blocking behaviour exactly, so `--lane ISSUE:STATE` is
# unchanged. It simply cannot claim a non-blocking actionability.
SURFACE_UNSPECIFIED = "unspecified"
# The fail-closed sink: a free-form / unrecognized token, or a `managed_sublane` claim
# whose provenance does not verify.
SURFACE_UNKNOWN = "unknown"

EXECUTION_SURFACES = frozenset(
    {
        SURFACE_MANAGED_SUBLANE,
        SURFACE_INTERNAL_TASK_AGENT,
        SURFACE_COORDINATOR_LOCAL,
        SURFACE_DETACHED_WORKTREE,
        SURFACE_UNSPECIFIED,
        SURFACE_UNKNOWN,
    }
)


# ---------------------------------------------------------------------------
# Dispatch ACK state. A delivery ACK is not completion (#13756): a gateway ACK says the
# request landed on the gateway, not that a worker is productively implementing.
# ---------------------------------------------------------------------------

DISPATCH_ACK_NONE = "none"
DISPATCH_ACK_GATEWAY_ACKED = "gateway_acked"
DISPATCH_ACK_WORKER_CONFIRMED = "worker_confirmed"

DISPATCH_ACK_STATES = frozenset(
    {
        DISPATCH_ACK_NONE,
        DISPATCH_ACK_GATEWAY_ACKED,
        DISPATCH_ACK_WORKER_CONFIRMED,
    }
)

# The ACK states that mean the request reached the gateway (worker confirmation implies
# the gateway leg succeeded).
_GATEWAY_DISPATCHED_ACKS = frozenset(
    {DISPATCH_ACK_GATEWAY_ACKED, DISPATCH_ACK_WORKER_CONFIRMED}
)


# ---------------------------------------------------------------------------
# Provenance.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneProvenance:
    """Machine-verifiable provenance for one fill-decision item (#13756 j#78320 item 3).

    ``execution_surface`` is the *claim*; :func:`resolve_execution_surface` decides what
    the claim actually resolves to. The identity fields are what make a
    ``managed_sublane`` claim checkable rather than narrative:

    - ``workspace`` / ``lane`` — the durable lane metadata (workspace + lane label).
    - ``issue_generation`` — the lane's issue generation, so a superseded / recovery lane
      is distinguishable from the live one.
    - ``lifecycle_revision`` — the lane lifecycle row revision the claim was read at.
    - ``durable_anchor`` — the Redmine issue / journal anchor the lane is driven from.
    - ``gateway_identity`` / ``worker_identity`` — the resolved pair.
    - ``dispatch_ack`` — one of :data:`DISPATCH_ACK_*`.

    Every field is a plain string supplied by the caller from the durable record. This
    dataclass verifies *presence and consistency*, not liveness: it cannot tell you the
    pane is alive, only that the caller can name what it claims to have run.
    """

    execution_surface: str = SURFACE_UNSPECIFIED
    workspace: str = ""
    lane: str = ""
    issue_generation: str = ""
    lifecycle_revision: str = ""
    durable_anchor: str = ""
    gateway_identity: str = ""
    worker_identity: str = ""
    dispatch_ack: str = DISPATCH_ACK_NONE

    def gateway_dispatched(self) -> bool:
        return self.dispatch_ack in _GATEWAY_DISPATCHED_ACKS

    def worker_confirmed(self) -> bool:
        return self.dispatch_ack == DISPATCH_ACK_WORKER_CONFIRMED


# The full provenance a `managed_sublane` claim must present (#13756 j#78320 item 3:
# workspace / lane / issue generation / lifecycle revision / durable anchor / gateway /
# worker identity). Every field is required, without exception — a managed sublane is
# precisely a lane whose durable lifecycle identity AND resolved gateway/worker pair are
# both nameable. Relaxing any of these (Review j#78471 finding 2) let a lane with no
# generation and no pair verify as a sublane, which is the fail-OPEN direction: it makes
# a superseded/recovery lane indistinguishable and lets an un-paired claim count toward
# the cap. `dispatch_ack` is required separately (it must be a *known* token, but `none`
# — resident-but-undispatched — is a legitimate value, so it is not an identity field).
_REQUIRED_SUBLANE_IDENTITY: tuple[str, ...] = (
    "workspace",
    "lane",
    "issue_generation",
    "lifecycle_revision",
    "durable_anchor",
    "gateway_identity",
    "worker_identity",
)


def missing_sublane_provenance(provenance: LaneProvenance) -> tuple[str, ...]:
    """The provenance fields a ``managed_sublane`` claim is missing (empty tuple = OK).

    Every field in :data:`_REQUIRED_SUBLANE_IDENTITY` is required unconditionally (#13756
    j#78320 item 3). A managed sublane always has a resolved gateway/worker pair — that
    is what makes it *managed* — so a lane that cannot name its pair is not a verified
    sublane even when it claims ``dispatch_ack=none``. The ACK level does not relax the
    identity requirement; it only records whether the dispatch has happened yet. An
    unrecognized ``dispatch_ack`` token is itself a failure (fail closed rather than
    reading an unknown ACK as ``none``).
    """
    missing = [
        field
        for field in _REQUIRED_SUBLANE_IDENTITY
        if not (getattr(provenance, field, "") or "").strip()
    ]
    if provenance.dispatch_ack not in DISPATCH_ACK_STATES:
        missing.append("dispatch_ack")
    return tuple(missing)


def resolve_execution_surface(provenance: LaneProvenance) -> str:
    """Resolve the *claimed* surface to its effective one (fail-closed, j#78320 item 5).

    - a ``managed_sublane`` claim resolves to :data:`SURFACE_MANAGED_SUBLANE` only when
      :func:`missing_sublane_provenance` is empty; otherwise it degrades to
      :data:`SURFACE_UNKNOWN`. An unverifiable sublane claim is never counted as one.
    - a recognized non-sublane surface resolves to itself (an internal task agent stays
      an internal task agent — it cannot be promoted by adding provenance).
    - anything else (free-form / unrecognized token) resolves to :data:`SURFACE_UNKNOWN`.
    """
    claimed = (provenance.execution_surface or "").strip()
    if claimed == SURFACE_MANAGED_SUBLANE:
        if missing_sublane_provenance(provenance):
            return SURFACE_UNKNOWN
        return SURFACE_MANAGED_SUBLANE
    if claimed in EXECUTION_SURFACES:
        return claimed
    return SURFACE_UNKNOWN


def is_verified_managed_sublane(provenance: LaneProvenance) -> bool:
    """True only for a ``managed_sublane`` claim whose provenance verifies.

    The single gate the actionability policy consults before honouring any non-blocking
    claim: only a verified managed sublane can have a dedicated gateway / worker to be
    ``delegated_in_flight`` on.
    """
    return resolve_execution_surface(provenance) == SURFACE_MANAGED_SUBLANE


def sublane_identity_key(provenance: LaneProvenance) -> tuple[str, str, str]:
    """The canonical identity of a managed sublane: (workspace, lane, issue_generation).

    Two verified sublanes with the same key are the *same* lane (a duplicate listing);
    two with different ``issue_generation`` are distinct lanes (a superseded lane and its
    recovery lane). :func:`project_capacity` uses this to avoid double-counting a
    duplicate and to fail closed when the same key carries conflicting facts.
    """
    return (
        provenance.workspace.strip(),
        provenance.lane.strip(),
        provenance.issue_generation.strip(),
    )


# The provenance facts that must agree when two items share a canonical identity key. If
# the same (workspace, lane, generation) is listed twice with a different lifecycle
# revision / anchor / pair / ACK — or a different blocking verdict — the coordinator has
# contradictory readings of one lane, so the projection fails that lane closed rather
# than picking one silently.
def _identity_conflict_facts(
    provenance: LaneProvenance, coordinator_blocking: bool
) -> tuple:
    return (
        provenance.lifecycle_revision.strip(),
        provenance.durable_anchor.strip(),
        provenance.gateway_identity.strip(),
        provenance.worker_identity.strip(),
        provenance.dispatch_ack,
        coordinator_blocking,
    )


# ---------------------------------------------------------------------------
# Capacity projection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapacityProjection:
    """The distinct counts a coordinator narrates lane occupancy from (j#78320 item 2).

    ``resident_managed_sublanes`` is the authority for sublane capacity: it counts
    verified managed sublanes only. The three refinements below it are subsets of that
    same population, so a reader can tell "a pair exists" from "the request reached the
    gateway" from "a worker is actually working":

    - ``gateway_dispatched_sublanes`` — dispatch ACK reached the gateway.
    - ``worker_confirmed_productive_sublanes`` — a worker confirmed *and* the lane is not
      coordinator-blocking. This is the only count that means "work is moving".
    - ``blocked_or_undispatched_sublanes`` — the remainder
      (``resident - worker_confirmed_productive``): resident sublanes that are blocked,
      undispatched, or gateway-acked but never worker-confirmed.

    ``internal_task_agents`` is counted **separately and never consumes or fills sublane
    capacity** — it can neither claim the pipeline is full nor claim it is productive.
    ``unverified_surface`` counts the fail-closed sink (:data:`SURFACE_UNKNOWN`): lanes
    whose surface claim could not be verified. ``other_surface`` counts the recognized
    non-sublane surfaces (coordinator-local / detached worktree / legacy unspecified).
    """

    resident_managed_sublanes: int = 0
    gateway_dispatched_sublanes: int = 0
    worker_confirmed_productive_sublanes: int = 0
    blocked_or_undispatched_sublanes: int = 0
    internal_task_agents: int = 0
    unverified_surface: int = 0
    other_surface: int = 0

    def as_payload(self) -> dict[str, int]:
        return {
            "resident_managed_sublanes": self.resident_managed_sublanes,
            "gateway_dispatched_sublanes": self.gateway_dispatched_sublanes,
            "worker_confirmed_productive_sublanes": (
                self.worker_confirmed_productive_sublanes
            ),
            "blocked_or_undispatched_sublanes": self.blocked_or_undispatched_sublanes,
            "internal_task_agents": self.internal_task_agents,
            "unverified_surface": self.unverified_surface,
            "other_surface": self.other_surface,
        }


@dataclass(frozen=True)
class SurfaceItem:
    """One lane reduced to what the projection needs: its provenance + blocking verdict.

    ``coordinator_blocking`` is the *resolved* verdict from the actionability policy, not
    a raw state class — a lane whose review is genuinely delegated in flight is not
    blocking, and therefore can be worker-confirmed productive.
    """

    provenance: LaneProvenance
    coordinator_blocking: bool


def project_capacity(items: Iterable[SurfaceItem]) -> CapacityProjection:
    """Render the verified capacity projection from an already-resolved lane set.

    Two invariants keep the count honest:

    - an internal task agent is tallied in ``internal_task_agents`` and **nowhere else**,
      so it can neither consume sublane capacity nor fill it;
    - a verified managed sublane is counted **once per canonical identity**
      (:func:`sublane_identity_key`). A duplicate listing of the same lane is collapsed
      (Review j#78471 finding 3: double-counting inflated the productive count). If the
      same identity is listed twice with conflicting facts (a different lifecycle
      revision / anchor / pair / ACK / blocking verdict), the coordinator holds
      contradictory readings of one lane, so that lane fails closed into
      ``unverified_surface`` instead of a guessed count.
    """
    # First pass: group verified sublanes by canonical identity, and detect conflicts.
    first_seen: dict[tuple[str, str, str], SurfaceItem] = {}
    conflicted: set[tuple[str, str, str]] = set()
    task_agents = 0
    unverified = 0
    other = 0

    for item in items:
        surface = resolve_execution_surface(item.provenance)
        if surface == SURFACE_MANAGED_SUBLANE:
            key = sublane_identity_key(item.provenance)
            prior = first_seen.get(key)
            if prior is None:
                first_seen[key] = item
            elif _identity_conflict_facts(
                item.provenance, item.coordinator_blocking
            ) != _identity_conflict_facts(
                prior.provenance, prior.coordinator_blocking
            ):
                conflicted.add(key)
            # A consistent duplicate is simply dropped (counted once via `first_seen`).
        elif surface == SURFACE_INTERNAL_TASK_AGENT:
            task_agents += 1
        elif surface == SURFACE_UNKNOWN:
            unverified += 1
        else:
            other += 1

    # A conflicted identity cannot be counted as a trustworthy sublane; it joins the
    # fail-closed unverified bucket (one entry per ambiguous identity).
    unverified += len(conflicted)

    resident = 0
    gateway_dispatched = 0
    worker_productive = 0
    for key, item in first_seen.items():
        if key in conflicted:
            continue
        resident += 1
        if item.provenance.gateway_dispatched():
            gateway_dispatched += 1
        if item.provenance.worker_confirmed() and not item.coordinator_blocking:
            worker_productive += 1

    return CapacityProjection(
        resident_managed_sublanes=resident,
        gateway_dispatched_sublanes=gateway_dispatched,
        worker_confirmed_productive_sublanes=worker_productive,
        blocked_or_undispatched_sublanes=resident - worker_productive,
        internal_task_agents=task_agents,
        unverified_surface=unverified,
        other_surface=other,
    )


__all__ = (
    "SURFACE_MANAGED_SUBLANE",
    "SURFACE_INTERNAL_TASK_AGENT",
    "SURFACE_COORDINATOR_LOCAL",
    "SURFACE_DETACHED_WORKTREE",
    "SURFACE_UNSPECIFIED",
    "SURFACE_UNKNOWN",
    "EXECUTION_SURFACES",
    "DISPATCH_ACK_NONE",
    "DISPATCH_ACK_GATEWAY_ACKED",
    "DISPATCH_ACK_WORKER_CONFIRMED",
    "DISPATCH_ACK_STATES",
    "LaneProvenance",
    "missing_sublane_provenance",
    "resolve_execution_surface",
    "is_verified_managed_sublane",
    "sublane_identity_key",
    "CapacityProjection",
    "SurfaceItem",
    "project_capacity",
)
