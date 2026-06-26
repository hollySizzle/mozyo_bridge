"""Live executor Redmine record package (Redmine #12558).

The #12557 live executor turns a #12550 :class:`RoutePlan` into ordered
side-effects (re-resolve a route identity, send a handoff, stamp a grandchild
lane) and must leave a *replayable* trail in Redmine so a parent coordinator can
audit the whole ``parent -> child -> grandchild`` route after the fact. This
module is that trail: the structured, public-safe record set the executor emits,
plus the fail-closed final-classification vocabulary the smoke contract fixes.

The record set mirrors ``## Redmine Record Package`` of
``vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md`` —
baseline, parent decision, child delivery, child result, grandchild realization,
worker evidence, callback outcome, final classification — and the per-record
field vocabulary follows
``vibes/docs/specs/delegated-coordinator-decision-records.md``.

Three invariants this module enforces so the #12474 smoke failures cannot recur:

- **The classification buckets are never conflated.** ``PASS`` /
  ``failed_acceptance`` / ``insufficient`` / ``contaminated`` / ``blocked`` /
  ``environmental`` (acceptance doc ``## Failure Classification``) are distinct
  tokens; :func:`classify_final` derives exactly one with an explicit
  precedence, and :func:`validate_classification` fails closed on anything else.
- **Notification success alone is never evidence.** A delivery that "sent" but
  whose route-identity re-resolution did not resolve, or whose Redmine write
  failed, is non-``PASS``. The classification inputs separate *delivered* from
  *route realized* from *durably recorded* so a green pane notification can
  never carry a run to ``PASS``.
- **No private topology in a tracked surface.** A pane id / host path is
  session-local, private topology (``vibes/docs/rules/public-private-boundary.md``).
  :class:`RouteExecutionRecord` splits public-safe ``fields`` from runtime-only
  ``runtime_evidence``; :meth:`RouteExecutionRecord.public_markdown` renders only
  the former, so the pasteable Redmine surface never bakes in a ``%N`` pane id.

Purity (mirrors the #12550 planner / #12553 ledger contract): this module never
opens tmux, never sends a handoff, never writes Redmine. The Redmine write seam
is the injected :class:`RouteRecordSink` protocol; the live, credential-gated
adapter is the deferred actuator follow-up, exactly as #12550 / #12553 deferred
theirs. The persistence-outcome vocabulary is reused from
:mod:`mozyo_bridge.domain.delivery_record_sink` so a write failure speaks the
same language across the codebase.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from mozyo_bridge.domain.delivery_record_sink import (
    PERSIST_DISABLED,
    PERSIST_OK,
    PERSIST_TRANSPORT_ERROR,
)
from mozyo_bridge.domain.route_identity_ledger import RouteResolution

# ---------------------------------------------------------------------------
# Final-classification vocabulary (acceptance doc ``## Failure Classification``
# / ``## Redmine Record Package`` Final Classification). Exactly one of these is
# the verdict of a run; they are deliberately distinct so a partial route, a
# contaminated read, and an environmental non-attempt never collapse into each
# other or into a false PASS.
# ---------------------------------------------------------------------------
CLASS_PASS: str = "PASS"
CLASS_FAILED_ACCEPTANCE: str = "failed_acceptance"
CLASS_INSUFFICIENT: str = "insufficient"
CLASS_CONTAMINATED: str = "contaminated"
CLASS_BLOCKED: str = "blocked"
CLASS_ENVIRONMENTAL: str = "environmental"

#: Every legal classification token.
VALID_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        CLASS_PASS,
        CLASS_FAILED_ACCEPTANCE,
        CLASS_INSUFFICIENT,
        CLASS_CONTAMINATED,
        CLASS_BLOCKED,
        CLASS_ENVIRONMENTAL,
    }
)
#: Every non-``PASS`` classification (for a single "did not pass" guard).
NON_PASS_CLASSIFICATIONS: frozenset[str] = VALID_CLASSIFICATIONS - {CLASS_PASS}

# ---------------------------------------------------------------------------
# Record kinds, in the order the executor emits them. The grandchild realization
# / worker evidence records are present only when the route shape includes a
# grandchild lane; baseline and final classification always bracket the package.
# ---------------------------------------------------------------------------
RECORD_BASELINE: str = "baseline"
RECORD_PARENT_DECISION: str = "parent_decision"
RECORD_CHILD_DELIVERY: str = "child_delivery"
RECORD_CHILD_RESULT: str = "child_result"
RECORD_GRANDCHILD_REALIZATION: str = "grandchild_realization"
RECORD_WORKER_EVIDENCE: str = "worker_evidence"
RECORD_CALLBACK_OUTCOME: str = "callback_outcome"
RECORD_FINAL_CLASSIFICATION: str = "final_classification"

#: Canonical package order; :class:`RouteRecordPackage` rejects out-of-order or
#: unknown kinds so a replay always reads top-down.
ROUTE_RECORD_ORDER: tuple[str, ...] = (
    RECORD_BASELINE,
    RECORD_PARENT_DECISION,
    RECORD_CHILD_DELIVERY,
    RECORD_CHILD_RESULT,
    RECORD_GRANDCHILD_REALIZATION,
    RECORD_WORKER_EVIDENCE,
    RECORD_CALLBACK_OUTCOME,
    RECORD_FINAL_CLASSIFICATION,
)
_RECORD_RANK: dict[str, int] = {kind: i for i, kind in enumerate(ROUTE_RECORD_ORDER)}

# ---------------------------------------------------------------------------
# Callback outcome vocabulary (decision-records spec §4.1 / acceptance doc
# Callback Outcome). A required callback target whose outcome is none of these
# recorded states keeps the run out of PASS.
# ---------------------------------------------------------------------------
CALLBACK_SENT: str = "sent"
CALLBACK_BLOCKED: str = "blocked"
CALLBACK_PENDING: str = "pending"
CALLBACK_NOT_APPLICABLE: str = "not_applicable"
#: Outcomes that count as a recorded (not pending) callback result.
CALLBACK_RECORDED_OUTCOMES: frozenset[str] = frozenset(
    {CALLBACK_SENT, CALLBACK_BLOCKED, CALLBACK_NOT_APPLICABLE}
)

# Persistence-outcome tokens reused from the delivery record sink so a route
# record write failure speaks the same vocabulary as every other Redmine write.
PERSIST_OK = PERSIST_OK
PERSIST_DISABLED = PERSIST_DISABLED
PERSIST_TRANSPORT_ERROR = PERSIST_TRANSPORT_ERROR


class RouteRecordError(ValueError):
    """A route record / package input is malformed (unknown kind, bad order).

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    delegation / ledger domain errors. A *programming* error (an unknown record
    kind, an out-of-order append, an unknown classification) raises; a runtime
    *outcome* (a blocked callback, a failed write) is carried in a record field,
    never raised.
    """


def _clean(value: object) -> str:
    """Trim a field value to a public-safe token (``None`` -> ``""``)."""
    return str(value).strip() if value is not None else ""


@dataclass(frozen=True)
class CallbackOutcome:
    """One required/optional callback target plus its recorded outcome.

    Mirrors decision-records spec §4.1 ``callback_targets``: a ``purpose`` (e.g.
    ``delegation_parent`` / ``owning_us_coordinator``), the durable ``route``
    anchor (never a pane id), whether the target is ``required``, and the
    recorded ``outcome``. A ``required`` target left at :data:`CALLBACK_PENDING`
    keeps the run out of PASS.
    """

    purpose: str
    route: str
    required: bool
    outcome: str = CALLBACK_PENDING

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", _clean(self.purpose))
        object.__setattr__(self, "route", _clean(self.route))
        object.__setattr__(self, "outcome", _clean(self.outcome) or CALLBACK_PENDING)
        if not self.purpose:
            raise RouteRecordError("callback outcome requires a purpose")
        if not self.route:
            raise RouteRecordError("callback outcome requires a durable route anchor")

    @property
    def is_recorded(self) -> bool:
        """True when the outcome is a recorded (non-pending) state."""
        return self.outcome in CALLBACK_RECORDED_OUTCOMES

    def field_value(self) -> str:
        """One public-safe line: ``purpose=route required outcome``."""
        req = "required" if self.required else "optional"
        return f"{self.purpose}={self.route} {req} outcome={self.outcome}"


def all_required_callbacks_recorded(targets: Sequence[CallbackOutcome]) -> bool:
    """True when every ``required`` target carries a recorded (non-pending) outcome.

    An empty target set is *not* a satisfied callback contract: a route that
    reached the callback step with no required target recorded has not proven the
    parent was notified, so this returns ``False`` (fail-closed, never a vacuous
    PASS).
    """
    required = [t for t in targets if t.required]
    if not required:
        return False
    return all(t.is_recorded for t in required)


@dataclass(frozen=True)
class RouteExecutionRecord:
    """One structured, public-safe record the executor emits to Redmine.

    :attr:`fields` is the ordered, public-safe ``key -> value`` body rendered
    into the pasteable Redmine markdown. :attr:`runtime_evidence` holds
    runtime-only values (a resolved ``%N`` pane id, an absolute worktree path)
    that are private topology: they ride on the in-process object for the live
    actuator but are **never** rendered into :meth:`public_markdown`, so a
    tracked fixture or a Redmine journal never bakes in a private pane id
    (``vibes/docs/rules/public-private-boundary.md``).
    """

    kind: str
    source_issue: str
    fields: tuple[tuple[str, str], ...] = ()
    runtime_evidence: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _clean(self.kind))
        object.__setattr__(self, "source_issue", _clean(self.source_issue))
        if self.kind not in _RECORD_RANK:
            raise RouteRecordError(
                f"unknown route record kind {self.kind!r}; expected one of "
                f"{', '.join(ROUTE_RECORD_ORDER)}"
            )
        if not self.source_issue:
            raise RouteRecordError(f"{self.kind} record requires a source_issue")
        object.__setattr__(
            self, "fields", tuple((_clean(k), _clean(v)) for k, v in self.fields)
        )
        object.__setattr__(
            self,
            "runtime_evidence",
            tuple((_clean(k), _clean(v)) for k, v in self.runtime_evidence),
        )

    @property
    def rank(self) -> int:
        """The record's position in the canonical package order."""
        return _RECORD_RANK[self.kind]

    def to_record(self) -> dict[str, object]:
        """Full serialization (public fields only; runtime evidence excluded).

        The runtime evidence is intentionally *not* serialized here: ``to_record``
        feeds the durable, pasteable surface and must stay public-safe. The live
        actuator reads :attr:`runtime_evidence` off the object directly.
        """
        return {
            "record_kind": self.kind,
            "source_issue": self.source_issue,
            "fields": {k: v for k, v in self.fields},
        }

    def public_markdown(self) -> str:
        """Render the public-safe Redmine record body (no private pane id/path)."""
        lines = [f"## {self.kind}", "", f"- record_kind: {self.kind}", f"- source_issue: {self.source_issue}"]
        lines.extend(f"- {key}: {value}" for key, value in self.fields)
        return "\n".join(lines)

    def public_pointer(self) -> str:
        """One-line public-safe pointer for a handoff chat / index."""
        return f"{self.kind} ({self.source_issue})"


