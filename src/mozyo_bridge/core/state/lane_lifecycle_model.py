"""Lane lifecycle — the pure model (Redmine #13689, Design Answer j#76741).

The closed vocabularies, the transition matrix, and the typed records of the lane
lifecycle component. Deliberately free of SQLite and of any I/O: the transition
matrix is the *policy*, and :mod:`mozyo_bridge.core.state.lane_lifecycle` is the
CAS store that enforces it durably. A caller may reason about a legal edge without
opening the store, and the edges can be pinned by tests that touch no DB.

The two axes are separate on purpose (design consultation j#76734):

- :data:`DISPOSITION_ACTIVE` … — what the coordinator *decided* about the lane.
- :data:`RELEASE_NOT_REQUESTED` … — how far a *release action* on that lane got.

Neither is a liveness fact. ``released`` records that a release command completed,
not that the slots are gone; process presence stays a live-inventory read
(``managed-state-model.md`` ``### 正本境界``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Sequence

# -- closed vocabularies -----------------------------------------------------

#: The lane holds its work and may receive sends.
DISPOSITION_ACTIVE = "active"
#: A recovery lane took over this issue; the old lane keeps its worktree but is
#: no longer the owner and is never a send target again (never returns to active).
DISPOSITION_SUPERSEDED = "superseded"
#: The lane's processes are (to be) released to reclaim capacity while its issue
#: stays open. A cold restart — files survive, agent context does not.
DISPOSITION_HIBERNATED = "hibernated"
#: Terminal.
DISPOSITION_RETIRED = "retired"

DISPOSITIONS = frozenset(
    {
        DISPOSITION_ACTIVE,
        DISPOSITION_SUPERSEDED,
        DISPOSITION_HIBERNATED,
        DISPOSITION_RETIRED,
    }
)

#: No release generation is open for this lane.
RELEASE_NOT_REQUESTED = "not_requested"
#: A release generation is open; its outcome is not yet recorded.
RELEASE_REQUESTED = "requested"
#: Some pinned slots closed and some did not — re-drivable (a pane close is
#: idempotent, unlike a send).
RELEASE_PARTIAL = "partial"
#: Every pinned slot of this generation was closed. Terminal *for the generation*.
RELEASE_RELEASED = "released"

RELEASE_STATES = frozenset(
    {
        RELEASE_NOT_REQUESTED,
        RELEASE_REQUESTED,
        RELEASE_PARTIAL,
        RELEASE_RELEASED,
    }
)

#: Allowed disposition edges (Design Answer D3). ``superseded -> active`` is
#: forbidden: reviving a superseded lane would re-create two active owners for one
#: issue, the very state this component makes unrepresentable.
_DISPOSITION_EDGES: dict[str, frozenset[str]] = {
    DISPOSITION_ACTIVE: frozenset(
        {DISPOSITION_SUPERSEDED, DISPOSITION_HIBERNATED, DISPOSITION_RETIRED}
    ),
    DISPOSITION_HIBERNATED: frozenset({DISPOSITION_ACTIVE, DISPOSITION_RETIRED}),
    DISPOSITION_SUPERSEDED: frozenset({DISPOSITION_RETIRED}),
    DISPOSITION_RETIRED: frozenset(),
}

#: Allowed release edges *within one action generation*. ``partial -> partial`` is
#: allowed: a retry that closes some-but-not-all remaining slots is progress, not a
#: conflict.
_RELEASE_EDGES: dict[str, frozenset[str]] = {
    RELEASE_NOT_REQUESTED: frozenset({RELEASE_REQUESTED}),
    RELEASE_REQUESTED: frozenset({RELEASE_PARTIAL, RELEASE_RELEASED}),
    RELEASE_PARTIAL: frozenset({RELEASE_PARTIAL, RELEASE_RELEASED}),
    RELEASE_RELEASED: frozenset(),
}

#: A lane may only come back to ``active`` when no release generation is in flight
#: (R1-F3 / R1-F2). ``requested`` / ``partial`` mean an actuator is (or may be)
#: closing this lane's pinned slots right now: silently clearing that generation
#: would let a half-closed lane re-enter the active roster and take sends while its
#: panes are still being killed. Only a finished generation (never opened, or fully
#: ``released``) may be cleared on rehydrate.
_REHYDRATABLE_RELEASE_STATES = frozenset({RELEASE_NOT_REQUESTED, RELEASE_RELEASED})

# -- CAS outcome vocabulary --------------------------------------------------

CAS_APPLIED = "applied"
CAS_NOT_FOUND = "not_found"
CAS_STALE_REVISION = "stale_revision"
CAS_UNEXPECTED_STATE = "unexpected_state"
CAS_FORBIDDEN_TRANSITION = "forbidden_transition"
CAS_ACTION_MISMATCH = "action_generation_mismatch"
CAS_OWNER_CONFLICT = "owner_conflict"
CAS_ALREADY_DECLARED = "already_declared"

# -- owner resolution vocabulary ---------------------------------------------

OWNER_RESOLVED = "resolved"
#: No active owner row for the issue — fail closed (never "probably that lane").
OWNER_ABSENT = "absent"
#: More than one active owner survived (only reachable if the index is missing on a
#: hand-edited DB) — fail closed rather than pick one.
OWNER_AMBIGUOUS = "ambiguous"
#: The store is absent / unreadable — fail closed. Never inferred as active.
OWNER_UNKNOWN = "unknown"


def norm(value: object) -> str:
    """Trim a raw field to a comparable token (``None`` -> ``""``)."""
    return str(value).strip() if value is not None else ""


# -- pure transition policy --------------------------------------------------


def disposition_transition_allowed(current: str, target: str) -> bool:
    """Is ``current -> target`` a legal disposition edge? (pure)"""
    return target in _DISPOSITION_EDGES.get(norm(current), frozenset())


def release_transition_allowed(current: str, target: str) -> bool:
    """Is ``current -> target`` a legal release edge within one generation? (pure)"""
    return target in _RELEASE_EDGES.get(norm(current), frozenset())


def rehydrate_allowed(process_release: str) -> bool:
    """May a lane in this release state come back to ``active``? (pure)

    The single policy both rehydrate paths share — ``transition_disposition`` to
    ``active`` and ``supersede_and_activate``'s promotion of an existing recovery
    lane (R1-F2 / R1-F3). An in-flight generation (``requested`` / ``partial``) is
    refused; there is deliberately no "cancel a release" state, so the caller must
    finish or abandon the generation through the release API, not by side-stepping
    it with a disposition write.
    """
    return norm(process_release) in _REHYDRATABLE_RELEASE_STATES


# -- records -----------------------------------------------------------------


@dataclass(frozen=True)
class LaneLifecycleKey:
    """The lane unit a lifecycle row belongs to."""

    repo_workspace_id: str
    lane_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_workspace_id", norm(self.repo_workspace_id))
        object.__setattr__(self, "lane_id", norm(self.lane_id))
        if not self.repo_workspace_id or not self.lane_id:
            raise ValueError(
                "lane lifecycle key requires a non-empty (repo_workspace_id, lane_id); "
                "a legacy lane with no lane id is out of scope (Redmine #13685)"
            )

    def as_row(self) -> tuple[str, str]:
        return (self.repo_workspace_id, self.lane_id)


class ReleasePinError(ValueError):
    """A release pin is unusable — never degraded into "one fewer slot" (R1-F4)."""


@dataclass(frozen=True)
class ReleasePin:
    """One managed slot pinned at release-request time.

    ``locator`` is the live locator observed when the release generation opened. It
    is **evidence, not authority** (Design Answer D3): the actuator re-resolves the
    stable identity ``(workspace, lane, role, assigned_name)`` against the live
    inventory and closes only when the live locator still matches this pin — so a
    slot that was recycled into a *new* agent is never killed by a stale action.

    Every field is required (R1-F4). A pin missing its role / assigned name /
    locator cannot express that stable identity at all, so it could never be
    re-resolved and would sit in the authority row as a slot nobody can act on.
    Rejecting it here keeps the row's pins meaning exactly "the slots this
    generation may close".
    """

    role: str
    assigned_name: str
    locator: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", norm(self.role))
        object.__setattr__(self, "assigned_name", norm(self.assigned_name))
        object.__setattr__(self, "locator", norm(self.locator))
        missing = [
            name
            for name in ("role", "assigned_name", "locator")
            if not getattr(self, name)
        ]
        if missing:
            raise ReleasePinError(
                "a release pin requires a non-empty role / assigned_name / locator "
                f"(missing: {', '.join(missing)}); an unresolvable slot is never pinned"
            )

    @property
    def stable_identity(self) -> tuple[str, str]:
        """The ``(role, assigned_name)`` half of the slot's identity within a lane."""
        return (self.role, self.assigned_name)

    def as_payload(self) -> dict[str, str]:
        return {
            "role": self.role,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
        }


