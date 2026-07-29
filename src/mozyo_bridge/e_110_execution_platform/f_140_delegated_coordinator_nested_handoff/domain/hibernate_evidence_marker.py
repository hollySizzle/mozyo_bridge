"""Hibernate-evidence marker grammar for the CI / dogfood / park producers (Redmine #14219 T2b, step 3).

The design ruling (#14219 j#85530) rules that the three durable authorities without an existing
lane-bound record — required-CI-green, dogfood-delegated, park-declared — are emitted as GENERIC
``[mozyo:workflow-event:gate=<kind>:...]`` evidence, NOT callback-required gates (they are absent
from ``GATE_BEARING_KINDS`` so they never trigger a callback). This module is their dedicated
renderer + strict parser, deliberately separate from the gate-bearing ``render_workflow_event_marker``
(which raises for a non-gate-bearing kind) and from the #14213 glance vocabulary.

Every marker carries the common lane envelope (step 1) plus its kind-specific authority fields, all
fail-closed. Each kind names not just its authority's VERDICT but the record that verdict can be
audited against (ruling j#85530 Q3) — a bare "it was green" / "it was delegated" is not evidence
anyone can go and check:

  * ``required_ci_green`` — head-bearing; ``workflow=<workflow/check identity>`` and
    ``run=<run_id>`` (both non-empty) and ``conclusion=success``. The run id alone does not say
    WHICH required check ran, so without the workflow identity an unrelated green run satisfies the
    conjunct.
  * ``dogfood_delegated`` — head-bearing (the exact delegated SHA); ``release_issue=<id>`` and
    ``acceptance=<acceptance/resume anchor>`` (both non-empty). The acceptance anchor is what the
    delegation can later be resumed and checked from; the release-issue-side receipt is corroborated
    by the producer (it lives on the other issue, not in this marker).
  * ``park_declared`` — lane-anchored only (no head). The envelope alone is NOT the basis: the
    producer additionally requires the governed fixed-field park journal to be recorded in the same
    note (``state`` / ``blocked_by`` / ``resume_condition`` / ``resume_owner``).

Missing / malformed / wrong-conclusion / conflicting evidence is a typed zero — never a lenient
default, never prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .marker_value_contract import is_exact_str
from .hibernate_evidence_envelope import (
    EnvelopeParseError,
    LaneEvidenceEnvelope,
    parse_lane_envelope,
    render_lane_envelope,
    require_marker_token,
)

# Evidence kinds (closed vocabulary). Each doubles as the marker ``gate=`` value.
EVIDENCE_REQUIRED_CI_GREEN = "required_ci_green"
EVIDENCE_DOGFOOD_DELEGATED = "dogfood_delegated"
EVIDENCE_PARK_DECLARED = "park_declared"

HIBERNATE_EVIDENCE_KINDS = frozenset({
    EVIDENCE_REQUIRED_CI_GREEN,
    EVIDENCE_DOGFOOD_DELEGATED,
    EVIDENCE_PARK_DECLARED,
})

#: The kinds whose evidence is about a specific commit (the envelope must carry ``head``).
_HEAD_BEARING_EVIDENCE = frozenset({EVIDENCE_REQUIRED_CI_GREEN, EVIDENCE_DOGFOOD_DELEGATED})

FIELD_RUN = "run"
FIELD_WORKFLOW = "workflow"
FIELD_CONCLUSION = "conclusion"
FIELD_RELEASE_ISSUE = "release_issue"
FIELD_ACCEPTANCE = "acceptance"

_CI_CONCLUSION_SUCCESS = "success"

# The marker channel (kept in step with the workflow-event vocabulary the parser already reads).
MARKER_CHANNEL_WORKFLOW_EVENT = "workflow-event"

# Closed parse-failure reasons (in addition to the envelope's own).
EVIDENCE_UNKNOWN_KIND = "evidence_unknown_kind"
EVIDENCE_MISSING_RUN = "evidence_missing_run"
EVIDENCE_MISSING_WORKFLOW = "evidence_missing_workflow"
EVIDENCE_CI_NOT_SUCCESS = "evidence_ci_not_success"
EVIDENCE_MISSING_RELEASE_ISSUE = "evidence_missing_release_issue"
EVIDENCE_MISSING_ACCEPTANCE = "evidence_missing_acceptance"
EVIDENCE_ABSENT = "evidence_absent"
EVIDENCE_CONFLICT = "evidence_conflict"

HIBERNATE_EVIDENCE_PARSE_REASONS = frozenset({
    EVIDENCE_UNKNOWN_KIND,
    EVIDENCE_MISSING_RUN,
    EVIDENCE_MISSING_WORKFLOW,
    EVIDENCE_CI_NOT_SUCCESS,
    EVIDENCE_MISSING_RELEASE_ISSUE,
    EVIDENCE_MISSING_ACCEPTANCE,
    EVIDENCE_ABSENT,
    EVIDENCE_CONFLICT,
})


@dataclass(frozen=True)
class HibernateEvidence:
    """One parsed hibernate-evidence marker: its kind, lane envelope, and kind-specific fields."""

    kind: str
    envelope: LaneEvidenceEnvelope
    extra: dict

    def as_payload(self) -> dict:
        return {"kind": self.kind, "envelope": self.envelope.as_payload(), "extra": dict(self.extra)}


@dataclass(frozen=True)
class EvidenceParseError:
    """A typed hibernate-evidence parse / resolve failure."""

    reason: str
    detail: str = ""


class _Unsupplied:
    """The type of :data:`_UNSUPPLIED` — a value no caller can construct or pass by accident."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "<unsupplied>"