# ---------------------------------------------------------------------------
# Record builders. Each accepts public-safe tokens (and, where relevant, a
# :class:`RouteResolution` whose ``public_pointer`` already omits the pane id)
# and routes any private value into ``runtime_evidence``.
# ---------------------------------------------------------------------------


def baseline_record(
    *,
    source_issue: str,
    test_model: str,
    fresh_panes: bool,
    base_commit: str,
    notes: str = "",
) -> RouteExecutionRecord:
    """Baseline: the test model + fresh-pane/worktree snapshot the run started from."""
    fields = [
        ("test_model", _clean(test_model)),
        ("fresh_panes", "yes" if fresh_panes else "no"),
        ("base_commit", _clean(base_commit)),
    ]
    if notes:
        fields.append(("notes", _clean(notes)))
    return RouteExecutionRecord(
        kind=RECORD_BASELINE, source_issue=source_issue, fields=tuple(fields)
    )


def parent_decision_record(
    *,
    source_issue: str,
    child_project: str,
    child_delegation: str,
    role_profile_chain: Sequence[str],
    basis: str,
    no_child_delegation_reason: str = "not_applicable",
) -> RouteExecutionRecord:
    """Parent delegation decision (decision-records spec §1)."""
    return RouteExecutionRecord(
        kind=RECORD_PARENT_DECISION,
        source_issue=source_issue,
        fields=(
            ("child_project", _clean(child_project)),
            ("child_delegation", _clean(child_delegation)),
            ("role_profile_chain", " -> ".join(_clean(r) for r in role_profile_chain)),
            ("decision_basis", _clean(basis)),
            ("no_child_delegation_reason", _clean(no_child_delegation_reason)),
        ),
    )