def encode_release_pins(pins: Sequence[ReleasePin]) -> str:
    """Serialize pinned slots for the row (deterministic, role-sorted)."""
    return json.dumps(
        [p.as_payload() for p in sorted(pins, key=lambda p: p.role)],
        ensure_ascii=False,
        sort_keys=True,
    )


def decode_release_pins(raw: str) -> tuple[ReleasePin, ...]:
    """Read pinned slots back. Empty means no pins; corrupt **raises** (R1-F4).

    A malformed row must not decode to a *shorter* pin list: the caller would then
    close some slots and believe the generation complete, leaving the dropped slots
    alive. An unreadable pin set is a fail-closed condition, not a degraded one.
    """
    if not norm(raw):
        return ()
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReleasePinError(f"release pins are not readable JSON: {exc}") from exc
    if not isinstance(loaded, list):
        raise ReleasePinError("release pins must be a list")
    pins: list[ReleasePin] = []
    for item in loaded:
        if not isinstance(item, dict):
            raise ReleasePinError(f"release pin is not an object: {item!r}")
        pins.append(
            ReleasePin(
                role=norm(item.get("role")),
                assigned_name=norm(item.get("assigned_name")),
                locator=norm(item.get("locator")),
            )
        )
    return tuple(pins)


def validate_release_pins(pins: Sequence[ReleasePin]) -> tuple[ReleasePin, ...]:
    """The pins a release generation may open with (non-empty, no duplicate slot).

    Two pins for the same ``(role, assigned_name)`` would make the generation's
    outcome ambiguous — which locator was the one that had to match? Reject rather
    than pick.
    """
    pinned = tuple(pins)
    if not pinned:
        raise ReleasePinError("a release generation requires at least one pinned slot")
    seen: set[tuple[str, str]] = set()
    for pin in pinned:
        if pin.stable_identity in seen:
            raise ReleasePinError(
                f"duplicate pinned slot {pin.stable_identity!r} in one release generation"
            )
        seen.add(pin.stable_identity)
    return pinned


