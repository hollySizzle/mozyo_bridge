"""Route identity ledger / pane-name live re-resolution (Redmine #12553).

The #12550 planner seam
(:mod:`mozyo_bridge.domain.delegation_route_planner`) emits *logical* route
targets (``child_gateway`` / ``grandchild_gateway`` / ``same_lane_worker`` /
``parent_coordinator``) and deliberately deferred the durable ledger that turns
one of those logical tokens — or a previously-observed route — back into a
*live* pane at handoff / callback time (planner module docstring,
``Route-identity targets, not stale pane ids``). This module is that deferred
ledger / re-resolution layer.

The governing rule (issue #12553 Required behavior #2) is that a ``pane_id`` is
a **cache / snapshot only and is never the route authority**. A long-running
sublane callback that trusts a saved ``%N`` can silently mis-route once tmux
renumbers panes or a different agent takes that slot. So the *authority* is the
stable identity tuple — ``(workspace_id, lane_id, role, pane_name)`` keyed by a
``route_id`` — and every handoff / callback re-scans the live pane inventory and
re-matches against that tuple. The cached ``last_seen_pane_id`` is only ever
used to *detect* staleness, never to address a target.

Fail-closed re-resolution outcomes (Required behavior #4):

- exactly one live identity match -> :data:`RESOLVE_OK` (the live pane id is
  refreshed from the inventory; a moved pane is transparently recovered).
- zero live matches -> :data:`TARGET_UNAVAILABLE`.
- more than one live match -> :data:`TARGET_AMBIGUOUS`.
- the cached ``last_seen_pane_id`` is still live but now carries a *different*
  identity (a stale snapshot that would mis-route) -> :data:`TARGET_STALE`.
- live panes share the lane+role but none carry the required stable
  pane-name / route-label metadata -> :data:`ROUTE_LABEL_MISSING`. A managed
  pane missing its stable label must **never** silently fall back to pane-id
  authority (Required behavior #5); it fails closed instead.

Purity (mirrors the #12550 planner contract): this module never opens tmux,
never sends a handoff, never writes Redmine, never mutates cockpit membership.
It consumes a *read-only* live inventory snapshot (the
:func:`mozyo_bridge.infrastructure.tmux_client.try_pane_lines` row shape) and
returns typed results. Persisting the ledger to DB / runtime state is a
serialization concern handled through :meth:`RouteIdentity.to_record` /
:meth:`RouteIdentity.from_record` and :meth:`RouteIdentityLedger.to_records`;
wiring that into the live ``state_store`` and the live ``handoff send`` path is
the actuator follow-up, exactly as the #12550 executor was deferred.

Public-record safety (Required behavior #8): a pane id is private, session-local
topology. The durable-record projections (:meth:`RouteIdentity.public_pointer`,
:meth:`RouteResolution.public_pointer`) omit it; only the stable, public-safe
identity tokens are echoed into anything pasteable. The raw ``last_seen_pane_id``
/ ``resolved_pane_id`` stay on the runtime objects for the in-process actuator.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Optional

# Integrate with the #12550 planner's logical route-identity vocabulary rather
# than redefining it, so the ledger and the planner cannot drift. The planner's
# fail-closed error is reused for the cross-boundary guard below.
from mozyo_bridge.domain.delegation_route_planner import (
    DelegationRoutePlanError,
    TARGET_CHILD_GATEWAY,
    TARGET_GRANDCHILD_GATEWAY,
    TARGET_PARENT_COORDINATOR,
    TARGET_SAME_LANE_WORKER,
)

# ---------------------------------------------------------------------------
# Re-resolution outcome tokens. These are the only statuses a resolution can
# carry; each non-OK token is a distinct fail-closed diagnostic so a caller can
# tell "the target moved" from "the target is gone" from "the cache is stale".
# ---------------------------------------------------------------------------
#: Exactly one live pane matched the stable identity -> resolved (pane id
#: refreshed from the live inventory).
RESOLVE_OK: str = "route_resolved"
#: Zero live panes matched the stable identity (Required behavior #4).
TARGET_UNAVAILABLE: str = "target_unavailable"
#: More than one live pane matched the stable identity (Required behavior #4).
TARGET_AMBIGUOUS: str = "target_ambiguous"
#: The cached ``last_seen_pane_id`` is still live but now carries a different
#: identity: trusting the snapshot would mis-route (Required behavior #4).
TARGET_STALE: str = "route_identity_stale"
#: Live panes share the lane+role but none carry the mandatory stable
#: pane-name / route-label metadata; a managed pane without it must not fall
#: back to pane-id authority (Required behavior #5).
ROUTE_LABEL_MISSING: str = "route_label_missing"

#: Non-OK statuses, for callers that want a single "did not resolve" guard.
FAIL_CLOSED_STATUSES: frozenset[str] = frozenset(
    {TARGET_UNAVAILABLE, TARGET_AMBIGUOUS, TARGET_STALE, ROUTE_LABEL_MISSING}
)

# ---------------------------------------------------------------------------
# Agent role tokens (the values tmux carries on ``@mozyo_agent_role``). Kept as
# small constants so the planner-token -> expected-role map below is explicit.
# ---------------------------------------------------------------------------
ROLE_CLAUDE: str = "claude"
ROLE_CODEX: str = "codex"

#: Which agent role each logical planner target must re-resolve to. A logical
#: target whose ledgered identity carries a different role is a malformed
#: re-resolution request and fails closed (see :func:`resolve_for_route_target`).
ROUTE_TARGET_EXPECTED_ROLE: dict[str, str] = {
    TARGET_CHILD_GATEWAY: ROLE_CODEX,
    TARGET_GRANDCHILD_GATEWAY: ROLE_CODEX,
    TARGET_PARENT_COORDINATOR: ROLE_CODEX,
    TARGET_SAME_LANE_WORKER: ROLE_CLAUDE,
}

# ---------------------------------------------------------------------------
# Live-inventory pane-record keys. The resolver consumes the read-only row shape
# produced by ``tmux_client.try_pane_lines`` (``id`` / ``workspace_id`` /
# ``lane_id`` / ``agent_role`` ...). The stable route label rides on
# ``route_label`` (alias ``pane_name``); absence means "no stable label", which
# fails closed rather than degrading to pane-id authority.
# ---------------------------------------------------------------------------
PANE_KEY_ID: str = "id"
PANE_KEY_WORKSPACE: str = "workspace_id"
PANE_KEY_LANE: str = "lane_id"
PANE_KEY_ROLE: str = "agent_role"
PANE_KEY_ROUTE_LABEL: str = "route_label"
PANE_KEY_ROUTE_LABEL_ALIAS: str = "pane_name"

#: Normalized stand-in for an unset ``lane_id`` (matches the cockpit/pane
#: convention where an empty lane is the workspace-default lane).
DEFAULT_LANE: str = "default"


class RouteIdentityError(ValueError):
    """A route-identity record is malformed (an empty stable field).

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    delegation / role-profile domain errors. Constructing an identity that could
    only ever be matched by pane id (e.g. an empty ``pane_name`` or ``route_id``)
    raises here rather than producing a record that silently relies on the cache.
    """


def _norm(value: object) -> str:
    """Trim a raw field to a comparable token (``None`` -> ``""``)."""
    return str(value).strip() if value is not None else ""


def _norm_lane(value: object) -> str:
    """Normalize a lane id, mapping an empty lane to :data:`DEFAULT_LANE`."""
    lane = _norm(value)
    return lane or DEFAULT_LANE


@dataclass(frozen=True)
class RouteIdentity:
    """The durable, stable identity of one managed route target.

    The match authority is :attr:`identity_key` —
    ``(workspace_id, lane_id, role, pane_name)`` — keyed in the ledger by
    :attr:`route_id`. :attr:`last_seen_pane_id` is a **cache / snapshot only**:
    it is recorded for staleness detection and audit, never used to address a
    target. The stable fields are required and non-empty; an identity that could
    only be matched by pane id is rejected at construction (Required behavior
    #2 / #5).

    :attr:`observed_at` is an opaque, caller-supplied observation stamp (an ISO
    string is conventional). It is stored verbatim so the domain stays pure and
    deterministic — this module never reads the clock.
    """

    workspace_id: str
    lane_id: str
    role: str
    pane_name: str
    route_id: str
    observed_at: str = ""
    last_seen_pane_id: str = ""

    def __post_init__(self) -> None:
        # Normalize once so every comparison and record echo is consistent.
        object.__setattr__(self, "workspace_id", _norm(self.workspace_id))
        object.__setattr__(self, "lane_id", _norm_lane(self.lane_id))
        object.__setattr__(self, "role", _norm(self.role))
        object.__setattr__(self, "pane_name", _norm(self.pane_name))
        object.__setattr__(self, "route_id", _norm(self.route_id))
        object.__setattr__(self, "observed_at", _norm(self.observed_at))
        object.__setattr__(self, "last_seen_pane_id", _norm(self.last_seen_pane_id))
        missing = [
            name
            for name in ("workspace_id", "role", "pane_name", "route_id")
            if not getattr(self, name)
        ]
        if missing:
            raise RouteIdentityError(
                "route identity requires non-empty stable fields "
                f"(missing: {', '.join(missing)}); a pane id is never the route "
                "authority"
            )

    @property
    def identity_key(self) -> tuple[str, str, str, str]:
        """The stable match key: ``(workspace_id, lane_id, role, pane_name)``."""
        return (self.workspace_id, self.lane_id, self.role, self.pane_name)

    @property
    def lane_role_key(self) -> tuple[str, str, str]:
        """The ``(workspace_id, lane_id, role)`` slot this identity lives in."""
        return (self.workspace_id, self.lane_id, self.role)

    def with_observation(self, *, pane_id: str, observed_at: str) -> "RouteIdentity":
        """Return a copy with a refreshed cached pane id + observation stamp.

        Used when re-resolution finds the live pane: the stable identity is
        unchanged, only the cache (``last_seen_pane_id`` / ``observed_at``) moves
        forward so the next staleness check compares against the latest snapshot.
        """
        return replace(
            self, last_seen_pane_id=_norm(pane_id), observed_at=_norm(observed_at)
        )

    def to_record(self) -> dict[str, str]:
        """Full serialization for DB / runtime-state persistence (cache included)."""
        return {
            "route_id": self.route_id,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "role": self.role,
            "pane_name": self.pane_name,
            "last_seen_pane_id": self.last_seen_pane_id,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "RouteIdentity":
        """Rebuild an identity from a persisted record (inverse of :meth:`to_record`)."""
        return cls(
            workspace_id=_norm(record.get("workspace_id")),
            lane_id=_norm(record.get("lane_id")),
            role=_norm(record.get("role")),
            pane_name=_norm(record.get("pane_name")),
            route_id=_norm(record.get("route_id")),
            observed_at=_norm(record.get("observed_at")),
            last_seen_pane_id=_norm(record.get("last_seen_pane_id")),
        )

    def public_pointer(self) -> str:
        """Public-safe one-line pointer (Required behavior #8: no pane id).

        Echoes only the stable identity tokens, so a durable record / Redmine
        journal can name the route without baking a private, session-local pane
        id into a pasteable surface.
        """
        return (
            f"route={self.route_id} ws={self.workspace_id} lane={self.lane_id} "
            f"role={self.role} pane_name={self.pane_name}"
        )


@dataclass(frozen=True)
class PaneObservation:
    """A read-only, normalized view of one live pane from the inventory snapshot.

    Built by :func:`observe_pane` from a ``try_pane_lines`` row. Carries only the
    identity tokens the resolver compares; the raw absolute cwd / host path are
    intentionally dropped so an observation is never a private-topology leak.
    """

    pane_id: str
    workspace_id: str
    lane_id: str
    role: str
    pane_name: str

    @property
    def identity_key(self) -> tuple[str, str, str, str]:
        """``(workspace_id, lane_id, role, pane_name)`` — compared to an identity."""
        return (self.workspace_id, self.lane_id, self.role, self.pane_name)

    @property
    def lane_role_key(self) -> tuple[str, str, str]:
        """``(workspace_id, lane_id, role)`` — the slot, ignoring the label."""
        return (self.workspace_id, self.lane_id, self.role)

    @property
    def has_route_label(self) -> bool:
        """True when this pane carries the mandatory stable pane-name / label."""
        return bool(self.pane_name)


def observe_pane(pane: Mapping[str, object]) -> PaneObservation:
    """Normalize one live ``try_pane_lines`` row into a :class:`PaneObservation`.

    The stable route label is read from ``route_label`` (alias ``pane_name``);
    when neither is present the observation has no route label and is therefore
    *not eligible* to authoritatively resolve a route — it never degrades to the
    pane id.
    """
    label = _norm(pane.get(PANE_KEY_ROUTE_LABEL))
    if not label:
        label = _norm(pane.get(PANE_KEY_ROUTE_LABEL_ALIAS))
    return PaneObservation(
        pane_id=_norm(pane.get(PANE_KEY_ID)),
        workspace_id=_norm(pane.get(PANE_KEY_WORKSPACE)),
        lane_id=_norm_lane(pane.get(PANE_KEY_LANE)),
        role=_norm(pane.get(PANE_KEY_ROLE)),
        pane_name=label,
    )


def observe_inventory(panes: Iterable[Mapping[str, object]]) -> list[PaneObservation]:
    """Normalize a whole live inventory snapshot into observations."""
    return [observe_pane(pane) for pane in panes]


@dataclass(frozen=True)
class RouteResolution:
    """The typed result of re-resolving one route identity against live panes.

    :attr:`status` is one of the module tokens. On :data:`RESOLVE_OK`,
    :attr:`resolved_pane_id` is the live pane id to address and :attr:`identity`
    is the stored identity refreshed with that pane id; :attr:`pane_id_refreshed`
    records whether the live pane id differed from the cached snapshot (a moved
    pane that was transparently recovered). On any fail-closed status the pane id
    is empty and :attr:`detail` carries a public-safe explanation.
    """

    status: str
    route_id: str
    resolved_pane_id: str = ""
    identity: Optional[RouteIdentity] = None
    pane_id_refreshed: bool = False
    considered: int = 0
    detail: str = ""

    @property
    def is_resolved(self) -> bool:
        """True only for a clean single-match resolution."""
        return self.status == RESOLVE_OK

    @property
    def is_fail_closed(self) -> bool:
        """True for any of the distinct fail-closed diagnostics."""
        return self.status in FAIL_CLOSED_STATUSES

    def public_pointer(self) -> str:
        """Public-safe one-line summary (Required behavior #8: no pane id)."""
        return f"route={self.route_id} status={self.status} considered={self.considered}"


def resolve_route(
    identity: RouteIdentity, inventory: Sequence[Mapping[str, object]]
) -> RouteResolution:
    """Re-resolve one stable route identity against a live pane inventory.

    The pane id is never trusted as the authority: every match is computed from
    the stable identity tuple, and the cached ``last_seen_pane_id`` is consulted
    only to *detect* a stale snapshot. See the module docstring for the full
    outcome table.
    """
    observations = observe_inventory(inventory)
    lane_role = [o for o in observations if o.lane_role_key == identity.lane_role_key]
    labeled = [o for o in lane_role if o.has_route_label]
    identity_matches = [o for o in labeled if o.identity_key == identity.identity_key]

    considered = len(lane_role)

    if len(identity_matches) == 1:
        match = identity_matches[0]
        refreshed = match.pane_id != identity.last_seen_pane_id
        return RouteResolution(
            status=RESOLVE_OK,
            route_id=identity.route_id,
            resolved_pane_id=match.pane_id,
            identity=identity.with_observation(
                pane_id=match.pane_id, observed_at=identity.observed_at
            ),
            pane_id_refreshed=refreshed,
            considered=considered,
            detail=(
                "live pane recovered via stable identity"
                if refreshed
                else "cached pane id still valid for stable identity"
            ),
        )

    if len(identity_matches) > 1:
        return RouteResolution(
            status=TARGET_AMBIGUOUS,
            route_id=identity.route_id,
            considered=considered,
            detail=(
                f"{len(identity_matches)} live panes match the stable identity; "
                "fail closed rather than guess a target"
            ),
        )

    # Zero clean identity matches. Distinguish a missing stable label, a stale
    # cache, and a genuinely absent target — each is its own fail-closed signal.
    #
    # Label-missing is checked first: when the exact lane/role slot is occupied
    # but *nothing* in it carries a route label, the actionable root cause is the
    # missing metadata (a managed pane was never stamped / lost its label), not a
    # foreign pane taking the slot. Resolving it would require falling back to
    # pane-id authority, which is exactly what Required behavior #5 forbids.
    if lane_role and not labeled:
        return RouteResolution(
            status=ROUTE_LABEL_MISSING,
            route_id=identity.route_id,
            considered=considered,
            detail=(
                "lane/role panes exist but none carry the required stable "
                "pane-name / route-label metadata; refuse to fall back to pane-id "
                "authority"
            ),
        )

    # The cached pane id is still live but now carries a different identity (a
    # foreign pane took the slot, or a labeled pane in the slot has another
    # name): trusting the snapshot would mis-route.
    if identity.last_seen_pane_id:
        cached = [o for o in observations if o.pane_id == identity.last_seen_pane_id]
        if cached and cached[0].identity_key != identity.identity_key:
            return RouteResolution(
                status=TARGET_STALE,
                route_id=identity.route_id,
                considered=considered,
                detail=(
                    "cached pane id is live but now carries a different identity; "
                    "the snapshot is stale and must not be used to route"
                ),
            )

    return RouteResolution(
        status=TARGET_UNAVAILABLE,
        route_id=identity.route_id,
        considered=considered,
        detail="no live pane matches the stable route identity",
    )


def resolve_for_route_target(
    target_token: str,
    identity: RouteIdentity,
    inventory: Sequence[Mapping[str, object]],
    *,
    cross_project: bool = False,
) -> RouteResolution:
    """Re-resolve a #12550 logical route target, enforcing the routing guards.

    Bridges the planner's logical vocabulary
    (:data:`~mozyo_bridge.domain.delegation_route_planner.TARGET_SAME_LANE_WORKER`
    and friends) to live re-resolution. Two fail-closed guards run before any
    live match, mirroring the planner's invariants:

    - A ``same_lane_worker`` (Claude) target may never be re-resolved across a
      project boundary — that is a direct cross-project Claude send and raises
      :class:`~mozyo_bridge.domain.delegation_route_planner.DelegationRoutePlanError`
      (Required behavior #6, defense-in-depth with the planner's own guard).
    - The ledgered identity's role must match the role the logical target
      expects (a gateway/coordinator token must resolve to a Codex pane, a
      worker token to a Claude pane); a mismatch is a malformed re-resolution
      request and raises.

    Once the guards pass, resolution delegates to :func:`resolve_route`.
    """
    expected_role = ROUTE_TARGET_EXPECTED_ROLE.get(target_token)
    if expected_role is None:
        raise DelegationRoutePlanError(
            f"unknown logical route target {target_token!r}; cannot re-resolve"
        )
    if target_token == TARGET_SAME_LANE_WORKER and cross_project:
        raise DelegationRoutePlanError(
            "same-lane worker route may never be re-resolved across a project "
            "boundary; route via the Codex gateway"
        )
    if identity.role != expected_role:
        raise DelegationRoutePlanError(
            f"route target {target_token!r} expects role {expected_role!r} but the "
            f"ledgered identity carries role {identity.role!r}"
        )
    return resolve_route(identity, inventory)


@dataclass
class RouteIdentityLedger:
    """An in-memory ledger of stable route identities keyed by ``route_id``.

    This is the runtime-state model #12553 fixes: a small, serializable store
    that holds one :class:`RouteIdentity` per managed route. Persisting it to the
    live ``state_store`` is a serialization concern handled through
    :meth:`to_records` / :meth:`from_records`; the actual DB wiring is the
    deferred actuator follow-up. Resolution (:meth:`resolve`) always re-scans the
    supplied live inventory, so a persisted ledger never carries stale routing
    authority across sessions — only stable identity plus a cache to invalidate.
    """

    _identities: dict[str, RouteIdentity] = field(default_factory=dict)

    def record(self, identity: RouteIdentity) -> None:
        """Insert or update the identity for ``identity.route_id``."""
        self._identities[identity.route_id] = identity

    def get(self, route_id: str) -> Optional[RouteIdentity]:
        """Return the stored identity for ``route_id`` (or ``None``)."""
        return self._identities.get(_norm(route_id))

    def remove(self, route_id: str) -> None:
        """Drop the identity for ``route_id`` if present."""
        self._identities.pop(_norm(route_id), None)

    def identities(self) -> tuple[RouteIdentity, ...]:
        """All stored identities (insertion order)."""
        return tuple(self._identities.values())

    def resolve(
        self, route_id: str, inventory: Sequence[Mapping[str, object]]
    ) -> RouteResolution:
        """Re-resolve a stored route against the live inventory, fail-closed.

        An unknown ``route_id`` is itself a fail-closed
        :data:`TARGET_UNAVAILABLE` (there is no identity to match), never an
        exception that a caller might paper over.
        """
        key = _norm(route_id)
        identity = self._identities.get(key)
        if identity is None:
            return RouteResolution(
                status=TARGET_UNAVAILABLE,
                route_id=key,
                detail="route_id is not recorded in the ledger",
            )
        return resolve_route(identity, inventory)

    def refresh(
        self, route_id: str, inventory: Sequence[Mapping[str, object]]
    ) -> RouteResolution:
        """Resolve and, on success, persist the refreshed cached pane id.

        Convenience for the actuator: when re-resolution recovers a moved pane,
        the ledger's cached ``last_seen_pane_id`` is advanced so the next
        staleness check compares against the latest snapshot. Fail-closed results
        leave the stored identity untouched.
        """
        resolution = self.resolve(route_id, inventory)
        if resolution.is_resolved and resolution.identity is not None:
            self._identities[resolution.identity.route_id] = resolution.identity
        return resolution

    def to_records(self) -> list[dict[str, str]]:
        """Serialize the whole ledger for DB / runtime-state persistence."""
        return [identity.to_record() for identity in self._identities.values()]

    @classmethod
    def from_records(
        cls, records: Iterable[Mapping[str, object]]
    ) -> "RouteIdentityLedger":
        """Rebuild a ledger from persisted records (inverse of :meth:`to_records`)."""
        ledger = cls()
        for record in records:
            ledger.record(RouteIdentity.from_record(record))
        return ledger