def child_delivery_record(
    *,
    source_issue: str,
    resolution: RouteResolution,
    role_profile: str,
    send_outcome: str,
    target_repo_gate: str = "",
) -> RouteExecutionRecord:
    """Child delivery: the parent -> child handoff under the route-identity contract.

    The route-identity resolution (``public_pointer`` — no pane id) and the send
    outcome are public; the resolved ``%N`` pane id is recorded as runtime
    evidence only. ``role_profile`` should be ``delegated_coordinator`` for the
    child gateway hop.
    """
    fields = [
        ("role_profile", _clean(role_profile)),
        ("route_identity_resolution", resolution.public_pointer()),
        ("live_resolution", resolution.status),
        ("pane_id_refreshed", "yes" if resolution.pane_id_refreshed else "no"),
        ("send_outcome", _clean(send_outcome)),
    ]
    if target_repo_gate:
        fields.append(("target_repo_gate", _clean(target_repo_gate)))
    runtime: list[tuple[str, str]] = []
    if resolution.resolved_pane_id:
        runtime.append(("resolved_pane_id", resolution.resolved_pane_id))
    return RouteExecutionRecord(
        kind=RECORD_CHILD_DELIVERY,
        source_issue=source_issue,
        fields=tuple(fields),
        runtime_evidence=tuple(runtime),
    )


