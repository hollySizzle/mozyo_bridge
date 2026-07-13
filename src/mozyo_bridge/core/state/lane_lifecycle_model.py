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


@dataclass(frozen=True)
class ReleasePin:
    """One managed slot pinned at release-request time.

    ``locator`` is the live locator observed when the release generation opened. It
    is **evidence, not authority** (Design Answer D3): the actuator re-resolves the
    stable identity ``(workspace, lane, role, assigned_name)`` against the live
    inventory and closes only when the live locator still matches this pin — so a
    slot that was recycled into a *new* agent is never killed by a stale action.
    """

    role: str
    assigned_name: str
    locator: str

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
    """Read pinned slots back; an unreadable / empty value yields no pins."""
    if not norm(raw):
        return ()
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(loaded, list):
        return ()
    pins: list[ReleasePin] = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        pins.append(
            ReleasePin(
                role=norm(item.get("role")),
                assigned_name=norm(item.get("assigned_name")),
                locator=norm(item.get("locator")),
            )
        )
    return tuple(pins)


@dataclass(frozen=True)
class LaneLifecycleRecord:
    """One lane unit's durable desired lifecycle."""

    repo_workspace_id: str
    lane_id: str
    issue_id: str = ""
    lane_disposition: str = DISPOSITION_ACTIVE
    process_release: str = RELEASE_NOT_REQUESTED
    revision: int = 1
    release_action_id: str = ""
    release_pins: str = ""
    decision_source: str = ""
    decision_journal: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def key(self) -> LaneLifecycleKey:
        return LaneLifecycleKey(self.repo_workspace_id, self.lane_id)

    @property
    def pins(self) -> tuple[ReleasePin, ...]:
        return decode_release_pins(self.release_pins)

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


__all__ = (
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