#: What a kind-specific producer parameter holds when the caller did not pass it. It is a dedicated
#: sentinel, not ``""`` (Redmine #14694 review j#93646 finding 3): spelling absence as the empty
#: string made "the caller passed nothing" and "the caller passed an empty value" the same input,
#: so ``park(run="")`` and ``dogfood(workflow="")`` were dropped instead of refused — the very
#: silent-drop this producer's rule forbids, reintroduced by the rule's own implementation. Only
#: ``CI(run="")`` happened to be refused, and only because CI requires a run.
_UNSUPPLIED = _Unsupplied()

#: The kind-specific fields each kind's marker CARRIES, in marker order. A field absent from a
#: kind's tuple is one that kind's marker cannot express, so supplying it is a producer error
#: rather than something to drop (see :func:`render_hibernate_evidence`).
_KIND_FIELDS = {
    EVIDENCE_REQUIRED_CI_GREEN: (FIELD_WORKFLOW, FIELD_RUN, FIELD_CONCLUSION),
    EVIDENCE_DOGFOOD_DELEGATED: (FIELD_RELEASE_ISSUE, FIELD_ACCEPTANCE),
    EVIDENCE_PARK_DECLARED: (),
}


def _required(value: object, *, kind: str, field: str) -> str:
    """The RAW value, or a producer error — never trimmed, coerced, or separator-bearing.

    Redmine #14694: this used to be ``str(value or "").strip()``, which normalized the caller's
    raw input into a clean canonical token before judging it — ``workflow=" check "`` /
    ``run=" run "`` became the durable authority ``workflow=check`` / ``run=run``, and ``run=12345``
    became the string ``"12345"``. The shared raw validator is the whole implementation now, so the
    marker's kind-specific fields and the envelope's identities cannot drift apart.
    """
    return require_marker_token(value, field=field, requirement=f"{kind} evidence")


def render_hibernate_evidence(
    kind: str,
    *,
    envelope: LaneEvidenceEnvelope,
    run: object = _UNSUPPLIED,
    workflow: object = _UNSUPPLIED,
    conclusion: object = _UNSUPPLIED,
    release_issue: object = _UNSUPPLIED,
    acceptance: object = _UNSUPPLIED,
) -> str:
    """Render a ``[mozyo:workflow-event:gate=<kind>:<envelope>:<extra>]`` marker, fail-closed.

    Validates the kind, the head requirement (head-bearing kinds need a non-empty envelope head),
    and the kind-specific fields; raises ``ValueError`` on a producer programming error (an
    unrenderable evidence marker must never be emitted).

    ONE rule covers every supplied value, whatever the kind (Redmine #14694): **what the caller
    supplies must appear in the marker, or the producer refuses.** The defect was a producer that
    quietly wrote something else, and it had two shapes, so a fix that closed only the first would
    leave the second wearing the same disguise:

    - a value the marker DOES carry, rewritten — ``workflow=" check "`` trimmed to ``check``;
    - a value the marker does NOT carry, dropped — ``render_hibernate_evidence(park, run="123")``
      rendered a park marker with no run in it, and ``conclusion="failure"`` on a CI marker was
      rendered as ``conclusion=success``. The caller asserted something; the durable record says
      nothing, or says the opposite.

    So a field this kind's marker cannot express is refused, and the one field whose value the
    producer states rather than echoes (``conclusion``, always ``success``) may only be restated.
    "Supplied" is asked as ``is _UNSUPPLIED``, an identity test against a sentinel no caller can
    pass: an explicit ``run=""`` is a value the caller wrote, and refusing it is the same rule, not
    an extra one.
    """
    # EXACT builtin, not just membership (review j#94038 blocker 2, swept to this field): `kind` is
    # f-string'd into `gate=`, so a `str` subclass equal to a real kind passed the closed vocabulary
    # and rendered `gate=evil:head=forged` — field injection into the gate field itself.
    if not is_exact_str(kind) or kind not in HIBERNATE_EVIDENCE_KINDS:
        raise ValueError(f"unknown hibernate evidence kind {kind!r}")
    if kind in _HEAD_BEARING_EVIDENCE and not envelope.head:
        raise ValueError(f"{kind} evidence requires a head-bearing envelope")

    carried = _KIND_FIELDS[kind]
    for field, value in (
        (FIELD_WORKFLOW, workflow),
        (FIELD_RUN, run),
        (FIELD_CONCLUSION, conclusion),
        (FIELD_RELEASE_ISSUE, release_issue),
        (FIELD_ACCEPTANCE, acceptance),
    ):
        if value is _UNSUPPLIED:
            continue
        if field not in carried:
            raise ValueError(f"{kind} evidence does not carry a {field}")
        _required(value, kind=kind, field=field)
    if conclusion is not _UNSUPPLIED and conclusion != _CI_CONCLUSION_SUCCESS:
        raise ValueError(
            f"hibernate evidence renders {FIELD_CONCLUSION}={_CI_CONCLUSION_SUCCESS} only, "
            f"got {conclusion!r}"
        )

    fields = [f"gate={kind}", render_lane_envelope(envelope)]
    if kind == EVIDENCE_REQUIRED_CI_GREEN:
        fields.append(f"{FIELD_WORKFLOW}={_required(workflow, kind=kind, field=FIELD_WORKFLOW)}")
        fields.append(f"{FIELD_RUN}={_required(run, kind=kind, field=FIELD_RUN)}")
        fields.append(f"{FIELD_CONCLUSION}={_CI_CONCLUSION_SUCCESS}")
    elif kind == EVIDENCE_DOGFOOD_DELEGATED:
        fields.append(
            f"{FIELD_RELEASE_ISSUE}={_required(release_issue, kind=kind, field=FIELD_RELEASE_ISSUE)}"
        )
        fields.append(
            f"{FIELD_ACCEPTANCE}={_required(acceptance, kind=kind, field=FIELD_ACCEPTANCE)}"
        )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{':'.join(fields)}]"