def child_result_record(
    *,
    source_issue: str,
    child_issue: str,
    grandchild_dispatch: str,
    no_dispatch_reason: str = "not_applicable",
) -> RouteExecutionRecord:
    """Child result: issue/request created + the grandchild dispatch decision."""
    return RouteExecutionRecord(
        kind=RECORD_CHILD_RESULT,
        source_issue=source_issue,
        fields=(
            ("child_issue", _clean(child_issue)),
            ("grandchild_dispatch", _clean(grandchild_dispatch)),
            ("no_dispatch_reason", _clean(no_dispatch_reason)),
        ),
    )


def grandchild_realization_record(
    *,
    source_issue: str,
    resolution: RouteResolution,
    realization: str,
    stamp_outcome: str,
    depth: int,
    parent: str,
) -> RouteExecutionRecord:
    """Grandchild realization: the visible lane, the stamp, and the live projection.

    Carries the ``KIND`` / ``DEPTH`` / ``PARENT`` projection the acceptance doc
    requires (``DEPTH=2`` and ``PARENT=<delegated coordinator>``) plus the
    route-identity resolution for the grandchild gateway; the resolved pane id
    stays in runtime evidence.
    """
    runtime: list[tuple[str, str]] = []
    if resolution.resolved_pane_id:
        runtime.append(("resolved_pane_id", resolution.resolved_pane_id))
    return RouteExecutionRecord(
        kind=RECORD_GRANDCHILD_REALIZATION,
        source_issue=source_issue,
        fields=(
            ("realization", _clean(realization)),
            ("route_identity_resolution", resolution.public_pointer()),
            ("live_resolution", resolution.status),
            ("stamp_outcome", _clean(stamp_outcome)),
            ("projection_depth", str(int(depth))),
            ("projection_parent", _clean(parent)),
        ),
        runtime_evidence=tuple(runtime),
    )


