"""The Unit state read model: four independent axes (pure, Redmine #15162).

The correction this model exists to make structural (#15151 j#101743): a previous
session read "the Redmine journal has not moved" together with "the worker is
mid-implementation" and collapsed them into a single ``blocked`` boolean. Both
inputs were *absences*, and neither was evidence of a blocker. A single boolean
had nowhere to put "I could not tell", so the read had to become a claim.

So this model refuses to collapse. Four axes are reported side by side and never
fold into each other:

``workflow``
    The durable record. Redmine issue status, the latest gate and its journal, and
    the next owner / action. This is the only axis that carries workflow truth.
``runtime``
    What the terminal runtime was observed to be doing (herdr / tmux). A *display
    and diagnostic* layer. ``ack-completion-receiver-state.md`` fixes the rule this
    axis obeys: a runtime observation is never promoted to workflow truth, review
    state, owner approval, or task completion.
``delivery``
    Whether a dispatch to the Unit's gateway / worker landed. Delivery ACK is not
    completion, and ``exit_code == 0`` is not delivery.
``health``
    Anomaly / degraded / freshness — the observation-quality axis, which says how
    much the other three can be trusted, and never what they say.

Every reported field is an :class:`ObservedField` carrying ``source`` /
``observed_at`` / ``freshness``, so a consumer can always tell *where* a value came
from and *how old* it is. There is no bare-scalar path: a value with no provenance
cannot be represented.

Two derivations are deliberately impossible here, because they were the actual
defect:

- **Absence is not a state.** An unread source, an unparsable timestamp, a journal
  that has not moved, a silent stdout, a pane with no new output, and a turn that
  ended are all :data:`VALUE_UNKNOWN` (or :data:`VALUE_UNCONFIRMED` for a delivery
  whose outcome was never observed). None of them derives ``blocked``, ``idle``, or
  ``completed``. :data:`FORBIDDEN_INFERENCE_BASES` names them so the prohibition is
  a pinned list rather than a docstring.
- **``blocked`` requires a blocker.** :class:`BlockedClaim` carries an authoritative
  blocker source, a reason, and a resume condition; :func:`admit_blocked` refuses a
  claim missing any of the three, or one sourced from a layer with no authority to
  declare it. A refused claim degrades the axis to :data:`VALUE_UNKNOWN` — the
  honest answer — rather than reporting an unsupported ``blocked``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (  # noqa: E501
    FRESHNESS_EXPIRED,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    READABILITY_PARTIAL,
    READABILITY_READABLE,
    READABILITY_UNREADABLE,
    SOURCE_HERDR,
    SOURCE_REDMINE,
)

# --- value vocabulary ------------------------------------------------------ #

#: The source could not be determined. The resting value of every field.
VALUE_UNKNOWN = "unknown"

#: Something was attempted and its outcome was never confirmed. Distinct from
#: :data:`VALUE_UNKNOWN`: "we dispatched and did not see it land" is a different
#: fact from "we did not look".
VALUE_UNCONFIRMED = "unconfirmed"

#: This Feature's addition to the ``runtime_observation`` ``source`` vocabulary:
#: no source was consulted for this field at all. Named rather than left blank so
#: an unconsulted field is visibly different from one read from a source that
#: happened to return nothing.
SOURCE_UNOBSERVED = "unobserved"

#: Axis names, in report order. A closed set: a new axis is a schema change.
AXIS_WORKFLOW = "workflow"
AXIS_RUNTIME = "runtime"
AXIS_DELIVERY = "delivery"
AXIS_HEALTH = "health"

AXES = (AXIS_WORKFLOW, AXIS_RUNTIME, AXIS_DELIVERY, AXIS_HEALTH)

#: Observations that MUST NOT be derived into ``blocked`` / ``idle`` / ``completed``.
#: Pinned as data so a test can assert the prohibition rather than trusting prose.
#: Each is an *absence*: it reports that nothing was seen, not that nothing happened.
FORBIDDEN_INFERENCE_BASES = frozenset(
    {
        "journal_not_updated",
        "pane_text",
        "stdout_silence",
        "turn_ended",
        "prompt_idle",
        "no_new_output",
    }
)

#: States no axis may derive from anything in :data:`FORBIDDEN_INFERENCE_BASES`.
UNDERIVABLE_STATES = frozenset({"blocked", "idle", "completed"})

#: Sources with the authority to declare the *workflow* axis blocked. Only the
#: durable record: ``ack-completion-receiver-state.md`` layer 3. A runtime signal
#: (layer 1) or a provider turn signal (layer 2) can never carry this.
WORKFLOW_BLOCKER_SOURCES = frozenset({SOURCE_REDMINE})

#: Sources with the authority to declare the *delivery* axis blocked: the durable
#: record and the delivery ledger, which are where a ``DeliveryOutcome`` lands.
DELIVERY_BLOCKER_SOURCES = frozenset({SOURCE_REDMINE, SOURCE_HERDR})


@dataclass(frozen=True)
class ObservedField:
    """One reported value with its full observation envelope.

    ``value`` is the fact. ``source`` / ``observed_at`` / ``freshness`` are the
    provenance the acceptance requires on **every** state field, and
    ``readability`` records whether the source could be read this time — the
    difference between "read it, it says unknown" and "could not read it".
    """

    value: str
    source: str = SOURCE_UNOBSERVED
    observed_at: Optional[str] = None
    freshness: str = FRESHNESS_UNKNOWN
    readability: str = READABILITY_UNREADABLE
    #: Why this field is unknown / degraded, when a short pointer helps. Never
    #: pane text, a credential, or a private path — a reason, not a transcript.
    note: Optional[str] = None

    @classmethod
    def unknown(
        cls,
        *,
        source: str = SOURCE_UNOBSERVED,
        note: Optional[str] = None,
    ) -> "ObservedField":
        """The resting field: nothing determined, nothing claimed."""
        return cls(value=VALUE_UNKNOWN, source=source, note=note)

    @classmethod
    def observed(
        cls,
        value: str,
        *,
        source: str,
        observed_at: Optional[str],
        freshness: str,
        readability: str = READABILITY_READABLE,
        note: Optional[str] = None,
    ) -> "ObservedField":
        """A field read from a real source.

        An empty / whitespace ``value`` degrades to :data:`VALUE_UNKNOWN` while
        keeping the provenance: the source *was* read, it just carried nothing.
        """
        text = (value or "").strip()
        return cls(
            value=text or VALUE_UNKNOWN,
            source=source,
            observed_at=observed_at,
            freshness=freshness,
            readability=readability,
            note=note,
        )

    @property
    def is_determined(self) -> bool:
        """True when this field carries an actual value."""
        return self.value not in (VALUE_UNKNOWN, VALUE_UNCONFIRMED)

    @property
    def is_current(self) -> bool:
        """True when the value is both readable and fresh.

        A stale / expired / unreadable field is never current, mirroring
        ``derive_display_state``'s fail-closed rule.
        """
        return (
            self.readability == READABILITY_READABLE
            and self.freshness == FRESHNESS_FRESH
        )

    def as_payload(self) -> dict:
        return {
            "value": self.value,
            "source": self.source,
            "observed_at": self.observed_at,
            "freshness": self.freshness,
            "readability": self.readability,
            "note": self.note,
        }


#: The resting field, shared so an unfilled axis costs nothing to construct.
UNKNOWN_FIELD = ObservedField.unknown()


@dataclass(frozen=True)
class BlockedClaim:
    """A claim that work is blocked, with the evidence that makes it admissible.

    All three parts are required by the acceptance, and each answers a different
    question a bare ``blocked`` leaves open: *who says so* (``blocker_source``,
    with ``durable_anchor`` pointing at the record), *why* (``reason``), and *what
    would unblock it* (``resume_condition``). A claim missing any part is not a
    weaker claim — it is not a claim, and :func:`admit_blocked` refuses it.
    """

    blocker_source: str
    reason: str
    resume_condition: str
    #: The durable record the claim was read from (e.g. ``#15151 j#102124``).
    durable_anchor: str = ""
    observed_at: Optional[str] = None
    freshness: str = FRESHNESS_UNKNOWN

    def as_payload(self) -> dict:
        return {
            "blocker_source": self.blocker_source,
            "reason": self.reason,
            "resume_condition": self.resume_condition,
            "durable_anchor": self.durable_anchor,
            "observed_at": self.observed_at,
            "freshness": self.freshness,
        }


def admit_blocked(
    claim: Optional[BlockedClaim], *, authoritative_sources: frozenset
) -> Optional[BlockedClaim]:
    """Return ``claim`` if it may be reported as ``blocked``, else ``None``.

    Fail-closed on every gap: no claim, a blank reason / resume condition / anchor,
    or a source without the authority to declare a block on this axis. The caller
    reports :data:`VALUE_UNKNOWN` for a refused claim — never ``blocked``, and
    never a softened "possibly blocked", which would be the same unsupported claim
    with a hedge in front of it.
    """
    if claim is None:
        return None
    if claim.blocker_source not in authoritative_sources:
        return None
    if not (claim.reason or "").strip():
        return None
    if not (claim.resume_condition or "").strip():
        return None
    if not (claim.durable_anchor or "").strip():
        return None
    return claim


@dataclass(frozen=True)
class WorkflowAxis:
    """Durable-record facts. The only axis carrying workflow truth.

    Sourced from Redmine. ``blocked`` is present only when an admissible
    :class:`BlockedClaim` backed it; otherwise ``state`` stays whatever the durable
    record said, or :data:`VALUE_UNKNOWN`.
    """

    state: ObservedField = UNKNOWN_FIELD
    issue_status: ObservedField = UNKNOWN_FIELD
    issue_id: ObservedField = UNKNOWN_FIELD
    latest_gate: ObservedField = UNKNOWN_FIELD
    latest_journal: ObservedField = UNKNOWN_FIELD
    next_owner: ObservedField = UNKNOWN_FIELD
    next_action: ObservedField = UNKNOWN_FIELD
    work_unit: ObservedField = UNKNOWN_FIELD
    blocked: Optional[BlockedClaim] = None

    def as_payload(self) -> dict:
        return {
            "state": self.state.as_payload(),
            "issue_status": self.issue_status.as_payload(),
            "issue_id": self.issue_id.as_payload(),
            "latest_gate": self.latest_gate.as_payload(),
            "latest_journal": self.latest_journal.as_payload(),
            "next_owner": self.next_owner.as_payload(),
            "next_action": self.next_action.as_payload(),
            "work_unit": self.work_unit.as_payload(),
            "blocked": self.blocked.as_payload() if self.blocked else None,
        }


@dataclass(frozen=True)
class RuntimeAxis:
    """Observed terminal-runtime state. Diagnostic only.

    ``roles`` carries the per-role observation (``gateway`` / ``worker`` / …) as
    ``(role, field)`` pairs so a Unit with several roles reports each separately
    instead of folding them into one "the Unit is busy".

    Nothing here is workflow truth. An ``idle``-looking runtime does not mean the
    task is done, and a ``turn_ended`` observation does not mean the worker
    finished — those readings are exactly what
    ``ack-completion-receiver-state.md`` forbids.
    """

    backend: ObservedField = UNKNOWN_FIELD
    roles: Tuple[Tuple[str, ObservedField], ...] = ()
    receive_method: ObservedField = UNKNOWN_FIELD

    def as_payload(self) -> dict:
        return {
            "backend": self.backend.as_payload(),
            "roles": [
                {"role": role, "observation": observed.as_payload()}
                for role, observed in self.roles
            ],
            "receive_method": self.receive_method.as_payload(),
            "authority_note": (
                "runtime observation only; never workflow truth, review state, "
                "owner approval, or task completion"
            ),
        }


@dataclass(frozen=True)
class DeliveryAxis:
    """Dispatch outcome toward the Unit's gateway / worker.

    ``outcome`` is :data:`VALUE_UNCONFIRMED` — not ``unknown``, and not
    ``failed`` — when a dispatch was recorded but its landing was never observed.
    Delivery ACK is not task completion, and a successful ACK says only that input
    reached the receiver runtime.
    """

    outcome: ObservedField = UNKNOWN_FIELD
    anomaly: ObservedField = UNKNOWN_FIELD
    anomaly_stale: ObservedField = UNKNOWN_FIELD
    blocked: Optional[BlockedClaim] = None

    def as_payload(self) -> dict:
        return {
            "outcome": self.outcome.as_payload(),
            "anomaly": self.anomaly.as_payload(),
            "anomaly_stale": self.anomaly_stale.as_payload(),
            "blocked": self.blocked.as_payload() if self.blocked else None,
            "authority_note": (
                "delivery ACK only; it does not imply the receiver processed the "
                "input or that the task completed"
            ),
        }


@dataclass(frozen=True)
class HealthAxis:
    """Observation quality: how much the other three axes can be trusted.

    ``degraded`` is true when any consulted source was unreadable or any reported
    field is stale / expired. ``notes`` records which source degraded, so an empty
    projection is never read as "nothing is wrong".
    """

    anomaly: ObservedField = UNKNOWN_FIELD
    degraded: bool = True
    freshness: str = FRESHNESS_UNKNOWN
    notes: Tuple[str, ...] = ()

    def as_payload(self) -> dict:
        return {
            "anomaly": self.anomaly.as_payload(),
            "degraded": self.degraded,
            "freshness": self.freshness,
            "notes": list(self.notes),
        }


def worst_freshness(fields: Tuple[ObservedField, ...]) -> str:
    """The least-fresh classification across ``fields`` (fail-closed).

    Ordering: ``unknown`` is worst (age cannot be asserted at all), then
    ``expired``, ``stale``, ``fresh``. An empty input is ``unknown`` — no evidence
    is not good evidence.
    """
    if not fields:
        return FRESHNESS_UNKNOWN
    rank = {
        FRESHNESS_FRESH: 3,
        FRESHNESS_STALE: 2,
        FRESHNESS_EXPIRED: 1,
        FRESHNESS_UNKNOWN: 0,
    }
    worst = min(fields, key=lambda f: rank.get(f.freshness, 0))
    return worst.freshness if worst.freshness in rank else FRESHNESS_UNKNOWN


def derive_health(
    fields: Tuple[ObservedField, ...],
    *,
    anomaly: ObservedField = UNKNOWN_FIELD,
    notes: Tuple[str, ...] = (),
) -> HealthAxis:
    """Derive the health axis from the fields the other axes reported.

    ``degraded`` is true unless every field is readable and fresh. This is the
    same fail-closed posture as ``derive_display_state``: a partial read is
    degraded, and an unread source is degraded — "healthy" is only claimed when
    everything backing it was actually current.
    """
    degraded = not fields or any(
        f.readability != READABILITY_READABLE or f.freshness != FRESHNESS_FRESH
        for f in fields
    )
    extra = list(notes)
    if any(f.readability == READABILITY_PARTIAL for f in fields):
        extra.append("at least one source was only partially readable")
    return HealthAxis(
        anomaly=anomaly,
        degraded=degraded,
        freshness=worst_freshness(fields),
        notes=tuple(extra),
    )


@dataclass(frozen=True)
class UnitStateReport:
    """The four axes for one resolved Unit, plus the Unit identity they describe."""

    unit: dict
    workflow: WorkflowAxis = field(default_factory=WorkflowAxis)
    runtime: RuntimeAxis = field(default_factory=RuntimeAxis)
    delivery: DeliveryAxis = field(default_factory=DeliveryAxis)
    health: HealthAxis = field(default_factory=HealthAxis)

    def as_payload(self) -> dict:
        return {
            "unit": self.unit,
            AXIS_WORKFLOW: self.workflow.as_payload(),
            AXIS_RUNTIME: self.runtime.as_payload(),
            AXIS_DELIVERY: self.delivery.as_payload(),
            AXIS_HEALTH: self.health.as_payload(),
            "read_only": True,
        }


__all__ = (
    "AXES",
    "AXIS_DELIVERY",
    "AXIS_HEALTH",
    "AXIS_RUNTIME",
    "AXIS_WORKFLOW",
    "BlockedClaim",
    "DELIVERY_BLOCKER_SOURCES",
    "DeliveryAxis",
    "FORBIDDEN_INFERENCE_BASES",
    "HealthAxis",
    "ObservedField",
    "RuntimeAxis",
    "SOURCE_UNOBSERVED",
    "UNDERIVABLE_STATES",
    "UNKNOWN_FIELD",
    "UnitStateReport",
    "VALUE_UNCONFIRMED",
    "VALUE_UNKNOWN",
    "WORKFLOW_BLOCKER_SOURCES",
    "WorkflowAxis",
    "admit_blocked",
    "derive_health",
    "worst_freshness",
)