def parse_hibernate_evidence(
    fields: Mapping[str, str], *, kind: str
) -> "HibernateEvidence | EvidenceParseError":
    """Parse one marker's field mapping as ``kind`` evidence, fail-closed.

    The envelope is parsed strictly (head required for a head-bearing kind); the kind-specific
    fields are then validated: CI needs a non-empty ``run`` and ``conclusion=success``; dogfood a
    non-empty ``release_issue``; park nothing beyond the envelope.
    """
    if kind not in HIBERNATE_EVIDENCE_KINDS:
        return EvidenceParseError(EVIDENCE_UNKNOWN_KIND, str(kind))

    bound = parse_lane_envelope(fields, require_head=kind in _HEAD_BEARING_EVIDENCE)
    if isinstance(bound, EnvelopeParseError):
        return EvidenceParseError(bound.reason, bound.detail)

    extra: dict = {}
    if kind == EVIDENCE_REQUIRED_CI_GREEN:
        workflow = str(fields.get(FIELD_WORKFLOW, "") or "").strip()
        if not workflow:
            return EvidenceParseError(EVIDENCE_MISSING_WORKFLOW)
        run = str(fields.get(FIELD_RUN, "") or "").strip()
        if not run:
            return EvidenceParseError(EVIDENCE_MISSING_RUN)
        if str(fields.get(FIELD_CONCLUSION, "") or "").strip() != _CI_CONCLUSION_SUCCESS:
            return EvidenceParseError(EVIDENCE_CI_NOT_SUCCESS)
        extra = {
            FIELD_WORKFLOW: workflow,
            FIELD_RUN: run,
            FIELD_CONCLUSION: _CI_CONCLUSION_SUCCESS,
        }
    elif kind == EVIDENCE_DOGFOOD_DELEGATED:
        release_issue = str(fields.get(FIELD_RELEASE_ISSUE, "") or "").strip()
        if not release_issue:
            return EvidenceParseError(EVIDENCE_MISSING_RELEASE_ISSUE)
        acceptance = str(fields.get(FIELD_ACCEPTANCE, "") or "").strip()
        if not acceptance:
            return EvidenceParseError(EVIDENCE_MISSING_ACCEPTANCE)
        extra = {FIELD_RELEASE_ISSUE: release_issue, FIELD_ACCEPTANCE: acceptance}

    return HibernateEvidence(kind=kind, envelope=bound, extra=extra)


def resolve_hibernate_evidence(
    markers: Sequence[Mapping[str, str]], *, kind: str
) -> "HibernateEvidence | EvidenceParseError":
    """Fold every marker of ``kind`` to a single evidence, fail-closed on absence / conflict.

    Only well-formed markers of the requested kind are considered. Zero → ``evidence_absent``;
    identical duplicates collapse; any two DIFFERING (envelope or extra) → ``evidence_conflict`` (a
    superseded / cross-lane record is never silently preferred). A malformed marker of the kind is a
    hard parse error (it is evidence someone tried to assert, not noise to skip).
    """
    parsed: list[HibernateEvidence] = []
    for fields in markers:
        if str(fields.get("gate", "") or "").strip() != kind:
            continue
        one = parse_hibernate_evidence(fields, kind=kind)
        if isinstance(one, EvidenceParseError):
            return one
        parsed.append(one)
    if not parsed:
        return EvidenceParseError(EVIDENCE_ABSENT)
    first = parsed[0]
    for other in parsed[1:]:
        if other != first:
            return EvidenceParseError(EVIDENCE_CONFLICT)
    return first