def worker_evidence_record(
    *,
    source_issue: str,
    resolution: RouteResolution,
    role_profile: str,
    send_outcome: str,
    fresh_projection: str,
    product_result: str = "pending",
) -> RouteExecutionRecord:
    """Worker evidence: the same-lane worker hop + the worker's own fresh projection.

    ``role_profile`` is ``implementation_worker``. The worker's self-observed
    projection and the product result are kept distinct from the route/profile
    evidence (acceptance doc Worker Evidence: "route/display/profile evidence と
    product result を分離").
    """
    runtime: list[tuple[str, str]] = []
    if resolution.resolved_pane_id:
        runtime.append(("resolved_pane_id", resolution.resolved_pane_id))
    return RouteExecutionRecord(
        kind=RECORD_WORKER_EVIDENCE,
        source_issue=source_issue,
        fields=(
            ("role_profile", _clean(role_profile)),
            ("route_identity_resolution", resolution.public_pointer()),
            ("live_resolution", resolution.status),
            ("send_outcome", _clean(send_outcome)),
            ("worker_fresh_projection", _clean(fresh_projection)),
            ("product_result", _clean(product_result)),
        ),
        runtime_evidence=tuple(runtime),
    )


def callback_outcome_record(
    *, source_issue: str, targets: Sequence[CallbackOutcome]
) -> RouteExecutionRecord:
    """Callback outcome: every required callback target and its recorded outcome."""
    pass_condition = (
        "all_required_callback_outcomes_recorded"
        if all_required_callbacks_recorded(targets)
        else "incomplete_required_callbacks"
    )
    fields = [(f"callback_target_{i}", t.field_value()) for i, t in enumerate(targets)]
    fields.append(("pass_condition", pass_condition))
    return RouteExecutionRecord(
        kind=RECORD_CALLBACK_OUTCOME, source_issue=source_issue, fields=tuple(fields)
    )


def final_classification_record(
    *, source_issue: str, classification: str, reason: str
) -> RouteExecutionRecord:
    """Final classification: the single verdict + reason (fail-closed on vocab)."""
    return RouteExecutionRecord(
        kind=RECORD_FINAL_CLASSIFICATION,
        source_issue=source_issue,
        fields=(
            ("classification", validate_classification(classification)),
            ("reason", _clean(reason)),
        ),
    )


# ---------------------------------------------------------------------------
# Final-classification derivation.
# ---------------------------------------------------------------------------


def validate_classification(value: str) -> str:
    """Return ``value`` if it is a legal classification, else fail closed.

    Guards every place a classification token is written so an unknown / mistyped
    bucket never reaches a durable record (the buckets must never be conflated).
    """
    token = _clean(value)
    if token not in VALID_CLASSIFICATIONS:
        raise RouteRecordError(
            f"unknown classification {value!r}; expected one of "
            f"{', '.join(sorted(VALID_CLASSIFICATIONS))}"
        )
    return token


