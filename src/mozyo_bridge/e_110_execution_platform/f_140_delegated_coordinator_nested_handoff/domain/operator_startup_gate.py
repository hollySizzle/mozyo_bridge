"""Durable ``operator_action_required`` startup-gate schema (Redmine #13812).

The projection tranche of #13762 (Design Answer j#78409 / Coordinator Verdict
j#78412). #13760 already detects a provider **startup screen** — a trust
confirmation, a first-run theme picker, a login prompt — at the pre-send
boundary and refuses the send (zero-send). But detection alone leaves a human-less
lane cycling: the screen is cleared by an operator in the provider's own UI, and
there is no *durable, high-level* surface that says which exact target is waiting,
under whose approval, at which generation, so the same original request can be
re-issued exactly once afterwards. This module is that surface's **typed durable
record** — a read-only projection of the blocker as an ``operator_action_required``
gate. It carries no authority to *clear* the screen; clearing stays an operator UI
action (the whole #13762 boundary), and applying / trust / auth mutation is out of
scope (a future exact-target provider API would be a separate security-gated issue).

Responsibility split (j#78409 "責務分離"), which this module keeps sharp:

- the provider profile ``startup_blockers`` (:mod:`...f_160_provider_registry.domain.agent_provider_startup_blocker`)
  is a **pure classifier token** — ``{id, all_of}`` and a version, nothing more. It
  never carries a role, a route, an approval, a locator, or a key sequence.
- **this gate record** is the durable **workflow authority**: exact target, owner
  approval scope, action generation, the original request pointer, resume state. It
  *references* a profile blocker id but never derives an approval from the profile.
- the action-time runtime preflight (the application projection,
  :mod:`...application.operator_startup_gate_projection`) resolves live
  target / provider / generation / startup state at action time; this record is a
  projection of what it observed, never the source of a live permission.

Durable-record safety (j#78409 schema note): a gate is **pasteable**. It stores no
absolute path, no pane body or its hash (a low-entropy dialog line is dictionary-
recoverable), no credential, and no login method. The repository identity is a single
**opaque digest** (:func:`repo_identity_digest`), never a checkout path. Every string
field is screened for a path / secret shape at construction, so a malformed projection
fails closed rather than journaling a private topology.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

#: The closed schema version. Bumped only by a deliberate, migration-aware change;
#: an unrecognized version fails closed at :meth:`OperatorStartupGate.from_record`.
OPERATOR_STARTUP_GATE_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Gate state vocabulary (j#78409 schema ``state``). An append-only transition
# lattice: a durable Redmine journal records each transition under the SAME
# ``gate_id`` / ``action_generation``. The projection tranche (#13812) only
# *emits* ``required`` and *classifies* an existing gate; the owner-approval and
# resume transitions are recorded by the resume tranche (#13813 / owner action).
# ---------------------------------------------------------------------------
#: An operator UI action is required; the receiver is sitting on a startup screen.
STATE_REQUIRED = "required"
#: The owner approved clearing THIS one target at THIS one action generation.
STATE_OWNER_APPROVED = "owner_approved"
#: The operator reported the UI action done (not yet re-verified from runtime).
STATE_OPERATOR_REPORTED_DONE = "operator_reported_done"
#: Action-time re-observation confirmed the startup screen is gone (startup-clear).
STATE_VERIFIED_CLEAR = "verified_clear"
#: The original request was re-issued exactly once; the gate is spent.
STATE_CONSUMED = "consumed"
#: A newer generation / changed target invalidated the gate before it was consumed.
STATE_SUPERSEDED = "superseded"

GATE_STATES: frozenset[str] = frozenset(
    {
        STATE_REQUIRED,
        STATE_OWNER_APPROVED,
        STATE_OPERATOR_REPORTED_DONE,
        STATE_VERIFIED_CLEAR,
        STATE_CONSUMED,
        STATE_SUPERSEDED,
    }
)

#: States past ``required`` require an owner approval record to be present: the
#: approval is what authorizes the transition, so a gate cannot claim
#: ``owner_approved`` (or beyond) without carrying the journal that granted it.
_STATES_REQUIRING_APPROVAL: frozenset[str] = frozenset(
    {
        STATE_OWNER_APPROVED,
        STATE_OPERATOR_REPORTED_DONE,
        STATE_VERIFIED_CLEAR,
        STATE_CONSUMED,
    }
)

#: Terminal states: a gate here is spent and can never actuate again.
TERMINAL_STATES: frozenset[str] = frozenset({STATE_CONSUMED, STATE_SUPERSEDED})

# ---------------------------------------------------------------------------
# Approval vocabulary (j#78409 schema ``approval``). The approval scope is pinned
# to exactly one target and one action generation; a global authentication / theme
# mutation is explicitly OUT of this scope (Coordinator Verdict j#78412 owner
# decision 2). The allowed action is an operator UI action only; the forbidden set
# is the closed list of automations this gate must never authorize.
# ---------------------------------------------------------------------------
APPROVAL_SCOPE_ONE_TARGET = "one_target_one_action_generation"
ALLOWED_ACTION_OPERATOR_UI = "operator_ui_only"
FORBIDDEN_ACTIONS: frozenset[str] = frozenset(
    {
        "raw_key",
        "generic_enter",
        "config_guess",
        "credential_capture",
        "permission_bypass",
    }
)

#: The original request is a durable ticket anchor. #13812 targets Redmine only
#: (the delegated-coordinator workflow's tracker); a non-Redmine source fails closed.
ORIGINAL_REQUEST_SOURCE_REDMINE = "redmine"

#: Resume ``dispatch_fence_state`` values the projection may express. The reserve /
#: send transitions themselves are the resume tranche (#13813); at projection time a
#: gate is only ever ``not_reserved`` (nothing has touched the outbox fence).
FENCE_NOT_RESERVED = "not_reserved"


class OperatorStartupGateError(ValueError):
    """An ``operator_startup_gate`` record violates the closed schema (fail-closed).

    Inherits :class:`ValueError` for the same fail-closed semantics as the sibling
    delegation / route-identity domain errors, so one ``except`` at a call site
    catches every schema violation.
    """


# ---------------------------------------------------------------------------
# Field guards. A gate is pasteable, so every free string is screened for a
# private path / secret shape; identifiers must be present and opaque.
# ---------------------------------------------------------------------------
_SECRET_TOKENS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
)


def _reject_path_or_secret_shaped(value: str, *, field_name: str) -> None:
    """Fail closed on a value shaped like a private path or a credential.

    A gate carries only public-safe, portable identifiers and an opaque digest. A
    filesystem / host path (separator, home prefix, URL scheme, Windows drive) or a
    credential-shaped token is exactly the private topology / secret a pasteable
    durable record must never journal (j#78409 "pane本文・credential非保存"). The
    :func:`repo_identity_digest` value is screened separately by
    :func:`_reject_non_digest` — it legitimately contains a ``:`` and is checked for
    the digest shape instead.
    """
    lowered = value.lower()
    if (
        "/" in value
        or "\\" in value
        or value.startswith("~")
        or "://" in value
        or (len(value) >= 2 and value[1] == ":")  # Windows drive, e.g. C:
    ):
        raise OperatorStartupGateError(
            f"operator startup gate {field_name} {value!r} is shaped like a private "
            f"host / filesystem path; a gate stores only portable, public-safe "
            f"identifiers and an opaque repo digest (no path separator, home prefix, "
            f"URL scheme, or drive letter)"
        )
    for token in _SECRET_TOKENS:
        if token in lowered:
            raise OperatorStartupGateError(
                f"operator startup gate {field_name} {value!r} carries a "
                f"credential-shaped token {token!r}; a gate never stores a secret, "
                f"key, or login method"
            )


def _require_token(value: object, *, field_name: str) -> str:
    """Coerce to a stripped, non-empty, path/secret-safe identifier (fail-closed)."""
    if not isinstance(value, str) or not value.strip():
        raise OperatorStartupGateError(
            f"operator startup gate {field_name} must be a non-empty string, got "
            f"{value!r}"
        )
    text = value.strip()
    _reject_path_or_secret_shaped(text, field_name=field_name)
    return text


def _reject_non_digest(value: str, *, field_name: str) -> None:
    """Fail closed unless ``value`` is an opaque ``<algo>:<hexdigest>`` digest.

    The repository identity must be stored as an opaque one-way digest, never a
    checkout path, so a durable record can name *which* repo without leaking *where*
    it is. A well-formed digest is ``<algo>:<hex>`` with a hex body long enough not
    to be a trivially reversible stub; anything else (a path, a bare label) is
    rejected. Build one with :func:`repo_identity_digest`.
    """
    algo, sep, body = value.partition(":")
    if (
        not sep
        or not algo
        or not algo.isalnum()
        or len(body) < 16
        or any(ch not in "0123456789abcdef" for ch in body)
    ):
        raise OperatorStartupGateError(
            f"operator startup gate {field_name} {value!r} is not an opaque digest; "
            f"expected '<algo>:<hexdigest>' (build it with repo_identity_digest so a "
            f"gate never stores a checkout path)"
        )


def repo_identity_digest(identity_token: str) -> str:
    """Opaque ``sha256:<hex>`` digest of a canonical repository identity token.

    The caller supplies an already-canonical identity string (a registry workspace
    id, a repo root token — resolved by the application layer, never a raw private
    path passed through). Hashing it yields a stable, one-way, pasteable digest: the
    same repository always projects the same digest, but the record carries no path.
    A blank token fails closed rather than digesting the empty string into a
    look-alike constant.
    """
    if not isinstance(identity_token, str) or not identity_token.strip():
        raise OperatorStartupGateError(
            "repo_identity_digest requires a non-empty canonical identity token"
        )
    digest = hashlib.sha256(identity_token.strip().encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _require_positive_generation(value: object, *, field_name: str) -> int:
    """Coerce to a positive int generation (fail-closed).

    ``bool`` is rejected even though it is an ``int`` subclass, so ``True`` does not
    silently read as generation ``1``. A non-positive generation is meaningless as a
    monotonic pin and fails closed.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperatorStartupGateError(
            f"operator startup gate {field_name} must be an integer, got {value!r}"
        )
    if value <= 0:
        raise OperatorStartupGateError(
            f"operator startup gate {field_name} must be a positive generation, got "
            f"{value!r}"
        )
    return value


@dataclass(frozen=True)
class OriginalRequest:
    """Pointer to the durable Implementation Request the gate resumes (j#78409).

    ``issue`` / ``journal`` are the Redmine anchor of the ORIGINAL request (e.g.
    #13760 j#77948), kept as opaque string ids. ``delivery_id`` is the deterministic
    q-enter logical payload id — a duplicate-detection handle, deliberately not the
    exactly-once authority (that stays the existing ``DispatchOutboxFence``, resume
    tranche). The gate stores the pointer only; it never inlines the request body.
    """

    source: str
    issue: str
    journal: str
    delivery_id: str

    def __post_init__(self) -> None:
        source = _require_token(self.source, field_name="original_request.source")
        if source != ORIGINAL_REQUEST_SOURCE_REDMINE:
            raise OperatorStartupGateError(
                f"operator startup gate original_request.source must be "
                f"{ORIGINAL_REQUEST_SOURCE_REDMINE!r} (#13812 targets Redmine), got "
                f"{source!r}"
            )
        object.__setattr__(self, "source", source)
        object.__setattr__(
            self, "issue", _require_token(self.issue, field_name="original_request.issue")
        )
        object.__setattr__(
            self,
            "journal",
            _require_token(self.journal, field_name="original_request.journal"),
        )
        object.__setattr__(
            self,
            "delivery_id",
            _require_token(self.delivery_id, field_name="original_request.delivery_id"),
        )

    def to_record(self) -> dict:
        return {
            "source": self.source,
            "issue": self.issue,
            "journal": self.journal,
            "delivery_id": self.delivery_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "OriginalRequest":
        _require_mapping(record, field_name="original_request")
        return cls(
            source=_get(record, "source"),
            issue=_get(record, "issue"),
            journal=_get(record, "journal"),
            delivery_id=_get(record, "delivery_id"),
        )


@dataclass(frozen=True)
class GateTarget:
    """The exact target the gate is pinned to (j#78409 schema ``target``).

    Every field is a stable identity token: ``workspace_id`` (registry authority),
    ``repo_identity_digest`` (opaque; :func:`repo_identity_digest`), ``execution_root``
    (repo-relative, ``"."`` at the root — never absolute), ``lane_id`` /
    ``target_role`` / ``target_assigned_name`` (durable managed identity),
    ``provider_id``, and a positive ``agent_generation`` (the attested live
    generation). The gate is honored only against a live re-observation that matches
    THIS tuple; a blank / mismatched / newer-generation observation is stale and
    zero-actuation (the projection's stale判定).
    """

    workspace_id: str
    repo_identity_digest: str
    execution_root: str
    lane_id: str
    target_role: str
    target_assigned_name: str
    provider_id: str
    agent_generation: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workspace_id",
            _require_token(self.workspace_id, field_name="target.workspace_id"),
        )
        digest = _require_stripped(
            self.repo_identity_digest, field_name="target.repo_identity_digest"
        )
        _reject_non_digest(digest, field_name="target.repo_identity_digest")
        object.__setattr__(self, "repo_identity_digest", digest)
        object.__setattr__(
            self,
            "execution_root",
            _require_execution_root(self.execution_root),
        )
        object.__setattr__(
            self, "lane_id", _require_token(self.lane_id, field_name="target.lane_id")
        )
        object.__setattr__(
            self,
            "target_role",
            _require_token(self.target_role, field_name="target.target_role"),
        )
        object.__setattr__(
            self,
            "target_assigned_name",
            _require_token(
                self.target_assigned_name, field_name="target.target_assigned_name"
            ),
        )
        object.__setattr__(
            self,
            "provider_id",
            _require_token(self.provider_id, field_name="target.provider_id"),
        )
        object.__setattr__(
            self,
            "agent_generation",
            _require_positive_generation(
                self.agent_generation, field_name="target.agent_generation"
            ),
        )

    @property
    def identity_key(self) -> tuple[str, str, str, str, str, str]:
        """The stable identity tuple, generation-independent.

        Two observations of the *same* managed target across relaunches share this
        key; they differ only in ``agent_generation``. The projection compares this
        key for identity mismatch and the generation separately for staleness.
        """
        return (
            self.workspace_id,
            self.repo_identity_digest,
            self.execution_root,
            self.lane_id,
            self.target_role,
            self.target_assigned_name,
        )

    def same_identity(self, other: "GateTarget") -> bool:
        """True when ``other`` names the same managed target (ignoring generation).

        ``provider_id`` is part of identity here even though it is not in
        :attr:`identity_key`: a target that resolved to a *different provider* is not
        the same target, so a provider change is a mismatch, not a generation bump.
        """
        return (
            self.identity_key == other.identity_key
            and self.provider_id == other.provider_id
        )

    def to_record(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "repo_identity_digest": self.repo_identity_digest,
            "execution_root": self.execution_root,
            "lane_id": self.lane_id,
            "target_role": self.target_role,
            "target_assigned_name": self.target_assigned_name,
            "provider_id": self.provider_id,
            "agent_generation": self.agent_generation,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "GateTarget":
        _require_mapping(record, field_name="target")
        return cls(
            workspace_id=_get(record, "workspace_id"),
            repo_identity_digest=_get(record, "repo_identity_digest"),
            execution_root=_get(record, "execution_root"),
            lane_id=_get(record, "lane_id"),
            target_role=_get(record, "target_role"),
            target_assigned_name=_get(record, "target_assigned_name"),
            provider_id=_get(record, "provider_id"),
            agent_generation=record.get("agent_generation"),
        )


@dataclass(frozen=True)
class GateClassification:
    """The screen classification the gate references (j#78409 schema ``classification``).

    ``blocker_id`` is a **pure profile token** — the id of the matched
    ``startup_blockers`` entry, the only thing about the screen a durable record may
    carry (#13760 invariant 3). ``profile_version`` / ``classifier_version`` pin the
    versions that produced the classification so a later re-read can tell a wording
    drift from a real change. ``observed_at`` is an opaque, caller-supplied stamp
    (the domain never reads the clock).
    """

    blocker_id: str
    profile_version: str
    classifier_version: str
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "blocker_id",
            _require_token(self.blocker_id, field_name="classification.blocker_id"),
        )
        object.__setattr__(
            self,
            "profile_version",
            _require_token(
                self.profile_version, field_name="classification.profile_version"
            ),
        )
        object.__setattr__(
            self,
            "classifier_version",
            _require_token(
                self.classifier_version, field_name="classification.classifier_version"
            ),
        )
        object.__setattr__(
            self,
            "observed_at",
            _require_token(self.observed_at, field_name="classification.observed_at"),
        )

    def to_record(self) -> dict:
        return {
            "blocker_id": self.blocker_id,
            "profile_version": self.profile_version,
            "classifier_version": self.classifier_version,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "GateClassification":
        _require_mapping(record, field_name="classification")
        return cls(
            blocker_id=_get(record, "blocker_id"),
            profile_version=_get(record, "profile_version"),
            classifier_version=_get(record, "classifier_version"),
            observed_at=_get(record, "observed_at"),
        )


@dataclass(frozen=True)
class GateApproval:
    """The owner approval that authorizes clearing ONE target at ONE generation.

    Present only once the owner has approved (states past ``required``). ``scope`` is
    pinned to :data:`APPROVAL_SCOPE_ONE_TARGET`, ``allowed_action`` to
    :data:`ALLOWED_ACTION_OPERATOR_UI`, and ``forbidden`` must be exactly
    :data:`FORBIDDEN_ACTIONS` — the gate can never widen its own authority to a raw
    key, a generic Enter, a config guess, a credential capture, or a permission
    bypass. ``source_journal`` is the owner approval journal anchor.
    """

    source_journal: str
    scope: str = APPROVAL_SCOPE_ONE_TARGET
    allowed_action: str = ALLOWED_ACTION_OPERATOR_UI
    forbidden: frozenset[str] = FORBIDDEN_ACTIONS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_journal",
            _require_token(self.source_journal, field_name="approval.source_journal"),
        )
        if self.scope != APPROVAL_SCOPE_ONE_TARGET:
            raise OperatorStartupGateError(
                f"operator startup gate approval.scope must be "
                f"{APPROVAL_SCOPE_ONE_TARGET!r}; a gate approval is pinned to one "
                f"target and one action generation, got {self.scope!r}"
            )
        if self.allowed_action != ALLOWED_ACTION_OPERATOR_UI:
            raise OperatorStartupGateError(
                f"operator startup gate approval.allowed_action must be "
                f"{ALLOWED_ACTION_OPERATOR_UI!r}; clearing a startup screen is an "
                f"operator UI action, got {self.allowed_action!r}"
            )
        forbidden = frozenset(self.forbidden)
        if forbidden != FORBIDDEN_ACTIONS:
            raise OperatorStartupGateError(
                f"operator startup gate approval.forbidden must be exactly "
                f"{sorted(FORBIDDEN_ACTIONS)}; the gate may never narrow the list of "
                f"automations it forbids, got {sorted(forbidden)}"
            )
        object.__setattr__(self, "forbidden", forbidden)

    def to_record(self) -> dict:
        return {
            "source_journal": self.source_journal,
            "scope": self.scope,
            "allowed_action": self.allowed_action,
            "forbidden": sorted(self.forbidden),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "GateApproval":
        _require_mapping(record, field_name="approval")
        raw_forbidden = record.get("forbidden", sorted(FORBIDDEN_ACTIONS))
        if not isinstance(raw_forbidden, (list, tuple, set, frozenset)):
            raise OperatorStartupGateError(
                f"operator startup gate approval.forbidden must be a list, got "
                f"{type(raw_forbidden).__name__}"
            )
        return cls(
            source_journal=_get(record, "source_journal"),
            scope=str(record.get("scope", APPROVAL_SCOPE_ONE_TARGET)),
            allowed_action=str(record.get("allowed_action", ALLOWED_ACTION_OPERATOR_UI)),
            forbidden=frozenset(str(item) for item in raw_forbidden),
        )


@dataclass(frozen=True)
class GateResume:
    """Resume state (j#78409 schema ``resume``); all-unset at projection time.

    The startup-clear re-observation, the outbox fence reserve, and the consumed
    delivery record are all the resume tranche's (#13813) to fill. At #13812
    projection time a fresh ``required`` gate always carries the default: nothing
    observed clear, ``not_reserved``, no consumed delivery.
    """

    startup_clear_observed_at: Optional[str] = None
    dispatch_fence_state: str = FENCE_NOT_RESERVED
    consumed_delivery_record: Optional[str] = None

    def __post_init__(self) -> None:
        if self.startup_clear_observed_at is not None:
            object.__setattr__(
                self,
                "startup_clear_observed_at",
                _require_token(
                    self.startup_clear_observed_at,
                    field_name="resume.startup_clear_observed_at",
                ),
            )
        object.__setattr__(
            self,
            "dispatch_fence_state",
            _require_token(
                self.dispatch_fence_state, field_name="resume.dispatch_fence_state"
            ),
        )
        if self.consumed_delivery_record is not None:
            object.__setattr__(
                self,
                "consumed_delivery_record",
                _require_token(
                    self.consumed_delivery_record,
                    field_name="resume.consumed_delivery_record",
                ),
            )

    def to_record(self) -> dict:
        return {
            "startup_clear_observed_at": self.startup_clear_observed_at,
            "dispatch_fence_state": self.dispatch_fence_state,
            "consumed_delivery_record": self.consumed_delivery_record,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "GateResume":
        _require_mapping(record, field_name="resume")
        clear = record.get("startup_clear_observed_at")
        consumed = record.get("consumed_delivery_record")
        return cls(
            startup_clear_observed_at=None if clear is None else str(clear),
            dispatch_fence_state=str(
                record.get("dispatch_fence_state", FENCE_NOT_RESERVED)
            ),
            consumed_delivery_record=None if consumed is None else str(consumed),
        )


@dataclass(frozen=True)
class OperatorStartupGate:
    """The durable ``operator_action_required`` startup gate (j#78409 schema).

    A pasteable projection of a provider startup blocker as a workflow-authoritative
    gate: which exact target is waiting, under whose approval, at which action
    generation, and how to resume the original request. Read-only by nature — this
    record never clears a screen and never actuates a send; it *describes* the
    operator action that must happen in the provider's own UI.
    """

    gate_id: str
    action_generation: int
    state: str
    original_request: OriginalRequest
    target: GateTarget
    classification: GateClassification
    approval: Optional[GateApproval] = None
    resume: GateResume = field(default_factory=GateResume)
    schema_version: int = OPERATOR_STARTUP_GATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "gate_id", _require_token(self.gate_id, field_name="gate_id")
        )
        object.__setattr__(
            self,
            "action_generation",
            _require_positive_generation(
                self.action_generation, field_name="action_generation"
            ),
        )
        if self.state not in GATE_STATES:
            raise OperatorStartupGateError(
                f"operator startup gate state {self.state!r} is not recognized; "
                f"allowed: {sorted(GATE_STATES)}"
            )
        if self.schema_version != OPERATOR_STARTUP_GATE_SCHEMA_VERSION:
            raise OperatorStartupGateError(
                f"operator startup gate schema_version {self.schema_version!r} is "
                f"unsupported; this build understands "
                f"{OPERATOR_STARTUP_GATE_SCHEMA_VERSION}"
            )
        if not isinstance(self.original_request, OriginalRequest):
            raise OperatorStartupGateError(
                "operator startup gate original_request must be an OriginalRequest"
            )
        if not isinstance(self.target, GateTarget):
            raise OperatorStartupGateError(
                "operator startup gate target must be a GateTarget"
            )
        if not isinstance(self.classification, GateClassification):
            raise OperatorStartupGateError(
                "operator startup gate classification must be a GateClassification"
            )
        if self.approval is not None and not isinstance(self.approval, GateApproval):
            raise OperatorStartupGateError(
                "operator startup gate approval must be a GateApproval or None"
            )
        if not isinstance(self.resume, GateResume):
            raise OperatorStartupGateError(
                "operator startup gate resume must be a GateResume"
            )
        # An approval is what authorizes a transition past `required`; a gate cannot
        # claim owner_approved (or beyond) without carrying the approval journal, and
        # a `required`/`superseded` gate must NOT carry one (it was never granted, or
        # it was invalidated before use).
        if self.state in _STATES_REQUIRING_APPROVAL and self.approval is None:
            raise OperatorStartupGateError(
                f"operator startup gate in state {self.state!r} must carry an owner "
                f"approval record"
            )
        if self.state in (STATE_REQUIRED, STATE_SUPERSEDED) and self.approval is not None:
            raise OperatorStartupGateError(
                f"operator startup gate in state {self.state!r} must not carry an "
                f"owner approval record (an approval is granted only on the "
                f"transition out of {STATE_REQUIRED!r})"
            )

    @property
    def is_terminal(self) -> bool:
        """True when the gate is spent (consumed or superseded) and cannot actuate."""
        return self.state in TERMINAL_STATES

    def to_record(self) -> dict:
        """Full, pasteable serialization (the durable-record shape).

        Safe by construction: no absolute path, no pane body / hash, no credential.
        ``approval`` is ``None`` until the owner grants it.
        """
        return {
            "schema_version": self.schema_version,
            "gate_id": self.gate_id,
            "action_generation": self.action_generation,
            "state": self.state,
            "original_request": self.original_request.to_record(),
            "target": self.target.to_record(),
            "classification": self.classification.to_record(),
            "approval": None if self.approval is None else self.approval.to_record(),
            "resume": self.resume.to_record(),
        }

    #: The durable record is already pasteable-safe, so the public projection IS the
    #: record. Kept as a named method so a caller reads intent (project for a journal)
    #: rather than reaching for ``to_record`` and wondering whether it redacts.
    def public_projection(self) -> dict:
        return self.to_record()

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "OperatorStartupGate":
        """Rebuild a gate from a persisted record (inverse of :meth:`to_record`)."""
        _require_mapping(record, field_name="operator_startup_gate")
        version = record.get("schema_version", OPERATOR_STARTUP_GATE_SCHEMA_VERSION)
        if version != OPERATOR_STARTUP_GATE_SCHEMA_VERSION:
            raise OperatorStartupGateError(
                f"operator startup gate schema_version {version!r} is unsupported; "
                f"this build understands {OPERATOR_STARTUP_GATE_SCHEMA_VERSION}"
            )
        approval_record = record.get("approval")
        resume_record = record.get("resume")
        return cls(
            gate_id=_get(record, "gate_id"),
            action_generation=record.get("action_generation"),
            state=str(_get(record, "state")),
            original_request=OriginalRequest.from_record(
                _require_child(record, "original_request")
            ),
            target=GateTarget.from_record(_require_child(record, "target")),
            classification=GateClassification.from_record(
                _require_child(record, "classification")
            ),
            approval=(
                None
                if approval_record is None
                else GateApproval.from_record(approval_record)
            ),
            resume=(
                GateResume()
                if resume_record is None
                else GateResume.from_record(resume_record)
            ),
        )


def build_required_gate(
    *,
    gate_id: str,
    action_generation: int,
    original_request: OriginalRequest,
    target: GateTarget,
    classification: GateClassification,
) -> OperatorStartupGate:
    """Construct a fresh ``required`` gate (the projection's positive output).

    A ``required`` gate carries no approval (the owner has not acted) and the default
    all-unset :class:`GateResume`. This is the only state the #13812 projection emits;
    every transition beyond it is recorded by the resume tranche or by owner action.
    """
    return OperatorStartupGate(
        gate_id=gate_id,
        action_generation=action_generation,
        state=STATE_REQUIRED,
        original_request=original_request,
        target=target,
        classification=classification,
        approval=None,
        resume=GateResume(),
    )


def operator_startup_gate_record_lines(gate: OperatorStartupGate) -> list[str]:
    """Render the pasteable durable-record projection lines (pure, redaction-safe).

    Follows the #13760 ``startup_admission_record_lines`` precedent: fixed tokens and
    a verdict only — no free text, no pane content, no absolute paths — so it is safe
    in a pasteable delivery record / Redmine journal. It names the exact target by its
    stable tokens and the opaque repo digest, the referenced blocker id, the approval
    scope, and the resume anchor, and states plainly that clearing the screen is an
    operator UI action this gate never performs.
    """
    approval = (
        f"owner-approved j#{gate.approval.source_journal} "
        f"(scope={gate.approval.scope}, action={gate.approval.allowed_action})"
        if gate.approval is not None
        else "awaiting owner approval"
    )
    return [
        (
            f"- operator_action_required (startup gate {gate.gate_id}, "
            f"action_generation={gate.action_generation}, state={gate.state}): the "
            f"{gate.target.provider_id} receiver is showing the "
            f"{gate.classification.blocker_id} startup screen, which cannot accept a "
            f"handoff body."
        ),
        (
            f"  target: workspace={gate.target.workspace_id} "
            f"repo={gate.target.repo_identity_digest} "
            f"execution_root={gate.target.execution_root} lane={gate.target.lane_id} "
            f"role={gate.target.target_role} name={gate.target.target_assigned_name} "
            f"agent_generation={gate.target.agent_generation}"
        ),
        (
            f"  classification: profile_version={gate.classification.profile_version} "
            f"classifier_version={gate.classification.classifier_version} "
            f"observed_at={gate.classification.observed_at}"
        ),
        (
            f"  approval: {approval}. original_request: "
            f"#{gate.original_request.issue} j#{gate.original_request.journal} "
            f"(delivery_id={gate.original_request.delivery_id})."
        ),
        (
            "  Clearing the screen is an operator action in the provider's own UI. "
            "This projection is read-only: it never answers the prompt, sends a key, "
            "or reserves the dispatch outbox. Once the operator clears the screen, "
            "re-issue THIS SAME original request through the high-level rail — it "
            "lands exactly once (Redmine #13812 / #13760 / #13813)."
        ),
    ]


# ---------------------------------------------------------------------------
# Small record-parsing helpers shared by the ``from_record`` inverses. Kept
# local so the schema stays one cohesive home (one-rule-one-home).
# ---------------------------------------------------------------------------
def _require_mapping(record: object, *, field_name: str) -> None:
    if not isinstance(record, Mapping):
        raise OperatorStartupGateError(
            f"operator startup gate {field_name} must be a mapping, got "
            f"{type(record).__name__}"
        )


def _require_child(record: Mapping[str, object], key: str) -> Mapping[str, object]:
    child = record.get(key)
    if not isinstance(child, Mapping):
        raise OperatorStartupGateError(
            f"operator startup gate {key!r} must be a mapping, got "
            f"{type(child).__name__}"
        )
    return child


def _get(record: Mapping[str, object], key: str) -> str:
    if key not in record:
        raise OperatorStartupGateError(
            f"operator startup gate record is missing required key {key!r}"
        )
    return str(record[key])


def _require_stripped(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperatorStartupGateError(
            f"operator startup gate {field_name} must be a non-empty string, got "
            f"{value!r}"
        )
    return value.strip()


def _require_execution_root(value: object) -> str:
    """Coerce ``execution_root`` to a repo-relative token (reject absolute/secret).

    ``execution_root`` is ``"."`` at the repository root or a repo-relative POSIX
    path (``projects/x``). Unlike every other field it may carry an *interior*
    forward slash — a repo-relative path is public-safe — but it must never be
    absolute (leading ``/``), a home prefix, a Windows path (backslash / drive), a
    URL, a parent-traversal (``..`` escapes the repo root), or carry a credential
    token. That keeps a durable record free of private host topology while still
    expressing a project sub-root.
    """
    text = _require_stripped(value, field_name="target.execution_root")
    lowered = text.lower()
    if (
        text.startswith("/")
        or text.startswith("~")
        or "\\" in text
        or "://" in text
        or (len(text) >= 2 and text[1] == ":")  # Windows drive, e.g. C:
        or text == ".."
        or text.startswith("../")
        or "/../" in text
        or text.endswith("/..")
    ):
        raise OperatorStartupGateError(
            f"operator startup gate target.execution_root {text!r} must be '.' or a "
            f"repo-relative POSIX path (no leading separator, home prefix, drive, URL "
            f"scheme, or parent traversal)"
        )
    for token in _SECRET_TOKENS:
        if token in lowered:
            raise OperatorStartupGateError(
                f"operator startup gate target.execution_root {text!r} carries a "
                f"credential-shaped token {token!r}"
            )
    return text


__all__ = (
    "ALLOWED_ACTION_OPERATOR_UI",
    "APPROVAL_SCOPE_ONE_TARGET",
    "FENCE_NOT_RESERVED",
    "FORBIDDEN_ACTIONS",
    "GATE_STATES",
    "OPERATOR_STARTUP_GATE_SCHEMA_VERSION",
    "ORIGINAL_REQUEST_SOURCE_REDMINE",
    "STATE_CONSUMED",
    "STATE_OPERATOR_REPORTED_DONE",
    "STATE_OWNER_APPROVED",
    "STATE_REQUIRED",
    "STATE_SUPERSEDED",
    "STATE_VERIFIED_CLEAR",
    "TERMINAL_STATES",
    "GateApproval",
    "GateClassification",
    "GateResume",
    "GateTarget",
    "OperatorStartupGate",
    "OperatorStartupGateError",
    "OriginalRequest",
    "build_required_gate",
    "operator_startup_gate_record_lines",
    "repo_identity_digest",
)