#: The durable-record systems a lifecycle decision may point at. A pointer into an
#: unknown system cannot be re-read at recovery time, so the vocabulary is closed.
DECISION_SOURCE_REDMINE = "redmine"
DECISION_SOURCES = frozenset({DECISION_SOURCE_REDMINE})


class DecisionPointerError(ValueError):
    """A durable decision pointer is missing / malformed (R1-F5); fail closed."""


def _positive_decimal(value: str, *, field: str) -> str:
    """A Redmine id: a positive decimal. Anything else cannot address a record."""
    if not value.isdigit() or int(value) <= 0:
        raise DecisionPointerError(
            f"a redmine {field} must be a positive decimal id, got {value!r}"
        )
    return value


@dataclass(frozen=True)
class DecisionPointer:
    """The durable record that authorizes one lifecycle write.

    ``(source, issue_id, journal_id)`` — a *pointer*, never a copy: the journal's
    body, the issue's status, and any approval stay in Redmine (``workflow_truth``
    is not duplicated into the DB, ``managed-state-model.md``).

    Required on every write that changes lifecycle authority (R1-F5). The component's
    recovery policy is ``operator_current_state``: it is rebuilt by an *explicit
    re-declare from the Redmine durable pointer*, which is only possible if each
    stored decision actually names the record that made it. Inheriting the previous
    write's pointer would leave a rehydrate decision pointing at the hibernate
    journal — an anchor that documents the wrong thing.

    **The anchor is always complete, even for a lane that owns no issue (R2-F1).** A
    Redmine journal is only addressable *through its issue* — the adapter reaches it
    as ``/issues/<id>.json``, and there is no journal-addressable endpoint — so a
    pointer without an issue id names nothing and cannot be re-read at recovery time.
    Both ids are therefore required and must be positive decimals.

    This is deliberately **not** the lane's owner binding. Whether a lane *owns* an
    issue (:attr:`LaneLifecycleRecord.issue_id`, legitimately empty for an unbound
    lane, Design Answer D2) and *which record decided* its current state (this
    pointer, never empty, D1) are different facts. Folding them into one field is
    what let an unbound lane store an unreadable anchor.
    """

    source: str
    issue_id: str
    journal_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", norm(self.source))
        object.__setattr__(self, "issue_id", norm(self.issue_id))
        object.__setattr__(self, "journal_id", norm(self.journal_id))
        if self.source not in DECISION_SOURCES:
            raise DecisionPointerError(
                f"unknown decision source {self.source!r}; "
                f"expected one of {sorted(DECISION_SOURCES)}"
            )
        _positive_decimal(self.issue_id, field="issue id")
        _positive_decimal(self.journal_id, field="journal id")

    def authorizes_binding(self, binding_issue_id: str) -> bool:
        """May this decision act on a lane bound to ``binding_issue_id``?

        An **unbound** lane (empty binding) may be decided by any valid anchor — the
        decision is about the lane, not about an ownership it does not hold. A lane
        that *does* own an issue may only be decided by a record filed on that same
        issue, so a decision cannot be anchored to an unrelated ticket.
        """
        binding = norm(binding_issue_id)
        return not binding or binding == self.issue_id