@dataclass(frozen=True)
class ClassificationInputs:
    """The orthogonal facts a final classification is derived from.

    Each flag is a *distinct* observation so the buckets cannot collapse:
    ``delivered`` (a pane notification landed) is deliberately **not** a field,
    because notification success alone is never evidence — what matters is
    whether the route *realized* and was *durably recorded*.
    """

    #: A forbidden read surface was reached (parent journals / prior smoke / ...).
    contaminated: bool = False
    #: A routing invariant was violated (cross-project Claude direct send, a
    #: required child/grandchild window not realized) — the planner's
    #: ``failed_acceptance`` bucket.
    invariant_violation: bool = False
    #: A route-identity re-resolution or realization gate failed closed
    #: (target_unavailable / ambiguous / stale / same-lane fallback).
    blocked: bool = False
    #: A required Redmine record write failed (a write failure is non-PASS).
    redmine_write_failed: bool = False
    #: An environmental non-attempt (marker timeout, tmux focus, network).
    environmental: bool = False
    #: The full route (every required hop) re-resolved and delivered.
    route_fully_realized: bool = True
    #: Every required callback target carries a recorded outcome.
    callbacks_recorded: bool = True
    #: The read boundary was insufficient (target anchor never read).
    insufficient_read: bool = False


def classify_final(inputs: ClassificationInputs) -> tuple[str, str]:
    """Derive the single final classification + reason, with explicit precedence.

    Precedence (first match wins), aligned with the acceptance oracle so a
    rejected route is never reported as PASS:

    1. **contaminated** — a forbidden read surface was reached.
    2. **failed_acceptance** — a routing invariant was violated.
    3. **blocked** — a re-resolution / realization gate failed closed.
    4. **environmental** — an environmental non-attempt, or a Redmine write
       failure (a write failure is fail-closed, never a silent PASS).
    5. **insufficient** — the read was insufficient, the route only partially
       realized, or a required callback is unrecorded.
    6. **PASS** — the full route realized, was durably recorded, and every
       required callback outcome is recorded.
    """
    if inputs.contaminated:
        return CLASS_CONTAMINATED, "read_boundary_contaminated"
    if inputs.invariant_violation:
        return CLASS_FAILED_ACCEPTANCE, "routing_invariant_violation"
    if inputs.blocked:
        return CLASS_BLOCKED, "route_resolution_or_realization_blocked"
    if inputs.redmine_write_failed:
        return CLASS_ENVIRONMENTAL, "redmine_record_write_failed"
    if inputs.environmental:
        return CLASS_ENVIRONMENTAL, "environmental_non_attempt"
    if inputs.insufficient_read:
        return CLASS_INSUFFICIENT, "read_boundary_insufficient"
    if not inputs.route_fully_realized:
        return CLASS_INSUFFICIENT, "route_not_fully_realized"
    if not inputs.callbacks_recorded:
        return CLASS_INSUFFICIENT, "required_callback_unrecorded"
    return CLASS_PASS, "route_realized_and_callbacks_recorded"


# ---------------------------------------------------------------------------
# Injected Redmine write seam (the live, credential-gated adapter is deferred).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteRecordReceipt:
    """The outcome of persisting one route record.

    :attr:`persisted` is the single "did the durable write succeed" guard the
    classifier consults; a ``False`` receipt makes the run non-PASS. :attr:`reason`
    reuses the :mod:`delivery_record_sink` ``PERSIST_*`` vocabulary.
    """

    persisted: bool
    reason: str = PERSIST_OK
    location: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "persisted": self.persisted,
            "reason": self.reason,
            "location": self.location,
        }


@runtime_checkable
class RouteRecordSink(Protocol):
    """The injected boundary that persists a route record to Redmine.

    The live implementation wraps a credential-gated Redmine journal write and is
    the deferred actuator follow-up; the executor and its classical tests depend
    only on this protocol, so a write failure (``persisted=False``) is exercised
    with a fake, never a real Redmine round-trip.
    """

    name: str

    def persist(self, record: RouteExecutionRecord) -> RouteRecordReceipt:
        ...


