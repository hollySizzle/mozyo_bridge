"""Pure pending-composer classifier for receiver quarantine (#13763)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


NO_PENDING_COMPOSER = "no_pending_composer"
CORRELATED_KNOWN_MARKER = "correlated_known_marker"
UNCORRELATED = "uncorrelated"
AMBIGUOUS = "ambiguous"
AGENT_WORKING = "agent_working"
IDENTITY_UNATTESTED = "identity_unattested"
GENERATION_MISMATCH = "generation_mismatch"
INVENTORY_UNREADABLE = "inventory_unreadable"

PENDING_COMPOSER_CLASSIFICATIONS = frozenset(
    {
        NO_PENDING_COMPOSER,
        CORRELATED_KNOWN_MARKER,
        UNCORRELATED,
        AMBIGUOUS,
        AGENT_WORKING,
        IDENTITY_UNATTESTED,
        GENERATION_MISMATCH,
        INVENTORY_UNREADABLE,
    }
)


@dataclass(frozen=True)
class PendingComposerSignal:
    """Content-free facts supplied by the transient live adapter.

    The composer body never crosses this boundary.  ``correlated_marker_ids``
    carries only delivery-ledger marker identities that the adapter positively
    found in the current composer and in the ledger.
    """

    inventory_readable: bool
    has_pending: Optional[bool]
    agent_state: str
    identity_attested: bool
    generation_matches: bool
    correlated_marker_ids: tuple[str, ...] = ()
    correlation_ambiguous: bool = False


@dataclass(frozen=True)
class PendingComposerClassification:
    label: str
    correlated_marker_id: str = ""

    @property
    def q_enter_recommended(self) -> bool:
        return self.label == CORRELATED_KNOWN_MARKER

    @property
    def quarantine_candidate(self) -> bool:
        return self.label in (UNCORRELATED, AMBIGUOUS)

    @property
    def blocked(self) -> bool:
        return not (self.q_enter_recommended or self.quarantine_candidate)

    def as_payload(self) -> dict[str, object]:
        return {
            "classification": self.label,
            "correlated_marker_id": self.correlated_marker_id or None,
            "q_enter_recommended": self.q_enter_recommended,
            "quarantine_candidate": self.quarantine_candidate,
            "blocked": self.blocked,
        }


def classify_pending_composer(
    signal: PendingComposerSignal,
) -> PendingComposerClassification:
    """Classify the exact current receiver, fail-closed by precedence."""
    if not signal.inventory_readable:
        return PendingComposerClassification(INVENTORY_UNREADABLE)
    if not signal.generation_matches:
        return PendingComposerClassification(GENERATION_MISMATCH)
    if not signal.identity_attested:
        return PendingComposerClassification(IDENTITY_UNATTESTED)
    if signal.agent_state.strip().lower() in ("busy", "working"):
        return PendingComposerClassification(AGENT_WORKING)
    if signal.has_pending is None:
        return PendingComposerClassification(INVENTORY_UNREADABLE)
    if not signal.has_pending:
        return PendingComposerClassification(NO_PENDING_COMPOSER)
    markers = tuple(dict.fromkeys(m for m in signal.correlated_marker_ids if m))
    if signal.correlation_ambiguous or len(markers) > 1:
        return PendingComposerClassification(AMBIGUOUS)
    if len(markers) == 1:
        return PendingComposerClassification(
            CORRELATED_KNOWN_MARKER, correlated_marker_id=markers[0]
        )
    return PendingComposerClassification(UNCORRELATED)


__all__ = (
    "AGENT_WORKING",
    "AMBIGUOUS",
    "CORRELATED_KNOWN_MARKER",
    "GENERATION_MISMATCH",
    "IDENTITY_UNATTESTED",
    "INVENTORY_UNREADABLE",
    "NO_PENDING_COMPOSER",
    "PENDING_COMPOSER_CLASSIFICATIONS",
    "UNCORRELATED",
    "PendingComposerClassification",
    "PendingComposerSignal",
    "classify_pending_composer",
)
