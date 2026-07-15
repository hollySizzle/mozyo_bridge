"""Governed review-result approval admission (Redmine #13518 review R2-F7 / R3-F2).

The re-audit found the review-generation fence (:mod:`...domain.review_generation`) had NO
production caller: ``GenerationLease`` / ``evaluate_approval_admissible`` /
``evaluate_integration_admissible`` were exercised only by unit tests, so the #13586
last-write-wins incident stayed reproducible — concurrent reviewers could still append a stale
approval and normal integration default-admitted it.

This module is the missing application wiring for the **approval WRITE** side: before a governed
``review_result`` approval is recorded, it composes

- a **durable single-consumer generation lease** (CAS over the shared workflow-runtime store) so at
  most one consumer ever commits a given review generation's approval, and
- the **pre-approval reread fence** (:func:`evaluate_approval_admissible`) so an approver whose
  snapshot predates a newer unresolved blocking finding is refused,

into one fail-closed :func:`admit_review_approval` decision. The integration ACTION-TIME fence
(:func:`evaluate_integration_admissible`, wired into the actual retire path — R3-F2) is the
complementary backstop: even if a stale approval were somehow written, integration re-derives
admissibility from the latest generation and refuses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (
    AdmissionResult,
    ReviewDecision,
    ReviewGeneration,
    evaluate_approval_admissible,
)

#: The approval was refused because a DIFFERENT consumer already holds this generation's lease —
#: a duplicate consumer of the same review generation is never admitted (#13518 R2-F7).
REASON_LEASE_HELD_BY_OTHER = "generation_lease_held_by_other_consumer"


@dataclass(frozen=True)
class GenerationLeaseStore:
    """Durable single-consumer lease over the shared workflow-runtime store (#13518 R3-F2).

    Adapts :class:`...core.state.workflow_runtime_store.WorkflowRuntimeStore`'s atomic
    ``acquire_generation_lease`` (``BEGIN IMMEDIATE`` CAS) to the generation identity, so the
    review-decision-commit fence is genuinely durable and cross-process safe rather than an
    in-memory dict. Injectable so tests can drive a fake store.
    """

    store: object  # WorkflowRuntimeStore (or a compatible fake with acquire_generation_lease)

    def acquire(self, generation: ReviewGeneration, consumer_id: str) -> bool:
        """CAS-acquire the durable lease for ``generation``; True iff this consumer now holds it."""
        return bool(self.store.acquire_generation_lease(generation.identity, str(consumer_id or "")))

    def holder(self, generation: ReviewGeneration):
        return self.store.generation_lease_holder(generation.identity)


def admit_review_approval(
    *,
    lease: GenerationLeaseStore,
    generation: ReviewGeneration,
    consumer_id: str,
    source_request_seq: int,
    decisions: Iterable[ReviewDecision],
) -> AdmissionResult:
    """Decide whether a ``review_result`` APPROVAL write is admissible (fail-closed).

    Two gates, in order:

    1. **Durable generation lease** — CAS-acquire the single-consumer lease for ``generation``. A
       different consumer already committing this generation refuses this write
       (:data:`REASON_LEASE_HELD_BY_OTHER`), so two reviewers never both commit the same generation.
    2. **Pre-approval reread fence** — :func:`evaluate_approval_admissible` refuses the approval when
       re-reading the source issue surfaces an unresolved blocking finding NEWER than the request
       this approver acted on (the exact #13586 case: B's snapshot predated A's finding).

    Only when both pass is the approval admissible. Pure decision (no IO beyond the injected lease).
    """
    decisions = list(decisions)
    if not lease.acquire(generation, consumer_id):
        return AdmissionResult(False, REASON_LEASE_HELD_BY_OTHER)
    return evaluate_approval_admissible(source_request_seq, decisions)


__all__ = (
    "REASON_LEASE_HELD_BY_OTHER",
    "GenerationLeaseStore",
    "admit_review_approval",
)