class NullRouteRecordSink:
    """A sink that records nothing and reports ``disabled`` (recording off).

    Distinct from a write *failure*: ``disabled`` is an intentional no-op, but
    the executor still treats a route that needed a durable record but got
    ``disabled`` as non-PASS, because an unrecorded route is not replayable.
    """

    name: str = "none"

    def persist(self, record: RouteExecutionRecord) -> RouteRecordReceipt:
        return RouteRecordReceipt(persisted=False, reason=PERSIST_DISABLED)


# ---------------------------------------------------------------------------
# Ordered package aggregator.
# ---------------------------------------------------------------------------


@dataclass
class RouteRecordPackage:
    """An ordered, append-only collection of the route records for one run.

    Enforces the canonical :data:`ROUTE_RECORD_ORDER`: an append whose kind ranks
    before the last appended kind fails closed, so a replay always reads
    top-down. Duplicate kinds are permitted (a route can emit two worker-evidence
    records for a multi-hop grandchild route) as long as order is non-decreasing.
    """

    source_issue: str
    _records: list[RouteExecutionRecord] = field(default_factory=list)

    def append(self, record: RouteExecutionRecord) -> None:
        if record.source_issue != _clean(self.source_issue):
            raise RouteRecordError(
                f"record source_issue {record.source_issue!r} does not match "
                f"package source_issue {self.source_issue!r}"
            )
        if self._records and record.rank < self._records[-1].rank:
            raise RouteRecordError(
                f"out-of-order record {record.kind!r} (rank {record.rank}) after "
                f"{self._records[-1].kind!r} (rank {self._records[-1].rank})"
            )
        self._records.append(record)

    def records(self) -> tuple[RouteExecutionRecord, ...]:
        return tuple(self._records)

    def kinds(self) -> tuple[str, ...]:
        return tuple(r.kind for r in self._records)

    def has_kind(self, kind: str) -> bool:
        return any(r.kind == kind for r in self._records)

    def to_payload(self) -> list[dict[str, object]]:
        return [r.to_record() for r in self._records]

    def public_markdown(self) -> str:
        """The whole package as one pasteable, public-safe Redmine surface."""
        return "\n\n".join(r.public_markdown() for r in self._records)


__all__ = (
    "CLASS_PASS",
    "CLASS_FAILED_ACCEPTANCE",
    "CLASS_INSUFFICIENT",
    "CLASS_CONTAMINATED",
    "CLASS_BLOCKED",
    "CLASS_ENVIRONMENTAL",
    "VALID_CLASSIFICATIONS",
    "NON_PASS_CLASSIFICATIONS",
    "RECORD_BASELINE",
    "RECORD_PARENT_DECISION",
    "RECORD_CHILD_DELIVERY",
    "RECORD_CHILD_RESULT",
    "RECORD_GRANDCHILD_REALIZATION",
    "RECORD_WORKER_EVIDENCE",
    "RECORD_CALLBACK_OUTCOME",
    "RECORD_FINAL_CLASSIFICATION",
    "ROUTE_RECORD_ORDER",
    "CALLBACK_SENT",
    "CALLBACK_BLOCKED",
    "CALLBACK_PENDING",
    "CALLBACK_NOT_APPLICABLE",
    "CALLBACK_RECORDED_OUTCOMES",
    "PERSIST_OK",
    "PERSIST_DISABLED",
    "PERSIST_TRANSPORT_ERROR",
    "RouteRecordError",
    "CallbackOutcome",
    "all_required_callbacks_recorded",
    "RouteExecutionRecord",
    "baseline_record",
    "parent_decision_record",
    "child_delivery_record",
    "child_result_record",
    "grandchild_realization_record",
    "worker_evidence_record",
    "callback_outcome_record",
    "final_classification_record",
    "validate_classification",
    "ClassificationInputs",
    "classify_final",
    "RouteRecordReceipt",
    "RouteRecordSink",
    "NullRouteRecordSink",
    "RouteRecordPackage",
)
