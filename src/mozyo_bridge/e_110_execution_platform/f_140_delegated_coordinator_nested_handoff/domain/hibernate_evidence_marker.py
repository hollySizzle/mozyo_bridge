"""Hibernate-evidence marker grammar for the CI / dogfood / park producers (Redmine #14219 T2b, step 3).

The design ruling (#14219 j#85530) rules that the three durable authorities without an existing
lane-bound record — required-CI-green, dogfood-delegated, park-declared — are emitted as GENERIC
``[mozyo:workflow-event:gate=<kind>:...]`` evidence, NOT callback-required gates (they are absent
from ``GATE_BEARING_KINDS`` so they never trigger a callback). This module is their dedicated
renderer + strict parser, deliberately separate from the gate-bearing ``render_workflow_event_marker``
(which raises for a non-gate-bearing kind) and from the #14213 glance vocabulary.

Every marker carries the common lane envelope (step 1) plus its kind-specific authority field, all
fail-closed:

  * ``required_ci_green`` — head-bearing; ``run=<run_id>`` (non-empty) and ``conclusion=success``.
  * ``dogfood_delegated`` — head-bearing (the exact delegated SHA); ``release_issue=<id>`` (non-empty).
  * ``park_declared`` — lane-anchored only (no head); the envelope IS the affirmative basis.

Missing / malformed / wrong-conclusion / conflicting evidence is a typed zero — never a lenient
default, never prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .hibernate_evidence_envelope import (
    EnvelopeParseError,
    LaneEvidenceEnvelope,
    parse_lane_envelope,
    render_lane_envelope,
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
FIELD_CONCLUSION = "conclusion"
FIELD_RELEASE_ISSUE = "release_issue"

_CI_CONCLUSION_SUCCESS = "success"

# The marker channel (kept in step with the workflow-event vocabulary the parser already reads).
MARKER_CHANNEL_WORKFLOW_EVENT = "workflow-event"

# Closed parse-failure reasons (in addition to the envelope's own).
EVIDENCE_UNKNOWN_KIND = "evidence_unknown_kind"
EVIDENCE_MISSING_RUN = "evidence_missing_run"
EVIDENCE_CI_NOT_SUCCESS = "evidence_ci_not_success"
EVIDENCE_MISSING_RELEASE_ISSUE = "evidence_missing_release_issue"
EVIDENCE_ABSENT = "evidence_absent"
EVIDENCE_CONFLICT = "evidence_conflict"

HIBERNATE_EVIDENCE_PARSE_REASONS = frozenset({
    EVIDENCE_UNKNOWN_KIND,
    EVIDENCE_MISSING_RUN,
    EVIDENCE_CI_NOT_SUCCESS,
    EVIDENCE_MISSING_RELEASE_ISSUE,
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


def render_hibernate_evidence(
    kind: str,
    *,
    envelope: LaneEvidenceEnvelope,
    run: str = "",
    conclusion: str = "",
    release_issue: str = "",
) -> str:
    """Render a ``[mozyo:workflow-event:gate=<kind>:<envelope>:<extra>]`` marker, fail-closed.

    Validates the kind, the head requirement (head-bearing kinds need a non-empty envelope head),
    and the kind-specific fields; raises ``ValueError`` on a producer programming error (an
    unrenderable evidence marker must never be emitted).
    """
    if kind not in HIBERNATE_EVIDENCE_KINDS:
        raise ValueError(f"unknown hibernate evidence kind {kind!r}")
    if kind in _HEAD_BEARING_EVIDENCE and not envelope.head:
        raise ValueError(f"{kind} evidence requires a head-bearing envelope")
    fields = [f"gate={kind}", render_lane_envelope(envelope)]
    if kind == EVIDENCE_REQUIRED_CI_GREEN:
        if not str(run).strip():
            raise ValueError("required_ci_green evidence requires a run id")
        fields.append(f"{FIELD_RUN}={str(run).strip()}")
        fields.append(f"{FIELD_CONCLUSION}={_CI_CONCLUSION_SUCCESS}")
    elif kind == EVIDENCE_DOGFOOD_DELEGATED:
        if not str(release_issue).strip():
            raise ValueError("dogfood_delegated evidence requires a release_issue")
        fields.append(f"{FIELD_RELEASE_ISSUE}={str(release_issue).strip()}")
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
        run = str(fields.get(FIELD_RUN, "") or "").strip()
        if not run:
            return EvidenceParseError(EVIDENCE_MISSING_RUN)
        if str(fields.get(FIELD_CONCLUSION, "") or "").strip() != _CI_CONCLUSION_SUCCESS:
            return EvidenceParseError(EVIDENCE_CI_NOT_SUCCESS)
        extra = {FIELD_RUN: run, FIELD_CONCLUSION: _CI_CONCLUSION_SUCCESS}
    elif kind == EVIDENCE_DOGFOOD_DELEGATED:
        release_issue = str(fields.get(FIELD_RELEASE_ISSUE, "") or "").strip()
        if not release_issue:
            return EvidenceParseError(EVIDENCE_MISSING_RELEASE_ISSUE)
        extra = {FIELD_RELEASE_ISSUE: release_issue}

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