@dataclass(frozen=True)
class LaneLifecycleRecord:
    """One lane unit's durable desired lifecycle.

    ``issue_id`` is the lane's **owner binding** — which issue this lane owns, empty
    when it owns none. ``decision_*`` is the **durable anchor** of the record that put
    the lane in its current state, and is always complete. The two are separate
    (R2-F1): an unbound lane still has a decision, and that decision must stay
    re-readable.
    """

    repo_workspace_id: str
    lane_id: str
    issue_id: str = ""
    lane_disposition: str = DISPOSITION_ACTIVE
    process_release: str = RELEASE_NOT_REQUESTED
    revision: int = 1
    release_action_id: str = ""
    release_pins: str = ""
    decision_source: str = ""
    decision_issue_id: str = ""
    decision_journal: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def key(self) -> LaneLifecycleKey:
        return LaneLifecycleKey(self.repo_workspace_id, self.lane_id)

    @property
    def pins(self) -> tuple[ReleasePin, ...]:
        return decode_release_pins(self.release_pins)

    @property
    def decision(self) -> Optional[DecisionPointer]:
        """The stored anchor, or ``None`` when this row has no re-readable one.

        ``None`` is the honest answer for a v1 row written before the anchor carried
        its issue (R2-F1): that row cannot be re-read from Redmine, and a caller must
        see that rather than a pointer that looks usable. The gap is surfaced, never
        back-filled with a guessed issue.
        """
        try:
            return DecisionPointer(
                source=self.decision_source,
                issue_id=self.decision_issue_id,
                journal_id=self.decision_journal,
            )
        except DecisionPointerError:
            return None

    def as_payload(self) -> dict[str, object]:
        return {
            "repo_workspace_id": self.repo_workspace_id,
            "lane_id": self.lane_id,
            "issue_id": self.issue_id,
            "lane_disposition": self.lane_disposition,
            "process_release": self.process_release,
            "revision": self.revision,
            "release_action_id": self.release_action_id,
            "release_pins": [p.as_payload() for p in self.pins],
            "decision_source": self.decision_source,
            "decision_issue_id": self.decision_issue_id,
            "decision_journal": self.decision_journal,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CasOutcome:
    """The result of one guarded write.

    ``applied`` is the only success signal. On refusal, ``reason`` names *why* the
    caller lost (stale revision, wrong expected state, forbidden edge, foreign
    action generation) so a duplicate / out-of-order caller can be diagnosed instead
    of silently no-op'ing. ``revision`` is the row's revision after the call —
    unchanged on refusal, ``0`` when there is no row.
    """

    applied: bool
    reason: str
    revision: int = 0


@dataclass(frozen=True)
class OwnerResolution:
    """Who owns an issue — fail-closed by construction."""

    status: str
    lane_id: str = ""
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == OWNER_RESOLVED and bool(self.lane_id)


def guard(
    current: LaneLifecycleRecord, expected_disposition: str, expected_revision: int
) -> Optional[CasOutcome]:
    """The shared expected-state + expected-revision guard (``None`` when it passes)."""
    if current.lane_disposition != norm(expected_disposition):
        return CasOutcome(
            applied=False, reason=CAS_UNEXPECTED_STATE, revision=current.revision
        )
    if current.revision != expected_revision:
        return CasOutcome(
            applied=False, reason=CAS_STALE_REVISION, revision=current.revision
        )
    return None


def recovery_refusal(
    incoming: Optional[LaneLifecycleRecord],
    *,
    issue: str,
    expected_disposition: Optional[str],
    expected_revision: Optional[int],
) -> Optional[CasOutcome]:
    """Guard the recovery side of a supersession (``None`` when it may be activated).

    Everything the *old* lane's guard does, the recovery lane needs too (R1-F2) — it
    is just as much a CAS target. Beyond the expected state + revision it also has to
    keep two invariants the old lane cannot: it must not already own a **different**
    issue (R1-F1), and it must not have a release generation in flight (R1-F3).
    """
    if incoming is None:
        if expected_disposition is not None or expected_revision is not None:
            # The caller expected an existing recovery lane; there is none.
            return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
        return None
    if expected_disposition is None or expected_revision is None:
        # An existing recovery lane may only be moved under an explicit expectation.
        return CasOutcome(
            applied=False, reason=CAS_UNEXPECTED_STATE, revision=incoming.revision
        )
    refusal = guard(incoming, expected_disposition, expected_revision)
    if refusal is not None:
        return refusal
    if incoming.issue_id and incoming.issue_id != issue:
        # Promoting it would leave `incoming.issue_id` with no owner at all.
        return CasOutcome(
            applied=False, reason=CAS_OWNER_CONFLICT, revision=incoming.revision
        )
    if incoming.lane_disposition not in (DISPOSITION_ACTIVE, DISPOSITION_HIBERNATED):
        return CasOutcome(
            applied=False, reason=CAS_FORBIDDEN_TRANSITION, revision=incoming.revision
        )
    if not rehydrate_allowed(incoming.process_release):
        return CasOutcome(
            applied=False, reason=CAS_FORBIDDEN_TRANSITION, revision=incoming.revision
        )
    return None


__all__ = (
    "recovery_refusal",
    "DECISION_SOURCES",
    "DECISION_SOURCE_REDMINE",
    "DecisionPointer",
    "DecisionPointerError",
    "ReleasePinError",
    "rehydrate_allowed",
    "validate_release_pins",
    "CAS_ACTION_MISMATCH",
    "CAS_ALREADY_DECLARED",
    "CAS_APPLIED",
    "CAS_FORBIDDEN_TRANSITION",
    "CAS_NOT_FOUND",
    "CAS_OWNER_CONFLICT",
    "CAS_STALE_REVISION",
    "CAS_UNEXPECTED_STATE",
    "DISPOSITIONS",
    "DISPOSITION_ACTIVE",
    "DISPOSITION_HIBERNATED",
    "DISPOSITION_RETIRED",
    "DISPOSITION_SUPERSEDED",
    "OWNER_ABSENT",
    "OWNER_AMBIGUOUS",
    "OWNER_RESOLVED",
    "OWNER_UNKNOWN",
    "RELEASE_NOT_REQUESTED",
    "RELEASE_PARTIAL",
    "RELEASE_RELEASED",
    "RELEASE_REQUESTED",
    "RELEASE_STATES",
    "CasOutcome",
    "LaneLifecycleKey",
    "LaneLifecycleRecord",
    "OwnerResolution",
    "ReleasePin",
    "decode_release_pins",
    "disposition_transition_allowed",
    "encode_release_pins",
    "guard",
    "norm",
    "release_transition_allowed",
)
